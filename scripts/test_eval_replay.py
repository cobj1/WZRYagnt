# -*- coding: utf-8 -*-
"""离线回放评估单测（EVAL-01）。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.decision.output import DecisionOutput  # noqa: E402
from wzry.evaluation.replay import (  # noqa: E402
    evaluate_session, load_actions, nearest_action, render_markdown, summarize,
)

PASS = []


def check(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print(f"PASS {name}")
    else:
        raise AssertionError(f"FAIL {name} {extra}")


def fake_decide(state, cooldowns):
    return DecisionOutput(action={"type": "map_move", "nx": 0.5, "ny": 0.5,
                                  "reason": "support_dps_lane"},
                          latency_ms=1.25)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        states = d / "states.jsonl"
        acts = d / "actions.jsonl"
        states.write_text("\n".join([
            json.dumps({"t": 0.0, "units": [], "minimap": {}, "ui": {}}),
            json.dumps({"t": 1.0, "units": [], "minimap": {}, "ui": {}}),
            json.dumps({"t": 2.0, "units": [], "minimap": {}, "ui": {}}),
            "{bad json",
        ]) + "\n", encoding="utf-8")
        acts.write_text("\n".join([
            json.dumps({"t": 0.9, "type": "map_move",
                        "reason": "support_dps_lane"}),
            "",
        ]), encoding="utf-8")

        loaded = load_actions(acts)
        check("动作解析跳过空行", len(loaded) == 1)
        check("最近动作命中", nearest_action(loaded, 1.0, 1.0)["type"] == "map_move")
        check("超容差不对齐", nearest_action(loaded, 3.0, 1.0) is None)

        r = evaluate_session(session="s1", states_path=states, actions_path=acts,
                             decide=fake_decide, tolerance=1.0)
        check("帧数", r.frames == 3)
        check("异常行计数", r.errors == 1)
        check("可对齐帧", r.matched == 2)
        check("type 一致", r.type_agree == 2)
        check("reason 一致", r.reason_agree == 2)
        check("延迟记录", r.latencies == [1.25, 1.25, 1.25])

        s = summarize([r])
        check("汇总帧", s["frames"] == 3 and s["matched"] == 2)
        check("汇总一致率", s["type_agreement"] == 1.0)
        check("汇总 p95", s["latency_p95_ms"] == 1.25)
        check("Markdown 生成", "动作 type 一致率" in render_markdown(s))

    if len(PASS) != 13:
        raise AssertionError(f"expected 13 checks, got {len(PASS)}")
    print(f"\nALL PASS ({len(PASS)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
