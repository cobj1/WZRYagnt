# -*- coding: utf-8 -*-
"""决策轨迹复盘入口：JSONL -> summary.json + HTML。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.observability.report import render_html, summarize_file  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", required=True, help="decision_trace.jsonl")
    ap.add_argument("--out", default=None, help="HTML 输出（默认同名 .html）")
    ap.add_argument("--json", default=None, help="summary JSON 输出")
    a = ap.parse_args()
    s = summarize_file(a.trace)
    if a.json:
        Path(a.json).write_text(json.dumps(s, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    out = Path(a.out) if a.out else Path(a.trace).with_suffix(".html")
    out.write_text(render_html(s, f"决策轨迹复盘 · {Path(a.trace).name}"),
                   encoding="utf-8")
    print(json.dumps(s, ensure_ascii=False))
    print(f"HTML -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
