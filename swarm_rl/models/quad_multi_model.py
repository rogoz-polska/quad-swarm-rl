import torch
from torch import nn

from sample_factory.algorithms.appo.model_utils import nonlinearity, EncoderBase, \
    register_custom_encoder, ENCODER_REGISTRY, fc_layer
from sample_factory.algorithms.utils.pytorch_utils import calc_num_elements

from gym_art.quadrotor_multi.params import obs_self_size_dict, obs_obst_size_dict, obs_neighbor_size_dict


class QuadNeighborhoodEncoder(nn.Module):
    def __init__(self, cfg, self_obs_dim, neighbor_obs_dim, neighbor_hidden_size, num_use_neighbor_obs):
        super().__init__()
        self.cfg = cfg
        self.self_obs_dim = self_obs_dim
        self.neighbor_obs_dim = neighbor_obs_dim
        self.neighbor_hidden_size = neighbor_hidden_size
        self.num_use_neighbor_obs = num_use_neighbor_obs


class QuadNeighborhoodEncoderDeepsets(QuadNeighborhoodEncoder):
    def __init__(self, cfg, neighbor_obs_dim, neighbor_hidden_size, use_spectral_norm, self_obs_dim, num_use_neighbor_obs):
        super().__init__(cfg, self_obs_dim, neighbor_obs_dim, neighbor_hidden_size, num_use_neighbor_obs)

        self.embedding_mlp = nn.Sequential(
            fc_layer(neighbor_obs_dim, neighbor_hidden_size, spec_norm=use_spectral_norm),
            nonlinearity(cfg),
            fc_layer(neighbor_hidden_size, neighbor_hidden_size, spec_norm=use_spectral_norm),
            nonlinearity(cfg)
        )

    def forward(self, self_obs, obs, all_neighbor_obs_size, batch_size, nbr_obs=None):
        obs_neighbors = obs[:, self.self_obs_dim:self.self_obs_dim + all_neighbor_obs_size] if nbr_obs is None else nbr_obs
        obs_neighbors = obs_neighbors.reshape(-1, self.neighbor_obs_dim)
        neighbor_embeds = self.embedding_mlp(obs_neighbors)
        neighbor_embeds = neighbor_embeds.reshape(batch_size, -1, self.neighbor_hidden_size)
        mean_embed = torch.mean(neighbor_embeds, dim=1)
        return mean_embed


