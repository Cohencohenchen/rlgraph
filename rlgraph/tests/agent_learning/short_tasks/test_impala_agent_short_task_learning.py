# Copyright 2018 The RLgraph authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import logging
import numpy as np
import os
import time
import unittest

from rlgraph.environments import GridWorld, OpenAIGymEnv
from rlgraph.agents import IMPALAAgent
from rlgraph.utils import root_logger
from rlgraph.tests.test_util import config_from_path, recursive_assert_almost_equal


class TestIMPALAAgentShortTaskLearning(unittest.TestCase):
    """
    Tests whether the DQNAgent can learn in simple environments.
    """
    root_logger.setLevel(level=logging.INFO)

    is_windows = os.name == "nt"

    def test_impala_on_2x2_grid_world(self):
        """
        Creates a single IMPALAAgent and runs it via a simple loop on a 2x2 GridWorld.
        """
        env = GridWorld("2x2")
        agent = IMPALAAgent.from_spec(
            config_from_path("configs/impala_agent_for_2x2_gridworld.json"),
            state_space=env.state_space,
            action_space=env.action_space,
            execution_spec=dict(seed=12),
            update_spec=dict(batch_size=16),
            optimizer_spec=dict(type="adam", learning_rate=0.05),
            batch_apply=True,
            batch_apply_action_adapter=False
        )

        learn_updates = 50
        for i in range(learn_updates):
            ret = agent.update()
            mean_return = self._calc_mean_return(ret)
            print("i={} Loss={:.4} Avg-reward={:.2}".format(i, float(ret[1]), mean_return))

        # Assume we have learned something.
        self.assertGreater(mean_return, -0.1)

        # Check the last action probs for the 2 valid next_states (start (after a reset) and one below start).
        action_probs = ret[3]["action_probs"].reshape((80, 4))
        next_states = ret[3]["states"][:, 1:].reshape((80,))
        for s_, probs in zip(next_states, action_probs):
            # Start state:
            # - Assume we picked "right" in state=1 (in order to step into goal state).
            # - OR we picked "up" or "left" in state=0 (start state).
            if s_ == 0:
                recursive_assert_almost_equal(probs[0], 0.0, decimals=2)
                self.assertTrue(probs[1] > 0.99 or probs[2] > 0.99)
                recursive_assert_almost_equal(probs[3], 0.0, decimals=2)
            # One below start: Assume we picked "down" in start state with very large probability.
            elif s_ == 1:
                recursive_assert_almost_equal(probs[0], 0.0, decimals=2)
                recursive_assert_almost_equal(probs[1], 0.0, decimals=2)
                recursive_assert_almost_equal(probs[2], 0.99, decimals=2)
                recursive_assert_almost_equal(probs[3], 0.0, decimals=2)

        agent.terminate()

    def test_impala_on_cart_pole(self):
        """
        Creates a single IMPALAAgent and runs it via a simple loop on CartPole-v0.
        """
        env = OpenAIGymEnv("CartPole-v0", visualize=self.is_windows)
        config_ = config_from_path("configs/impala_agent_for_cartpole.json")
        #config_["environment_spec"]["visualize"] = self.is_windows
        agent = IMPALAAgent.from_spec(
            config_,
            state_space=env.state_space,
            action_space=env.action_space,
            execution_spec=dict(seed=12),
            update_spec=dict(batch_size=8),
            optimizer_spec=dict(type="adam", learning_rate=0.01),
            num_workers=4,
            worker_sample_size=20
        )

        learn_updates = 300
        mean_returns = []
        for i in range(learn_updates):
            ret = agent.update()
            mean_return = self._calc_mean_return(ret)
            mean_returns.append(mean_return)
            print("i={} Loss={:.4} Avg-reward={:.2}".format(i, float(ret[1]), mean_return))

        # Assume we have learned something.
        self.assertGreater(np.nanmean(mean_returns), 40.0)

        time.sleep(3)
        agent.terminate()
        time.sleep(3)

    @staticmethod
    def _calc_mean_return(records):
        size = records[3]["rewards"].size
        rewards = records[3]["rewards"].reshape((size,))
        terminals = records[3]["terminals"].reshape((size,))
        returns = list()
        return_ = 0.0
        for r, t in zip(rewards, terminals):
            return_ += r
            if t:
                returns.append(return_)
                return_ = 0.0

        return np.mean(returns)
