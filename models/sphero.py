import tensorflow as tf
from scipy.spatial import cKDTree
import open3d.ml.tf as ml3d
import numpy as np
from utils.tools.losses import get_window_func, compute_density, compute_pressure
from utils.tools.neighbor import reduce_subarrays_sum_multi

from .pbf_real import PBFReal


class SPHeroConv(tf.keras.layers.Layer):
    def __init__(
            self,
            filters,
            activation=None,
            use_bias=True,
            radius_search_ignore_query_points=True,
            **kwargs):
        super(SPHeroConv, self).__init__(**kwargs)
        self.filters = filters
        self.activation = tf.keras.activations.get(activation)  # Ensure activation is a Keras activation function
        self.use_bias = use_bias
        self.radius_search_ignore_query_points = radius_search_ignore_query_points
        self.fixed_radius_search = ml3d.layers.FixedRadiusSearch(ignore_query_point=radius_search_ignore_query_points,dtype=tf.int32)

    def build(self, input_shape):
        self.in_channels = input_shape[-1]
        self.kernel = self.add_weight(
            name="kernel",
            shape=[4, self.in_channels, self.filters],
            initializer='glorot_uniform',
            trainable=True
        )
        if self.use_bias:
            self.bias = self.add_weight(
                name="bias",
                shape=[self.filters],
                initializer='zeros',
                trainable=True
            )

    def call(self,
             input_features,  # [N_in, 3]
             input_positions,
             output_positions,
             extents):
        neighbors = self.fixed_radius_search(input_positions, output_positions, extents)
        neighbors_index, neighbors_row_splits, _ = neighbors
        # neighbors_index = tf.cast(neighbors_index, tf.int32)
        neighbors_row_splits = tf.cast(neighbors_row_splits, tf.int32)
        # 获取每个query点的邻居数
        neighbors_counts = neighbors_row_splits[1:] - neighbors_row_splits[:-1]
        expanded_query = tf.repeat(output_positions, neighbors_counts, axis=0)
        difference = tf.gather(input_positions, neighbors_index) - expanded_query  # [_, 3]

        # 计算极坐标和权重
        spherical_coords = self.cartesian_to_spherical(difference, extents)  # [_, 4]
        kernel = tf.tensordot(spherical_coords, self.kernel, axes=1)  # [_, C_in, C_out]

        neighbors_feats = tf.gather(input_features, neighbors_index)  # [_, C_in]
        out = tf.reduce_sum(tf.expand_dims(neighbors_feats, axis=-1) * kernel, axis=1)  # [_, C_out]
        out_features = reduce_subarrays_sum_multi(out, neighbors_row_splits)  # [N_out, C_out]

        if self.use_bias:
            out_features += self.bias

        if self.activation:
            out_features = self.activation(out_features)

        return out_features, neighbors

    def cartesian_to_spherical(self, cartesian_coords, extents=1):
        # 计算径向距离 r
        r_plane_2 = cartesian_coords[..., 0] ** 2 + cartesian_coords[..., 1] ** 2
        r_2 = r_plane_2 + cartesian_coords[..., 2] ** 2

        # 防止除以零
        r_safe = tf.maximum(tf.sqrt(r_2), 1e-10)
        # 计算 cos(phi) 和 sin(theta), cos(theta)
        cos_phi = cartesian_coords[..., 2] / r_safe

        r_plane_safe = tf.maximum(tf.sqrt(r_plane_2), 1e-10)
        sin_theta = cartesian_coords[..., 1] / r_plane_safe
        cos_theta = cartesian_coords[..., 0] / r_plane_safe

        # 标准化径向距离
        r_normalized = r_safe / extents

        return tf.stack([r_normalized, cos_phi, sin_theta, cos_theta], axis=-1)