class QuadNeighborhoodEncoderAttention(QuadNeighborhoodEncoder):
    def __init__(self, cfg, neighbor_obs_dim, neighbor_hidden_size, use_spectral_norm, self_obs_dim, num_use_neighbor_obs):
        super().__init__(cfg, self_obs_dim, neighbor_obs_dim, neighbor_hidden_size, num_use_neighbor_obs)

        self.self_obs_dim = self_obs_dim

        # outputs e_i from the paper
        self.embedding_mlp = nn.Sequential(
            fc_layer(self_obs_dim + neighbor_obs_dim, neighbor_hidden_size, spec_norm=use_spectral_norm),
            nonlinearity(cfg),
            fc_layer(neighbor_hidden_size, neighbor_hidden_size, spec_norm=use_spectral_norm),
            nonlinearity(cfg)
        )

        #  outputs h_i from the paper
        self.neighbor_value_mlp = nn.Sequential(
            fc_layer(neighbor_hidden_size, neighbor_hidden_size, spec_norm=use_spectral_norm),
            nonlinearity(cfg),
            fc_layer(neighbor_hidden_size, neighbor_hidden_size, spec_norm=use_spectral_norm),
            nonlinearity(cfg),
        )

        # outputs scalar score alpha_i for each neighbor i
        self.attention_mlp = nn.Sequential(
            fc_layer(neighbor_hidden_size * 2, neighbor_hidden_size, spec_norm=use_spectral_norm),  # neighbor_hidden_size * 2 because we concat e_i and e_m
            nonlinearity(cfg),
            fc_layer(neighbor_hidden_size, neighbor_hidden_size, spec_norm=use_spectral_norm),
            nonlinearity(cfg),
            fc_layer(neighbor_hidden_size, 1),
        )

    def forward(self, self_obs, obs, all_neighbor_obs_size, batch_size, nbr_obs=None):
        obs_neighbors = obs[:, self.self_obs_dim:self.self_obs_dim + all_neighbor_obs_size]
        obs_neighbors = obs_neighbors.reshape(-1, self.neighbor_obs_dim)

        # concatenate self observation with neighbor observation

        self_obs_repeat = self_obs.repeat(self.num_use_neighbor_obs, 1)
        mlp_input = torch.cat((self_obs_repeat, obs_neighbors), dim=1)
        neighbor_embeddings = self.embedding_mlp(mlp_input)  # e_i in the paper https://arxiv.org/pdf/1809.08835.pdf

        neighbor_values = self.neighbor_value_mlp(neighbor_embeddings)  # h_i in the paper

        neighbor_embeddings_mean_input = neighbor_embeddings.reshape(batch_size, -1, self.neighbor_hidden_size)
        neighbor_embeddings_mean = torch.mean(neighbor_embeddings_mean_input, dim=1)  # e_m in the paper
        neighbor_embeddings_mean_repeat = neighbor_embeddings_mean.repeat(self.num_use_neighbor_obs, 1)

        attention_mlp_input = torch.cat((neighbor_embeddings, neighbor_embeddings_mean_repeat), dim=1)
        attention_weights = self.attention_mlp(attention_mlp_input).view(batch_size, -1)  # alpha_i in the paper
        attention_weights_softmax = torch.nn.functional.softmax(attention_weights, dim=1)
        attention_weights_softmax = attention_weights_softmax.view(-1, 1)

        final_neighborhood_embedding = attention_weights_softmax * neighbor_values
        final_neighborhood_embedding = final_neighborhood_embedding.view(batch_size, -1, self.neighbor_hidden_size)
        final_neighborhood_embedding = torch.sum(final_neighborhood_embedding, dim=1)

        return final_neighborhood_embedding


class QuadNeighborhoodEncoderMlp(QuadNeighborhoodEncoder):
    def __init__(self, cfg, neighbor_obs_dim, neighbor_hidden_size, use_spectral_norm, self_obs_dim, num_use_neighbor_obs):
        super().__init__(cfg, self_obs_dim, neighbor_obs_dim, neighbor_hidden_size, num_use_neighbor_obs)

        self.self_obs_dim = self_obs_dim

        self.neighbor_mlp = nn.Sequential(
            fc_layer(neighbor_obs_dim * num_use_neighbor_obs, neighbor_hidden_size, spec_norm=use_spectral_norm),
            nonlinearity(cfg),
            fc_layer(neighbor_hidden_size, neighbor_hidden_size, spec_norm=use_spectral_norm),
            nonlinearity(cfg),
            fc_layer(neighbor_hidden_size, neighbor_hidden_size, spec_norm=use_spectral_norm),
            nonlinearity(cfg),
        )

    def forward(self, self_obs, obs, all_neighbor_obs_size, batch_size, nbr_obs=None):
        obs_neighbors = obs[:, self.self_obs_dim:self.self_obs_dim + all_neighbor_obs_size]
        final_neighborhood_embedding = self.neighbor_mlp(obs_neighbors)
        return final_neighborhood_embedding


