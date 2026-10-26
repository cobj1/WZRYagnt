# -*- coding: utf-8 -*-
"""把"结算页胜负"回填进存量会话 manifest。

为什么：价值模型的唯一监督信号是胜负，而 45 局存量数据 **0 局有 result**（见
`docs/数据专题/训练数据契约-2026-10-26.md`）。`wzry/vision/match_result.py` 已能
从结算页横幅判读，但只接进了实时 `--result auto`，存量数据没有回填入口。

本脚本做三件事（默认 dry-run，不写盘）：
  1. 扫描每个会话 `frames/` 的最后 K 张，逐张 classify；
  2. 取置信度最高的一次判定（没有就记 `unknown`，并写明原因，**不猜**）；
  3. `--apply` 时把 `result` + `meta.result` 合并进 manifest（原子替换，保留其余键）。

⚠️ 胜利横幅尚无真机样本（色相假设），默认只接受 `loss`；要接受 `win` 需显式 `--allow-win`。

用法：
  venv/Scripts/python.exe -X utf8 scripts/backfill_session_result.py
  venv/Scripts/python.exe -X utf8 scripts/backfill_session_result.py --apply --allow-win
  venv/Scripts/python.exe -X utf8 scripts/backfill_session_result.py --json ../hokdata/result_backfill.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MATCHES = ROOT / "data" / "matches"
IMG_EXT = {".png", ".jpg", ".jpeg"}
DEFAULT_TAIL = 8


def list_frames(session_dir: Path) -> list[Path]:
    fdir = session_dir / "frames"
    if not fdir.is_dir():
        return []
    return sorted(p for p in fdir.iterdir() if p.suffix.lower() in IMG_EXT)


def classify_session(session_dir: Path, tail: int, allow_win: bool) -> dict:
    import cv2
    from wzry.vision.match_result import classify

    frames = list_frames(session_dir)
    out = {
        "session": session_dir.name,
        "frames_total": len(frames),
        "scanned": 0,
        "result": None,
        "conf": 0.0,
        "frame": None,
        "reason": "",
    }
    if not frames:
        out["reason"] = "no_frames"
        return out

    best = (None, 0.0, None, {})
    for path in frames[-tail:]:
        img = cv2.imread(str(path))
        if img is None:
            continue
        out["scanned"] += 1
        res, conf, detail = classify(img, allow_unverified_win=allow_win)
        if res is not None and conf > best[1]:
            best = (res, conf, path.name, detail)

    if best[0] is None:
        out["reason"] = "no_settlement_banner"
        return out
    out.update({"result": best[0], "conf": round(best[1], 4),
                "frame": best[2], "reason": "banner"})
    return out


def merge_manifest(session_dir: Path, row: dict, *, overwrite: bool) -> str:
    path = session_dir / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return "manifest_unreadable"
    if data.get("result") and not overwrite:
        return "skip_existing"
    data["result"] = row["result"]
    meta = data.get("meta") or {}
    meta["result"] = {
        "value": row["result"],
        "source": "frame_scan",
        "frame": row["frame"],
        "conf": row["conf"],
        "classified_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "detector": "wzry/vision/match_result.py",
    }
    data["meta"] = meta
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return "written"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tail", type=int, default=DEFAULT_TAIL, help="每局扫描最后 K 张帧")
    ap.add_argument("--apply", action="store_true", help="真正写 manifest（默认 dry-run）")
    ap.add_argument("--allow-win", action="store_true",
                    help="接受胜利判定（横幅色相未用真机样本验证）")
    ap.add_argument("--overwrite", action="store_true", help="覆盖已有 result")
    ap.add_argument("--json", default="", help="结果写到该路径")
    args = ap.parse_args()

    sessions = sorted(p for p in MATCHES.iterdir() if p.is_dir() and (p / "states.jsonl").exists())
    if not sessions:
        print("没有会话", file=sys.stderr)
        return 2

    rows, actions = [], []
    for sdir in sessions:
        row = classify_session(sdir, args.tail, args.allow_win)
        if row["result"] and args.apply:
            row["action"] = merge_manifest(sdir, row, overwrite=args.overwrite)
        elif row["result"]:
            row["action"] = "would_write"
        else:
            row["action"] = "skipped"
        actions.append(row["action"])
        rows.append(row)

    detected = [r for r in rows if r["result"]]
    print(f"会话 {len(rows)}；扫到帧的 {sum(1 for r in rows if r['scanned'])}；"
          f"判定出结果 {len(detected)}")
    print(f"{'会话':<26}{'帧总数':>7}{'扫描':>5}{'结果':>6}{'conf':>7}  动作")
    for r in sorted(rows, key=lambda x: (-(x["conf"] or 0), x["session"]))[:25]:
        print(f"{r['session']:<26}{r['frames_total']:>7}{r['scanned']:>5}"
              f"{str(r['result'] or '-'):>6}{(r['conf'] or 0):>7.2f}  {r['action']}")
    if len(rows) > 25:
        print(f"... 其余 {len(rows) - 25} 条省略")

    tally = {k: actions.count(k) for k in set(actions)}
    print("\n动作汇总:", tally)
    if not args.apply:
        print("（dry-run：未写盘；确认后加 --apply）")

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": "apply" if args.apply else "dry-run",
        "allow_win": args.allow_win,
        "sessions": len(rows),
        "detected": len(detected),
        "tally": tally,
        "rows": rows,
    }
    if args.json:
        out = Path(args.json)
        if not out.is_absolute():
            out = ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写出 {out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
