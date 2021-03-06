import tensorflow as tf


def get_propability_map(cv, depth_map, depth_start, depth_interval):
    """ get probability map from cost volume
    :param cv: shape: b, d, h, w
    :param depth_map: shape: b, 1, h, w
    :return prob_map: shape: b, h, w, 1
    """
    # depth_map = tf.transpose(depth_map, [0, 2, 3, 1])

    def _repeat_(x, num_repeats):
        """ repeat each element num_repeats times """
        x = tf.reshape(x, [-1])
        ones = tf.ones((1, num_repeats), dtype='int32')
        x = tf.reshape(x, shape=(-1, 1))
        x = tf.matmul(x, ones)
        return tf.reshape(x, [-1])

    shape = tf.shape(depth_map)
    batch_size = shape[0]
    height = shape[1]
    width = shape[2]
    depth = tf.shape(cv)[1]

    # byx coordinate, batched & flattened
    b_coordinates = tf.range(batch_size)
    y_coordinates = tf.range(height)
    x_coordinates = tf.range(width)
    b_coordinates, y_coordinates, x_coordinates = tf.meshgrid(b_coordinates, y_coordinates, x_coordinates)
    b_coordinates = _repeat_(b_coordinates, batch_size)
    y_coordinates = _repeat_(y_coordinates, batch_size)
    x_coordinates = _repeat_(x_coordinates, batch_size)

    # d coordinate (floored and ceiled), batched & flattened
    d_coordinates = tf.reshape((depth_map - depth_start) / depth_interval, [-1])
    d_coordinates_left0 = tf.clip_by_value(tf.cast(tf.floor(d_coordinates), 'int32'), 0, depth - 1)
    d_coordinates_left1 = tf.clip_by_value(d_coordinates_left0 - 1, 0, depth - 1)
    d_coordinates1_right0 = tf.clip_by_value(tf.cast(tf.ceil(d_coordinates), 'int32'), 0, depth - 1)
    d_coordinates1_right1 = tf.clip_by_value(d_coordinates1_right0 + 1, 0, depth - 1)

    # voxel coordinates
    voxel_coordinates_left0 = tf.stack(
        [b_coordinates, d_coordinates_left0, y_coordinates, x_coordinates], axis=1)
    voxel_coordinates_left1 = tf.stack(
        [b_coordinates, d_coordinates_left1, y_coordinates, x_coordinates], axis=1)
    voxel_coordinates_right0 = tf.stack(
        [b_coordinates, d_coordinates1_right0, y_coordinates, x_coordinates], axis=1)
    voxel_coordinates_right1 = tf.stack(
        [b_coordinates, d_coordinates1_right1, y_coordinates, x_coordinates], axis=1)

    # get probability image by gathering and interpolation
    prob_map_left0 = tf.gather_nd(cv, voxel_coordinates_left0)
    prob_map_left1 = tf.gather_nd(cv, voxel_coordinates_left1)
    prob_map_right0 = tf.gather_nd(cv, voxel_coordinates_right0)
    prob_map_right1 = tf.gather_nd(cv, voxel_coordinates_right1)
    prob_map = prob_map_left0 + prob_map_left1 + prob_map_right0 + prob_map_right1
    prob_map = tf.reshape(prob_map, [batch_size, height, width, 1])

    return prob_map


