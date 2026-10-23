# -*- coding: utf-8 -*-
"""局内采集：周期性 dump 全英雄状态数组 + 战绩面板 ground truth 截图。

用途：为 KDA / 累计经济字段定位提供时间序列（配合 analyze_hero_fields.py）。

用法（后台跑 ~6 分钟）：
  venv/Scripts/python.exe -X utf8 reverse/collect_hero_fields.py --out reverse/out/mem/structs
"""
import argparse
import json
import sys
import time
from pathlib import Path

import subprocess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision.mem_source import (  # noqa: E402
    MemSource, HERO_ARR_STRIDE, HERO_ARR_COUNT, SELF_OBJ_ID)

ADB = r"D:\app\MuMuPlayer\nx_main\adb.exe"
SERIAL = "127.0.0.1:16384"
SCORE_BTN = (1665, 50)      # 战绩面板开关
SCORE_CLOSE = (1843, 121)   # 面板 X


def tap(x, y):
    subprocess.run([ADB, "-s", SERIAL, "shell", "input", "tap", str(x), str(y)],
                   capture_output=True)


def screenshot(path):
    with open(path, "wb") as f:
        subprocess.run([ADB, "-s", SERIAL, "exec-out", "screencap", "-p"],
                       stdout=f, capture_output=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="reverse/out/mem/structs")
    ap.add_argument("--cycles", type=int, default=24)
    ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--gt-at", default="4,12,20",
                    help="在这些 cycle 序号处抓战绩面板 ground truth")
    a = ap.parse_args()
    outdir = ROOT / a.out
    outdir.mkdir(parents=True, exist_ok=True)
    gt_cycles = {int(x) for x in a.gt_at.split(",") if x.strip()}

    src = MemSource(serial=SERIAL)
    print("pid", src.connect(), flush=True)
    states = src.read_hero_states()
    if not states:
        print("no hero array; are we in a match?", flush=True)
        return 1
    print("arrays", [hex(b) for b in src._hero_arrays], flush=True)

    meta = {"t0": time.time(), "dumps": []}
    base = src._hero_arrays[0]
    for k in range(a.cycles):
        t = time.time()
        # ground truth：开面板 -> 截图 -> 关面板 -> 立刻 dump
        if k in gt_cycles:
            tap(*SCORE_BTN)
            time.sleep(1.6)
            shot = outdir / f"gt_{k:02d}.png"
            screenshot(shot)
            tap(*SCORE_CLOSE)
            time.sleep(0.8)
        raw = src.m.read(base, HERO_ARR_STRIDE * HERO_ARR_COUNT)
        fp = outdir / f"d_{k:02d}.bin"
        fp.write_bytes(raw)
        meta["dumps"].append({"k": k, "t": t - meta["t0"], "file": fp.name,
                              "gt": k in gt_cycles,
                              "gt_png": f"gt_{k:02d}.png" if k in gt_cycles else None})
        print(f"[{k:02d}] t={t-meta['t0']:6.1f}s bytes={len(raw)} "
              f"{'GT' if k in gt_cycles else ''}", flush=True)
        time.sleep(max(0.0, a.interval - (time.time() - t)))
    (outdir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print("done ->", outdir, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
