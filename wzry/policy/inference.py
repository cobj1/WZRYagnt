# -*- coding: utf-8 -*-
"""BCNet / BCNetMem 推理封装：GameState -> 动作原语 dict。

两种权重都支持：
  - 旧 BCNet（无记忆）：checkpoint 里没有 gru.* 键
  - BCNetMem（带 GRU 记忆）：checkpoint 里有 gru.* 键 -> 自动识别
带记忆时需要在帧间**持久携带 hidden**，并在下列时机 reset()：
  阵亡复活 / 换局 / 阶段切换（否则记忆会跨局污染，表现为"上一局的交战史影响这一局"）。

解码规则（与 encoding.encode_action 对齐）：
  - act: 10 类 ['move','skill1','skill2','skill3','attack','buy','recall',
          'summoner','restore','none'] -> argmax
  - move 时同时给出 theta（8 向 bin）、r 以及 **target**（2 维目标点）；
    规则数据训出来的模型走 target 路径（theta 由执行器现场航向推算），见 encode_action 注释
"""
import math
from pathlib import Path

import numpy as np
import torch

from wzry.policy.model import BCNet, BCNetMem
from wzry.train.encoding import encode_state

ACT_NAMES = ["move", "skill1", "skill2", "skill3", "attack",
             "buy", "recall", "summoner", "restore", "none"]
SKILL_MAP = {"skill1": 1, "skill2": 2, "skill3": 3}
BIN_ANGLE = 2 * math.pi / 8


class BCNetInference:
    def __init__(self, checkpoint, device=None):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        state = torch.load(str(checkpoint), map_location=self.device)
        if isinstance(state, dict) and "state_dict" in state:
            state = state["state_dict"]
        self.has_memory = any(k.startswith("gru.") for k in state.keys())
        self.net = BCNetMem() if self.has_memory else BCNet()
        self.net.load_state_dict(state)
        self.net.eval().to(self.device)
        self._hidden = None
        self.last_infer_ms = 0.0
        self.n_frames = 0

    def reset(self, reason=""):
        """重置记忆（阵亡复活/换局/阶段切换时必须调）。"""
        prev = self._hidden is not None
        self._hidden = None
        self.last_reset_reason = reason
        return prev

    def decide(self, state_dict: dict, reset=False) -> dict:
        """GameState dict -> 动作 dict（与 Agent v2 decide 输出同构）。

        reset=True 时先清空记忆再算这一帧（例如刚复活）。
        """
        import time
        if reset:
            self.reset("explicit")
        enc = encode_state(state_dict)
        units = torch.from_numpy(enc["units"]).unsqueeze(0).to(self.device)
        mask = torch.from_numpy(enc["unit_mask"]).unsqueeze(0).to(self.device)
        grid = torch.from_numpy(enc["grid"]).unsqueeze(0).to(self.device)
        ui = torch.from_numpy(enc["ui"]).unsqueeze(0).to(self.device)
        t0 = time.perf_counter()
        with torch.no_grad():
            if self.has_memory:
                out, self._hidden = self.net(units, mask, grid, ui, hidden=self._hidden)
            else:
                out = self.net(units, mask, grid, ui)
        self.last_infer_ms = (time.perf_counter() - t0) * 1000.0
        self.n_frames += 1

        move_bin = int(out["move_theta"].argmax(-1).item())
        theta = (move_bin + 0.5) * BIN_ANGLE  # bin 中心
        r = float(out["move_r"].squeeze().item())
        target = out["target"][0].detach().cpu().numpy()
        act_i = int(out["act"].argmax(-1).item())
        act_name = ACT_NAMES[act_i]

        action = {"theta": theta, "r": max(0.05, min(1.0, r)),
                  "act_name": act_name, "move_bin": move_bin,
                  "target": [round(float(target[0]), 4), round(float(target[1]), 4)],
                  "memory": self.has_memory}
        if act_name in SKILL_MAP:
            action["skill_id"] = SKILL_MAP[act_name]
            action["reason"] = f"bc:skill{action['skill_id']}"
        elif act_name == "attack":
            action["attack"] = True
            action["reason"] = "bc:attack"
        elif act_name == "recall":
            action["recall"] = True
            action["reason"] = "bc:recall"
        elif act_name == "restore":
            action["restore"] = True
            action["reason"] = "bc:restore"
        elif act_name == "summoner":
            action["summoner"] = True
            action["reason"] = "bc:summoner"
        else:
            action["reason"] = f"bc:{act_name}"
        return action

    def stats(self):
        return {"memory": self.has_memory, "frames": self.n_frames,
                "last_infer_ms": round(self.last_infer_ms, 2),
                "hidden_alive": self._hidden is not None}
