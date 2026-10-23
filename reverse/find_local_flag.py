# -*- coding: utf-8 -*-
"""在英雄记录里找「是不是本地玩家」的标志位 —— 纯内存，不用视觉。

判据（三条同时满足）：
  1. 每一帧里，10 个英雄中**恰好 1 个**该字段非零；
  2. 这个非零的槽位**跨帧不变**（同一个人始终是玩家）；
  3. 值稳定（通常是 1，允许 0/1 抖动但不允许连续量）。

为什么相信这条判据：本地玩家在一局里唯一且固定，任何"只有一个人为真"的布尔量
都只能来自身份标记。这比"用视觉血条比例猜"硬得多。

用法：
    venv/Scripts/python.exe -X utf8 reverse/find_local_flag.py temp/probe_target/t3.npz
"""
from __future__ import annotations

import sys

import numpy as np


def scan(rec: np.ndarray, cids, slots, min_keep: float = 0.9):
    """rec: (T, 10, W) uint32。返回 [(off, width, slot_idx, nonzero_ratio, values)]"""
    T, N, W = rec.shape
    out = []
    for width, dtype, step in ((1, np.uint8, 1), (4, np.uint32, 4)):
        view = rec.view(dtype)                      # (T,10,W*4/width)
        cols = view.shape[2]
        for c in range(0, cols - step + 1):
            col = view[:, :, c]
            # 每帧恰好 1 个非零
            per_frame = (col != 0).sum(axis=1)
            ok_frames = per_frame == 1
            if ok_frames.mean() < min_keep:
                continue
            # 非零的槽位必须始终是同一个
            idx = np.argmax(col != 0, axis=1)
            who = idx[ok_frames]
            if len(set(who.tolist())) != 1:
                continue
            si = int(who[0])
            vals = np.unique(col[ok_frames, si])
            if len(vals) > 2:                       # 布尔量最多两种取值
                continue
            out.append((c * step, width, si, float(ok_frames.mean()),
                        [int(v) for v in vals]))
    out.sort(key=lambda r: (-r[3], r[0]))
    return out


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "temp/probe_target/t3.npz"
    z = np.load(path)
    rec, cids, slots = z["rec"], z["cids"], z["slots"]
    print(f"{path}: {rec.shape[0]} 帧, cids={[int(x) for x in cids]}")
    hits = scan(rec, cids, slots)
    print(f"\n「每帧恰好 1 个槽非零 + 槽位固定」的偏移 {len(hits)} 个：\n")
    for off, width, si, ratio, vals in hits[:20]:
        print(f" +0x{off:04x} (u{width * 8:<2})  slot{si}=cid{int(cids[si])}  "
              f"覆盖 {ratio:.0%} 帧  取值={vals}")
    if not hits:
        print("（没有：本地玩家标记可能不在记录里，或在 actor/组件上）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
