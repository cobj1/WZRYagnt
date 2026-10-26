# -*- coding: utf-8 -*-
"""决策轨迹汇总与 HTML 面板单测（OBS-01）。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.observability.report import render_html, summarize, summarize_file  # noqa: E402

PASS = []


def check(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print(f"PASS {name}")
    else:
        raise AssertionError(f"FAIL {name} {extra}")


def main() -> int:
    rows = [
        {"source": "rule", "action_type": "map_move", "reason": "dps_lane_push",
         "latency_ms": 1.0, "loop_ms": 20.0, "capture_lag_ms": 30.0,
         "infer_ms": 10.0, "hp": 0.8,
         "task": {"goal": "dps_lane_push"}},
        {"source": "optimizer", "action_type": "skill", "reason": "hook",
         "latency_ms": 3.0, "loop_ms": 25.0, "capture_lag_ms": 40.0,
         "infer_ms": 12.0, "hp": 0.2},
        {"source": "rule", "action_type": "map_move", "reason": "escape_turret",
         "latency_ms": 2.0, "loop_ms": 22.0, "capture_lag_ms": 35.0,
         "hp": 0.5},
    ]
    s = summarize(rows)
    check("帧数", s["frames"] == 3)
    check("来源分布", s["sources"]["rule"] == 2 and s["sources"]["optimizer"] == 1)
    check("动作分布", s["actions"]["map_move"] == 2)
    check("低血帧", s["low_hp_frames"] == 1)
    check("p95 存在", s["timing"]["latency_ms"]["p95_ms"] is not None)
    check("任务意图", s["tasks"]["dps_lane_push"] == 1)

    html = render_html(s)
    check("HTML 自包含", "<!doctype html>" in html and "决策来源" in html)
    check("HTML 无未转义注入", "<script>" not in render_html(
        summarize([{"source": "<script>", "action_type": "x"}])))

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "t.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        check("文件汇总", summarize_file(p)["frames"] == 3)

    if len(PASS) != 9:
        raise AssertionError(f"expected 9 checks, got {len(PASS)}")
    print(f"\nALL PASS ({len(PASS)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