class QuadUniformNeighborEncoder(EncoderBase):
    # use one encoder for obstacles and drones. Assumes same observation space for both objects
    def __init__(self, cfg, obs_space, timing):
        super().__init__(cfg, timing)
        self.self_obs_dim = obs_self_size_dict[cfg.quads_obs_repr]
        self.neighbor_hidden_size = cfg.quads_neighbor_hidden_size
        self.neighbor_obs_type = cfg.neighbor_obs_type
        assert self.neighbor_obs_type == 'pos_vel_size', "Neighbor obs type must be pos_vel_size in order to combine with obstacle obs"

        self.use_spectral_norm = cfg.use_spectral_norm
        self.obstacle_mode = cfg.quads_obstacle_mode
        self.quads_obst_model_type = cfg.quads_obst_model_type
        assert self.obstacle_mode != 'no_obstacles', "Only use this encoder when combining obstacle obs and drone obs into one network"
        self.obst_type = cfg.obst_obs_type
        assert self.obst_type == 'pos_vel_size', "Obstacle obs type must be pos_vel_size in order to combine with drone obs"


        self.num_use_neighbor_obs = cfg.nearest_nbrs

        self.neighbor_obs_dim = obs_neighbor_size_dict[self.neighbor_obs_type]

        # encode the neighboring drone's observations
        neighbor_encoder_out_size = 0
        self.neighbor_encoder = None

        if self.num_use_neighbor_obs > 0:
            neighbor_encoder_type = cfg.quads_neighbor_encoder_type
            if neighbor_encoder_type == 'mean_embed':
                self.neighbor_encoder = QuadNeighborhoodEncoderDeepsets(cfg, self.neighbor_obs_dim,
                                                                        self.neighbor_hidden_size, self.use_spectral_norm,
                                                                        self.self_obs_dim, self.num_use_neighbor_obs)
            elif neighbor_encoder_type == 'attention':
                self.neighbor_encoder = QuadNeighborhoodEncoderAttention(cfg, self.neighbor_obs_dim,
                                                                         self.neighbor_hidden_size, self.use_spectral_norm,
                                                                         self.self_obs_dim, self.num_use_neighbor_obs)
            elif neighbor_encoder_type == 'mlp':
                self.neighbor_encoder = QuadNeighborhoodEncoderMlp(cfg, self.neighbor_obs_dim,
                                                                   self.neighbor_hidden_size, self.use_spectral_norm,
                                                                   self.self_obs_dim, self.num_use_neighbor_obs)
            elif neighbor_encoder_type == 'no_encoder':
                self.neighbor_encoder = None  # blind agent
            else:
                raise NotImplementedError

        if self.neighbor_encoder:
            neighbor_encoder_out_size = self.neighbor_hidden_size

        fc_encoder_layer = cfg.hidden_size
        # encode the current drone's observations
        self.self_encoder = nn.Sequential(
            fc_layer(self.self_obs_dim, fc_encoder_layer, spec_norm=self.use_spectral_norm),
            nonlinearity(cfg),
            fc_layer(fc_encoder_layer, fc_encoder_layer, spec_norm=self.use_spectral_norm),
            nonlinearity(cfg)
        )
        self_encoder_out_size = calc_num_elements(self.self_encoder, (self.self_obs_dim,))

        total_encoder_out_size = self_encoder_out_size + neighbor_encoder_out_size

        # this is followed by another fully connected layer in the action parameterization, so we add a nonlinearity here
        self.feed_forward = nn.Sequential(
            fc_layer(total_encoder_out_size, 2 * cfg.hidden_size, spec_norm=self.use_spectral_norm),
            nn.Tanh(),
        )

        self.encoder_out_size = 2 * cfg.hidden_size


    def forward(self, obs_dict):
        obs = obs_dict['obs']
        obs_self = obs[:, :self.self_obs_dim]
        self_embed = self.self_encoder(obs_self)
        embeddings = self_embed
        batch_size = obs_self.shape[0]
        # relative xyz and vxyz for the entire minibatch (batch dimension is batch_size * num_neighbors)
        all_neighbor_obs_size = self.neighbor_obs_dim * self.num_use_neighbor_obs
        neighborhood_embedding = self.neighbor_encoder(obs_self, obs, all_neighbor_obs_size, batch_size)
        embeddings = torch.cat((embeddings, neighborhood_embedding), dim=1)

        out = self.feed_forward(embeddings)
        return out


