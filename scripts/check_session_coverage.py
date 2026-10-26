# -*- coding: utf-8 -*-
"""会话字段覆盖率检查（采数闸门）。

回答一个问题：**这一局采到的数据，够不够喂决策层/价值模型？**

判定字段（与 `MatchRecorder.coverage_gate()` 同源）：
  clock_s            对局时钟        >= 0.95
  scoreboard         双方经济/击杀    >= 0.90
  units.pos_minimap  单位小地图坐标   >= 0.90
  ui.hp              自身血量         >= 0.95

用法：
  venv/Scripts/python.exe -X utf8 scripts/check_session_coverage.py
  venv/Scripts/python.exe -X utf8 scripts/check_session_coverage.py --strict --json ../hokdata/session_coverage.json
  venv/Scripts/python.exe -X utf8 scripts/check_session_coverage.py --sessions 2026102 --worst 5
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATCHES = ROOT / "data" / "matches"

FIELDS = {
    "clock_s": 0.95,
    "scoreboard": 0.90,
    "units.pos_minimap": 0.90,
    "ui.hp": 0.95,
}
EXTRA = ("units", "minimap.dots")


def scan_session(path: Path) -> dict:
    hits = {k: 0 for k in list(FIELDS) + list(EXTRA)}
    frames = 0
    bad = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                st = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            frames += 1
            ui = st.get("ui") or {}
            units = st.get("units") or []
            if units:
                hits["units"] += 1
                if any(isinstance(u, dict) and u.get("pos_minimap") for u in units):
                    hits["units.pos_minimap"] += 1
            if isinstance(ui.get("hp"), (int, float)):
                hits["ui.hp"] += 1
            clock = st.get("clock_s")
            if not isinstance(clock, (int, float)):
                clock = ui.get("match_clock")
            if not isinstance(clock, (int, float)):
                clock = (st.get("extra") or {}).get("mem_clock_s")
            if isinstance(clock, (int, float)):
                hits["clock_s"] += 1
            if st.get("scoreboard"):
                hits["scoreboard"] += 1
            if (st.get("minimap") or {}).get("dots"):
                hits["minimap.dots"] += 1
    denom = max(1, frames)
    result = None
    man = path.parent / "manifest.json"
    if man.exists():
        try:
            result = json.loads(man.read_text(encoding="utf-8")).get("result")
        except (OSError, json.JSONDecodeError):
            result = None
    return {
        "session": path.parent.name,
        "frames": frames,
        "bad_lines": bad,
        "result": result,
        "coverage": {k: round(v / denom, 4) for k, v in hits.items()},
    }


def verdict(cov: dict) -> list[str]:
    return [f"{k} {cov.get(k, 0):.0%}<{need:.0%}" for k, need in FIELDS.items()
            if cov.get(k, 0.0) < need]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", default=None, help="逗号分隔的会话名前缀，默认全部")
    ap.add_argument("--worst", type=int, default=0, help="只打印覆盖率最差的 N 个会话")
    ap.add_argument("--json", default="", help="把结果写到该路径")
    ap.add_argument("--strict", action="store_true", help="任一会话未达标则返回 1")
    args = ap.parse_args()

    prefixes = [s.strip() for s in (args.sessions or "").split(",") if s.strip()]
    paths = [p for p in sorted(MATCHES.glob("*/states.jsonl"))
             if not prefixes or any(p.parent.name.startswith(x) for x in prefixes)]
    if not paths:
        print("没有匹配的会话", file=sys.stderr)
        return 2

    rows = [scan_session(p) for p in paths]
    rows.sort(key=lambda r: (sum(r["coverage"][k] for k in FIELDS), -r["frames"]))

    total_frames = sum(r["frames"] for r in rows)
    agg = {k: (round(sum(r["coverage"][k] * r["frames"] for r in rows) / max(1, total_frames), 4))
           for k in list(FIELDS) + list(EXTRA)}

    with_result = sum(1 for r in rows if r.get("result") in {"win", "loss", "draw"})
    print(f"会话 {len(rows)} / 帧 {total_frames} / 有胜负结果 {with_result}/{len(rows)}")
    print(f"整体覆盖: " + "  ".join(f"{k}={v:.1%}" for k, v in agg.items()))
    print()
    print(f"{'会话':<26}{'帧':>6}  " + "  ".join(f"{k[:14]:>14}" for k in FIELDS))
    show = rows[:args.worst] if args.worst else rows
    for r in show:
        cells = "  ".join(f"{r['coverage'][k]:>14.1%}" for k in FIELDS)
        print(f"{r['session']:<26}{r['frames']:>6}  {cells}")

    failing = [r for r in rows if verdict(r["coverage"])]
    print()
    if failing:
        print(f"未达标会话 {len(failing)}/{len(rows)}（示例 {min(3, len(failing))} 条）：")
        for r in failing[:3]:
            print(f"  - {r['session']}: " + "; ".join(verdict(r["coverage"])))
    else:
        print("全部会话通过采数闸门")

    if with_result < len(rows):
        print(f"\n[警告] {len(rows) - with_result} 个会话没有胜负结果 —— "
              f"价值模型无法训练（用 MatchRecorder.on_result() 补记录）")

    payload = {
        "gate": FIELDS,
        "sessions": len(rows),
        "frames": total_frames,
        "sessions_with_result": with_result,
        "result_missing": [r["session"] for r in rows
                           if r.get("result") not in {"win", "loss", "draw"}],
        "aggregate": agg,
        "failing": [{"session": r["session"], "reasons": verdict(r["coverage"])}
                    for r in failing],
        "per_session": rows,
    }
    if args.json:
        out = Path(args.json)
        if not out.is_absolute():
            out = ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出 {out}")

    if args.strict and failing:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
