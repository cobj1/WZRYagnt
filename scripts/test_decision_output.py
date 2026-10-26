# -*- coding: utf-8 -*-
"""DecisionOutput / DecisionEngine 单测（ARCH-01/02 phase 1）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.decision.engine import DecisionEngine  # noqa: E402
from wzry.decision.output import DecisionOutput  # noqa: E402
from wzry.runtime.action import Action  # noqa: E402

PASS = []


def check(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print(f"PASS {name}")
    else:
        raise AssertionError(f"FAIL {name} {extra}")


def main() -> int:
    out = DecisionOutput(action={"type": "map_move", "nx": 0.4, "ny": 0.6,
                                 "reason": "support_dps_lane"})
    check("reason 从 action 继承", out.reason == "support_dps_lane")
    check("action_type", out.action_type == "map_move")
    act = out.to_action()
    check("map_move 可转 Action", isinstance(act, Action) and act.type == "map_move")
    check("Action 携带参数", act.params.get("nx") == 0.4)
    check("none 不转 Action", DecisionOutput(action={"type": "none"}).to_action() is None)

    try:
        DecisionOutput(action={"nx": 0.4})
        check("缺 type 必须报错", False)
    except ValueError:
        check("缺 type 必须报错", True)

    calls = []

    def rule(state, cooldowns):
        calls.append((state, cooldowns))
        return {"type": "skill", "id": 2, "reason": "enemy_in_skill2_range"}

    eng = DecisionEngine(rule)
    got = eng.decide_rule({"t": 1.0}, {"skill2_t": 0.0})
    check("engine 透传 action", got.action["id"] == 2)
    check("engine reason", got.reason == "enemy_in_skill2_range")
    check("engine frames", eng.frames == 1 and eng.last is got)
    check("engine 参数透传", calls == [({"t": 1.0}, {"skill2_t": 0.0})])
    check("engine latency", got.latency_ms >= 0.0)

    def bad(state, cooldowns):
        return None

    try:
        DecisionEngine(bad).decide_rule({}, {})
        check("非法返回必须报错", False)
    except TypeError:
        check("非法返回必须报错", True)

    out2 = DecisionOutput(action={"type": "move", "theta": 0.5, "duration_ms": 380,
                                  "reason": "lane"})
    rt = out2.validated_action()
    check("协议回环保留 duration", rt.get("duration_ms") == 380, f"{rt}")
    check("协议回环保留 reason", rt.get("reason") == "lane", f"{rt}")
    check("协议回环保留参数", rt.get("theta") == 0.5, f"{rt}")
    proto = out2.to_action()
    check("Action 携带 reason", proto.reason == "lane", f"{proto}")
    check("Action 携带 duration", proto.duration_ms == 380, f"{proto}")

    adopted = eng.adopt({"type": "map_move", "nx": 0.2, "ny": 0.8,
                         "reason": "dps_lane_push"},
                        source="optimizer", latency_ms=3.5)
    check("adopt 来源", adopted.source == "optimizer")
    check("adopt 不改帧数", eng.frames == 1)
    check("adopt 计入接管", eng.adoptions == 1)
    check("summary 来源计数", eng.summary()["sources"].get("optimizer") == 1)
    check("summary 指标", eng.summary()["metrics"]["counters"]
          .get("decision.source.optimizer") == 1)
    try:
        eng.adopt({"type": "none"}, source="bogus")
        check("非法来源必须报错", False)
    except ValueError:
        check("非法来源必须报错", True)

    if len(PASS) != 23:
        raise AssertionError(f"expected 11 checks, got {len(PASS)}")
    print(f"\nALL PASS ({len(PASS)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
