# -*- coding: utf-8 -*-
"""Agent v3 单元测试：BCNet 推理解码 + 安全护栏（mock 模型，不依赖真机）。"""
import math
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import torch

from wzry.policy.inference import BCNetInference, ACT_NAMES, BIN_ANGLE
from scripts.m3_agent_v3 import apply_guards, decode_action


class MockNet(torch.nn.Module):
    def __init__(self, move_bin=3, act_i=0, r=0.8):
        super().__init__()
        self.move_bin = move_bin
        self.act_i = act_i
        self.r = r

    def forward(self, units, mask, grid, ui):
        B = units.shape[0]
        t = torch.full((B, 8), -10.0)
        t[:, self.move_bin] = 0.0
        a = torch.full((B, len(ACT_NAMES)), -10.0)
        a[:, self.act_i] = 0.0
        return {"move_theta": torch.log_softmax(t, -1),
                "move_r": torch.full((B, 1), self.r),
                "act": torch.log_softmax(a, -1),
                "target": torch.full((B, 2), 0.5)}


class TestBCInference(unittest.TestCase):
    def setUp(self):
        self.net = MockNet()
        self.inf = BCNetInference.__new__(BCNetInference)
        self.inf.net = self.net
        self.inf.device = "cpu"
        # __new__ 绕过 __init__，需手工补齐 decide() 依赖的实例属性；
        # 缺 has_memory 会让三个用例直接 AttributeError。
        self.inf.has_memory = False
        self.inf._hidden = None
        self.inf.last_infer_ms = 0.0
        self.inf.n_frames = 0

    def _state(self):
        return {"t": 100.0, "screen_size": [1280, 720],
                "units": [{"cls": "enemy_hero", "screen": [0.6, 0.4, 0.1, 0.2]}],
                "minimap": {"found": True, "dots": {"blue": [[0.5, 0.5]], "red": [[0.7, 0.6]], "yellow": []}},
                "ui": {"gold": 1000, "level": 5, "hp": 0.8}}

    def test_move_decoding(self):
        """move_bin=3 -> theta = (3+0.5)*BIN_ANGLE = 78.75°（右上）。"""
        act = self.inf.decide(self._state())
        self.assertAlmostEqual(act["theta"], 3.5 * BIN_ANGLE, places=5)
        self.assertEqual(act["move_bin"], 3)
        self.assertAlmostEqual(act["r"], 0.8, places=5)

    def test_skill_decoding(self):
        """act=skill2 -> skill_id=2。"""
        self.net.act_i = ACT_NAMES.index("skill2")
        act = self.inf.decide(self._state())
        self.assertEqual(act["skill_id"], 2)

    def test_none_action(self):
        """act=none -> 无 skill/attack/move_bin 字段（外部护栏会转 none）。"""
        act = self.inf.decide(self._state())
        self.assertNotIn("skill_id", act)
        self.assertNotIn("attack", act)

    def test_throttle(self):
        """2 技能节流 3s。"""
        cd = {"skill1": 0.0, "skill2": 0.0, "skill": 0.0}
        a1 = apply_guards({"type": "skill", "id": 2}, 100.0, cd)
        self.assertEqual(a1["type"], "skill")
        a2 = apply_guards({"type": "skill", "id": 2}, 101.0, cd)
        self.assertEqual(a2["type"], "none")
        a3 = apply_guards({"type": "skill", "id": 2}, 103.5, cd)
        self.assertEqual(a3["type"], "skill")

    def test_debounce(self):
        """技能后 50ms 内移动被抑制。"""
        cd = {"skill1": 0.0, "skill2": 0.0, "skill": 100.0}
        a = apply_guards({"type": "move", "theta": 1.0, "r": 0.8}, 100.03, cd)
        self.assertEqual(a["type"], "none")
        a2 = apply_guards({"type": "move", "theta": 1.0, "r": 0.8}, 100.2, cd)
        self.assertEqual(a2["type"], "move")


class TestDecodeAction(unittest.TestCase):
    """回归保护：10 个 act_name 必须各自映射到正确动作类型。

    历史缺陷：主循环曾用 `reason.startswith("bc:")` 兜底 pass，把 move / recall /
    restore / summoner / buy 全部吞掉（move 的 reason 恰为 "bc:move"），
    导致 BC 策略只能放技能和普攻、完全无法移动。以下用例锁死该行为。
    """

    EXPECT = {
        "move": "move", "skill1": "skill", "skill2": "skill", "skill3": "skill",
        "attack": "attack", "buy": "none", "recall": "recall",
        "summoner": "summoner", "restore": "restore", "none": "none",
    }

    def _decide(self, act_name):
        """用 MockNet 走真实 decide()，确保输入与线上格式一致。"""
        net = MockNet()
        net.act_i = ACT_NAMES.index(act_name)
        inf = BCNetInference.__new__(BCNetInference)
        inf.net, inf.device = net, "cpu"
        inf.has_memory, inf._hidden = False, None
        inf.last_infer_ms, inf.n_frames = 0.0, 0
        return inf.decide({
            "t": 100.0, "screen_size": [1280, 720],
            "units": [{"cls": "enemy_hero", "screen": [0.6, 0.4, 0.1, 0.2]}],
            "minimap": {"found": True,
                        "dots": {"blue": [[0.5, 0.5]], "red": [[0.7, 0.6]], "yellow": []}},
            "ui": {"gold": 1000, "level": 5, "hp": 0.8},
        })

    def test_all_act_names_dispatch(self):
        for name in ACT_NAMES:
            with self.subTest(act_name=name):
                got = decode_action(self._decide(name))["type"]
                self.assertEqual(got, self.EXPECT[name],
                                 f"{name} 分派错误：得到 {got}，期望 {self.EXPECT[name]}")

    def test_move_keeps_theta_and_r(self):
        act = decode_action(self._decide("move"))
        self.assertEqual(act["type"], "move")
        self.assertAlmostEqual(act["theta"], 3.5 * BIN_ANGLE, places=5)
        self.assertAlmostEqual(act["r"], 0.8, places=5)

    def test_unknown_act_name_is_noop(self):
        self.assertEqual(decode_action({})["type"], "none")


if __name__ == "__main__":
    unittest.main(verbosity=2)
