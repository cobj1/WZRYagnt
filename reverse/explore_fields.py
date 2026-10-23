# -*- coding: utf-8 -*-
"""探索：对某个单位的全部组件做“时间差分”，列出会变化的字段。

用途：找技能 CD、MP、等级、金币等未知字段。
  - 过滤单调递增的计时器；
  - 过滤指针；
  - 只保留有符号 int 在 ±200000 内、且有至少 4 次变化的字段。

用法：
  python explore_fields.py --id 106 --samples 50 --interval 0.2
"""
import argparse
import struct
import time
from pathlib import Path

from livemem import LiveMem

BATTLE_GLOBAL = 0x4FF4520
ACTOR_ATTR_OFF = 0x1D0
ACTOR_POS_OFF = 0x230
SLOT_BASE = 0x110
SLOT_STRIDE = 0x18


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, required=True)
    ap.add_argument("--samples", type=int, default=50)
    ap.add_argument("--interval", type=float, default=0.2)
    ap.add_argument("--serial", default="127.0.0.1:16384")
    a = ap.parse_args()

    m = LiveMem(serial=a.serial)
    print("pid", m.find_pid())
    m.refresh_maps()

    def region(x):
        for st, en, _off, p in m.maps:
            if st <= x < en:
                return (st, en, p)
        return None

    def u64(x):
        if not region(x):
            return None
        b = m.read(x, 8)
        return int.from_bytes(b, "little") if len(b) == 8 else None

    rt = m.resolve("libGameCore.so", BATTLE_GLOBAL)
    A = u64(rt)
    B = u64(A) if A else None
    T = u64(B) if B else None
    mgr = u64(T + 0x388) if T else None
    arr = u64(mgr + 0x30)
    cnt = int.from_bytes(m.read(mgr + 0x4C, 4), "little")
    print("mgr", hex(mgr), "arr", hex(arr), "cnt", cnt)

    actor = None
    for i in range(cnt):
        aa = struct.unpack_from("<Q", m.read(arr + i * 0x18, 8))[0]
        if not region(aa):
            continue
        h = m.read(aa, 0x40)
        cid = struct.unpack_from("<i", h, 0x28)[0]
        typ = struct.unpack_from("<i", h, 0x30)[0]
        if cid == a.id and typ == 0:
            actor = aa
            break
    if not actor:
        print("actor not found")
        return 1
    print("actor", hex(actor))

    targets = [("actor", actor, 0x200),
               ("unit+0x2a0", actor + 0x2A0, 0x200)]
    for i in range(15):
        p = u64(actor + SLOT_BASE + i * SLOT_STRIDE)
        if p and region(p):
            targets.append((f"slot{i}", p, 0x200))
    print("targets", [(n, hex(p)) for n, p, _s in targets])

    def spans_for(items):
        entries = []
        for addr, size in items:
            r = region(addr)
            if not r:
                return None
            entries.append((addr, addr + size, r))
        entries.sort()
        cl = []
        for lo, hi, r in entries:
            if cl and cl[-1][2][:2] == r[:2] and lo - cl[-1][1] <= 0x40000:
                cl[-1][1] = max(cl[-1][1], hi)
            else:
                cl.append([lo, hi, r])
        spans = []
        for lo, hi, r in cl:
            start = lo & ~0xFFF
            end = min((hi + 0xFFF) & ~0xFFF, r[1])
            if end > start:
                spans.append((start, end - start))
        return spans

    def pf(spans, addr, off, fmt):
        size = struct.calcsize(fmt)
        x = addr + off
        for st, data in spans:
            if st <= x and x + size <= st + len(data):
                return struct.unpack_from(fmt, data, x - st)
        return None

    items = [(p, size) for _n, p, size in targets]
    spans = spans_for(items)
    if not spans:
        print("span build failed")
        return 1
    print("spans", [(hex(s), hex(n)) for s, n in spans])

    series = {(ti, off): [] for ti in range(len(targets)) for off in range(0, 0x200, 4)}
    for k in range(a.samples):
        data = m.read_spans(spans)
        if not data:
            print("read failed at", k)
            break
        for ti, (_n, p, size) in enumerate(targets):
            for off in range(0, 0x200, 4):
                v = pf(data, p, off, "<i")
                if v is not None:
                    series[(ti, off)].append(v[0])
        time.sleep(a.interval)
    print("samples", a.samples)

    out = []
    for (ti, off), vals in series.items():
        if len(vals) < 5:
            continue
        if len(set(vals)) < 4:
            continue
        if all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1)):
            continue          # 单调递增 -> 计时器
        if all(v > 0x10000000 or v < -0x10000000 for v in vals):
            continue          # 指针
        if not all(-200000 <= v <= 200000 for v in vals):
            continue
        out.append((ti, off, vals, max(vals) - min(vals)))
    out.sort(key=lambda x: -x[3])
    print("--- candidates", len(out), "---")
    for ti, off, vals, rng in out[:120]:
        name, p, _s = targets[ti]
        vt = u64(p)
        head = struct.unpack_from("<Q", m.read(p, 8))[0]
        print(f"{name:<10} vt=0x{vt:x} +0x{off:03x} range={rng:<7} "
              f"first={vals[:6]} last={vals[-4:]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
