import os
import time
import torch
import torch.nn as nn
from torch.distributions import MultivariateNormal
import numpy as np
import numpy.random as rd
from copy import deepcopy
from tensorboardX import SummaryWriter


def layer_norm(layer, std=1.0, bias_const=1e-6):
    torch.nn.init.orthogonal_(layer.weight, std)
    torch.nn.init.constant_(layer.bias, bias_const)


class ActorMPO(nn.Module):
    def __init__(self, mid_dim, state_dim, action_dim):
        super().__init__()
        self.action_dim = action_dim
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        lay_dim = mid_dim
        self.net_state = nn.Sequential(nn.Linear(state_dim, mid_dim), nn.ReLU(),
                                       # nn.Linear(mid_dim, lay_dim), nn.ReLU(),
                                       nn.Linear(mid_dim, lay_dim), nn.ReLU())
        # nn.Linear(mid_dim, lay_dim), nn.Hardswish())
        self.net_a_loc = nn.Linear(lay_dim, action_dim)  # the average of action
        self.net_a_cholesky = nn.Linear(lay_dim, (action_dim * (action_dim + 1)) // 2)
        self.softplus = nn.Softplus(threshold=18.)
        layer_norm(self.net_a_loc, std=0.01)  # output layer for action, it is no necessary.
        layer_norm(self.net_a_cholesky, std=0.01)

    def forward(self, state):
        return self.net_a_loc(self.net_state(state)).tanh()  # action

    def get_distribution(self, state):
        loc, cholesky = self.get_loc_cholesky(state)
        return MultivariateNormal(loc=loc, scale_tril=cholesky)

    def get_loc_cholesky(self, state):
        t_tmp = self.net_state(state)
        a_loc = self.net_a_loc(t_tmp)  # NOTICE! it is a_loc without .tanh()
        a_cholesky_vector = self.net_a_cholesky(t_tmp)
        cholesky_diag_index = torch.arange(self.action_dim, dtype=torch.long) + 1
        cholesky_diag_index = (cholesky_diag_index * (cholesky_diag_index + 1)) // 2 - 1
        a_cholesky_vector[:, cholesky_diag_index] = self.softplus(a_cholesky_vector[:, cholesky_diag_index])
        tril_indices = torch.tril_indices(row=self.action_dim, col=self.action_dim, offset=0)
        a_cholesky = torch.zeros(size=(a_loc.shape[0], self.action_dim, self.action_dim), dtype=torch.float32,
                                 device=t_tmp.device)
        a_cholesky[:, tril_indices[0], tril_indices[1]] = a_cholesky_vector
        return a_loc, a_cholesky

    def get_action(self, state):
        pi = self.get_distribution(state)
        return pi.sample()  # re-parameterize

    def get_actions(self, state, sampled_actions_num):
        pi_action = self.get_distribution(state)
        return pi_action.sample((sampled_actions_num,))


class CriticTwin(nn.Module):
    def __init__(self, mid_dim, state_dim, action_dim, if_use_dn=False):
        super().__init__()

        self.net_sa = nn.Sequential(nn.Linear(state_dim + action_dim, mid_dim), nn.ReLU(),
                                    nn.Linear(mid_dim, mid_dim), nn.ReLU(),
                                    nn.Linear(mid_dim, mid_dim), nn.ReLU())
        out_dim = mid_dim
        self.net_q1 = nn.Linear(out_dim, 1)
        self.net_q2 = nn.Linear(out_dim, 1)
        layer_norm(self.net_q1, std=0.1)
        layer_norm(self.net_q2, std=0.1)

    def forward(self, state, action):
        tmp = self.net_sa(torch.cat((state, action), dim=1))
        return self.net_q1(tmp)  # one Q value

    def get_q1_q2(self, state, action):
        tmp = self.net_sa(torch.cat((state, action), dim=1))
        return self.net_q1(tmp), self.net_q2(tmp)  # two Q values


class BinarySearchTree:
    """Binary Search Tree for PER

    Contributor: Github GyChou, Github mississippiu
    Reference: https://github.com/kaixindelele/DRLib/tree/main/algos/pytorch/td3_sp
    Reference: https://github.com/jaromiru/AI-blog/blob/master/SumTree.py
    """

    def __init__(self, memo_len):
        self.memo_len = memo_len  # replay buffer len
        self.prob_ary = np.zeros((memo_len - 1) + memo_len)  # parent_nodes_num + leaf_nodes_num
        self.max_len = len(self.prob_ary)
        self.now_len = self.memo_len - 1  # pointer
        self.indices = None
        self.depth = int(np.log2(self.max_len))

        # PER.  Prioritized Experience Replay. Section 4
        # alpha, beta = 0.7, 0.5 for rank-based variant
        # alpha, beta = 0.6, 0.4 for proportional variant
        self.per_alpha = 0.6  # alpha = (Uniform:0, Greedy:1)
        self.per_beta = 0.4  # beta = (PER:0, NotPER:1)

    def update_id(self, data_id, prob=10):  # 10 is max_prob
        tree_id = data_id + self.memo_len - 1
        if self.now_len == tree_id:
            self.now_len += 1

        delta = prob - self.prob_ary[tree_id]
        self.prob_ary[tree_id] = prob

        while tree_id != 0:  # propagate the change through tree
            tree_id = (tree_id - 1) // 2  # faster than the recursive loop
            self.prob_ary[tree_id] += delta

    def update_ids(self, data_ids, prob=10):  # 10 is max_prob
        ids = data_ids + self.memo_len - 1
        self.now_len += (ids >= self.now_len).sum()

        upper_step = self.depth - 1
        self.prob_ary[ids] = prob  # here, ids means the indices of given children (maybe the right ones or left ones)
        p_ids = (ids - 1) // 2

        while upper_step:  # propagate the change through tree
            ids = p_ids * 2 + 1  # in this while loop, ids means the indices of the left children
            self.prob_ary[p_ids] = self.prob_ary[ids] + self.prob_ary[ids + 1]
            p_ids = (p_ids - 1) // 2
            upper_step -= 1

        self.prob_ary[0] = self.prob_ary[1] + self.prob_ary[2]
        # because we take depth-1 upper steps, ps_tree[0] need to be updated alone

    def get_leaf_id(self, v):
        """Tree structure and array storage:

        Tree index:
              0       -> storing priority sum
            |  |
          1     2
         | |   | |
        3  4  5  6    -> storing priority for transitions
        Array type for storing: [0, 1, 2, 3, 4, 5, 6]
        """
        parent_idx = 0
        while True:
            l_idx = 2 * parent_idx + 1  # the leaf's left node
            r_idx = l_idx + 1  # the leaf's right node
            if l_idx >= (len(self.prob_ary)):  # reach bottom, end search
                leaf_idx = parent_idx
                break
            else:  # downward search, always search for a higher priority node
                if v <= self.prob_ary[l_idx]:
                    parent_idx = l_idx
                else:
                    v -= self.prob_ary[l_idx]
                    parent_idx = r_idx
        return min(leaf_idx, self.now_len - 2)  # leaf_idx

    def get_indices_is_weights(self, batch_size, beg, end):
        self.per_beta = min(1., self.per_beta + 0.001)

        # get random values for searching indices with proportional prioritization
        values = (rd.rand(batch_size) + np.arange(batch_size)) * (self.prob_ary[0] / batch_size)

        # get proportional prioritization
        leaf_ids = np.array([self.get_leaf_id(v) for v in values])
        self.indices = leaf_ids - (self.memo_len - 1)

        prob_ary = self.prob_ary[leaf_ids] / self.prob_ary[beg:end].min()
        is_weights = np.power(prob_ary, -self.per_beta)  # important sampling weights
        return self.indices, is_weights

    def td_error_update(self, td_error):  # td_error = (q-q).detach_().abs()
        prob = td_error.squeeze().clamp(1e-6, 10).pow(self.per_alpha)
        prob = prob.cpu().numpy()
        self.update_ids(self.indices, prob)


class ReplayBuffer:
    def __init__(self, cwd, max_len, state_dim, action_dim, if_per, if_save_buffer=False):
        """Experience Replay Buffer

        save environment transition in a continuous RAM for high performance training
        we save trajectory in order and save state and other (action, reward, mask, ...) separately.

        `int max_len` the maximum capacity of ReplayBuffer. First In First Out
        `int state_dim` the dimension of state
        `int action_dim` the dimension of action (action_dim==1 for discrete action)
        `bool if_per` Prioritized Experience Replay for sparse reward
        """
        self.cwd = cwd
        self.device = torch.device("cpu")
        self.max_len = max_len
        self.now_len = 0
        self.next_idx = 0
        self.if_full = False
        self.action_dim = action_dim
        self.if_per = if_per
        self.if_save_buffer = if_save_buffer
        if if_per:
            self.tree = BinarySearchTree(max_len)

        self.buf_state = torch.empty((max_len, state_dim), dtype=torch.float32, device=self.device)
        self.buf_action = torch.empty((max_len, action_dim), dtype=torch.float32, device=self.device)
        self.buf_reward = torch.empty((max_len, 1), dtype=torch.float32, device=self.device)
        self.buf_mask = torch.empty((max_len, 1), dtype=torch.float32, device=self.device)

    def append_buffer(self, state, action, reward, mask):  # CPU array to CPU array
        state = torch.as_tensor(state, dtype=torch.float32, device=self.device)
        action = torch.as_tensor(action, dtype=torch.float32, device=self.device)
        reward = torch.as_tensor(reward, dtype=torch.float32, device=self.device)
        mask = torch.as_tensor(mask, dtype=torch.float32, device=self.device)

        self.buf_state[self.next_idx] = state
        self.buf_action[self.next_idx] = action
        self.buf_reward[self.next_idx] = reward
        self.buf_mask[self.next_idx] = mask

        if self.if_per:
            self.tree.update_id(self.next_idx)

        self.next_idx += 1
        if self.next_idx >= self.max_len:
            self.if_full = True
            self.next_idx = 0

        if self.if_full:
            if self.if_save_buffer:
                self.save_buffer()
                self.if_save_buffer = False

    def extend_buffer(self, state, action, reward, mask):  # CPU array to CPU array
        state = torch.as_tensor(state, dtype=torch.float32, device=self.device)
        action = torch.as_tensor(action, dtype=torch.float32, device=self.device)
        reward = torch.as_tensor(reward, dtype=torch.float32, device=self.device)
        mask = torch.as_tensor(mask, dtype=torch.float32, device=self.device)

        size = len(state)
        next_idx = self.next_idx + size

        if self.if_per:
            for data_id in (np.arange(self.next_idx, next_idx) % self.max_len):
                self.tree.update_ids(data_id)

        if next_idx > self.max_len:
            if next_idx > self.max_len:
                self.buf_state[self.next_idx:self.max_len] = state[:self.max_len - self.next_idx]
                self.buf_action[self.next_idx:self.max_len] = action[:self.max_len - self.next_idx]
                self.buf_reward[self.next_idx:self.max_len] = reward[:self.max_len - self.next_idx]
                self.buf_mask[self.next_idx:self.max_len] = mask[:self.max_len - self.next_idx]
            self.if_full = True
            next_idx = next_idx - self.max_len

            self.buf_state[0:next_idx] = state[-next_idx:]
            self.buf_action[0:next_idx] = action[-next_idx:]
            self.buf_reward[0:next_idx] = reward[-next_idx:]
            self.buf_mask[0:next_idx] = mask[-next_idx:]
        else:
            self.buf_state[self.next_idx:next_idx] = state
            self.buf_action[self.next_idx:next_idx] = action
            self.buf_reward[self.next_idx:next_idx] = reward
            self.buf_mask[self.next_idx:next_idx] = mask
        self.next_idx = next_idx

    def sample_batch(self, batch_size) -> tuple:
        """randomly sample a batch of data for training

        :int batch_size: the number of data in a batch for Stochastic Gradient Descent
        :return torch.Tensor reward: reward.shape==(now_len, 1)
        :return torch.Tensor mask:   mask.shape  ==(now_len, 1), mask = 0.0 if done else gamma
        :return torch.Tensor action: action.shape==(now_len, action_dim)
        :return torch.Tensor state:  state.shape ==(now_len, state_dim)
        :return torch.Tensor state:  state.shape ==(now_len, state_dim), next state
        """
        if self.if_per:
            beg = -self.max_len
            end = (self.now_len - self.max_len) if (self.now_len < self.max_len) else None

            indices, is_weights = self.tree.get_indices_is_weights(batch_size, beg, end)

            return (self.buf_reward[indices],
                    self.buf_mask[indices],
                    self.buf_action[indices],
                    self.buf_state[indices],
                    self.buf_state[indices + 1],
                    torch.as_tensor(is_weights, dtype=torch.float32, device=self.device))
        else:
            indices = torch.randint(self.now_len - 1, size=(batch_size,), device=self.device)
            return (self.buf_reward[indices],
                    self.buf_mask[indices],
                    self.buf_action[indices],
                    self.buf_state[indices],
                    self.buf_state[indices + 1])

    def update_now_len_before_sample(self):
        """update the a pointer `now_len`, which is the current data number of ReplayBuffer
        """
        self.now_len = self.max_len if self.if_full else self.next_idx

    def empty_buffer_before_explore(self):
        """we empty the buffer by set now_len=0. On-policy need to empty buffer before exploration
        """
        self.next_idx = 0
        self.now_len = 0
        self.if_full = False

    def save_buffer(self, file_name='buffer_data'):
        self.update_now_len_before_sample()
        os.makedirs(self.cwd + '/' + file_name, exist_ok=True)
        np.save(self.cwd + '/' + file_name + '/state', self.buf_state[:self.now_len].detach().numpy())
        np.save(self.cwd + '/' + file_name + '/action', self.buf_action[:self.now_len].detach().numpy())
        np.save(self.cwd + '/' + file_name + '/reward', self.buf_reward[:self.now_len].detach().numpy())
        np.save(self.cwd + '/' + file_name + '/mask', self.buf_mask[:self.now_len].detach().numpy())
        print("Saved " + file_name + " in " + self.cwd)

    def load_buffer(self, file_path):
        state = np.load(file_path + '/state.npy')
        action = np.load(file_path + '/action.npy')
        reward = np.load(file_path + '/reward.npy')
        mask = np.load(file_path + '/mask.npy')
        if state.shape[0] >= self.max_len:
            print(f"Buffer len is too short, update max_len with {state.shape[0]}")
            self.now_len = self.max_len = state.shape[0]
            self.next_idx = 0
            self.if_full = True
            self.buf_state = torch.tensor(state, dtype=torch.float32, device=self.device)
            self.buf_action = torch.tensor(action, dtype=torch.float32, device=self.device)
            self.buf_reward = torch.tensor(reward, dtype=torch.float32, device=self.device)
            self.buf_mask = torch.tensor(mask, dtype=torch.float32, device=self.device)
        else:
            self.now_len = state.shape[0]
            self.next_idx = self.now_len
            self.if_full = False
            self.buf_state[:state.shape[0], :] = torch.tensor(state, dtype=torch.float32, device=self.device)
            self.buf_action[:state.shape[0], :] = torch.tensor(action, dtype=torch.float32, device=self.device)
            self.buf_reward[:state.shape[0], :] = torch.tensor(reward, dtype=torch.float32, device=self.device)
            self.buf_mask[:state.shape[0], :] = torch.tensor(mask, dtype=torch.float32, device=self.device)
        print("Loaded in " + file_path)


class AgentMPO():
    def __init__(self, args=None):
        self.device_name = "cpu" if args is None else args.device_name
        self.learning_rate = 1e-4 if args is None else args.agent['learning_rate']
        self.soft_update_tau = 2 ** -8 if args is None else args.agent['soft_update_tau']  # 5e-3 ~= 2 ** -8

        self.epsilon = 1e-1
        self.epsilon_penalty = 1e-3
        self._num_samples = 64 if args is None else args.agent['num_samples']
        self.epsilon_mean = 1e-2 if args is None else args.agent['epsilon_mean']
        self.epsilon_stddev = 1e-4 if args is None else args.agent['epsilon_stddev']
        self.init_log_temperature = 1. if args is None else args.agent['init_log_temperature']
        self.init_log_alpha_mean = 1. if args is None else args.agent['init_log_alpha_mean']
        self.init_log_alpha_stddev = 10. if args is None else args.agent['init_log_alpha_stddev']
        self._per_dim_constraining = True
        self._action_penalization = True

        self.MPO_FLOAT_EPSILON = 1e-8
        self.dual_learning_rate = 1e-2
        self.update_index = 0
        self.update_period = 10
        self.train_record = {}

    def init(self, net_dim, state_dim, action_dim, if_per=False):
        self.state_dim = state_dim
        self.action_dim = action_dim
        self.device = torch.device(self.device_name)

        d = self.action_dim if self._per_dim_constraining else 1
        self.log_alpha_mean = self.init_log_alpha_mean * torch.ones(d, dtype=torch.float32, device=self.device)
        self.log_alpha_stddev = self.init_log_alpha_stddev * torch.ones(d, dtype=torch.float32, device=self.device)
        self.log_temperature = self.init_log_temperature * torch.ones(1, dtype=torch.float32, device=self.device)
        self.log_alpha_mean = torch.autograd.Variable(self.log_alpha_mean, requires_grad=True)
        self.log_alpha_stddev = torch.autograd.Variable(self.log_alpha_stddev, requires_grad=True)
        self.log_temperature = torch.autograd.Variable(self.log_temperature, requires_grad=True)
        self.dual_optimizer = torch.optim.Adam((self.log_alpha_mean,
                                                self.log_alpha_stddev,
                                                self.log_temperature), self.dual_learning_rate)
        self.log_num_actions = np.log(self._num_samples)
        self.act = ActorMPO(net_dim, state_dim, action_dim).to(self.device)
        self.act_target = deepcopy(self.act)
        self.cri = CriticTwin(net_dim, state_dim, action_dim).to(self.device)
        self.cri_target = deepcopy(self.cri)

        self.act_optimizer = torch.optim.Adam(self.act.parameters(), lr=self.learning_rate)
        self.cri_optimizer = torch.optim.Adam(self.cri.parameters(), lr=self.learning_rate)
        self.criterion = torch.nn.SmoothL1Loss(reduction='none' if if_per else 'mean')
        self.softplus = torch.nn.Softplus(threshold=18)
        self.get_obj_critic = self.get_obj_critic_raw

    @staticmethod
    def select_action(policy, state, explore_rate=1.):
        states = torch.as_tensor((state,), dtype=torch.float32).detach_()
        action = policy.get_action(states)[0]
        return action.detach().numpy()

    def update_net_multi_step(self, buffer, target_step, batch_size, repeat_times):
        for i in range(int(target_step * repeat_times)):
            if_record = True if i == (int(target_step * repeat_times) - 1) else False
            train_record = self.update_net_one_step(buffer, batch_size, if_record)
        return train_record

    def update_net_one_step(self, buffer, batch_size, if_record):
        # Policy Evaluation
        obj_critic, target_pi, target_q, next_s, sampled_a = self.get_obj_critic(buffer, batch_size)
        self.cri_optimizer.zero_grad()
        obj_critic.backward()
        self.cri_optimizer.step()
        self.update_index += 1
        self.soft_update(self.cri_target, self.cri, self.soft_update_tau)
        # Policy Improvation
        # Sample N additional action for each state
        alpha_mean = self.softplus(input=self.log_alpha_mean) + self.MPO_FLOAT_EPSILON
        alpha_stddev = self.softplus(input=self.log_alpha_stddev) + self.MPO_FLOAT_EPSILON
        temperature = self.softplus(input=self.log_temperature) + self.MPO_FLOAT_EPSILON
        online_loc, online_cholesky = self.act.get_loc_cholesky(next_s)  # (B,)
        # with torch.no_grad():
        #     sampled_a = target_pi.sample((self._num_samples,))  # (N, B, dim-a)
        #     expanded_s = state[None, ...].expand(self._num_samples, -1, -1)  # (N, B, dim-s)
        #     target_q = torch.min(*self.cri_target.get_q1_q2(
        #         expanded_s.reshape(-1, state.shape[1]),  # (N * B, dim-s)
        #         sampled_a.tanh().reshape(-1, self.action_dim)  # (N * B, dim-a)
        #     )).reshape(self._num_samples, batch_size)  # (N, B, dim-a)
        # target_q = self.cri_target(
        #     expanded_s.reshape(-1, state.shape[1]),  # (N * B, dim-s)
        #     sampled_a.tanh().reshape(-1, self.action_dim)  # (N * B, dim-a)
        # ).reshape(self._num_samples, batch_size)  # (N, B, dim-a)

        # Decompose the online policy into fixed-mean & fixed-stddev distributions.
        # This has been documented as having better performance in bandit settings,
        # see e.g. https://arxiv.org/pdf/1812.02256.pdf.
        fixed_stddev_dist = MultivariateNormal(online_loc, scale_tril=target_pi.scale_tril)
        fixed_mean_dist = MultivariateNormal(target_pi.loc, scale_tril=online_cholesky)
        # Computes normalized importance weights for the policy optimization.
        tempered_q_values = target_q / temperature  # no grad
        normalized_weights = torch.softmax(tempered_q_values, dim=0).detach_()  # no grad

        # Compute the decomposed policy losses.
        loss_policy_mean = self.compute_cross_entropy_loss(
            sampled_a, normalized_weights, fixed_stddev_dist)
        loss_policy_stddev = self.compute_cross_entropy_loss(
            sampled_a, normalized_weights, fixed_mean_dist)
        # loss_policy = self.compute_cross_entropy_loss(sampled_a, normalized_weights, pi)

        # Compute the decomposed KL between the target and online policies.
        kl_mean = torch.distributions.kl_divergence(target_pi, fixed_stddev_dist)
        kl_stddev = torch.distributions.kl_divergence(target_pi, fixed_mean_dist)

        # Lagrangian Dual Problem
        loss_temperature = self.compute_temperature_loss(tempered_q_values, temperature, self.epsilon)

        # Compute the alpha-weighted KL-penalty loss
        loss_alpha_mean, loss_kl_mean = self.compute_lagrangian_multipliers_kl_loss(kl_mean, alpha_mean, self.epsilon)
        loss_alpha_stddev, loss_kl_stddev = self.compute_lagrangian_multipliers_kl_loss(kl_stddev, alpha_stddev,
                                                                                        self.epsilon)
        # loss_dual = loss_alpha_mean + loss_alpha_stddev + loss_temperature
        loss_dual = loss_temperature
        self.dual_optimizer.zero_grad()
        loss_dual.backward()
        self.dual_optimizer.step()

        # Lagrangian Primal Problem
        # Combine losses.
        loss_policy = loss_policy_mean + loss_policy_stddev
        loss_kl_penalty = loss_kl_mean + loss_kl_stddev
        loss = loss_policy + loss_kl_penalty
        # loss_dual = loss_alpha_mean + loss_alpha_stddev

        self.act_optimizer.zero_grad()
        loss.backward()
        self.act_optimizer.step()

        # self.update_index += 1
        # if self.update_index // self.update_period == 1:
        #     self.soft_update(self.act_target, self.act, 1.)
        #     self.update_index = 0
        self.soft_update(self.act_target, self.act, self.soft_update_tau)

        # debug
        self.train_record.update(a_avg=alpha_mean.mean().item(),
                                 a_std=alpha_stddev.mean().item(),
                                 t=temperature.item(),
                                 obj_a=loss_policy.item(),
                                 obj_c=obj_critic.item(),
                                 obj_t=loss_temperature.item(),
                                 obj_d=loss_dual.item(),
                                 kl_mean=torch.distributions.kl_divergence(target_pi, fixed_stddev_dist).mean(
                                     dim=0).item(),
                                 kl_std=torch.distributions.kl_divergence(target_pi, fixed_mean_dist).mean(
                                     dim=0).item(),
                                 est_q=torch.max(target_q, dim=0)[0].mean().item(),
                                 )
        return self.train_record

    # def get_obj_critic_raw(self, buffer, batch_size):
    #     with torch.no_grad():
    #         reward, mask, action, state, next_s = buffer.sample_batch(batch_size)
    #         reward = reward.to(self.device)
    #         mask = mask.to(self.device)
    #         action = action.to(self.device)
    #         state = state.to(self.device)
    #         next_s = next_s.to(self.device)
    #         target_pi = self.act_target.get_distribution(next_s)
    #         next_q = torch.min(*self.cri_target.get_q1_q2(next_s, target_pi.sample().tanh()))
    #         q_label = reward + mask * next_q
    #     q1, q2 = self.cri.get_q1_q2(state, action)
    #     obj_critic = self.criterion(q1, q_label) + self.criterion(q2, q_label)
    #     return obj_critic, target_pi, next_q, next_s

    def get_obj_critic_raw(self, buffer, batch_size):
        with torch.no_grad():
            reward, mask, action, state, next_s = buffer.sample_batch(batch_size)
            reward = reward.to(self.device)
            mask = mask.to(self.device)
            action = action.to(self.device)
            state = state.to(self.device)
            next_s = next_s.to(self.device)

            target_pi = self.act_target.get_distribution(next_s)
            sampled_next_a = target_pi.sample((self._num_samples,))  # (N, B, dim-action)
            ex_next_s = next_s[None, ...].expand(self._num_samples, -1, -1)  # (N, B, dim-action)
            ex_next_q = torch.min(*self.cri_target.get_q1_q2(
                ex_next_s.reshape(-1, self.state_dim),
                sampled_next_a.tanh().reshape(-1, self.action_dim)
            )).reshape(self._num_samples, batch_size)
            next_q = ex_next_q.mean(dim=0).unsqueeze(dim=1)
            q_label = reward + mask * next_q

        q1, q2 = self.cri.get_q1_q2(state, action.tanh())
        obj_critic = self.criterion(q1, q_label) + self.criterion(q2, q_label)
        return obj_critic, target_pi, ex_next_q, next_s, sampled_next_a

    def compute_temperature_loss(self,
                                 q_values: torch.Tensor,
                                 temperature: torch.autograd.Variable,
                                 epsilon: float):
        q_logsumexp = torch.logsumexp(q_values, axis=0)
        loss_temperature = epsilon + q_logsumexp.mean() - self.log_num_actions
        loss_temperature = temperature * loss_temperature
        return loss_temperature

    def compute_cross_entropy_loss(self,
                                   sampled_actions: torch.Tensor,
                                   normalized_weights: torch.Tensor,
                                   pi: torch.distributions.Distribution) -> torch.Tensor:
        # Compute the M-step loss.
        log_prob = pi.log_prob(sampled_actions)
        # Compute the weighted average log-prob using the normalized weights.
        loss_policy_gradient = - torch.sum(log_prob * normalized_weights, dim=0)
        # Return the mean loss over the batch of states.
        return torch.mean(loss_policy_gradient, dim=0)

    # def compute_parametric_kl_penalty_loss(self,
    #                                        kl: torch.Tensor,
    #                                        alpha: torch.autograd.Variable) -> [torch.Tensor]:
    #     # Compute the mean KL over the batch.
    #     mean_kl = torch.mean(kl, dim=0)
    #     # Compute the regularization.
    #     loss_kl = torch.mean(mean_kl * alpha.detach(), dim=0)
    #     # loss_kl = torch.sum(alpha.detach() * (epsilon - mean_kl), dim=0)
    #     return loss_kl

    def compute_lagrangian_multipliers_kl_loss(self,
                                               kl: torch.Tensor,
                                               alpha: torch.autograd.Variable,
                                               epsilon: float) -> [torch.Tensor]:
        # Compute the mean KL over the batch.
        mean_kl = torch.mean(kl, dim=0)
        loss_alpha = torch.sum(alpha * (epsilon - mean_kl.detach()), dim=0)
        loss_kl = torch.mean(mean_kl * alpha.detach(), dim=0)
        return loss_alpha, loss_kl

    def update_lagrangian_multipliers_sgd(self,
                                          kl: torch.Tensor,
                                          alpha: torch.autograd.Variable,
                                          epsilon: float) -> [torch.Tensor]:
        # Compute the mean KL over the batch.
        mean_kl = torch.mean(kl, dim=0)
        loss_alpha = torch.sum(alpha * (epsilon - mean_kl.detach()), dim=0)
        return loss_alpha

    def update_lagrangian_multipliers_pid(self,
                                          kl: torch.Tensor,
                                          alpha: torch.autograd.Variable,
                                          epsilon: float) -> [torch.Tensor]:
        # Compute the mean KL over the batch.
        mean_kl = torch.mean(kl, dim=0)
        loss_alpha = torch.sum(alpha * (epsilon - mean_kl.detach()), dim=0)
        return loss_alpha

    def save_load_model(self, cwd, if_save):
        """save or load model files

        :str cwd: current working directory, we save model file here
        :bool if_save: save model or load model
        """
        act_save_path = '{}/actor.pth'.format(cwd)
        cri_save_path = '{}/critic.pth'.format(cwd)

        def load_torch_file(network, save_path):
            network_dict = torch.load(save_path, map_location=lambda storage, loc: storage)
            network.load_state_dict(network_dict)

        if if_save:
            if self.act is not None:
                torch.save(self.act.state_dict(), act_save_path)
            if self.cri is not None:
                torch.save(self.cri.state_dict(), cri_save_path)
        elif (self.act is not None) and os.path.exists(act_save_path):
            load_torch_file(self.act, act_save_path)
            print("Loaded act:", cwd)
        elif (self.cri is not None) and os.path.exists(cri_save_path):
            load_torch_file(self.cri, cri_save_path)
            print("Loaded cri:", cwd)
        else:
            print("FileNotFound when load_model: {}".format(cwd))

    @staticmethod
    def soft_update(target_net, current_net, tau):
        """soft update a target network via current network

        :nn.Module target_net: target network update via a current network, it is more stable
        :nn.Module current_net: current network update via an optimizer
        """
        for tar, cur in zip(target_net.parameters(), current_net.parameters()):
            tar.data.copy_(cur.data.__mul__(tau) + tar.data.__mul__(1 - tau))

    def to_cpu(self):
        device = torch.device('cpu')
        if next(self.act.parameters()).is_cuda:
            self.act.to(device)
        if next(self.cri.parameters()).is_cuda:
            self.cri.to(device)

    def to_device(self):
        if not next(self.act.parameters()).is_cuda:
            self.act.to(self.device)
        if not next(self.cri.parameters()).is_cuda:
            self.cri.to(self.device)


def make_env(env_dict, seed=0):
    import gym
    env = gym.make(env_dict['id'])
    env.seed(seed=seed)
    return env


class TensorBoard:
    _writer = None

    @classmethod
    def get_writer(cls, load_path=None):
        if cls._writer:
            return cls._writer
        cls._writer = SummaryWriter(load_path)
        return cls._writer


class RecordEpisode:
    def __init__(self):
        self.l_reward = []
        self.record = {}

    def add_record(self, reward, info=None):
        self.l_reward.append(reward)
        if info is not None:
            for k, v in info.items():
                if k not in self.record.keys():
                    self.record[k] = []
                self.record[k].append(v)

    def get_result(self):
        results = {}
        results['episode'] = {}
        #######Reward#######
        rewards = np.array(self.l_reward)
        results['episode']['avg_reward'] = rewards.mean()
        results['episode']['std_reward'] = rewards.std()
        results['episode']['max_reward'] = rewards.max()
        results['episode']['min_reward'] = rewards.min()
        results['episode']['return'] = rewards.sum()
        #######Total#######
        results['total'] = {}
        results['total']['step'] = rewards.shape[0]

        return results

    def clear(self):
        self.l_reward = []
        self.record = {}


def calc(np_array):
    if len(np_array.shape) > 1:
        np_array = np_array.sum(dim=1)
    return {'avg': np_array.mean(),
            'std': np_array.std(),
            'max': np_array.max(),
            'min': np_array.min(),
            'mid': np.median(np_array)}


class Evaluator():
    def __init__(self, args):
        self.cwd = args.cwd
        self.writer = TensorBoard.get_writer(args.cwd)
        self.target_reward = args.env['target_reward']
        self.eval_times = args.evaluator['eval_times']
        self.break_step = args.evaluator['break_step']
        self.satisfy_reward_stop = args.evaluator['satisfy_reward_stop']

        self.total_step = 0
        self.curr_step = 0
        self.record_satisfy_reward = False
        self.curr_max_return = -1e10
        self.if_save_model = False
        self.total_time = 0
        self.train_record = {}
        self.eval_record = {}

    def add_train_record(self, result):
        if len(self.train_record) == 0:
            for k in result.keys():
                self.train_record[k] = {}
                for i, v in result[k].items():
                    self.train_record[k][i] = [v]
        else:
            for k in result.keys():
                for i, v in result[k].items():
                    self.train_record[k][i].append(v)

    def add_eval_record(self, result):
        if len(self.eval_record) == 0:
            for k in result.keys():
                self.eval_record[k] = {}
                for i, v in result[k].items():
                    self.eval_record[k][i] = [v]
        else:
            for k in result.keys():
                for i, v in result[k].items():
                    self.eval_record[k][i].append(v)

    def clear_train_and_eval_record(self):
        self.train_record = {}
        self.eval_record = {}

    def update_totalstep(self, totalstep):
        self.curr_step = totalstep
        self.total_step += totalstep

    def analyze_result(self):
        if len(self.train_record) > 0:
            for k in self.train_record.keys():
                for i, v in self.train_record[k].items():
                    self.train_record[k][i] = calc(np.array(v))
        if len(self.eval_record) > 0:
            for k in self.eval_record.keys():
                for i, v in self.eval_record[k].items():
                    self.eval_record[k][i] = calc(np.array(v))
            _return = self.eval_record['episode']['return']['avg']
        else:
            _return = self.train_record['episode']['return']['avg']
        if _return > self.curr_max_return:
            self.curr_max_return = _return
            self.if_save_model = True
            if (self.curr_max_return > self.target_reward) and (self.satisfy_reward_stop):
                self.record_satisfy_reward = True

    def tb_algo(self, algo_record):
        for k, v in algo_record.items():
            self.writer.add_scalar(f'algo/{k}', v, self.total_step - self.curr_step)

    def tb_train(self):
        for k in self.train_record.keys():
            for i, elements in self.train_record[k].items():
                for calc, v in elements.items():
                    self.writer.add_scalar(f'train_{k}_{i}/{calc}', v, self.total_step - self.curr_step)

    def tb_eval(self):
        for k in self.eval_record.keys():
            for i, elements in self.eval_record[k].items():
                for calc, v in elements.items():
                    self.writer.add_scalar(f'eval_{k}_{i}/{calc}', v, self.total_step - self.curr_step)

    def iter_print(self, algo_record, eval_record, use_time):
        print_info = f"|{'Step':>8}  {'MaxR':>8}|" + \
                     f"{'avgR':>8}  {'stdR':>8}" + \
                     f"{'avgS':>6}  {'stdS':>4} |"
        for key in algo_record.keys():
            print_info += f"{key:>8}"
        print_info += " |"
        print(print_info)
        print_info = f"|{self.total_step:8.2e}  {self.curr_max_return:8.2f}|" + \
                     f"{eval_record['episode']['return']['avg']:8.2f}  {eval_record['episode']['return']['std']:8.2f}" + \
                     f"{eval_record['total']['step']['avg']:6.2f}  {eval_record['total']['step']['std']:4.0f} |"
        for key in algo_record.keys():
            print_info += f"{algo_record[key]:8.2f}"
        print_info += " |"
        print(print_info)
        self.total_time += use_time
        print_info = f"| UsedTime:{use_time:8.3f}s  TotalTime:{self.total_time:8.0f}s"
        if self.if_save_model:
            print_info += " |  Save model!"
        print(print_info)

    def save_model(self, agent):
        if self.if_save_model:
            agent.to_cpu()
            act_save_path = f'{self.cwd}/actor.pth'
            torch.save(agent.act.state_dict(), act_save_path)
            if agent.cri is None:
                for i in range(len(agent.cris)):
                    cri_save_path = f'{self.cwd}/critic{i}.pth'
                    torch.save(agent.cris[i].state_dict(), cri_save_path)
            else:
                cri_save_path = f'{self.cwd}/critic.pth'
                torch.save(agent.cri.state_dict(), cri_save_path)
        self.if_save_model = False


default_config = {
    'cwd': None,
    'if_cwd_time': True,
    'random_seed': 0,
    'gpu_id': 1,  # <0 cpu
    'load_buffer_path': None,
    'env': {
        'id': 'LunarLanderContinuous-v2',
        'state_dim': 8,
        'action_dim': 2,
        'if_discrete_action': False,
        'target_reward': 0,
        'max_step': 500,
    },
    'agent': {
        'class_name': AgentMPO,
        'explore_rate': 1.,
        'num_samples': 20,
        'epsilon_mean': 1e-2,
        'epsilon_stddev': 1e-4,
        'init_log_temperature': 1.,
        'init_log_alpha_mean': 1.,
        'init_log_alpha_stddev': 10.,
        'learning_rate': 1e-4,
        'soft_update_tau': 2 ** -8,
        'net_dim': 2 ** 8,
    },
    'interactor': {
        'sample_size': 1000,  # evaluation gap
        'reward_scale': 2 ** 0,
        'gamma': 0.99,
        'batch_size': 2 ** 8,
        'policy_reuse': 2 ** 1,  # no work
        'interact_model': 'one',
        'random_explore_num': 1000,
    },
    'buffer': {
        'max_buf': 2 ** 20,
        'if_per': False,  # for off policy
    },
    'evaluator': {
        'eval_times': 4,  # for every rollout_worker
        'break_step': 1e6,
        'eval_gap_step': 1e4,
        'satisfy_reward_stop': False,
    }
}


class Arguments:
    def __init__(self, config=default_config):
        # choose the GPU for running. gpu_id is None means set it automatically
        self.device_name = "cuda:" + str(config['gpu_id']) if config['gpu_id'] >= 0 else "cpu"
        # current work directory. cwd is None means set it automatically
        self.cwd = config['cwd'] if 'cwd' in config.keys() else None
        # current work directory with time.
        self.if_cwd_time = config['if_cwd_time'] if 'cwd' in config.keys() else False
        # initialize random seed in self.init_before_training()
        self.random_seed = config['random_seed']
        self.load_buffer_path = config['load_buffer_path'] if 'load_buffer_path' in config.keys() else None

        # id state_dim action_dim reward_dim target_reward horizon_step
        self.env = config['env']
        # Deep Reinforcement Learning algorithm
        self.agent = config['agent']
        self.agent['agent_name'] = self.agent['class_name']().__class__.__name__
        self.interactor = config['interactor']
        self.buffer = config['buffer']
        self.evaluator = config['evaluator']
        self.config = default_config

    def init_before_training(self, if_main=True):
        '''set cwd automatically'''
        if self.cwd is None:
            if self.if_cwd_time:
                import datetime
                curr_time = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
            else:
                curr_time = 'current'
            self.cwd = f'./logs/{self.env["id"]}-{self.agent["agent_name"]}/' \
                       f'exp_{self.interactor["interact_model"]}_{curr_time}_{self.device_name}'

        import shutil  # remove history according to bool(if_remove)
        shutil.rmtree(self.cwd, ignore_errors=True)
        print("| Remove history")
        os.makedirs(self.cwd, exist_ok=True)
        '''save exp parameters'''
        from ruamel.yaml.main import YAML
        yaml = YAML()
        del self.config['agent']['class_name']
        del self.config['if_cwd_time']
        self.config['cwd'] = self.cwd
        with open(self.cwd + '/parameters.yaml', 'w', encoding="utf-8") as f:
            yaml.dump(self.config, f)
        del self.config

        torch.set_default_dtype(torch.float32)
        torch.manual_seed(self.random_seed)
        np.random.seed(self.random_seed)


def interact(config=default_config):
    args = Arguments(config)
    args.init_before_training()
    agent = args.agent['class_name'](args=args)
    env = make_env(args.env, args.random_seed)
    eval_env = make_env(args.env, args.random_seed)
    agent.init(net_dim=args.agent['net_dim'],
               state_dim=args.env['state_dim'],
               action_dim=args.env['action_dim'],
               if_per=args.buffer['if_per'])
    buffer = ReplayBuffer(cwd=args.cwd,
                          max_len=args.buffer['max_buf'],
                          state_dim=args.env['state_dim'],
                          action_dim=1 if args.env['if_discrete_action'] else args.env['action_dim'],
                          if_per=args.buffer['if_per'],
                          if_save_buffer=args.buffer['if_save_buffer'])
    evaluator = Evaluator(args)
    env_max_step = args.env['max_step']
    reward_scale = args.interactor['reward_scale']
    gamma = args.interactor['gamma']
    random_explore_num = args.interactor['random_explore_num']
    break_step = args.evaluator['break_step']
    sample_size = args.interactor['sample_size']
    batch_size = args.interactor['batch_size']
    policy_reuse = args.interactor['policy_reuse']
    interact_model = args.interactor['interact_model']
    eval_gap_step = args.evaluator['eval_gap_step']

    ### random explore
    if args.load_buffer_path is None:
        actual_step = 0
        while actual_step < random_explore_num:
            state = env.reset()
            for i in range(env_max_step):
                action = env.action_space.sample()
                next_s, reward, done, _ = env.step(action)
                done = True if i == (env_max_step - 1) else done
                buffer.append_buffer(state,
                                     action,
                                     reward * reward_scale,
                                     0.0 if done else gamma)
                if done:
                    break
                state = next_s
            actual_step += i
        buffer.save_buffer(file_name='buffer_random_explore')
    else:
        buffer.load_buffer(args.load_buffer_path)

    total_step = 0
    record_episode = RecordEpisode()
    ### interact one step model
    if interact_model == 'one':
        start_time = time.time()
        agent.to_cpu()
        record_t = 0
        while (total_step < break_step):
            state = env.reset()
            for i in range(env_max_step):
                total_step += 1
                action = agent.select_action(agent.act, state, explore_rate=args.agent['explore_rate'])
                next_s, reward, done, _ = env.step(np.tanh(action))
                done = True if i == (env_max_step - 1) else done
                buffer.append_buffer(state,
                                     action,
                                     reward * reward_scale,
                                     0.0 if done else gamma)
                record_episode.add_record(reward)
                if_record = total_step // eval_gap_step - record_t
                record_t = total_step // eval_gap_step
                ### update agent network
                buffer.update_now_len_before_sample()
                agent.to_device()
                algo_record = agent.update_net_one_step(buffer, batch_size, if_record=if_record)
                agent.to_cpu()

                if done:
                    evaluator.add_train_record(record_episode.get_result())
                    record_episode.clear()
                    break
                state = next_s

                if if_record:
                    evaluator.update_totalstep(sample_size)
                    ### evaluate in env
                    for _ in range(evaluator.eval_times):
                        state = eval_env.reset()
                        for i in range(env_max_step):
                            action = agent.act(torch.as_tensor((state,), dtype=torch.float32).detach_())
                            next_s, reward, done, _ = eval_env.step(action.detach().numpy()[0])
                            done = True if i == (env_max_step - 1) else done
                            record_episode.add_record(reward)
                            if done:
                                break
                            state = next_s
                        evaluator.add_eval_record(record_episode.get_result())
                        record_episode.clear()

                    ### record in tb
                    evaluator.analyze_result()
                    evaluator.tb_train()
                    evaluator.tb_eval()
                    evaluator.tb_algo(algo_record)
                    evaluator.iter_print(algo_record, evaluator.eval_record, (time.time() - start_time))
                    evaluator.save_model(agent)
                    evaluator.clear_train_and_eval_record()
                    start_time = time.time()
    ### interact multi-step model
    elif interact_model == 'multi':
        agent.to_cpu()
        record_t = 0
        while (total_step < break_step):
            start_time = time.time()
            ### explore env sample_size step
            actual_step = 0
            while actual_step < sample_size:
                state = env.reset()
                for i in range(env_max_step):
                    action = agent.select_action(agent.act, state, explore_rate=args.agent['explore_rate'])
                    next_s, reward, done, _ = env.step(np.tanh(action))
                    done = True if i == (env_max_step - 1) else done
                    buffer.append_buffer(state,
                                         action,
                                         reward * reward_scale,
                                         0.0 if done else gamma)
                    record_episode.add_record(reward)
                    actual_step += 1
                    if done:
                        evaluator.add_train_record(record_episode.get_result())
                        record_episode.clear()
                        break
                    state = next_s
            total_step += actual_step
            ### update agent network
            buffer.update_now_len_before_sample()
            agent.to_device()
            algo_record = agent.update_net_multi_step(buffer=buffer,
                                                      target_step=actual_step,
                                                      batch_size=batch_size,
                                                      repeat_times=policy_reuse)
            agent.to_cpu()
            evaluator.update_totalstep(actual_step)

            if_record = total_step // eval_gap_step - record_t
            record_t = total_step // eval_gap_step
            if if_record:
                ### evaluate in env
                for _ in range(evaluator.eval_times):
                    state = eval_env.reset()
                    for i in range(env_max_step):
                        action = agent.act(torch.as_tensor((state,), dtype=torch.float32).detach_())
                        next_s, reward, done, _ = eval_env.step(action.detach().numpy()[0])
                        done = True if i == (env_max_step - 1) else done
                        record_episode.add_record(reward)
                        if done:
                            break
                        state = next_s
                    evaluator.add_eval_record(record_episode.get_result())
                    record_episode.clear()

                ### record in tb
                evaluator.analyze_result()
                evaluator.tb_train()
                evaluator.tb_eval()
                evaluator.tb_algo(algo_record)
                evaluator.iter_print(algo_record, evaluator.eval_record, (time.time() - start_time))
                evaluator.save_model(agent)
                evaluator.clear_train_and_eval_record()


def demo_test_one_step_mpo():
    mpo_config = {
        'cwd': None,
        'if_cwd_time': False,
        'random_seed': 0,
        'gpu_id': 1,  # <0 cpu
        'load_buffer_path': None,
        # 'env': {
        #     'id': 'Hopper-v2',
        #     'state_dim': 11,
        #     'action_dim': 3,
        #     'if_discrete_action': False,
        #     'target_reward': 0,
        #     'max_step': 1000,
        # },
        'env': {
            'id': 'Reacher-v2',
            'state_dim': 11,
            'action_dim': 2,
            'if_discrete_action': False,
            'target_reward': 20,
            'max_step': 50,
        },
        # 'env': {
        #     'id': 'LunarLanderContinuous-v2',
        #     'state_dim': 8,
        #     'action_dim': 2,
        #     'if_discrete_action': False,
        #     'target_reward': 0,
        #     'max_step': 500,
        # },
        'agent': {
            'class_name': AgentMPO,
            'explore_rate': 1.,
            'num_samples': 64,
            'epsilon_mean': 1e-2,
            'epsilon_stddev': 1e-4,
            'init_log_temperature': 1.,
            'init_log_alpha_mean': 1.,
            'init_log_alpha_stddev': 10.,
            'learning_rate': 1e-4,
            'soft_update_tau': 2 ** -8,
            'net_dim': 2 ** 8,
        },
        'interactor': {
            'sample_size': 1000,  # no work
            'reward_scale': 2 ** 0,
            'gamma': 0.99,
            'batch_size': 2 ** 8,
            'policy_reuse': 2 ** 1,  # no work
            'interact_model': 'one',
            'random_explore_num': 1000,
        },
        'buffer': {
            'max_buf': 2 ** 20,
            'if_per': False,  # for off policy
            'if_save_buffer': False,
        },
        'evaluator': {
            'eval_times': 4,  # for every rollout_worker
            'break_step': 1e6,
            'eval_gap_step': 6e3,
            'satisfy_reward_stop': False,
        }
    }
    interact(mpo_config)


if __name__ == '__main__':
    demo_test_one_step_mpo()
