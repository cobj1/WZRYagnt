#!/usr/bin/env python
"""在全可写堆里找 (begin,end) 指针对：差值必须是 0xD0 的整数倍。

为什么这个判据够硬：两个相邻的 8 字节值，差值恰好落在 [2*0xD0, 800*0xD0] 且能被
208 整除，随机数据的概率约 800/2^64；而真实的 `std::vector` 三件套
`(begin, end, cap)` 里前两个正好满足。所以先纯算术筛，只对幸存者读内存验证。

用法（对局中）：
    python reverse/find_vector_pairs.py                # 预算 300s，扫可写段
    python reverse/find_vector_pairs.py --budget 600
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
CHUNK = 8 * 1024 * 1024


def rec_ok(buf: bytes, off: int) -> bool:
    if off < 0 or len(buf) < off + 0x20:
        return False
    rid = struct.unpack_from("<i", buf, off)[0]
    camp = struct.unpack_from("<i", buf, off + 8)[0]
    typ = struct.unpack_from("<i", buf, off + 0x0C)[0]
    x, y, z = struct.unpack_from("<3i", buf, off + 0x14)
    return (0 < rid < 10_000_000 and camp in (0, 1, 2) and 0 < typ < 60
            and -90000 <= x <= 90000 and -90000 <= z <= 90000
            and -3000 <= y <= 3000 and not (x == 0 and z == 0))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="127.0.0.1:16384")
    ap.add_argument("--budget", type=int, default=300)
    ap.add_argument("--min-mb", type=int, default=0)
    a = ap.parse_args()

    src = MemSource(serial=a.serial)
    pid = src.connect()
    print("pid =", pid, flush=True)
    m = src.m
    raw = m._run(["exec-out", f"cat /proc/{pid}/maps"]).decode("utf-8", "replace")
    regs = []
    for line in raw.splitlines():
        p = line.split(None, 5)
        if len(p) < 5 or "r" not in p[1] or "w" not in p[1]:
            continue
        lo, hi = p[0].split("-")
        lo, hi = int(lo, 16), int(hi, 16)
        if hi - lo >= a.min_mb * 1024 * 1024:
            regs.append((lo, hi - lo))
    total = sum(s for _l, s in regs)
    print(f"可写段 {len(regs)} 个，{total/1e9:.2f} GB，预算 {a.budget}s", flush=True)

    t0 = time.time()
    cands, scanned = [], 0
    for lo, size in regs:
        if time.time() - t0 > a.budget:
            print("预算到", flush=True)
            break
        addr = lo
        while addr < lo + size:
            if time.time() - t0 > a.budget:
                break
            n = min(CHUNK, lo + size - addr)
            buf = m.read(addr, n)
            if not buf:
                addr += n
                continue
            scanned += len(buf)
            if len(buf) >= 16:
                u = np.frombuffer(buf[: len(buf) // 8 * 8], dtype=np.uint64)
                x = u[:-1]
                d = u[1:] - x
                mask = ((d >= 2 * STRIDE) & (d <= 800 * STRIDE)
                        & (d % STRIDE == 0) & (x > 0x100000)
                        & (x < 0x8000_0000_0000))
                for i in np.nonzero(mask)[0]:
                    cands.append((addr + int(i) * 8,
                                  int(x[i]), int(x[i]) + int(d[i])))
            addr += n

    print(f"扫描 {scanned/1e9:.2f}GB / {time.time()-t0:.0f}s，算术候选 {len(cands)}",
          flush=True)

    good = []
    for holder, begin, end in cands[:400]:
        buf = m.read(begin, min(end - begin, STRIDE * 6))
        if len(buf) < 0x20:
            continue
        n = (end - begin) // STRIDE
        ok = sum(1 for k in range(min(n, 4)) if rec_ok(buf, k * STRIDE))
        if ok >= min(n, 4):
            good.append((holder, begin, end, n))
    print(f"通过内容校验的数组: {len(good)}", flush=True)
    for holder, begin, end, n in good[:8]:
        print(f"  holder=0x{holder:x} begin=0x{begin:x} n={n}")
        buf = m.read(begin, min(n, 6) * STRIDE)
        for k in range(min(n, 6)):
            off = k * STRIDE
            rid = struct.unpack_from("<i", buf, off)[0]
            camp = struct.unpack_from("<i", buf, off + 8)[0]
            typ = struct.unpack_from("<i", buf, off + 0x0C)[0]
            x, y, z = struct.unpack_from("<3i", buf, off + 0x14)
            print(f"      [{k}] id={rid:<8} camp={camp} type={typ:<3} xyz=({x},{y},{z})")
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
