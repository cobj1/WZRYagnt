# -*- coding: utf-8 -*-
"""自动标定小地图上的**野区营地点位**（不靠人工猜坐标）。

为什么需要
    旧的 `MONSTER_PTS` / `BUFF_PTS` 与真机小地图**对不上**：逐点复核只有 5/20 落在真实
    图标上（`M12` 偏 0.27，`B4/B5` 偏 0.12），还漏了 12 个真实点位。用错点位做"证据门控"，
    得到的只能是变了数值的常量（详见 mm_rules_v7 里 MM_MONSTER_MODE 的说明）。

原理（只用"时间稳定性"这一条，不猜颜色形状）
    营地图标是**常驻**的：几乎每一帧都在同一像素位置绘制；
    而英雄/小兵/野怪是移动的，落在同一像素的概率极低。
    1. 每帧在小地图裁剪上取金橙色掩码 -> 连通域 -> 候选质心；
    2. 全部帧的候选点按半径聚类；
    3. 出现率 ≥ `--min-rate` 的簇 = 常驻结构（营地/主宰）；
    4. 用**周围亮度**剔除 UI 叠层：队友头像是亮色圆环（V≈120~180），
       地图背景暗（V≈70~95）—— 实测阈值 110 能干净分开（见 --max-v）。

关于 `--min-rate` 为什么默认 0.4（而不是 0.9）
    小地图上会叠**队友头像**（3~5 个大圆盘），它们会**遮住**下面的营地。实测：
    15 号的 3 个会话里，被遮住那一半的营地点出现率只有 0~10%，而未被遮的是 100%。
    同一批点在 17 号会话里都是 100%，且**坐标逐点相同**（子集关系）——说明不是坐标漂移，
    只是被遮挡。遮挡不改变位置，所以把阈值放宽就能把"有些会话被挡住的真营地"找回来；
    而移动单位/伪点不可能在同一像素反复出现，因此放宽不会引入假点（仍受亮度过滤保护）。

用法
    python scripts/calibrate_jungle_points.py                 # 只打印
    python scripts/calibrate_jungle_points.py --write         # 写 configs/jungle_points.json
    python scripts/calibrate_jungle_points.py --sessions A B  # 指定会话
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision.mm_rules_v7 import resolve_mm_box  # noqa: E402

OUT = ROOT / "configs" / "jungle_points.json"


def gold_mask(mm):
    hsv = cv2.cvtColor(mm, cv2.COLOR_BGR2HSV)
    H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    m = ((H >= 8) & (H <= 40) & (S >= 100) & (V >= 120)).astype(np.uint8) * 255
    return cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))


def collect(frames):
    """-> [ [(nx, ny, area), ...] per frame ]"""
    out = []
    for f in frames:
        x0, y0, s = resolve_mm_box(f)
        mm = f[y0:y0 + s, x0:x0 + s]
        if mm.shape[0] < s or mm.shape[1] < s:
            continue
        n, _lab, st, cent = cv2.connectedComponentsWithStats(gold_mask(mm), 8)
        cur = []
        for i in range(1, n):
            area, w, h = st[i, cv2.CC_STAT_AREA], st[i, cv2.CC_STAT_WIDTH], st[i, cv2.CC_STAT_HEIGHT]
            if 8 <= area <= 260 and 2 <= w <= 16 and 2 <= h <= 16:
                cur.append((cent[i][0] / s, cent[i][1] / s, int(area)))
        out.append(cur)
    return out


def cluster(per_frame, radius=0.022):
    cl = []      # [cx, cy, {frame_idx}, [area...]]
    for fi, cur in enumerate(per_frame):
        for (x, y, a) in cur:
            best, bd = None, radius
            for c in cl:
                d = ((c[0] - x) ** 2 + (c[1] - y) ** 2) ** 0.5
                if d < bd:
                    best, bd = c, d
            if best is None:
                cl.append([x, y, {fi}, [a]])
            else:
                best[2].add(fi)
                best[3].append(a)
    return cl


def patch_v(frames, nx, ny, r=7):
    vals = []
    for f in frames:
        x0, y0, s = resolve_mm_box(f)
        mm = f[y0:y0 + s, x0:x0 + s]
        px, py = int(nx * s), int(ny * s)
        p = mm[max(0, py - r):py + r + 1, max(0, px - r):px + r + 1]
        if p.size:
            vals.append(cv2.cvtColor(p, cv2.COLOR_BGR2HSV)[..., 2].mean())
    return float(np.mean(vals)) if vals else 0.0


def main():
    ap = argparse.ArgumentParser(description="自动标定小地图野区营地点位")
    ap.add_argument("--sessions", nargs="*", help="会话名（默认自动挑有 frames 的 1280x576 会话）")
    ap.add_argument("--min-rate", type=float, default=0.4, help="常驻判定：出现率下限（见文件头说明）")
    ap.add_argument("--max-v", type=float, default=98.0,
                    help="周围亮度上限（剔除 UI 叠层）。实测真营地 V<=93.4、头像叠层 V>=103")
    ap.add_argument("--radius", type=float, default=0.022, help="聚类半径（归一化）")
    ap.add_argument("--write", action="store_true", help="写入 configs/jungle_points.json")
    args = ap.parse_args()

    names = args.sessions
    if not names:
        names = [Path(d).name for d in sorted(glob.glob(str(ROOT / "data" / "matches" / "*")))
                 if glob.glob(str(Path(d) / "frames" / "*.png"))]
    frames = []
    for n in names:
        for p in sorted(glob.glob(str(ROOT / "data" / "matches" / n / "frames" / "*.png"))):
            f = cv2.imdecode(np.fromfile(p, np.uint8), cv2.IMREAD_COLOR)
            if f is not None and f.shape[0] == 576:
                frames.append(f)
    if not frames:
        raise SystemExit("没有可用帧（需要 1280x576 会话且 frames/ 非空）")
    print(f"会话 {len(names)} 个 | 帧 {len(frames)}")

    per = collect(frames)
    cl = cluster(per, args.radius)
    nf = len(per)
    rows = sorted(cl, key=lambda c: -len(c[2]))
    print(f"\n候选簇 {len(rows)} 个（出现率 >= {args.min_rate:.0%} 且周围 V < {args.max_v:.0f} 的保留）")
    print(f"{'#':>3} {'x':>6} {'y':>6} {'出现率':>7} {'中位面积':>7} {'周围V':>6}  判定")
    pts = []
    for c in rows:
        rate = len(c[2]) / nf
        if rate < args.min_rate:
            continue
        med = int(np.median(c[3]))
        v = patch_v(frames, c[0], c[1])
        big = med >= 30
        keep = v < args.max_v
        tag = ("大图标" if big else "小图标") if keep else "剔除(UI叠层/边角)"
        print(f"   {c[0]:6.3f} {c[1]:6.3f} {rate:7.1%} {med:7d} {v:6.1f}  {tag}")
        if keep:
            pts.append({"n": [round(c[0], 3), round(c[1], 3)], "kind": "camp",
                        "big": bool(big), "rate": round(rate, 3), "area": med})

    pts.sort(key=lambda p: (p["n"][1], p["n"][0]))
    print(f"\n保留 {len(pts)} 个（boss {sum(1 for p in pts if p['kind']=='boss')} / "
          f"camp {sum(1 for p in pts if p['kind']=='camp')}）")
    for p in pts:
        print(f"   {p['kind']:4s} {p['n']}  rate={p['rate']}")

    if args.write:
        cfg = {
            "version": 1,
            "note": "小地图营地点位（归一化到小地图方形框）。**只表示营地位置，不表示野怪存活**。"
                    "由 scripts/calibrate_jungle_points.py 从真机帧自动聚类得到。",
            "source": {"sessions": names, "frames": len(frames),
                       "min_rate": args.min_rate, "max_v": args.max_v,
                       "radius": args.radius},
            "points": pts,
        }
        OUT.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已写入 {OUT}")
    else:
        print("\n（未写盘；加 --write 保存）")


if __name__ == "__main__":
    main()
