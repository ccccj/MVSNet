from tensorpack import *
from tensorpack.utils import logger
from nn_utils import *
from loss_utils import *
from tensorpack.tfutils.summary import (add_moving_summary, add_param_summary, add_tensor_summary)
from tensorpack.tfutils import optimizer, gradproc
from summary_utils import add_image_summary
import tensorflow as tf
from tensorpack.tfutils.gradproc import SummaryGradient
from DataManager import Cam

""" monkey-patch """
enable_argscope_for_module(tf.layers)


def get_depth_meta(cams, depth_num):
    """

    :param cams: shape: batch, view_num
    :return: depth_start, depth_interval
    """
    with tf.variable_scope('depth_meta'):
        ref_cam = cams[:, 0]
        logger.warn('cams shape: {}'.format(cams.get_shape().as_list()))
        logger.warn('ref_cam shape: {}'.format(ref_cam.get_shape().as_list()))
        logger.warn('ref_cam type: {}'.format(type(ref_cam)))

        batch_size = tf.shape(cams)[0]
        # depth_start = tf.reshape(
        #     tf.slice(ref_cam, [0, 1, 3, 0], [batch_size, 1, 1, 1]), [batch_size], name='depth_start')
        depth_start = tf.reshape(
            tf.slice(cams, [0, 0, 1, 3, 0], [batch_size, 1, 1, 1, 1]), [batch_size], name='depth_start')
        # depth_interval = tf.reshape(
        #     tf.slice(ref_cam, [0, 1, 3, 1], [batch_size, 1, 1, 1]), [batch_size], name='depth_interval')
        depth_interval = tf.reshape(
            tf.slice(cams, [0, 0, 1, 3, 1], [batch_size, 1, 1, 1, 1]), [batch_size], name='depth_interval')

        # depth_end = tf.add(depth_start, (tf.cast(depth_num, tf.float32) - 1) * depth_interval, name='depth_end')
        depth_end = depth_start + (tf.cast(depth_num, tf.float32) - 1) * depth_interval
        depth_end = tf.identity(depth_end, 'depth_end')
        # depth_start = tf.map_fn(lambda cam: Cam.get_depth_meta(cam, 'depth_min'), ref_cam)
        # assert depth_start.get_shape().as_list() == [batch_size]
        # depth_interval = tf.map_fn(lambda cam: Cam.get_depth_meta(cam, 'depth_interval'), ref_cam)
        # assert depth_interval.get_shape().as_list() == [batch_size]

    return depth_start, depth_interval, depth_end


def center_image(imgs):
    """

    :param imgs: shape: b, view_num, h, w, c
    :return:
    """
    assert len(imgs.get_shape().as_list()) == 5
    moments = tf.nn.moments(tf.cast(imgs, tf.float32), axes=(2, 3), keep_dims=True)
    return (imgs - moments[0]) / (moments[1] + 1e-7)


