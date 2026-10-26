# -*- coding: utf-8 -*-
"""推理部署自检入口（MODEL-02）。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.decision.inference import inspect, load_manifest  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--load", action="store_true", help="真正加载模型（较慢）")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    rep = inspect(load_manifest(a.manifest), ROOT, load=a.load)
    text = json.dumps(rep, ensure_ascii=False, indent=2)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    print(text)
    return 0 if rep["primary_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