class SPHeroNet(PBFReal):
    def __init__(self,
                 name="SPHeroNet",
                 timestep=0.02,
                 grav=-9.81,
                 rest_dens=1000.0,
                 viscosity=0.02,
                 particle_radii=[0.025],
                 query_radii=None,
                 use_mass=True,
                 use_acc=False,
                 use_vel=True,
                 use_feats=False,
                 use_box_feats=True,
                 transformation={},
                 ignore_query_points=True,
                 loss={
                     "weighted_mse": {
                         "typ": "weighted_mse",
                         "fac": 128.0,
                         "gamma": 0.5,
                         "neighbor_scale": 0.025
                     }
                 },
                 dens_feats=False,
                 pres_feats=False,
                 stiffness=20.0,
                 layer_channels=[32, 64, 64, 3],
                 out_scale=[0.01, 0.01, 0.01],
                 window_dens='poly6',
                 **kwargs):

        super().__init__(name=name,
                         timestep=timestep,
                         particle_radii=particle_radii,
                         transformation=transformation,
                         grav=grav,
                         loss=loss,
                         query_radii=query_radii,
                         density0=rest_dens,
                         viscosity=viscosity,
                         window_dens=window_dens,
                         **kwargs)
        self.query_radii = particle_radii[0] * 2 if query_radii is None else query_radii
        diameter = 2.0 * particle_radii[0]
        volume = diameter ** 3
        self.fluid_mass = volume * self.m_density0

        self.use_mass = use_mass
        self.use_vel = use_vel
        self.use_acc = use_acc
        self.use_feats = use_feats
        self.use_box_feats = use_box_feats
        self.dens_feats = dens_feats
        self.pres_feats = pres_feats
        self.stiffness = stiffness
        self.out_scale = tf.constant(out_scale)
        self.channels = layer_channels[0]
        self.ignore_query_points = ignore_query_points
        self.layer_channels = layer_channels
        self.viscosity = viscosity

        self._all_convs = []

        self.fluid_convs = self.get_cconv(name='fluid_obs',
                                          filters=self.channels,
                                          activation=None)

        self.fluid_dense = tf.keras.layers.Dense(units=self.channels,
                                                 name="fluid_dense",
                                                 activation=None)

        self.obs_convs = self.get_cconv(name='obs_conv',
                                        filters=self.channels,
                                        activation=None)

        self.obs_dense = tf.keras.layers.Dense(units=self.channels,
                                               name="obs_dense",
                                               activation=None)

        self.convs = []
        self.denses = []
        for i in range(1, len(self.layer_channels)):
            ch = self.layer_channels[i]
            conv = self.get_cconv(name='conv{0}'.format(i),
                                  filters=ch,
                                  activation=None,
                                  ignore_query_points=self.ignore_query_points)
            self.convs.append(conv)
            dense = tf.keras.layers.Dense(units=ch,
                                          name="dense{0}".format(i),
                                          activation=None)
            self.denses.append(dense)

    def get_cconv(self,
                  name,
                  activation=None,
                  ignore_query_points=None,
                  **kwargs):

        if ignore_query_points is None:
            ignore_query_points = self.ignore_query_points
        conv = SPHeroConv(
            name=name,
            activation=activation,
            radius_search_ignore_query_points=ignore_query_points,
            **kwargs)

        self._all_convs.append((name, conv))
        return conv

    def preprocess(self,
                   data,
                   training=True,
                   vel_corr=None,
                   tape=None,
                   **kwargs):
        pos, vel, solid_masses = super(SPHeroNet, self).preprocess(data, training, vel_corr, tape, **kwargs)
        _pos, _vel, acc, feats, box, bfeats = data
        self.solid_masses = 1.2 * solid_masses
        #
        # preprocess features
        #
        # compute the extent of the filters (the diameter)
        fluid_feats = [tf.ones_like(pos[:, :1])]
        box_feats = [tf.ones_like(box[:, :1])]
        if self.use_mass:
            fluid_feats.append(fluid_feats[0] * self.fluid_mass)
            box_feats.append(self.solid_masses[:, tf.newaxis])
        if self.use_vel:
            fluid_feats.append(vel)
        if self.use_acc:
            fluid_feats.append(acc)
        if self.use_feats:
            fluid_feats.append(feats)
        if self.use_box_feats:
            box_feats.append(bfeats)

        all_pos = tf.concat([pos, box], axis=0)
        self.all_pos = all_pos
        if self.dens_feats or self.pres_feats:
            dens = compute_density(all_pos, all_pos, self.query_radii,
                                   mass=tf.stack([self.fluid_mass * tf.ones_like(pos[:, :1]), self.solid_masses],
                                                 axis=0))
            if self.dens_feats:
                fluid_feats.append(tf.expand_dims(dens[:tf.shape(pos)[0]], -1))
                box_feats.append(tf.expand_dims(dens[tf.shape(pos)[0]:], -1))
            if self.pres_feats:
                pres = compute_pressure(all_pos,
                                        all_pos,
                                        dens,
                                        self.m_density0,
                                        stiffness=self.stiffness)
                fluid_feats.append(tf.expand_dims(pres[:tf.shape(pos)[0]], -1))
                box_feats.append(tf.expand_dims(pres[tf.shape(pos)[0]:], -1))

        fluid_feats = tf.concat(fluid_feats, axis=-1)
        box_feats = tf.concat(box_feats, axis=-1)

        self.inp_feats = fluid_feats
        self.inp_bfeats = box_feats
        if tape is not None:
            tape.watch(self.inp_feats)
            tape.watch(self.inp_bfeats)

        ans_conv, f_nns = self.fluid_convs(fluid_feats, pos, all_pos, self.query_radii)
        ans_dense = self.fluid_dense(fluid_feats)

        ans_obs, s_nns = self.obs_convs(box_feats, box, all_pos, self.query_radii)
        ans_dense_obs = self.obs_dense(box_feats)

        ans_dense = tf.concat([ans_dense, ans_dense_obs], axis=0)

        feats = tf.concat([ans_conv, ans_obs, ans_dense], axis=-1)

        self.fluid_nns, self.solid_nns = f_nns, s_nns
        return [pos, vel, feats]

    def forward(self, prev, data, training=True, **kwargs):
        pos, vel, feats = prev
        _pos, _vel, acc, _feats, box, bfeats = data
        # feats = feats[:tf.shape(pos)[0]]

        ans_convs = [feats]  # [self.channels*3]
        first = True
        for conv, dense in zip(self.convs, self.denses):
            feats = tf.keras.activations.relu(ans_convs[-1])
            # ans = []
            if first:
                ans_conv, _ = conv(feats, self.all_pos, pos, self.query_radii)
                ans_dense = dense(feats[:tf.shape(pos)[0]])
                first = False
            else:
                ans_conv, _ = conv(feats, pos, pos, self.query_radii)
                ans_dense = dense(feats)
            if ans_dense[-1].shape[-1] == ans_convs[-1].shape[-1]:
                ans = ans_conv + ans_dense + ans_convs[-1]
            else:
                ans = ans_conv + ans_dense
            ans_convs.append(ans)

        out = ans_convs[-1]
        if out.shape[-1] == 1:
            out = tf.repeat(out, 3, axis=-1)
        elif out.shape[-1] == 2:
            out = tf.concat([out, out[:, :1]], axis=-1)

        #
        # scale to better match the scale of the output distribution
        #
        pcnt = tf.shape(pos)[0]
        self.pos_correction = self.out_scale * out[:pcnt]
        self.obs = self.out_scale * out[pcnt:]

        pos2_corrected, vel2_corrected = self.compute_new_pos_vel(_pos, _vel, pos, vel, self.pos_correction)

        return [pos2_corrected, vel2_corrected]

    def postprocess(self, prev, data, training=True, vel_corr=None, **kwargs):
        #
        # postprocess output of network
        #
        pos, vel = prev
        _pos, _vel, acc, feats, box, bfeats = data

        group_position = tf.concat([pos, box], axis=0)
        group_masses = tf.concat([self.fluid_mass * tf.ones_like(pos[:, 0]), self.solid_masses], axis=0)
        self.densities = compute_density(pos, group_position, self.query_radii, mass=group_masses)

        # pos, vel = super(SPHeroNet, self).postprocess(prev, data, training, vel_corr, **kwargs)
        return [pos, vel]

    def loss(self, results, data):
        loss = {}

        pos, vel = results
        target, target_vel = data[1], data[4]

        # compute the number of fluid neighbors.
        # this info is used in the loss function during training.
        fluid_num = tf.shape(pos)[0]
        num_fluid_neighbors = tf.cast(
            self.fluid_nns.neighbors_row_splits[1:] - self.fluid_nns.neighbors_row_splits[:-1], tf.float32)[:fluid_num]
        num_solid_neighbors = tf.cast(
            self.solid_nns.neighbors_row_splits[1:] - self.solid_nns.neighbors_row_splits[:-1], tf.float32)[:fluid_num]

        num_fluid_neighbors, num_solid_neighbors = \
            tf.stop_gradient(num_fluid_neighbors), tf.stop_gradient(num_solid_neighbors)

        for n, l in self.loss_fn.items():
            loss[n] = l(target,
                        pos,
                        pre_steps=data[3],
                        num_fluid_neighbors=num_fluid_neighbors,
                        num_solid_neighbors=num_solid_neighbors)
        return loss