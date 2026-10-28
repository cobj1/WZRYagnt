# -*- coding: utf-8 -*-
"""小地图 `detect_minimap` 融合块 · 离线 A/B（证明"该块是死路径"）。

背景（`docs/小地图实现对比-2026-10-26.md` §2）：
  m2 主循环有一段"模型 mm_red/mm_blue/mm_green 覆盖 tracker dots"的融合块。
  默认 ONNX 后端（`hok_yolov5s.onnx`，8 个通用类）**没有 mm_* 类**，
  `YoloDetector.detect_minimap` 在该后端下是**无条件 return []**。
  → 融合块永不生效，dots 100% 来自 MMTrackerV7。

原计划"真机跑一周确认日志从不出现非空覆盖"在无设备时无法执行，
本脚本用生产帧直接给出等价证据（且比日志更强：逐帧判定，不是抽样）。

三条判据：
  A1 ONNX 后端 `detect_minimap` 在 N 张生产帧上返回非空次数 = 0
  A2 同批帧上 "开融合" 与 "关融合" 的 dots 序列化后**逐字节相同**
  A3 m2 源码已按后端门控（`_mm_backend == "hok_yolov5_onnx"` 时跳过）

用法：
  venv/Scripts/python.exe -X utf8 scripts/bench_mm_fusion_ab.py
  venv/Scripts/python.exe -X utf8 scripts/bench_mm_fusion_ab.py --limit 60
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MATCHES = ROOT / "data" / "matches"
ONNX = ROOT / "runs" / "detect" / "hok_yolov5" / "hok_yolov5s.onnx"


def _frames(limit: int | None) -> list[Path]:
    fs = sorted(MATCHES.glob("*/frames/*.png"))
    if limit:
        # 均匀抽样，避免只覆盖前几局（语料按会话聚集）
        step = max(1, len(fs) // limit)
        fs = fs[::step][:limit]
    return fs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="均匀抽样的帧数上限")
    ap.add_argument("--json", type=Path, default=ROOT / "runs" / "eval" / "mm_fusion_ab.json")
    args = ap.parse_args()

    if not ONNX.exists():
        print(f"[SKIP] 模型不存在：{ONNX}")
        return 0

    from wzry.vision.detector import YoloDetector
    from wzry.vision.mm_tracker_v7 import MMTrackerV7

    det = YoloDetector(str(ONNX), conf=0.35, intra_threads=8)
    print(f"[info] backend={det.backend} frames={len(_frames(args.limit))}")

    frames = _frames(args.limit)
    nonempty = 0
    diff = 0
    per_frame = []
    for i, fp in enumerate(frames):
        frame = cv2.imread(str(fp))
        if frame is None:
            continue
        # A1：ONNX 后端下 detect_minimap 必须恒空
        mdets = det.detect_minimap(frame[0:232, 0:232])
        if mdets:
            nonempty += 1
            per_frame.append({"frame": str(fp), "n_dets": len(mdets),
                              "classes": [d.cls for d in mdets]})

        # A2：开/关融合后 dots 是否一致。tracker 有状态，必须各用一份新实例跑同一帧序列，
        # 因此这里逐帧独立跑（单帧 tracker 的 dots 只依赖本帧，足够验证"覆盖"这件事）
        t_off = MMTrackerV7()
        mm_off = t_off.update(frame)
        dots_off = json.dumps(mm_off.get("dots"), sort_keys=True, ensure_ascii=False)

        t_on = MMTrackerV7()
        mm_on = t_on.update(frame)
        if mdets:                       # 模拟融合块：有结果才覆盖
            msz = 232.0
            for key, cls in (("red", "mm_red"), ("blue", "mm_blue"), ("green", "mm_green")):
                sel = [d for d in mdets if d.cls == cls and d.conf >= 0.60]
                if sel:
                    mm_on["dots"][key] = [[round(d.center[0] / msz, 4),
                                           round(d.center[1] / msz, 4)] for d in sel]
        dots_on = json.dumps(mm_on.get("dots"), sort_keys=True, ensure_ascii=False)
        if dots_on != dots_off:
            diff += 1
        if (i + 1) % 50 == 0:
            print(f"  ... {i + 1}/{len(frames)}  非空={nonempty} dots不一致={diff}")

    n = len(frames)
    print("\n=== 结果 ===")
    print(f"帧数                  : {n}")
    print(f"A1 非空 detect_minimap: {nonempty} ({nonempty / max(n, 1):.1%})")
    print(f"A2 dots 不一致帧      : {diff} ({diff / max(n, 1):.1%})")

    src = (ROOT / "scripts" / "m2_agent_v2.py").read_text(encoding="utf-8")
    a3 = 'hok_yolov5_onnx' in src and '_mm_backend' in src
    print(f"A3 m2 已按后端门控    : {a3}")

    ok = (nonempty == 0) and (diff == 0) and a3
    print(f"\n判定: {'PASS —— 融合块在 ONNX 后端确为死路径，门控安全' if ok else 'FAIL —— 见上方计数'}")

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps({
        "frames": n, "nonempty_detect_minimap": nonempty,
        "dots_mismatch": diff, "m2_gated": a3,
        "backend": det.backend, "pass": ok,
        "nonempty_samples": per_frame[:10],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[out] {args.json}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
