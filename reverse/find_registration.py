# -*- coding: utf-8 -*-
"""运行时定位 IL2CPP 注册表：绕过被剥离的静态符号。

背景：Il2CppDumper / Il2CppInspectorRedux 在本包上均失败，原因都是
      「No matches via data/code heuristics」——生成代码里的
      g_CodeRegistration / g_MetadataRegistration 被剥离。
      但 .text 未加密（多深度采样均为合法 ARM64），说明只是注册表被隐藏。

思路：il2cpp_init 在运行时会把注册表写进可写段（.data/.bss）。
      用 livemem 读这些段的活内存，按结构特征扫描：
        Il2CppMetadataRegistration(v29) ≈
          {(int32 count, pad, ptr)} × 7 + {(size_t count, ptr)}
      命中后把运行时地址换算回静态 vaddr，即可喂给 Il2CppDumper 手动模式。

用法：python find_registration.py [--so libil2cpp.so]
"""
import argparse
import struct
import sys

from livemem import LiveMem


def mapped_ranges(maps):
    return [(s, e) for s, e, _f, _p in maps]


def in_ranges(v, rs):
    return any(s <= v < e for s, e in rs)


def scan_metadata_reg(blob, live_base, maps, so_name, m):
    """在 blob 里找 {(u32 cnt, u32 pad, u64 ptr)} 连续 ≥6 组的结构。"""
    rs = mapped_ranges(maps)
    hits = []
    n = len(blob) - 8 * 12
    for off in range(0, n, 8):
        cnt, pad, ptr = struct.unpack_from("<IIQ", blob, off)
        if pad != 0 or not (200 <= cnt <= 2_000_000):
            continue
        if ptr and not in_ranges(ptr, rs):
            continue
        pairs = 1
        while off + pairs * 16 + 16 <= len(blob):
            c2, p2, q2 = struct.unpack_from("<IIQ", blob, off + pairs * 16)
            if p2 != 0 or not (1 <= c2 <= 2_000_000):
                break
            if q2 and not in_ranges(q2, rs):
                break
            pairs += 1
        if pairs >= 6:
            live = live_base + off
            sv = m.elf(so_name).off_to_vaddr(live_to_fileoff(live, m, so_name))
            hits.append((pairs, off, live, sv))
    return hits


def live_to_fileoff(live, m, so_name):
    for start, end, seg_fo, _p in m.segments(so_name):
        if start <= live < end:
            return seg_fo + (live - start)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--so", default="libil2cpp.so")
    a = ap.parse_args()
    m = LiveMem()
    if not m.find_pid():
        print("未找到游戏进程")
        return 1
    m.refresh_maps()
    print(f"pid={m.pid} maps={len(m.maps)}")
    segs = [s for s in m.segments(a.so) if "w" in m.maps[0][3] or True]
    best = []
    for start, end, seg_fo, path in m.segments(a.so):
        # 取可写段（.data/.bss）
        line = next((l for l in m.maps if l[0] == start), None)
        size = end - start
        if size < 0x10000 or size > 0x2000000:
            continue
        try:
            blob = m.read(start, size)
        except Exception as e:
            print(f"  read fail 0x{start:x}: {e}")
            continue
        if len(blob) != size:
            print(f"  short read at 0x{start:x}: {len(blob)}/{size}")
            continue
        hits = scan_metadata_reg(blob, start, m.maps, a.so, m)
        for pairs, off, live, sv in hits[:5]:
            print(f"  candidate seg=0x{start:x} off=0x{off:x} pairs={pairs} "
                  f"live=0x{live:x} static_vaddr={'0x%x' % sv if sv else 'n/a'} "
                  f"count0={struct.unpack_from('<I', blob, off)[0]}")
            best.append((pairs, live, sv))
    print(f"candidates: {len(best)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