def get_homographies_yeeef(batch_left_cam, batch_right_cam, depth_num, depth_start, depth_interval):
    # shape of left_cam and right_cam: (b, )
    batch_size = batch_left_cam.get_shape().as_list()[0]
    with tf.name_scope('get_homographies'):
        # cameras (K, R, t)
        R_left = tf.squeeze(tf.slice(batch_left_cam, [0, 0, 0, 0], [-1, 1, 3, 3]), axis=1)
        R_right = tf.squeeze(tf.slice(batch_right_cam, [0, 0, 0, 0], [-1, 1, 3, 3]), axis=1)
        t_left = tf.squeeze(tf.slice(batch_left_cam, [0, 0, 0, 3], [-1, 1, 3, 1]), axis=1)
        t_right = tf.squeeze(tf.slice(batch_right_cam, [0, 0, 0, 3], [-1, 1, 3, 1]), axis=1)
        K_left = tf.squeeze(tf.slice(batch_left_cam, [0, 1, 0, 0], [-1, 1, 3, 3]), axis=1)
        K_right = tf.squeeze(tf.slice(batch_right_cam, [0, 1, 0, 0], [-1, 1, 3, 3]), axis=1)

        assert R_left.get_shape().as_list() == [batch_size, 3, 3], "R_left's shape: {}". \
            format(R_left.get_shape().as_list())
        assert t_left.get_shape().as_list() == [batch_size, 3, 1], "t_left's shape: {}". \
            format(R_left.get_shape().as_list())
        assert K_left.get_shape().as_list() == [batch_size, 3, 3], "K_left's shape: {}". \
            format(K_left.get_shape().as_list())

        # depth
        batch_size = tf.shape(depth_start)[0]
        depth_start_mat = tf.tile(tf.reshape(depth_start, (batch_size, 1)), (1, depth_num))
        depth_interval_mat = tf.tile(tf.reshape(depth_interval, (batch_size, 1)), (1, depth_num))
        depth_range_mat = tf.tile(tf.reshape(tf.cast(tf.range(depth_num), tf.float32), [1, depth_num]), (batch_size, 1))
        depth = depth_start_mat + depth_range_mat * depth_interval_mat

        # preparation
        num_depth = tf.shape(depth)[1]
        K_left_inv = tf.matrix_inverse(K_left)
        R_left_trans = tf.transpose(R_left, perm=[0, 2, 1])
        R_right_trans = tf.transpose(R_right, perm=[0, 2, 1])

        fronto_direction = tf.slice(R_left, [0, 2, 0], [-1, 1, 3])  # (B, D, 1, 3)

        c_left = -tf.matmul(R_left_trans, t_left)
        c_right = -tf.matmul(R_right_trans, t_right)  # (B, D, 3, 1)
        c_relative = tf.subtract(c_right, c_left)

        # compute
        batch_size = tf.shape(R_left)[0]
        temp_vec = tf.matmul(c_relative, fronto_direction)
        depth_mat = tf.tile(tf.reshape(depth, [batch_size, num_depth, 1, 1]), [1, 1, 3, 3])

        temp_vec = tf.tile(tf.expand_dims(temp_vec, axis=1), [1, num_depth, 1, 1])

        middle_mat0 = tf.eye(3, batch_shape=[batch_size, num_depth]) - temp_vec / depth_mat
        middle_mat1 = tf.tile(tf.expand_dims(tf.matmul(R_left_trans, K_left_inv), axis=1), [1, num_depth, 1, 1])
        middle_mat2 = tf.matmul(middle_mat0, middle_mat1)

        homographies = tf.matmul(
            tf.tile(
                tf.expand_dims(K_right, axis=1),
                [1, num_depth, 1, 1]
            ),
            tf.matmul(
                tf.tile(
                    tf.expand_dims(R_right, axis=1),
                    [1, num_depth, 1, 1]
                ),
                middle_mat2
            )
        )

    return homographies


def get_homographies(left_cam, right_cam, depth_num, depth_start, depth_interval):
    with tf.name_scope('get_homographies'):
        # cameras (K, R, t)
        R_left = tf.slice(left_cam, [0, 0, 0, 0], [-1, 1, 3, 3])
        R_right = tf.slice(right_cam, [0, 0, 0, 0], [-1, 1, 3, 3])
        t_left = tf.slice(left_cam, [0, 0, 0, 3], [-1, 1, 3, 1])
        t_right = tf.slice(right_cam, [0, 0, 0, 3], [-1, 1, 3, 1])
        K_left = tf.slice(left_cam, [0, 1, 0, 0], [-1, 1, 3, 3])
        K_right = tf.slice(right_cam, [0, 1, 0, 0], [-1, 1, 3, 3])

        # depth
        depth_num = tf.reshape(tf.cast(depth_num, 'int32'), [])

        depth = depth_start + tf.cast(tf.range(depth_num), tf.float32) * depth_interval
        # preparation
        num_depth = tf.shape(depth)[0]
        K_left_inv = tf.matrix_inverse(tf.squeeze(K_left, axis=1))
        R_left_trans = tf.transpose(tf.squeeze(R_left, axis=1), perm=[0, 2, 1])
        R_right_trans = tf.transpose(tf.squeeze(R_right, axis=1), perm=[0, 2, 1])

        fronto_direction = tf.slice(tf.squeeze(R_left, axis=1), [0, 2, 0], [-1, 1, 3])          # (B, D, 1, 3)

        c_left = -tf.matmul(R_left_trans, tf.squeeze(t_left, axis=1))
        c_right = -tf.matmul(R_right_trans, tf.squeeze(t_right, axis=1))                        # (B, D, 3, 1)
        c_relative = tf.subtract(c_right, c_left)

        # compute
        batch_size = tf.shape(R_left)[0]
        temp_vec = tf.matmul(c_relative, fronto_direction)
        depth_mat = tf.tile(tf.reshape(depth, [batch_size, num_depth, 1, 1]), [1, 1, 3, 3])

        temp_vec = tf.tile(tf.expand_dims(temp_vec, axis=1), [1, num_depth, 1, 1])

        middle_mat0 = tf.eye(3, batch_shape=[batch_size, num_depth]) - temp_vec / depth_mat
        middle_mat1 = tf.tile(tf.expand_dims(tf.matmul(R_left_trans, K_left_inv), axis=1), [1, num_depth, 1, 1])
        middle_mat2 = tf.matmul(middle_mat0, middle_mat1)

        homographies = tf.matmul(tf.tile(K_right, [1, num_depth, 1, 1])
                     , tf.matmul(tf.tile(R_right, [1, num_depth, 1, 1])
                     , middle_mat2))

    return homographies


