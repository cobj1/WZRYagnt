# -*- coding: utf-8 -*-
"""探 libGameCore.so .data 指向的 guest 匿名段对象，识别带虚表的 C++ 对象。

上一版按"guest 堆区"过滤得到 0 个候选，是因为 livemem 丢掉了无路径名的匿名段。
修正后 .data 里有 2568 个槽位指向 0x40003bc6f000..0x40003ca00000（紧随
libGameCore 最后一个段，即 .bss / 运行时全局区）。本脚本回读这些对象头：
若首 8 字节指向 libGameCore 任一映射段，则该对象带虚表（C++ 多态对象）。

用法：python probe_bss_objects.py [--cap 300]
"""
import argparse
import struct
import sys
from collections import Counter, defaultdict

from livemem import LiveMem

SO = "libGameCore.so"
DATA_VADDR = 0x4FF8890
DATA_SIZE = 0x276308
GUEST_MIN = 0x4000000000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=300)
    a = ap.parse_args()

    m = LiveMem()
    if not m.find_pid():
        print("未找到游戏进程")
        return 1
    m.refresh_maps()

    lib_ranges = [(s, e) for s, e, _f, _p in m.segments(SO)]

    def in_lib(v):
        return any(s <= v < e for s, e in lib_ranges)

    # guest 匿名段（无路径名 或 [anon:...]）
    anon = [(s, e) for s, e, _f, p in m.maps
            if s >= GUEST_MIN and (p.strip() == "" or p.startswith("[anon"))]

    def in_anon(v):
        return any(s <= v < e for s, e in anon)

    blob = m.read(m.resolve(SO, DATA_VADDR), DATA_SIZE)
    slots = defaultdict(list)
    for i in range(0, len(blob) - 8, 8):
        v, = struct.unpack_from("<Q", blob, i)
        if v >= GUEST_MIN and in_anon(v):
            slots[v].append(DATA_VADDR + i)
    print(f"pid={m.pid}  anon-segment targets: {len(slots)} unique, "
          f"{sum(len(v) for v in slots.values())} slots")

    vt = Counter()
    per_vt = defaultdict(list)
    checked = 0
    for p, sl in list(slots.items())[: a.cap]:
        b = m.read(p, 8)
        checked += 1
        if len(b) != 8:
            continue
        q, = struct.unpack("<Q", b)
        if in_lib(q):
            vt[q] += 1
            per_vt[q].append((p, sl[0]))

    print(f"checked={checked}  with-vtable={sum(vt.values())}")
    print("--- vtable groups (same vtable = same class), top 15 ---")
    for q, n in vt.most_common(15):
        fo = next((fo + (q - s) for s, e, fo, _p in m.segments(SO) if s <= q < e), None)
        sv = m.elf(SO).off_to_vaddr(fo) if fo is not None else None
        ex = per_vt[q][:3]
        print(f"  vtable 0x{q:x} static={'0x%x' % sv if sv else '?'}  n={n}  "
              f"objs={[hex(x[0]) for x in ex]}")
    print("--- first 15 objects ---")
    for q, lst in list(per_vt.items())[:15]:
        for p, slot in lst[:1]:
            print(f"  obj 0x{p:x}  vtable 0x{q:x}  slot_static=0x{slot:x}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
