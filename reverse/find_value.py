# -*- coding: utf-8 -*-
"""按值/字符串扫描内存（金币/时间的已知值定位）。

用法：
  python find_value.py 1436            # u32 + ASCII + UTF-16
  python find_value.py 1197 --scan-ms 8
"""
import argparse
import struct
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from livemem import LiveMem


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("value", type=int)
    ap.add_argument("--serial", default="127.0.0.1:16384")
    a = ap.parse_args()
    v = a.value
    pats = [("u32", struct.pack("<I", v)),
            ("ascii", str(v).encode()),
            ("utf16", str(v).encode("utf-16le"))]
    m = LiveMem(serial=a.serial)
    print("pid", m.find_pid(), flush=True)
    m.refresh_maps()
    regions = []
    for st, en, _off, p in m.maps:
        if not p:
            continue
        if (p in ("[anon:scudo:primary]", "[anon:scudo:secondary]")
                or p.startswith("[anon:Mem_") or p.startswith("[anon:dalvik-")):
            if en - st < 300 * 1024 * 1024:
                regions.append((st, en, p))
    t0 = time.time()
    hits = []
    for st, en, name in regions:
        addr = st
        while addr < en:
            n = min(0x800000, en - addr)
            buf = m.read(addr, n)
            if len(buf) < 16:
                break
            for kind, pat in pats:
                pos = 0
                while True:
                    i = buf.find(pat, pos)
                    if i < 0:
                        break
                    hits.append((addr + i, kind, name))
                    pos = i + 1
                    if len(hits) > 5000:
                        break
                if len(hits) > 5000:
                    break
            addr += n
        print(name, "done", hex(addr), "hits", len(hits),
              "t=%.0fs" % (time.time() - t0), flush=True)
        if len(hits) > 5000:
            break
    print("total hits", len(hits), flush=True)
    by_kind = {}
    for addr, kind, name in hits:
        by_kind.setdefault(kind, []).append((addr, name))
    for kind, lst in by_kind.items():
        print(f"--- {kind}: {len(lst)}")
        for addr, name in lst[:40]:
            ctx = m.read(addr - 8, 0x30)
            print(f"  0x{addr:x} {name} ctx={ctx.hex()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