def build_cost_volume(view_homographies, feature_maps, depth_num):
    _, view_num, h, w, c = feature_maps.get_shape().as_list()

    # shape: b, h, w, c
    ref_feature_map = feature_maps[:, 0]
    with tf.variable_scope('cost_volume_homography'):
        depth_costs = []
        for d in range(depth_num):
            # compute cost (variation metric)
            ave_feature = ref_feature_map
            ave_feature2 = tf.square(ref_feature_map)
            for view in range(0, view_num - 1):
                homography = tf.slice(view_homographies[view], begin=[0, d, 0, 0], size=[-1, 1, 3, 3])
                homography = tf.squeeze(homography, axis=1)
                # warped_view_feature = homography_warping(view_towers[view].get_output(), homography)
                warped_view_feature = tf_transform_homography(feature_maps[:, view + 1], homography)
                ave_feature = ave_feature + warped_view_feature
                ave_feature2 = ave_feature2 + tf.square(warped_view_feature)
            ave_feature = ave_feature / view_num
            ave_feature2 = ave_feature2 / view_num
            cost = ave_feature2 - tf.square(ave_feature)
            # shape of cost: b, h, w, c
            # shape of depth_costs: depth_num, b, h, w, c
            depth_costs.append(cost)
        # shape of cost_volume: b, depth_num, h, w, c
        cost_volume = tf.stack(depth_costs, axis=1)

    return cost_volume


def tf_transform_homography(input_image, homography):

    # tf.contrib.image.transform is for pixel coordinate but our
    # homograph parameters are for image coordinate (x_p = x_i + 0.5).
    # So need to change the corresponding homography parameters

    homography = tf.reshape(homography, [-1, 9])
    a0 = tf.slice(homography, [0, 0], [-1, 1])
    a1 = tf.slice(homography, [0, 1], [-1, 1])
    a2 = tf.slice(homography, [0, 2], [-1, 1])
    b0 = tf.slice(homography, [0, 3], [-1, 1])
    b1 = tf.slice(homography, [0, 4], [-1, 1])
    b2 = tf.slice(homography, [0, 5], [-1, 1])
    c0 = tf.slice(homography, [0, 6], [-1, 1])
    c1 = tf.slice(homography, [0, 7], [-1, 1])
    c2 = tf.slice(homography, [0, 8], [-1, 1])
    a_0 = a0 - c0 / 2
    a_1 = a1 - c1 / 2
    a_2 = (a0 + a1) / 2 + a2 - (c0 + c1) / 4 - c2 / 2
    b_0 = b0 - c0 / 2
    b_1 = b1 - c1 / 2
    b_2 = (b0 + b1) / 2 + b2 - (c0 + c1) / 4 - c2 / 2
    c_0 = c0
    c_1 = c1
    c_2 = c2 + (c0 + c1) / 2
    homo = []
    homo.append(a_0)
    homo.append(a_1)
    homo.append(a_2)
    homo.append(b_0)
    homo.append(b_1)
    homo.append(b_2)
    homo.append(c_0)
    homo.append(c_1)
    homo.append(c_2)
    homography = tf.stack(homo, axis=1)
    homography = tf.reshape(homography, [-1, 9])

    homography_linear = tf.slice(homography, begin=[0, 0], size=[-1, 8])
    homography_linear_div = tf.tile(tf.slice(homography, begin=[0, 8], size=[-1, 1]), [1, 8])
    homography_linear = tf.div(homography_linear, homography_linear_div)
    warped_image = tf.contrib.image.transform(
        input_image, homography_linear, interpolation='BILINEAR')

    # return input_image
    return warped_image


