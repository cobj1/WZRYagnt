# -*- coding: utf-8 -*-
"""标定小地图方形框, 写入 configs/calibration_absolute.json 的 "minimap_box"。

## 为什么需要

`wzry/vision/mm_rules_v7.py` 原先写死 `MM_SIZE = 242` 且隐含原点 `(0, 0)` —— 这是
**16:9(1280x720, MuMu 模拟器)** 基准。而真机 Redmi K60 是 **20:9(1280x576)**，帧高不同
-> 帧高既变, 小地图的边长与横向位置都变。用旧框裁剪, 20 个已知塔点的颜色判据
(`_tower_alive`) 只有 8/10 与 5/10 通过; 用对框是 10/10 与 10/10。

实测规律: **边长 ≈ 0.336 × 帧高**（16:9: 242/720=0.3361；20:9: 194/576=0.3368），
**横向偏移随布局/设备变**（16:9 = 0；20:9 = 63）-> 必须逐分辨率标定。

## 方法（任务对齐, 不用人工看地图）

不靠"找一张地图图片来对照", 而是**直接用下游真正关心的判据打分**:
在 20 个已知塔点(BLUE_TOWER_PTS/RED_TOWER_PTS)上跑 `_tower_alive`, 搜索 (x, size)
使**通过数最多**。地图几何多年稳定, 塔点位置本身无需重标(已在 16:9 帧上验证 96.6% 全中),
需要标定的只是"小地图画在屏幕的哪个方框里"。

## 用法

    venv\\Scripts\\python.exe -X utf8 scripts\\calibrate_minimap_box.py            # 扫 data/matches/*/frames
    venv\\Scripts\\python.exe -X utf8 scripts\\calibrate_minimap_box.py --dir <帧目录> --write
"""
import argparse
import glob
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision.mm_rules_v7 import (  # noqa: E402
    BLUE_TOWER_PTS, MM_SIZE_FRAC_H, RED_TOWER_PTS, _tower_alive,
)

CALIB = ROOT / "configs" / "calibration_absolute.json"


def _score(frame, x, size, pts_b, pts_r):
    """(x, size) 下 20 个塔点的通过数（越大越好）。"""
    if size <= 20 or x < 0 or x + size > frame.shape[1] or size > frame.shape[0]:
        return -1
    mm = frame[0:size, x:x + size]
    n = 0
    for a, b in pts_b:
        if _tower_alive(mm, int(a * size), int(b * size), "ally"):
            n += 1
    for a, b in pts_r:
        if _tower_alive(mm, int(a * size), int(b * size), "enemy"):
            n += 1
    return n


def calibrate(frames, max_frames=14, verbose=True):
    """帧列表 -> {(w,h): (x, y, size)}。逐帧取最优 (x,size), 再取众数。"""
    by_size = defaultdict(list)
    for f in frames:
        h, w = f.shape[:2]
        by_size[(w, h)].append(f)
    out = {}
    for (w, h), fs in sorted(by_size.items()):
        s0 = int(round(MM_SIZE_FRAC_H * h))
        sizes = [s for s in range(s0 - 4, s0 + 5, 2) if s >= 40]
        xs = list(range(0, int(0.12 * w) + 1, 3))
        votes = Counter()
        for fr in fs[:max_frames]:
            best = None
            for size in sizes:
                for x in xs:
                    sc = _score(fr, x, size, BLUE_TOWER_PTS, RED_TOWER_PTS)
                    if best is None or sc > best[0]:
                        best = (sc, x, size)
            if best and best[0] > 0:
                votes[(best[1], best[2])] += 1
        if not votes:
            out[(w, h)] = (0, 0, s0)
            if verbose:
                print(f'  {w}x{h}: 未取得有效投票 -> 回退 (0, 0, {s0})')
            continue
        (bx, bs), n = votes.most_common(1)[0]
        out[(w, h)] = (bx, 0, bs)
        if verbose:
            print(f'  {w}x{h}: 最优框 x={bx} y=0 size={bs}  '
                  f'(帧数 {len(fs)}, 投票 {n}/{min(len(fs), max_frames)}, 满分=20)')
            # 用"选定框"在全部帧上评分（不再重跑完整搜索，否则 200+ 帧会跑很久）
            tot = [_score(fr, bx, bs, BLUE_TOWER_PTS, RED_TOWER_PTS) for fr in fs]
            tot = [t for t in tot if t >= 0]
            print(f'            选定框全帧得分: 中位 {int(np.median(tot))}/20, '
                  f'最小 {min(tot)}/20, 满分帧 {sum(1 for t in tot if t == 20)}/{len(tot)}')
    return out


def main():
    ap = argparse.ArgumentParser(description="标定小地图方形框")
    ap.add_argument("--dir", default=None, help="帧目录（默认扫 data/matches/*/frames）")
    ap.add_argument("--write", action="store_true", help="写回 calibration_absolute.json")
    ap.add_argument("--max-frames", type=int, default=14)
    args = ap.parse_args()

    if args.dir:
        paths = sorted(glob.glob(str(Path(args.dir) / "*.png")))
    else:
        paths = sorted(glob.glob(str(ROOT / "data" / "matches" / "*" / "frames" / "*.png")))
    frames = []
    for p in paths:
        img = cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is not None:
            frames.append(img)
    print(f"读入帧 {len(frames)} 张（来自 {len(paths)} 个文件）")
    if not frames:
        print("没有可用帧。")
        return 1

    table = calibrate(frames, max_frames=args.max_frames)
    print("\n标定结果 minimap_box:")
    cfg_in = {}
    if CALIB.exists():
        cfg_in = json.loads(CALIB.read_text(encoding="utf-8"))
    old = cfg_in.get("minimap_box") or {}
    merged = dict(old)
    for (w, h), (x, y, s) in table.items():
        merged[f"{w}x{h}"] = [int(x), int(y), int(s)]
        print(f'  "{w}x{h}": [{int(x)}, {int(y)}, {int(s)}]')
    if args.write:
        cfg_in["minimap_box"] = merged
        CALIB.write_text(json.dumps(cfg_in, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已写回 {CALIB}")
    else:
        print("\n（未写回；加 --write 生效）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
