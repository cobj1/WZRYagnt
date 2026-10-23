# -*- coding: utf-8 -*-
"""A 方案：扫 player 对象。

静态依据（GetGold getter 0x34704b8）：
    handle = *(host + 0x108)
    id     =  *(host + 0x110)
    player = *(handle + 0x1d0)

stage1: 在堆里找 handle 对象 h：
    *(u64*)h        in libGameCore（vtable）
    *(u32*)(h+0xc)  1..1e6（ref id）
    *(u64*)(h+0x1d0) 已映射（player）
stage2: 找引用 h 的 host：
    qword == h，host = qaddr - 0x108，且 *(u32*)(host+0x110) == id
stage3: 用金币 23394（或 --gold）过滤 host/player。

用法：
  python find_player_obj.py --gold 23394 --stage1-only
"""
import argparse
import struct
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from livemem import LiveMem

GAME_LO, GAME_HI = 0x400036A00000, 0x40003BC6F000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", type=int, default=23394)
    ap.add_argument("--max-gold", type=int, default=None)
    ap.add_argument("--stage1-only", action="store_true")
    ap.add_argument("--vt", type=lambda x: int(x, 0), default=None)
    ap.add_argument("--serial", default="127.0.0.1:16384")
    a = ap.parse_args()
    gmax = a.max_gold if a.max_gold is not None else a.gold + 200

    m = LiveMem(serial=a.serial)
    print("pid", m.find_pid(), flush=True)
    m.refresh_maps()

    regions = []
    for st, en, _off, p in m.maps:
        if p in ("[anon:scudo:primary]", "[anon:scudo:secondary]"):
            regions.append((st, en, p))
    print("regions", len(regions), "MB",
          sum(en - st for st, en, _ in regions) / 1e6, flush=True)

    # 映射区间表（用于判断指针是否已映射）
    rngs = np.array(sorted((st, en) for st, en, _off, _p in m.maps), dtype=np.uint64)
    starts, ends = rngs[:, 0], rngs[:, 1]

    def mapped(vals):
        idx = np.searchsorted(starts, vals, side="right") - 1
        ok = idx >= 0
        ok[ok] &= vals[ok] < ends[idx[ok]]
        return ok

    handles = []       # (addr, id)
    CH = 0x800000
    t0 = time.time()
    for st, en, name in regions:
        addr = st
        while addr < en:
            n = min(CH, en - addr)
            buf = m.read(addr, n)
            if len(buf) < 0x2000:
                break
            q = np.frombuffer(buf, dtype=np.uint64)
            u = np.frombuffer(buf, dtype=np.uint32)
            lim = len(q) - (0x1D8 // 8)
            if lim <= 0:
                break
            idx = np.arange(0, lim)
            vt = q[idx]
            ids = u[idx * 2 + 3]
            pl = q[idx + (0x1D0 // 8)]
            if a.vt is not None:
                mask = ((vt == a.vt) & (ids > 0) & (ids < 1000000)
                        & (pl > 0x1000000000))
            else:
                mask = ((vt >= GAME_LO) & (vt < GAME_HI)
                        & (ids > 0) & (ids < 1000000)
                        & (pl > 0x1000000000))
            if mask.any():
                pv = pl[mask]
                good = mapped(pv)
                sel = idx[mask][good]
                pid = ids[mask][good]
                for i, hid in zip(sel, pid):
                    handles.append((addr + int(i) * 8, int(hid)))
            addr += n
        print(name, "done", hex(addr), "handles", len(handles),
              "t=%.0fs" % (time.time() - t0), flush=True)
    print("stage1 handles", len(handles), flush=True)
    if a.stage1_only or not handles:
        for h, i in handles[:10]:
            print("  handle", hex(h), "id", i)
        return 0

    # stage2: 在一遍扫描里找 qword == handle
    hs = np.array(sorted(set(h for h, _ in handles)), dtype=np.uint64)
    hid_map = {h: i for h, i in handles}
    hosts = []
    for st, en, name in regions:
        addr = st
        while addr < en:
            n = min(CH, en - addr)
            buf = m.read(addr, n)
            if len(buf) < 0x120:
                break
            q = np.frombuffer(buf, dtype=np.uint64)
            mask = np.isin(q, hs)
            for i in np.nonzero(mask)[0]:
                h = int(q[i])
                host = addr + int(i) * 8 - 0x108
                if host < st:
                    continue
                hb = m.read(host + 0x110, 4)
                if len(hb) < 4:
                    continue
                if int.from_bytes(hb, "little") == hid_map.get(h, -1):
                    hosts.append((host, h))
            addr += n
        print(name, "stage2 done", "hosts", len(hosts),
              "t=%.0fs" % (time.time() - t0), flush=True)
    print("hosts", len(hosts), flush=True)

    # stage3: host/player 里找金币
    for host, h in hosts[:200]:
        player = int.from_bytes(m.read(h + 0x1D0, 8), "little")
        if not player:
            continue
        for label, base in (("host", host), ("player", player)):
            b = m.read(base, 0x2000)
            for off in range(0, len(b) - 4, 4):
                v = struct.unpack_from("<i", b, off)[0]
                if a.gold <= v <= gmax:
                    print(f"HIT gold={v} {label}=0x{base:x}+0x{off:x} "
                          f"host=0x{host:x} handle=0x{h:x}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
