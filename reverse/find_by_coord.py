#!/usr/bin/env python
"""按坐标锚点找 0xD0 记录：给出 (x,z)，在可写内存里搜 x 与 z 相隔 8 字节的位置。

记录布局（静态还原）：`+0x14 = x`、`+0x18 = y`、`+0x1C = z`，所以 x 与 z 相差 8 字节。
用建筑/防御塔坐标做锚最稳（它们不动），命中后 `base = pos - 0x14`，
再检查 id/camp/type 是否合法，最后沿 ±0xD0 扩展出数组范围。

用法：
    python reverse/find_by_coord.py -44700 -44700      # 蓝方中路塔（cid 92 的出生点）
    python reverse/find_by_coord.py 44700 44700
    python reverse/find_by_coord.py --budget 300
"""
from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wzry.vision.mem_source import MemSource  # noqa: E402

STRIDE = 0xD0
CHUNK = 8 * 1024 * 1024


def rec_at(m, base: int):
    buf = m.read(base, STRIDE)
    if len(buf) < 0x20:
        return None
    rid = struct.unpack_from("<i", buf, 0)[0]
    camp = struct.unpack_from("<i", buf, 8)[0]
    typ = struct.unpack_from("<i", buf, 0x0C)[0]
    x, y, z = struct.unpack_from("<3i", buf, 0x14)
    if not (0 < rid < 10_000_000 and 0 <= camp <= 3 and 0 < typ < 60):
        return None
    if not (-90000 <= x <= 90000 and -90000 <= z <= 90000):
        return None
    return {"base": base, "id": rid, "camp": camp, "type": typ, "x": x, "y": y, "z": z}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("x", type=int)
    ap.add_argument("z", type=int)
    ap.add_argument("--serial", default="127.0.0.1:16384")
    ap.add_argument("--budget", type=int, default=300)
    a = ap.parse_args()

    src = MemSource(serial=a.serial)
    pid = src.connect()
    print(f"pid={pid} 锚点 x={a.x} z={a.z}", flush=True)
    m = src.m
    raw = m._run(["exec-out", f"cat /proc/{pid}/maps"]).decode("utf-8", "replace")
    regs = []
    for line in raw.splitlines():
        p = line.split(None, 5)
        if len(p) < 5 or "r" not in p[1] or "w" not in p[1]:
            continue
        lo, hi = p[0].split("-")
        regs.append((int(lo, 16), int(hi, 16) - int(lo, 16)))
    print(f"可写段 {len(regs)}，{sum(s for _l, s in regs)/1e9:.2f} GB", flush=True)

    px = struct.pack("<i", a.x)
    t0 = time.time()
    hits, scanned, done = [], 0, 0
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
            i = buf.find(px)
            while i >= 0:
                if i + 12 <= len(buf):
                    z = struct.unpack_from("<i", buf, i + 8)[0]
                    if z == a.z:
                        hits.append(addr + i)
                i = buf.find(px, i + 4)
            addr += n
        done += 1
        if hits:
            break

    print(f"扫描 {scanned/1e9:.2f}GB/{len(regs)}段 用时 {time.time()-t0:.0f}s，"
          f"x 命中 {len(hits)}", flush=True)

    good = []
    for pos in hits[:200]:
        r = rec_at(m, pos - 0x14)
        if r and r["x"] == a.x and r["z"] == a.z:
            good.append(r)
    print(f"能解释成记录基址的: {len(good)}", flush=True)
    for r in good[:6]:
        print(f"  base=0x{r['base']:x} id={r['id']} camp={r['camp']} "
              f"type={r['type']} xyz=({r['x']},{r['y']},{r['z']})")
        # 沿 ±0xD0 扩展
        lo = r["base"]
        while rec_at(m, lo - STRIDE):
            lo -= STRIDE
        hi = r["base"] + STRIDE
        while rec_at(m, hi):
            hi += STRIDE
        print(f"    数组 begin=0x{lo:x} end=0x{hi:x} n={(hi-lo)//STRIDE}")
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
