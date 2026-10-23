#!/usr/bin/env python
"""对局内搜索 0xD0 逻辑单元记录数组（批量读，跑得快）。

关键：小段有 6000+ 个，逐个 `m.read` 是 6000 次 adb 往返（~3 分钟）。
`LiveMem.read_spans` 能把多个区间塞进**一次** exec-out，这里按 150 段一批，
总共几十次往返即可扫完 1.3GB 小段。

签名（numpy 向量化）：两条相距 0xD0 的记录都满足
  阵营 +0x08 ∈ [0,3]、类型 +0x0C ∈ [1,60]、x/z 在 ±90000 且非原点、两条坐标不同。

命中后：
  1. 用真值核对（建筑/野怪的 +0x278 出生点 == 实时位置）；
  2. 沿 ±0xD0 扩展出 begin/end 与条数；
  3. 用英雄坐标验证（同 id 偏差 ≤ 500 命中 ≥ 3 才认可）。

用法：
    python reverse/hunt_logic.py                 # 默认只扫 4KB~4MB 的小可写段
    python reverse/hunt_logic.py --max-mb 64     # 放大到 64MB 段
    python reverse/hunt_logic.py --budget 300
"""
from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from wzry.vision.mem_source import MemSource  # noqa: E402

STRIDE = 0xD0
BATCH = 150


def writable_regions(m, pid, lo_size, hi_size):
    raw = m._run(["exec-out", f"cat /proc/{pid}/maps"]).decode("utf-8", "replace")
    out = []
    for line in raw.splitlines():
        p = line.split(None, 5)
        if len(p) < 5:
            continue
        a, b = p[0].split("-")
        a, b = int(a, 16), int(b, 16)
        if "r" not in p[1] or "w" not in p[1]:
            continue
        sz = b - a
        if lo_size <= sz <= hi_size:
            out.append((a, sz))
    return out


def scan_buf(buf: bytes, base: int):
    n4 = len(buf) // 4
    if n4 < 64:
        return []
    b = np.frombuffer(buf[: n4 * 4], dtype=np.uint32)
    bi = b.view(np.int32)
    k = b.size - 60
    camp1, type1 = b[2:2 + k], b[3:3 + k]
    camp2, type2 = b[54:54 + k], b[55:55 + k]
    x1, z1 = bi[5:5 + k], bi[7:7 + k]
    x2, z2 = bi[57:57 + k], bi[59:59 + k]
    cond = ((camp1 <= 3) & (type1 >= 1) & (type1 < 60)
            & (camp2 <= 3) & (type2 >= 1) & (type2 < 60)
            & (x1 > -90000) & (x1 < 90000) & (z1 > -90000) & (z1 < 90000)
            & (x2 > -90000) & (x2 < 90000) & (z2 > -90000) & (z2 < 90000)
            & ~((x1 == 0) & (z1 == 0)) & ~((x2 == 0) & (z2 == 0))
            & ((x1 != x2) | (z1 != z2)))
    return [base + int(i) * 4 for i in np.nonzero(cond)[0]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="127.0.0.1:16384")
    ap.add_argument("--min-kb", type=int, default=4)
    ap.add_argument("--max-mb", type=int, default=4)
    ap.add_argument("--budget", type=int, default=240)
    a = ap.parse_args()

    src = MemSource(serial=a.serial)
    pid = src.connect()
    print("pid =", pid, flush=True)
    m = src.m
    regs = writable_regions(m, pid, a.min_kb * 1024, a.max_mb * 1024 * 1024)
    total = sum(s for _a, s in regs)
    print(f"目标段 {len(regs)} 个，合计 {total/1e6:.0f} MB，预算 {a.budget}s", flush=True)

    t0 = time.time()
    hits, scanned = [], 0
    for i in range(0, len(regs), BATCH):
        if time.time() - t0 > a.budget:
            print("预算到", flush=True)
            break
        batch = regs[i:i + BATCH]
        blobs = m.read_spans(batch)
        if not blobs:
            for start, size in batch:              # 回退：逐段读
                buf = m.read(start, size)
                if buf:
                    scanned += len(buf)
                    hits += scan_buf(buf, start)
            continue
        for start, data in blobs:
            scanned += len(data)
            hits += scan_buf(data, start)
        print(f"  {scanned/1e6:6.0f}MB  {len(hits)} 候选  {time.time()-t0:5.1f}s", flush=True)
        if len(hits) > 4000:                        # 签名太松就先停，改锚点法
            break

    print(f"\n扫描 {scanned/1e6:.0f}MB 用时 {time.time()-t0:.1f}s，候选 {len(hits)}", flush=True)

    # 真值：建筑/野怪 +0x278
    truth = {}
    for u in src.snapshot():
        if u.config_id in (92, 95, 21, 35, 44, 46, 16100, 16101):
            raw = m.read(u.actor, 0x2A0)
            x, _y, z = struct.unpack_from("<3i", raw, 0x278)
            truth[u.config_id] = (x, z)

    shown = 0
    for c in hits[:600]:
        buf = m.read(c, STRIDE)
        if len(buf) < 0x20:
            continue
        rid = struct.unpack_from("<i", buf, 0)[0]
        camp = struct.unpack_from("<i", buf, 8)[0]
        typ = struct.unpack_from("<i", buf, 0x0C)[0]
        x, y, z = struct.unpack_from("<3i", buf, 0x14)
        tag = ""
        for cid, (tx, tz) in truth.items():
            if abs(x - tx) <= 2 and abs(z - tz) <= 2:
                tag = f"  ★ == cid={cid} 出生点"
        if truth and not tag:
            continue
        # 向左扩
        lo = c
        while True:
            nb = m.read(lo - STRIDE, STRIDE)
            if len(nb) < 0x20:
                break
            t2 = struct.unpack_from("<i", nb, 0x0C)[0]
            cp2 = struct.unpack_from("<i", nb, 8)[0]
            if not (0 < t2 < 60 and cp2 <= 3):
                break
            lo -= STRIDE
            if c - lo > STRIDE * 400:
                break
        hi = c + STRIDE
        while True:
            nb = m.read(hi, STRIDE)
            if len(nb) < 0x20:
                break
            t2 = struct.unpack_from("<i", nb, 0x0C)[0]
            cp2 = struct.unpack_from("<i", nb, 8)[0]
            if not (0 < t2 < 60 and cp2 <= 3):
                break
            hi += STRIDE
            if hi - c > STRIDE * 400:
                break
        shown += 1
        print(f"\n候选 base=0x{c:x} id={rid} camp={camp} type={typ} xyz=({x},{y},{z}){tag}")
        print(f"  数组 begin=0x{lo:x} end=0x{hi:x} n={(hi-lo)//STRIDE}")
        if shown >= 6:
            break
    if shown == 0:
        print("没有候选能与真值对上。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
