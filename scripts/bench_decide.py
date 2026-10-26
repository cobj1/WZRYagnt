# -*- coding: utf-8 -*-
"""重构后规则决策链延迟基准（PERF-01）。

测量对象是 **DecisionEngine.decide_rule(state, cooldowns)** —— 即 m2 每帧
实际调用的入口（含 context/engage/combat/combo/safety 抽层后的全部规则）。
用合成状态覆盖四类场景，避免依赖设备与历史数据。

注意：这不等于主循环延迟；capture/infer 的实局分位由 `--decision-trace`
在真机上采集。本脚本只回答"决策层是否仍满足 40ms 预算"。

用法：
    venv/Scripts/python.exe -X utf8 scripts/bench_decide.py --strict
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from m2_agent_v2 import decide  # noqa: E402
from m2_agent_v2_test import fresh_cd, state  # noqa: E402
from wzry.decision.engine import DecisionEngine  # noqa: E402

BUDGET_MS = 40.0


def scenarios() -> dict[str, tuple[dict, dict]]:
    def enemy(cx, cy):
        return {"cls": "enemy_hero", "screen": [cx, cy, 0.06, 0.06],
                "conf": 0.9, "camp_src": "pixel"}

    base_ui = {"hp": 0.8, "mp": 0.8, "skill_states": {}}
    idle = state()
    idle["ui"] = dict(base_ui)
    idle["extra"] = {}

    fight = state(units=[enemy(0.55, 0.5), enemy(0.58, 0.52)],
                  minimap={"found": True, "dots": {"red": [(0.6, 0.4)],
                                                   "blue": [], "green": [(0.5, 0.6)]}})
    fight["ui"] = dict(base_ui)
    fight["extra"] = {"ally_bars": [], "enemy_bars": []}

    low = state(units=[enemy(0.52, 0.5)])
    low["ui"] = {"hp": 0.2, "mp": 0.3, "skill_states": {}}
    low["extra"] = {"enemy_bars": []}

    push = state(minimap={"found": True,
                          "dots": {"red": [(0.8, 0.2)], "blue": [],
                                   "green": [(0.2, 0.8)]}})
    push["ui"] = {"hp": 0.9, "mp": 0.9, "skill_states": {}}
    push["extra"] = {"ally_bars": []}
    return {"idle": (idle, fresh_cd()), "fight": (fight, fresh_cd()),
            "low_hp": (low, fresh_cd()), "push": (push, fresh_cd())}


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(math.ceil(p / 100.0 * len(s))) - 1))
    return s[k]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=200)
    ap.add_argument("--budget", type=float, default=BUDGET_MS)
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    eng = DecisionEngine(decide)
    report = {"iters": a.iters, "budget_ms": a.budget, "scenarios": {}}
    failed = []
    for name, (st, cd) in scenarios().items():
        for _ in range(20):
            eng.decide_rule(dict(st), dict(cd))
        xs = []
        errs = 0
        for _ in range(a.iters):
            t0 = time.perf_counter()
            try:
                eng.decide_rule(dict(st), dict(cd))
            except Exception:
                errs += 1
            xs.append((time.perf_counter() - t0) * 1000.0)
        p95 = pct(xs, 95)
        report["scenarios"][name] = {
            "n": len(xs), "errors": errs,
            "p50_ms": round(statistics.median(xs), 3),
            "p95_ms": round(p95, 3), "max_ms": round(max(xs), 3),
            "ok": errs == 0 and p95 <= a.budget,
        }
        if not report["scenarios"][name]["ok"]:
            failed.append(name)
    report["engine"] = {"frames": eng.frames, "adoptions": eng.adoptions,
                        "sources": dict(eng.source_counts)}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if a.json:
        Path(a.json).write_text(text, encoding="utf-8")
    print(text)
    return 1 if (a.strict and failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
