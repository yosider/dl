"""PPO RL algorithm.

https://arxiv.org/abs/1707.06347
"""
from dl.rl.trainers import RolloutTrainer
from dl.rl.modules import Policy
from dl import logger
import gin
import time
import torch
import torch.nn as nn
import numpy as np


@gin.configurable(blacklist=['logdir'])
class PPO(RolloutTrainer):
    """PPO algorithm."""

    def __init__(self,
                 logdir,
                 env_fn,
                 policy_fn,
                 optimizer=torch.optim.Adam,
                 epochs_per_rollout=10,
                 max_grad_norm=None,
                 ent_coef=0.01,
                 vf_coef=0.5,
                 clip_param=0.2,
                 **kwargs):
        """Init."""
        super().__init__(logdir, env_fn, **kwargs)
        self.epochs_per_rollout = epochs_per_rollout
        self.max_grad_norm = max_grad_norm
        self.ent_coef = ent_coef
        self.vf_coef = vf_coef
        self.clip_param = clip_param

        self.pi = policy_fn(self.env).to(self.device)
        self.opt = optimizer(self.pi.parameters())
        self.init_rollout_storage(self.pi(self._ob).state_out, ['logp'])
        self.mse = nn.MSELoss(reduction='none')

        self.eval_env = self.make_eval_env()

    def state_dict(self):
        """State dict."""
        return {
            'pi': self.pi.state_dict(),
            'opt': self.opt.state_dict(),
        }

    def load_state_dict(self, state_dict):
        """Load state dict."""
        self.pi.load_state_dict(state_dict['pi'])
        self.opt.load_state_dict(state_dict['opt'])

    def act(self, ob, state_in, mask):
        """Produce decision from model."""
        outs = self.pi(ob, state_in, mask)
        keys = {'logp': outs.dist.log_prob(outs.action)}
        return outs, keys

    def step(self):
        """Compute rollout, loss, and update model."""
        self.pi.train()
        self.rollout()
        losses = {'total': [], 'pi': [], 'value': [], 'entropy': []}
        for _ in range(self.epochs_per_rollout):
            for batch in self.rollout_sampler():
                self.opt.zero_grad()
                loss = self.loss(batch)
                for k, v in loss.items():
                    losses[k].append(v.detach().cpu().numpy())
                loss['total'].backward()
                if self.max_grad_norm:
                    nn.utils.clip_grad_norm_(self.pi.parameters(),
                                             self.max_grad_norm)
                self.opt.step()
        for k, v in losses.items():
            logger.add_scalar(f'loss/{k}', np.mean(v), self.t, time.time())

    def evaluate(self):
        """Evaluate model."""
        self.rl_evaluate(self.pi)
        self.rl_record(self.pi)

    def loss(self, batch):
        """Compute loss."""
        outs = self.pi(batch['ob'], batch['state'], batch['mask'])
        loss = {}

        # compute policy loss
        logp = outs.dist.log_prob(batch['ac'])
        assert logp.shape == batch['logp'].shape
        ratio = torch.exp(logp - batch['logp'])
        assert ratio.shape == batch['atarg'].shape
        ploss1 = ratio * batch['atarg']
        ploss2 = torch.clamp(ratio, 1.0-self.clip_param,
                             1.0+self.clip_param) * batch['atarg']
        pi_loss = -torch.min(ploss1, ploss2).mean()
        loss['pi'] = pi_loss

        # compute value loss
        vloss1 = 0.5 * self.mse(outs.value, batch['vtarg'])
        vpred_clipped = batch['vpred'] + (
            outs.value - batch['vpred']).clamp(-self.clip_param,
                                               self.clip_param)
        vloss2 = 0.5 * self.mse(vpred_clipped, batch['vtarg'])
        vf_loss = torch.max(vloss1, vloss2).mean()
        loss['value'] = vf_loss

        # compute entropy loss
        ent_loss = outs.dist.entropy().mean()
        loss['entropy'] = ent_loss

        tot_loss = pi_loss + self.vf_coef * vf_loss - self.ent_coef * ent_loss
        loss['total'] = tot_loss
        return loss


if __name__ == '__main__':

    import unittest
    import shutil
    from dl.rl.envs import make_atari_env
    from dl.rl.modules import ActorCriticBase
    from dl.rl.util import conv_out_shape
    from dl.modules import Categorical
    import torch.nn.functional as F

    class NatureDQN(ActorCriticBase):
        """Deep network from https://www.nature.com/articles/nature14236."""

        def build(self):
            """Build network."""
            self.conv1 = nn.Conv2d(4, 32, 8, 4)
            self.conv2 = nn.Conv2d(32, 64, 4, 2)
            self.conv3 = nn.Conv2d(64, 64, 3, 1)
            shape = self.observation_space.shape[1:]
            for c in [self.conv1, self.conv2, self.conv3]:
                shape = conv_out_shape(shape, c)
            self.nunits = 64 * np.prod(shape)
            self.fc = nn.Linear(self.nunits, 512)
            self.vf = nn.Linear(512, 1)
            self.dist = Categorical(512, self.action_space.n)

        def forward(self, x):
            """Forward."""
            x = x.float() / 255.
            x = F.relu(self.conv1(x))
            x = F.relu(self.conv2(x))
            x = F.relu(self.conv3(x))
            x = F.relu(self.fc(x.view(-1, self.nunits)))
            return self.dist(x), self.vf(x)

    class TestPPO(unittest.TestCase):
        """Test case."""

        def test_feed_forward_ppo(self):
            """Test feed forward ppo."""
            def env_fn(rank):
                return make_atari_env('Pong', rank=rank, frame_stack=4)

            def policy_fn(env):
                return Policy(NatureDQN(env.observation_space,
                                        env.action_space))

            ppo = PPO('test',
                      env_fn,
                      policy_fn,
                      maxt=1000,
                      eval=True,
                      eval_period=1000)
            ppo.train()
            shutil.rmtree('test')

    unittest.main()