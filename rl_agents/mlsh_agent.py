from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import numpy as np
import tensorflow as tf
from pysc2.lib import actions
from pysc2.lib import features

# from agents.mlsh_network import build_net
from rl_agents.mlsh_network import build_net
import utils as U


class MLSHAgent(object):
  """An agent specifically for solving the mini-game maps."""
  def __init__(self, training, msize, ssize, num_subpol, subpol_frames, name='MLSH/MLSHAgent'):
    self.name = name
    self.training = training
    self.summary = []
    # Minimap size, screen size and info size
    assert msize == ssize
    self.msize = msize
    self.ssize = ssize
    self.isize = len(actions.FUNCTIONS)
    self.num_subpol = num_subpol
    self.subpol_frames = subpol_frames
    self.ep_subpol_choices = []

    self.steps_on_subpol = 0
    self.cur_subpol = 0

  def setup(self, sess, summary_writer):
    self.sess = sess
    self.summary_writer = summary_writer


  def initialize(self):
    init_op = tf.global_variables_initializer()
    self.sess.run(init_op)


  def reset(self):
    # Epsilon schedule
    self.epsilon = [0.05, 0.2]


  def build_model(self, reuse, dev, ntype):
    with tf.variable_scope(self.name) and tf.device(dev):
      if reuse:
        tf.get_variable_scope().reuse_variables()
        assert tf.get_variable_scope().reuse

      # Set inputs of networks
      self.minimap = tf.placeholder(tf.float32, [None, U.minimap_channel(), self.msize, self.msize], name='minimap')
      self.screen = tf.placeholder(tf.float32, [None, U.screen_channel(), self.ssize, self.ssize], name='screen')
      self.info = tf.placeholder(tf.float32, [None, self.isize], name='info')

      # Build networks
      net = build_net(self.minimap, self.screen, self.info, self.msize, self.ssize, len(actions.FUNCTIONS), ntype, self.num_subpol)
      self.spatial_actions, self.non_spatial_actions, self.value, self.subpol_choice = net

      print(self.value)
      print(self.spatial_actions)

      # Create training operation for the subpolicies:
      # Set targets and masks
      self.valid_spatial_action = tf.placeholder(tf.float32, [None], name='valid_spatial_action')
      self.spatial_action_selected = tf.placeholder(tf.float32, [None, self.ssize**2], name='spatial_action_selected')
      self.valid_non_spatial_action = tf.placeholder(tf.float32, [None, len(actions.FUNCTIONS)], name='valid_non_spatial_action')
      self.non_spatial_action_selected = tf.placeholder(tf.float32, [None, len(actions.FUNCTIONS)], name='non_spatial_action_selected')
      self.value_target = tf.placeholder(tf.float32, [None], name='value_target')

      self.subpol_train_ops = []

      # Build the optimizer
      self.learning_rate = tf.placeholder(tf.float32, None, name='learning_rate')
      opt = tf.train.RMSPropOptimizer(self.learning_rate, decay=0.99, epsilon=1e-10)

      for pol_id in range(self.num_subpol):
        # Compute log probability

        self.spatial_action = self.spatial_actions[pol_id]
        self.non_spatial_action = self.non_spatial_actions[pol_id]

        spatial_action_prob = tf.reduce_sum(self.spatial_action * self.spatial_action_selected, axis=1)
        spatial_action_log_prob = tf.log(tf.clip_by_value(spatial_action_prob, 1e-10, 1.))
        non_spatial_action_prob = tf.reduce_sum(self.non_spatial_action * self.non_spatial_action_selected, axis=1)
        valid_non_spatial_action_prob = tf.reduce_sum(self.non_spatial_action * self.valid_non_spatial_action, axis=1)
        valid_non_spatial_action_prob = tf.clip_by_value(valid_non_spatial_action_prob, 1e-10, 1.)
        non_spatial_action_prob = non_spatial_action_prob / valid_non_spatial_action_prob
        non_spatial_action_log_prob = tf.log(tf.clip_by_value(non_spatial_action_prob, 1e-10, 1.))
        self.summary.append(tf.summary.histogram('spatial_action_prob_' + str(pol_id), spatial_action_prob))
        self.summary.append(tf.summary.histogram('non_spatial_action_prob_' + str(pol_id), non_spatial_action_prob))

        # Compute losses, more details in https://arxiv.org/abs/1602.01783
        # Policy loss and value loss
        action_log_prob = self.valid_spatial_action * spatial_action_log_prob + non_spatial_action_log_prob
        advantage = tf.stop_gradient(self.value_target - self.value)
        policy_loss = - tf.reduce_mean(action_log_prob * advantage)
        value_loss = - tf.reduce_mean(self.value * advantage)
        self.summary.append(tf.summary.scalar('policy_loss', policy_loss))
        self.summary.append(tf.summary.scalar('value_loss', value_loss))

        # TODO: policy penalty
        loss = policy_loss + value_loss

        grads = opt.compute_gradients(loss)

        cliped_grad = []
        for grad, var in grads:

          print(grad)
          # Ignore gradients for other output layers for other subpolicies
          if grad == None:
            continue
          self.summary.append(tf.summary.histogram(var.op.name, var))
          self.summary.append(tf.summary.histogram(var.op.name+'/grad', grad))
          grad = tf.clip_by_norm(grad, 10.0)
          cliped_grad.append([grad, var])

        self.subpol_train_ops.append(opt.apply_gradients(cliped_grad))
        self.summary_op = tf.summary.merge(self.summary)

      # Create training operation for the master policy:

      self.saver = tf.train.Saver(max_to_keep=100, keep_checkpoint_every_n_hours=1)


  def step(self, obs):

    minimap = np.array(obs.observation['minimap'], dtype=np.float32)
    minimap = np.expand_dims(U.preprocess_minimap(minimap), axis=0)

    screen = np.array(obs.observation['screen'], dtype=np.float32)
    screen = np.expand_dims(U.preprocess_screen(screen), axis=0)
    # TODO: only use available actions
    info = np.zeros([1, self.isize], dtype=np.float32)
    info[0, obs.observation['available_actions']] = 1

    # Only change master's choice of subpolicy every self.subpol_frames steps
    if self.steps_on_subpol % self.subpol_frames == 0:

      # Get softmax outputs for choosing subpolicy
      feed = {self.minimap: minimap,
              self.screen: screen,
              self.info: info}
      subpol_choice = self.sess.run(
        self.subpol_choice,
        feed_dict=feed)


      # Choose max probability output for subpolicy
      self.cur_subpol = np.argmax(subpol_choice)
      self.steps_on_subpol = 0

      # Choose subpolicy using epsilon-greedy method
      if self.training and np.random.rand() < self.epsilon[1]:
        self.cur_subpol = np.random.randint(0, self.num_subpol)

      print('SUBPOLICY CHOICE: ' + str(subpol_choice))

    self.steps_on_subpol += 1

    # Store subpol_choice at each step for later call to update()
    self.ep_subpol_choices.append(self.cur_subpol)

    # Run the graph for the current subpolicy to get action
    feed = {self.minimap: minimap,
            self.screen: screen,
            self.info: info}
    non_spatial_action, spatial_action = self.sess.run(
      [self.non_spatial_actions[self.cur_subpol], self.spatial_actions[self.cur_subpol]],
      feed_dict=feed)

    # Select an action and a spatial target
    non_spatial_action = non_spatial_action.ravel()
    spatial_action = spatial_action.ravel()
    valid_actions = obs.observation['available_actions']
    act_id = valid_actions[np.argmax(non_spatial_action[valid_actions])]
    target = np.argmax(spatial_action)
    target = [int(target // self.ssize), int(target % self.ssize)]

    # print('Taking action with ID: ' + str(act_id))

    if False:
      print(actions.FUNCTIONS[act_id].name, target)

    # Epsilon greedy exploration
    if self.training and np.random.rand() < self.epsilon[0]:
      act_id = np.random.choice(valid_actions)
    if self.training and np.random.rand() < self.epsilon[1]:
      dy = np.random.randint(-4, 5)
      target[0] = int(max(0, min(self.ssize-1, target[0]+dy)))
      dx = np.random.randint(-4, 5)
      target[1] = int(max(0, min(self.ssize-1, target[1]+dx)))

    # Set act_id and act_args
    act_args = []
    for arg in actions.FUNCTIONS[act_id].args:
      if arg.name in ('screen', 'minimap', 'screen2'):
        act_args.append([target[1], target[0]])
      else:
        act_args.append([0])  # TODO: Be careful
    return actions.FunctionCall(act_id, act_args)


  def update(self, rbs, disc, lr, cter):
    master_disc = disc # TODO: pass this as argument instead ? and tune the discount ?
    # Compute R, which is value of the last observation
    obs = rbs[-1][-1]
    if obs.last():
      R = 0
    else:
      minimap = np.array(obs.observation['minimap'], dtype=np.float32)
      minimap = np.expand_dims(U.preprocess_minimap(minimap), axis=0)
      screen = np.array(obs.observation['screen'], dtype=np.float32)
      screen = np.expand_dims(U.preprocess_screen(screen), axis=0)
      info = np.zeros([1, self.isize], dtype=np.float32)
      info[0, obs.observation['available_actions']] = 1

      feed = {self.minimap: minimap,
              self.screen: screen,
              self.info: info}
      R = self.sess.run(self.value, feed_dict=feed)[0]

    # Compute targets and masks
    minimaps = []
    screens = []
    infos = []

    value_target = np.zeros([len(rbs)], dtype=np.float32)
    value_target[-1] = R

    valid_spatial_action = np.zeros([len(rbs)], dtype=np.float32)
    spatial_action_selected = np.zeros([len(rbs), self.ssize**2], dtype=np.float32)
    valid_non_spatial_action = np.zeros([len(rbs), len(actions.FUNCTIONS)], dtype=np.float32)
    non_spatial_action_selected = np.zeros([len(rbs), len(actions.FUNCTIONS)], dtype=np.float32)

    rbs.reverse()
    self.ep_subpol_choices.reverse()

    # process the observations from the replay to use them for the update:
    for i, [obs, action, next_obs] in enumerate(rbs):

      minimap = np.array(obs.observation['minimap'], dtype=np.float32)
      minimap = np.expand_dims(U.preprocess_minimap(minimap), axis=0)
      screen = np.array(obs.observation['screen'], dtype=np.float32)
      screen = np.expand_dims(U.preprocess_screen(screen), axis=0)
      info = np.zeros([1, self.isize], dtype=np.float32)
      info[0, obs.observation['available_actions']] = 1

      minimaps.append(minimap)
      screens.append(screen)
      infos.append(info)

      reward = obs.reward
      act_id = action.function
      act_args = action.arguments

      value_target[i] = reward + disc * value_target[i-1]

      valid_actions = obs.observation["available_actions"]
      valid_non_spatial_action[i, valid_actions] = 1
      non_spatial_action_selected[i, act_id] = 1

      args = actions.FUNCTIONS[act_id].args
      for arg, act_arg in zip(args, act_args):
        if arg.name in ('screen', 'minimap', 'screen2'):
          ind = act_arg[1] * self.ssize + act_arg[0]
          valid_spatial_action[i] = 1
          spatial_action_selected[i, ind] = 1

    minimaps = np.concatenate(minimaps, axis=0)
    screens = np.concatenate(screens, axis=0)
    infos = np.concatenate(infos, axis=0)

    # Update each subpolicy using gradient descent on the steps of this episode for which that
    # subpolicy was being used
    for pol_id in range(self.num_subpol):

      # print('UPDATING SUBPOLICY: ' + str(pol_id) + '\n\n')

      pol_inds = np.where(np.array(self.ep_subpol_choices) == pol_id)[0]
      # print('Step indices for policy: ' + str(pol_inds) + '\n')

      # No game steps in this episode used this policy
      if len(pol_inds) == 0:
        continue

      # Train
      feed = {self.minimap: minimaps[pol_inds],
              self.screen: screens[pol_inds],
              self.info: infos[pol_inds],
              self.value_target: value_target[pol_inds],
              self.valid_spatial_action: valid_spatial_action[pol_inds],
              self.spatial_action_selected: spatial_action_selected[pol_inds],
              self.valid_non_spatial_action: valid_non_spatial_action[pol_inds],
              self.non_spatial_action_selected: non_spatial_action_selected[pol_inds],
              self.learning_rate: lr}
      _ = self.sess.run(self.subpol_train_ops[pol_id], feed_dict=feed)
      # self.summary_writer.add_summary(summary, cter)

    # Update the master policy

    # TODO:
    # master policy takes decisions every N steps so it should:
    # sum up rewards over N steps
    # compute advantage
    # update
    # note there is something to figure out with learning rate

    # get decisions made by master policy every self.subpol_frames steps:
    master_choices = [v for i,v in enumerate(self.ep_subpol_choices) if i % self.subpol_frames == 0]
    
    # sum rewards gotten between each change of subpolicy and compute values:
    assert len(rbs) % self.subpol_frames == 0 # TODO: deal with weird cases
    master_value_target = np.zeros([int(len(rbs) / self.subpol_frames)], dtype=np.float32)
    master_value_target[-1] = R

    for i in range(int(len(rbs) / self.subpol_frames)):
      sum_rewards = sum([obs.reward for obs,_,_ in rbs[i:(i+self.subpol_frames)]])
      master_value_target[i] = reward + master_disc * master_value_target[i-1]

    master_choice_inds = list(range(0, len(rbs), self.subpol_frames))

    feed = {self.minimap: minimaps[master_choice_inds],
            self.screen: screens[master_choice_inds],
            self.info: infos[master_choice_inds],
            self.value_target: value_target[master_choice_inds],
            self.valid_spatial_action: valid_spatial_action[master_choice_inds],
            self.spatial_action_selected: spatial_action_selected[master_choice_inds],
            self.valid_non_spatial_action: valid_non_spatial_action[master_choice_inds],
            self.non_spatial_action_selected: non_spatial_action_selected[master_choice_inds],
            self.learning_rate: lr}
    _ = self.sess.run(self.master_train_op, feed_dict=feed)

    # re-initialize master policy:

    # TODO !

    self.ep_subpol_choices = []


  def save_model(self, path, count):
    self.saver.save(self.sess, path+'/model.pkl', count)


  def load_model(self, path):
    ckpt = tf.train.get_checkpoint_state(path)
    self.saver.restore(self.sess, ckpt.model_checkpoint_path)
    return int(ckpt.model_checkpoint_path.split('-')[-1])
