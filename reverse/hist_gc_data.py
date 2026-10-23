# -*- coding: utf-8 -*-
"""统计 libGameCore.so .data 里所有 u64 的落点分布（不做任何假设）。

用途：Route B 第一诊断步。上一版按"guest 匿名堆区"过滤得到 0 个候选，
      说明对"对象分配在哪"的假设不成立。这里直接分类所有指针落点，
      看清 .data 到底引用了哪些内存区域。
"""
import struct
import sys
from collections import Counter

from livemem import LiveMem

SO = "libGameCore.so"
DATA_VADDR = 0x4FF8890
DATA_SIZE = 0x276308


def main() -> int:
    m = LiveMem()
    if not m.find_pid():
        print("未找到游戏进程")
        return 1
    m.refresh_maps()
    data_rt = m.resolve(SO, DATA_VADDR)
    blob = m.read(data_rt, DATA_SIZE)
    print(f"pid={m.pid}  .data live=0x{data_rt:x}  len={len(blob)}")

    buckets = Counter()
    samples = {}
    unmapped = 0
    for i in range(0, len(blob) - 8, 8):
        v, = struct.unpack_from("<Q", blob, i)
        if v < 0x10000:
            continue
        hit = None
        for s, e, _f, p in m.maps:
            if s <= v < e:
                name = p.strip() or "(anon)"
                name = name.split("/")[-1][:34]
                hit = f"{name}"
                samples.setdefault(hit, (hex(v), hex(s), hex(e)))
                break
        if hit is None:
            unmapped += 1
            samples.setdefault("(unmapped)", (hex(v), "-", "-"))
        else:
            buckets[hit] += 1

    print(f"total non-trivial u64 slots scanned; unmapped={unmapped}")
    print("--- top 25 pointer targets (by mapped file/region) ---")
    for name, n in buckets.most_common(25):
        s = samples.get(name, ("", "", ""))
        print(f"  {n:>6}  {name:<36} e.g. {s[0]} in {s[1]}..{s[2]}")
    print(f"  {unmapped:>6}  (unmapped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
