# -*- coding: utf-8 -*-
"""扫 libGameCore.so 的 .data，找出带虚表的 C++ 对象（Route B）。

思路
----
C++ 对象首字段是虚表指针，指向本模块的 .text/.rodata。而 .data 里存着
指向这些对象的全局指针。因此：
  1) 读 live 的 libGameCore .data；
  2) 挑出「落在 guest 匿名堆区」的 u64 —— 候选对象指针 P；
  3) 回读 *(u64*)P，若落在本模块的任一映射段内 -> 该对象有虚表；
  4) 按虚表地址聚类（同一虚表 = 同一类），并把虚表地址换算回静态 vaddr。

用法：python scan_gc_objects.py [--cap 200]
"""
import argparse
import struct
import sys
from collections import Counter

from livemem import LiveMem

SO = "libGameCore.so"
DATA_VADDR = 0x4FF8890       # .data 段虚拟地址
DATA_SIZE = 0x276308         # .data 段大小
GUEST_MIN = 0x4000000000     # guest 地址下界


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=200)
    a = ap.parse_args()

    m = LiveMem()
    if not m.find_pid():
        print("未找到游戏进程")
        return 1
    m.refresh_maps()

    segs = m.segments(SO)
    print(f"pid={m.pid}  {SO} segments={len(segs)}")
    lib_ranges = [(s, e) for s, e, _f, _p in segs]

    def in_lib(v):
        return any(s <= v < e for s, e in lib_ranges)

    # guest 匿名堆区（有路径的不算；[anon:*] / 空路径算）
    heap_ranges = [(s, e) for s, e, _f, p in m.maps
                   if s >= GUEST_MIN and (p.strip() == "" or "[anon" in p or p.startswith("["))]

    def in_heap(v):
        return any(s <= v < e for s, e in heap_ranges)

    data_rt = m.resolve(SO, DATA_VADDR)
    blob = m.read(data_rt, DATA_SIZE)
    print(f".data live=0x{data_rt:x} len={len(blob)}  heap_ranges={len(heap_ranges)}")

    cand = {}
    for i in range(0, len(blob) - 8, 8):
        v, = struct.unpack_from("<Q", blob, i)
        if v >= GUEST_MIN and in_heap(v):
            cand.setdefault(v, []).append(DATA_VADDR + i)
    print(f"heap-pointing slots: {sum(len(v) for v in cand.values())}  unique objects: {len(cand)}")

    items = list(cand.items())[: a.cap]
    vt = Counter()
    rows = []
    for p, slots in items:
        b = m.read(p, 8)
        if len(b) != 8:
            continue
        q, = struct.unpack("<Q", b)
        if in_lib(q):
            vt[q] += 1
            rows.append((p, q, slots[:3]))

    print(f"objects with vtable: {len(rows)} / {len(items)}")
    print("--- vtable groups (same vtable = same class) ---")
    for q, n in vt.most_common(12):
        sv = m.elf(SO).off_to_vaddr(fileoff_of(q, m))
        print(f"  vtable 0x{q:x}  static={'0x%x' % sv if sv else '?'}  count={n}")
    print("--- sample objects ---")
    for p, q, slots in rows[:12]:
        print(f"  obj 0x{p:x}  vtable 0x{q:x}  data_slots={[hex(s) for s in slots]}")
    return 0


def fileoff_of(live, m):
    for s, e, fo, _p in m.segments(SO):
        if s <= live < e:
            return fo + (live - s)
    return None


if __name__ == "__main__":
    sys.exit(main())