class QuadMultiEncoder(EncoderBase):
    # Mean embedding encoder based on the DeepRL for Swarms Paper
    def __init__(self, cfg, obs_space, timing):
        super().__init__(cfg, timing)
        # internal params -- cannot change from cmd line
        self.self_obs_dim = obs_self_size_dict[cfg.quads_obs_repr]
        self.neighbor_hidden_size = cfg.quads_neighbor_hidden_size

        self.neighbor_obs_type = cfg.neighbor_obs_type
        self.use_spectral_norm = cfg.use_spectral_norm
        self.obstacle_mode = cfg.quads_obstacle_mode

        self.quads_obst_model_type = cfg.quads_obst_model_type

        if cfg.quads_local_obst_obs == -1:
            self.local_obstacle_num = cfg.quads_obstacle_num
        else:
            self.local_obstacle_num = cfg.quads_local_obst_obs

        if cfg.quads_local_obst_obs == 0 or cfg.quads_obstacle_mode == 'no_obstacles':
            self.use_obst_encoder = False
        else:
            self.use_obst_encoder = True

        if self.neighbor_obs_type == 'none':
            self.num_use_neighbor_obs = 0
        else:
            if cfg.quads_local_obs == -1:
                self.num_use_neighbor_obs = cfg.quads_num_agents - 1
            else:
                self.num_use_neighbor_obs = cfg.quads_local_obs

        self.neighbor_obs_dim = obs_neighbor_size_dict[self.neighbor_obs_type]

        self.obstacle_obs_dim = obs_obst_size_dict[cfg.obst_obs_type]

        # encode the neighboring drone's observations
        neighbor_encoder_out_size = 0
        self.neighbor_encoder = None

        if self.num_use_neighbor_obs > 0:
            neighbor_encoder_type = cfg.quads_neighbor_encoder_type
            if neighbor_encoder_type == 'mean_embed':
                self.neighbor_encoder = QuadNeighborhoodEncoderDeepsets(cfg, self.neighbor_obs_dim,
                                                                        self.neighbor_hidden_size, self.use_spectral_norm,
                                                                        self.self_obs_dim, self.num_use_neighbor_obs)
            elif neighbor_encoder_type == 'attention':
                self.neighbor_encoder = QuadNeighborhoodEncoderAttention(cfg, self.neighbor_obs_dim,
                                                                         self.neighbor_hidden_size, self.use_spectral_norm,
                                                                         self.self_obs_dim, self.num_use_neighbor_obs)
            elif neighbor_encoder_type == 'mlp':
                self.neighbor_encoder = QuadNeighborhoodEncoderMlp(cfg, self.neighbor_obs_dim,
                                                                   self.neighbor_hidden_size, self.use_spectral_norm,
                                                                   self.self_obs_dim, self.num_use_neighbor_obs)
            elif neighbor_encoder_type == 'no_encoder':
                self.neighbor_encoder = None  # blind agent
            else:
                raise NotImplementedError

        if self.neighbor_encoder:
            neighbor_encoder_out_size = self.neighbor_hidden_size

        fc_encoder_layer = cfg.hidden_size
        # encode the current drone's observations
        self.self_encoder = nn.Sequential(
            fc_layer(self.self_obs_dim, fc_encoder_layer, spec_norm=self.use_spectral_norm),
            nonlinearity(cfg),
            fc_layer(fc_encoder_layer, fc_encoder_layer, spec_norm=self.use_spectral_norm),
            nonlinearity(cfg)
        )
        self_encoder_out_size = calc_num_elements(self.self_encoder, (self.self_obs_dim,))

        # encode the obstacle observations
        obstacle_encoder_out_size = 0
        if self.obstacle_mode != 'no_obstacles' and self.use_obst_encoder:
            if cfg.quads_larger_obst_encoder:
                self.obstacle_hidden_size = cfg.quads_obstacle_hidden_size  # internal param
                self.obstacle_encoder = nn.Sequential(
                    fc_layer(self.obstacle_obs_dim, self.obstacle_hidden_size, spec_norm=self.use_spectral_norm),
                    nonlinearity(cfg),
                    fc_layer(self.obstacle_hidden_size, self.obstacle_hidden_size * 2, spec_norm=self.use_spectral_norm),
                    nonlinearity(cfg),
                    fc_layer(self.obstacle_hidden_size * 2, self.obstacle_hidden_size * 2, spec_norm=self.use_spectral_norm),
                    nonlinearity(cfg),
                    fc_layer(self.obstacle_hidden_size * 2, self.obstacle_hidden_size * 2, spec_norm=self.use_spectral_norm),
                    nonlinearity(cfg),
                    fc_layer(self.obstacle_hidden_size * 2, self.obstacle_hidden_size, spec_norm=self.use_spectral_norm),
                    nonlinearity(cfg),
                )
                obstacle_encoder_out_size = calc_num_elements(self.obstacle_encoder, (self.obstacle_obs_dim,))
            else:
                self.obstacle_hidden_size = cfg.quads_obstacle_hidden_size  # internal param
                self.obstacle_encoder = nn.Sequential(
                    fc_layer(self.obstacle_obs_dim, self.obstacle_hidden_size, spec_norm=self.use_spectral_norm),
                    nonlinearity(cfg),
                    fc_layer(self.obstacle_hidden_size, self.obstacle_hidden_size, spec_norm=self.use_spectral_norm),
                    nonlinearity(cfg),
                )
                obstacle_encoder_out_size = calc_num_elements(self.obstacle_encoder, (self.obstacle_obs_dim,))

        if self.quads_obst_model_type == 'nei_obst':
            in_size = neighbor_encoder_out_size + obstacle_encoder_out_size
            out_size = neighbor_encoder_out_size + obstacle_encoder_out_size
            self.feed_forward_nei_obst = nn.Sequential(
                fc_layer(in_size, out_size, spec_norm=self.use_spectral_norm),
                nn.Tanh(),
            )

        total_encoder_out_size = self_encoder_out_size + neighbor_encoder_out_size + obstacle_encoder_out_size

        # this is followed by another fully connected layer in the action parameterization, so we add a nonlinearity here
        self.feed_forward = nn.Sequential(
            fc_layer(total_encoder_out_size, 2 * cfg.hidden_size, spec_norm=self.use_spectral_norm),
            nn.Tanh(),
        )

        self.encoder_out_size = 2 * cfg.hidden_size

    def forward(self, obs_dict):
        obs = obs_dict['obs']
        obs_self = obs[:, :self.self_obs_dim]
        self_embed = self.self_encoder(obs_self)
        embeddings = self_embed
        # embeddings = obs_self
        batch_size = obs_self.shape[0]
        # relative xyz and vxyz for the entire minibatch (batch dimension is batch_size * num_neighbors)
        all_neighbor_obs_size = self.neighbor_obs_dim * self.num_use_neighbor_obs

        if self.quads_obst_model_type == 'nei_obst':
            neighborhood_embedding = self.neighbor_encoder(obs_self, obs, all_neighbor_obs_size, batch_size)
            # embeddings = torch.cat((embeddings, neighborhood_embedding), dim=1)

            obs_obstacles = obs[:, self.self_obs_dim + all_neighbor_obs_size:]
            obs_obstacles = obs_obstacles.reshape(-1, self.obstacle_obs_dim)
            obstacle_embeds = self.obstacle_encoder(obs_obstacles)
            obstacle_embeds = obstacle_embeds.reshape(batch_size, -1, self.obstacle_hidden_size)
            obstacle_mean_embed = torch.mean(obstacle_embeds, dim=1)
            # embeddings = torch.cat((embeddings, obstacle_mean_embed), dim=1)

            neighbot_obst_embeddings = torch.cat((neighborhood_embedding, obstacle_mean_embed), dim=1)

            out_nei_obst = self.feed_forward_nei_obst(neighbot_obst_embeddings)

            embeddings = torch.cat((embeddings, out_nei_obst), dim=1)

        else:
            if self.num_use_neighbor_obs > 0 and self.neighbor_encoder:
                neighborhood_embedding = self.neighbor_encoder(obs_self, obs, all_neighbor_obs_size, batch_size)
                embeddings = torch.cat((embeddings, neighborhood_embedding), dim=1)

            if self.obstacle_mode != 'no_obstacles' and self.use_obst_encoder:
                obs_obstacles = obs[:, self.self_obs_dim + all_neighbor_obs_size:]
                obs_obstacles = obs_obstacles.reshape(-1, self.obstacle_obs_dim)
                obstacle_embeds = self.obstacle_encoder(obs_obstacles)
                obstacle_embeds = obstacle_embeds.reshape(batch_size, -1, self.obstacle_hidden_size)
                obstacle_mean_embed = torch.mean(obstacle_embeds, dim=1)
                embeddings = torch.cat((embeddings, obstacle_mean_embed), dim=1)

        out = self.feed_forward(embeddings)
        return out


def register_models():
    quad_custom_encoder_name = 'quad_multi_encoder'
    if quad_custom_encoder_name not in ENCODER_REGISTRY:
        register_custom_encoder(quad_custom_encoder_name, QuadUniformNeighborEncoder)