class MVSNet(ModelDesc):

    # height = 512
    # width = 640
    data_format = 'channels_last'  # do not change
    lambda_ = 1.

    weight_decay = 1.  # FIXME: it is useless!

    """
    To apply on normalization parameters, use '.*/W|.*/gamma|.*/beta'
    """
    regularization_pattern = '.*/W |.*/b$'  # FIXME: it is useless!

    debug_param_summary = True

    base_lr = 1e-3

    """Step interval to decay learning rate."""
    decay_steps = 10000

    """Learning rate decay rate"""
    decay_rate = 0.9

    def __init__(self, depth_num, bn_training, bn_trainable, batch_size, branch_function, is_refine, height, width,
                 view_num, regularize_type):
        super(MVSNet, self).__init__()
        # self.is_training = is_training
        self.bn_training = bn_training
        self.bn_trainable = bn_trainable
        self.depth_num = depth_num
        self.batch_size = batch_size
        self.branch_function = branch_function
        self.is_refine = is_refine
        self.height = height
        self.width = width
        self.view_num = view_num
        self.regularize_type = regularize_type

    def inputs(self):
        return [
            tf.placeholder(tf.float32, [None, self.view_num, self.height, self.width, 3], 'imgs'),
            tf.placeholder(tf.float32, [None, self.view_num, 2, 4, 4], 'cams'),
            # tf.placeholder(tf.float32, [None, self.height, self.width, 1], 'seg_map'),
            tf.placeholder(tf.float32, [None, self.height // 4, self.width // 4, 1], 'gt_depth'),
        ]

    def _preprocess(self, imgs, gt_depth):
        with tf.variable_scope('preprocess'):
            imgs = center_image(imgs)
            ref_img = imgs[:, 0]
            ref_img = tf.identity(ref_img, name='ref_img')
            return imgs, gt_depth, ref_img

    def build_graph(self, imgs, cams, gt_depth):
        # preprocess
        imgs, gt_depth, ref_img = self._preprocess(imgs, gt_depth)

        with argscope([tf.layers.conv3d, tf.layers.conv3d_transpose, mvsnet_gn,
                       Conv2D, Conv2DTranspose, MaxPooling, AvgPooling, BatchNorm],
                      data_format=self.data_format),\
             argscope(tf.layers.batch_normalization, axis=-1):
            # feature extraction
            # shape: b, view_num, h/4, w/4, c
            feature_maps = feature_extraction_net(imgs, self.branch_function)

            # get depth_start and depth_interval batch-wise
            depth_start, depth_interval, depth_end = get_depth_meta(cams, depth_num=self.depth_num)

            # warping layer
            # shape of cost_volume: b, depth_num, h/4, w/4, c
            cost_volume = warping_layer('warping', feature_maps, cams, depth_start
                                        , depth_interval, self.depth_num)
            # cost_volume = tf.get_variable('fake_cost_volume', (1, 32, 192, 128, 160))

            if self.regularize_type == '3DCNN':
                # cost volume regularization
                # regularized_cost_volume: b, d, h/4, w/4
                regularized_cost_volume = cost_volume_regularization(cost_volume, self.bn_training, self.bn_trainable)
                # regularized_cost_volume = simple_cost_volume_regularization(cost_volume, self.bn_training, self.bn_trainable)
                # shape of coarse_depth: b, 1, h/4, w/4
                # shape of prob_map: b, h/4, w/4, 1
                # TODO: no need to pass batch_size as param, actually, it is needed, because it is needed in the graph buiding
                coarse_depth, prob_map = soft_argmin('soft_argmin', regularized_cost_volume, depth_start, depth_end,
                                                     self.depth_num,
                                                     depth_interval, self.batch_size)

                # shape of refine_depth: b, 1, h/4, w/4
                if self.is_refine:
                    refine_depth = depth_refinement(coarse_depth, ref_img, depth_start, depth_end)
                    loss_coarse, *_ = mvsnet_regression_loss(gt_depth, coarse_depth, depth_interval, 'coarse_loss')
                    loss_refine, less_one_accuracy, less_three_accuracy = mvsnet_regression_loss(gt_depth, refine_depth,
                                                                                                 depth_interval,
                                                                                                 'refine_loss')
                else:
                    refine_depth = coarse_depth
                    # loss_coarse, *_ = mvsnet_regression_loss(gt_depth, coarse_depth, depth_interval, 'coarse_loss')
                    loss_refine, less_one_accuracy, less_three_accuracy = mvsnet_regression_loss(gt_depth, refine_depth,
                                                                                                 depth_interval,
                                                                                                 'refine_loss')
                    loss_coarse = tf.identity(loss_refine, name='loss_coarse')

                # FIXME: it is weried because I never use refine part
                coarse_depth = tf.identity(coarse_depth, 'coarse_depth')
                refine_depth = tf.identity(refine_depth, 'refine_depth')
                prob_map = tf.identity(prob_map, 'prob_map')
                loss = tf.add(loss_refine / 2, loss_coarse * self.lambda_ / 2, name='loss')
                less_one_accuracy = tf.identity(less_one_accuracy, name='less_one_accuracy')
                less_three_accuracy = tf.identity(less_three_accuracy, name='less_three_accuracy')

            else:
                prob_volume = gru_regularization(cost_volume, self.bn_training, self.bn_trainable)
                loss, mae, less_one_accuracy, less_three_accuracy, coarse_depth = \
                    mvsnet_classification_loss(
                        prob_volume, gt_depth, self.depth_num, depth_start, depth_interval)
                coarse_depth = tf.identity(coarse_depth, 'coarse_depth')
                refine_depth = tf.identity(coarse_depth, 'refine_depth')
                # prob_map = get_propability_map(prob_volume, coarse_depth, depth_start, depth_interval)

            with tf.variable_scope('summaries'):
                with tf.device('/cpu:0'):
                    if self.regularize_type == '3DCNN':
                        add_moving_summary(loss, loss_coarse, loss_refine, less_one_accuracy, less_three_accuracy)
                    else:
                        add_moving_summary(loss, less_one_accuracy, less_three_accuracy)

                if self.regularize_type == '3DCNN':
                    add_image_summary(prob_map, name='prob_map')
                add_image_summary(coarse_depth
                                  , name='coarse_depth')
                add_image_summary(refine_depth
                                  , name='refine_depth')
                add_image_summary(ref_img, name='rgb')
                add_image_summary(gt_depth, name='gt_depth')

            if self.debug_param_summary:
                with tf.device('/gpu:0'):
                    add_param_summary(
                        ['.*/W', ['histogram', 'rms']],
                        ['.*/gamma', ['histogram', 'mean']],
                        ['.*/beta', ['histogram', 'mean']]
                    )
                    # all_vars = [var for var in tf.trainable_variables() if "gamma" in var.name or 'beta' in var.name]
                    # grad_vars = tf.gradients(loss, all_vars)
                    # for var, grad in zip(all_vars, grad_vars):
                    #     add_tensor_summary(grad, ['histogram', 'rms'], name=var.name + '-grad')
                    # all_vars = [var for var in tf.trainable_variables()]
                    # grad_vars = tf.gradients(loss, all_vars)
                    # for var, grad in zip(all_vars, grad_vars):
                    #     add_tensor_summary(grad, ['histogram'], name=var.name + '-grad')

        return loss

    def optimizer(self):
        lr = tf.train.exponential_decay(
            self.base_lr,
            global_step=get_global_step_var(),
            decay_steps=self.decay_steps,
            decay_rate=self.decay_rate,
            name='learning-rate'
        )
        opt = tf.train.RMSPropOptimizer(learning_rate=lr)
        tf.summary.scalar('lr', lr)
        return optimizer.apply_grad_processors(
            opt, [
                gradproc.SummaryGradient()
            ]
        )
