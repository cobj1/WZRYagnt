# -*- coding: utf-8 -*-
"""在 .bss + 战斗对象指针里找 CActorManager（真实对局用）。

判据：
  p+0x30  = actor 数组基址（元素 0x18，首 qword = actor*）
  p+0x4c / p+0x6c = 两套计数，p+0x70 = 选择子（0 -> 0x4c）
  actor->vtable[0x98] 必须落在 libGameCore .text
真实对局里前若干个 actor 的 vtable 应有多种（英雄/小兵/塔/野怪）。

用法：python find_actor_mgr.py
"""
import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from livemem import LiveMem  # noqa: E402

BATTLE_GLOBAL = 0x4FF4520
TEXT_LO, TEXT_HI = 0x400036A00000, 0x40003BC6F000


def main():
    m = LiveMem()
    m.find_pid()
    m.refresh_maps()

    def region(a):
        for st, en, _off, p in m.maps:
            if st <= a < en:
                return (st, en, p)
        return None

    def u64(a):
        if not region(a):
            return None
        b = m.read(a, 8)
        return int.from_bytes(b, "little") if len(b) == 8 else None

    def u32(a):
        if not region(a):
            return None
        b = m.read(a, 4)
        return int.from_bytes(b, "little") if len(b) == 4 else None

    def validate(p, cnt, arr):
        if not arr or not region(arr):
            return None
        n = min(cnt, 24)
        raw = m.read(arr, n * 0x18)
        if len(raw) < n * 0x18:
            return None
        vts, actors = set(), []
        for i in range(n):
            actor = struct.unpack_from("<Q", raw, i * 0x18)[0]
            if not actor or not region(actor):
                continue
            vt = u64(actor)
            if not vt or not region(vt):
                continue
            fn = u64(vt + 0x98)
            if fn and TEXT_LO <= fn < TEXT_HI:
                vts.add(vt)
                actors.append(actor)
        if len(actors) < 5 or len(vts) < 2:
            return None
        return vts, actors

    def report(tag, p, cnt, arr, r):
        vts, actors = r
        print(f"*** {tag} p=0x{p:x} count={cnt} arr=0x{arr:x} "
              f"valid={len(actors)} vtables={[hex(x) for x in sorted(vts)]}")
        for i, a in enumerate(actors[:3]):
            head = m.read(a, 0x100)
            print(f"    actor[{i}]=0x{a:x} head={head.hex()}")

    battle = u64(u64(m.resolve("libGameCore.so", BATTLE_GLOBAL)))
    print("battle", hex(battle) if battle else None)

    # 1) battle 对象里的指针
    if battle:
        raw = m.read(battle, 0x4000)
        for off in range(0, len(raw) - 8, 8):
            v = struct.unpack_from("<Q", raw, off)[0]
            if not v or not region(v):
                continue
            arr = u64(v + 0x30)
            c4c, c6c = u32(v + 0x4C), u32(v + 0x6C)
            sel = u32(v + 0x70)
            cnt = c6c if sel else c4c
            if not cnt or cnt > 3000 or sel > 1:
                continue
            r = validate(v, cnt, arr)
            if r:
                report(f"battle+0x{off:x}", v, cnt, arr, r)

    # 2) .bss 匿名区全扫（numpy 向量化初筛）
    segs = m.segments("libGameCore.so")
    bss = region(max(s[1] for s in segs) + 0x1000)
    if not bss:
        print("no bss region")
        return 1
    st, en, _ = bss
    size = en - st
    print(f"scan bss {st:x}-{en:x} ({size/1e6:.1f}MB)")
    buf = m.read(st, size)
    if len(buf) < size:
        print("short read", len(buf), size)
        size = len(buf)
    a64 = np.frombuffer(buf, dtype=np.uint64)
    u32v = np.frombuffer(buf, dtype=np.uint32)
    n = size // 8
    idx = np.arange(0, n - 12)
    arrs = a64[idx + 6]
    c4c = u32v[idx * 2 + 19]
    c6c = u32v[idx * 2 + 27]
    sel = u32v[idx * 2 + 28]
    cnt = np.where(sel == 0, c4c, c6c)
    mask = (cnt >= 5) & (cnt <= 3000) & (sel <= 1) & (arrs > 0x1000000000)
    cand = idx[mask]
    print("vector candidates:", len(cand))
    found = 0
    for i in cand[:2000]:
        p = st + int(i) * 8
        cnt_i = int(cnt[i])
        arr_i = int(arrs[i])
        if not region(arr_i):
            continue
        r = validate(p, cnt_i, arr_i)
        if r:
            report("bss", p, cnt_i, arr_i, r)
            found += 1
            if found >= 5:
                break
    print("done, found", found)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
