# -*- coding: utf-8 -*-
"""Action 执行边界回归（ARCH-01）。

验证生产队列里的 `Action` 对象经 `apply_action` 还原后，与直接传 dict
得到完全相同的执行原语调用。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from m2_agent_v2 import apply_action  # noqa: E402
from wzry.runtime.action import Action  # noqa: E402

PASS = []


def check(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print(f"PASS {name}")
    else:
        raise AssertionError(f"FAIL {name} {extra}")


class FakeEx:
    hold_enabled = False

    def __init__(self):
        self.calls = []

    def attack(self, priority):
        self.calls.append(("attack", priority))

    def summoner(self):
        self.calls.append(("summoner",))

    def recall(self):
        self.calls.append(("recall",))

    def restore(self):
        self.calls.append(("restore",))

    def release_stick(self):
        self.calls.append(("release_stick",))


def run(action):
    ex = FakeEx()
    cd = {}
    apply_action(ex, action, cd)
    return ex.calls


def main() -> int:
    check("dict 普攻", run({"type": "attack", "priority": "tower"})
          == [("attack", "tower")])
    check("Action 普攻",
          run(Action.from_decision({"type": "attack", "priority": "tower",
                                    "reason": "engage"}))
          == [("attack", "tower")])
    check("Action 召唤师先松手",
          run(Action.from_decision({"type": "summoner"}))
          == [("release_stick",), ("summoner",)])
    check("Action 回城先松手",
          run(Action.from_decision({"type": "recall"}))
          == [("release_stick",), ("recall",)])
    check("Action 恢复先松手",
          run(Action.from_decision({"type": "restore"}))
          == [("release_stick",), ("restore",)])
    check("Action 携带 reason",
          Action.from_decision({"type": "attack", "reason": "x"}).reason == "x")
    check("Action 携带 duration",
          Action.from_decision({"type": "move", "duration_ms": 380}).duration_ms == 380)

    proto = Action.from_decision({"type": "move", "theta": 0.5,
                                  "duration_ms": 380, "reason": "lane"})
    check("Action 往返逐键一致",
          proto.to_legacy_dict() == {"type": "move", "theta": 0.5,
                                     "duration_ms": 380, "reason": "lane"})

    if len(PASS) != 8:
        raise AssertionError(f"expected 8 checks, got {len(PASS)}")
    print(f"\nALL PASS ({len(PASS)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
