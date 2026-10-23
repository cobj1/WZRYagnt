# -*- coding: utf-8 -*-
"""在堆里找“英雄ID + 有符号坐标”记录。

背景：.bss 里有一张静态表（0x58 步长），蓝方坐标为负、红方为正，
      例如蓝 106=(-56379,-53367)、红 163=(55757,57652)。
      本脚本找同构的**堆内**记录（可能是实时位置表）。

用法：python find_live_pos.py 106 163
"""
import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from livemem import LiveMem  # noqa: E402


def main():
    ids = [int(x) for x in sys.argv[1:]] or [106]
    m = LiveMem()
    m.find_pid()
    m.refresh_maps()
    regions = []
    for st, en, _off, p in m.maps:
        if not p:
            continue
        if (p in ("[anon:scudo:primary]", "[anon:scudo:secondary]")
                or p.startswith("[anon:Mem_")):
            if en - st < 300 * 1024 * 1024:
                regions.append((st, en, p))
    print("ids", ids, "regions", len(regions),
          "MB", sum(en - st for st, en, _ in regions) / 1e6)
    idset = np.array(ids, dtype=np.uint32)
    CH = 0x800000
    hits = 0
    for st, en, name in regions:
        addr = st
        while addr < en:
            n = min(CH, en - addr)
            buf = m.read(addr, n)
            if len(buf) < 0x100:
                break
            u = np.frombuffer(buf, dtype=np.uint32)
            lim = len(u) - 0x20
            idx = np.arange(0, lim)
            # 布局 A：ID@+8, x@+0x10, y@+0x14, z@+0x18
            x = u[idx + 4].astype(np.int64)
            y = u[idx + 5].astype(np.int64)
            z = u[idx + 6].astype(np.int64)
            xs = np.where(x > 0x7fffffff, x - 0x100000000, x)
            zs = np.where(z > 0x7fffffff, z - 0x100000000, z)
            mask = (np.isin(u[idx + 2], idset)
                    & (np.abs(xs) < 65000) & (np.abs(zs) < 65000)
                    & (y < 4) & ((np.abs(xs) + np.abs(zs)) > 1000))
            # 布局 B：ID@+0x34, x@+0x3c, y@+0x40, z@+0x44
            x2 = u[idx + 15].astype(np.int64)
            z2 = u[idx + 17].astype(np.int64)
            y2 = u[idx + 16].astype(np.int64)
            x2s = np.where(x2 > 0x7fffffff, x2 - 0x100000000, x2)
            z2s = np.where(z2 > 0x7fffffff, z2 - 0x100000000, z2)
            mask2 = (np.isin(u[idx + 13], idset)
                     & (np.abs(x2s) < 65000) & (np.abs(z2s) < 65000)
                     & (y2 < 4) & ((np.abs(x2s) + np.abs(z2s)) > 1000))
            for i in idx[mask][:20]:
                p = addr + int(i) * 4
                print(f"HIT-A 0x{p:x} {name} id={struct.unpack_from('<i',m.read(p,4+8),8)[0]}"
                      f" pos=({xs[i]},{y[i]},{zs[i]})")
                hits += 1
            for i in idx[mask2][:20]:
                p = addr + int(i) * 4
                print(f"HIT-B 0x{p:x} {name} id={struct.unpack_from('<i',m.read(p,0x38),0x34)[0]}"
                      f" pos=({x2s[i]},{y2[i]},{z2s[i]})")
                hits += 1
            addr += n
    print("done hits", hits)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
