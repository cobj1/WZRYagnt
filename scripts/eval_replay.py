# -*- coding: utf-8 -*-
"""离线回放评估入口（EVAL-01）。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from m2_agent_v2 import decide  # noqa: E402
from wzry.decision.engine import DecisionEngine  # noqa: E402
from wzry.evaluation.replay import (  # noqa: E402
    evaluate_session, render_markdown, summarize,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT / "data" / "matches"))
    ap.add_argument("--sessions", nargs="*", default=None)
    ap.add_argument("--tolerance", type=float, default=1.0)
    ap.add_argument("--limit", type=int, default=0, help="每会话最多帧数")
    ap.add_argument("--json", default=None)
    ap.add_argument("--md", default=None)
    ap.add_argument("--min-type-agreement", type=float, default=None,
                    help="低于该 type 一致率则退出 1（回归门禁用）")
    a = ap.parse_args()
    root = Path(a.root)
    dirs = sorted(p for p in root.iterdir() if p.is_dir())
    if a.sessions:
        dirs = [p for p in dirs if p.name in set(a.sessions)]
    eng = DecisionEngine(decide)
    results = []
    for d in dirs:
        states, actions = d / "states.jsonl", d / "actions.jsonl"
        if not states.exists():
            continue
        results.append(evaluate_session(
            session=d.name, states_path=states, actions_path=actions,
            decide=eng.decide_rule, tolerance=a.tolerance, limit=a.limit))
    summary = summarize(results)
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    if a.json:
        Path(a.json).write_text(text, encoding="utf-8")
    if a.md:
        Path(a.md).write_text(render_markdown(summary), encoding="utf-8")
    print(text)
    if (a.min_type_agreement is not None
            and (summary["type_agreement"] or 0.0) < a.min_type_agreement):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
