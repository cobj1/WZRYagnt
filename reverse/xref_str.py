#!/usr/bin/env python
"""字符串 -> 引用点（ARM64 adrp+add / adrp+ldr）。

为什么要单独写一个：现有 `gc.strrefs.tsv` 的第一列是**引用指令地址**、不是字符串地址，
排查时容易看错；而按字符串找引用需要处理 `adrp` 的 21 位立即数，手算易错。
这里一次给全：字符串 VA、所有引用点、引用点前后各若干条指令。

用法：
    python reverse/xref_str.py location_x
    python reverse/xref_str.py "solder configid" --before 12 --after 24
    python reverse/xref_str.py location_x --all      # 不限 6 处
"""
from __future__ import annotations

import argparse
import struct
import sys
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "reverse"))

from rule_consts import _load  # noqa: E402


def segments(data: bytes):
    e_phoff = struct.unpack_from("<Q", data, 0x20)[0]
    e_phentsize = struct.unpack_from("<H", data, 0x36)[0]
    e_phnum = struct.unpack_from("<H", data, 0x38)[0]
    out = []
    for i in range(e_phnum):
        o = e_phoff + i * e_phentsize
        if struct.unpack_from("<I", data, o)[0] != 1:
            continue
        p_off = struct.unpack_from("<Q", data, o + 0x08)[0]
        p_va = struct.unpack_from("<Q", data, o + 0x10)[0]
        p_fsz = struct.unpack_from("<Q", data, o + 0x20)[0]
        p_flg = struct.unpack_from("<I", data, o + 0x04)[0]
        out.append((p_off, p_va, p_fsz, p_flg))
    return out


def off_to_va(segs, off: int):
    for p_off, p_va, p_fsz, _f in segs:
        if p_off <= off < p_off + p_fsz:
            return p_va + (off - p_off)
    return None


def find_strings(data: bytes, needle: bytes, segs, limit: int):
    out, start = [], 0
    while len(out) < limit:
        i = data.find(needle, start)
        if i < 0:
            break
        out.append((i, off_to_va(segs, i)))
        start = i + 1
    return out


def xrefs(data: bytes, segs, target: int, limit: int):
    page = target & ~0xFFF
    lo12 = target & 0xFFF
    hits = []
    for p_off, p_va, p_fsz, flg in segs:
        if not (flg & 1):
            continue
        chunk = data[p_off:p_off + p_fsz]
        words = array("I")
        words.frombytes(chunk[: len(chunk) // 4 * 4])
        for idx, w in enumerate(words):
            if (w & 0x9F000000) != 0x90000000:
                continue
            immlo = (w >> 29) & 3
            immhi = (w >> 5) & 0x7FFFF
            imm = (immhi << 2) | immlo
            if imm & (1 << 20):
                imm -= (1 << 21)
            pc = p_va + idx * 4
            if ((pc & ~0xFFF) + (imm << 12)) != page:
                continue
            rd = w & 0x1F
            for k in range(1, 6):
                if idx + k >= len(words):
                    break
                w2 = words[idx + k]
                rn = (w2 >> 5) & 0x1F
                if rn != rd:
                    continue
                if (w2 & 0xFFC00000) == 0x91000000:          # add x, x, #imm12
                    if ((w2 >> 10) & 0xFFF) == lo12:
                        hits.append((pc, pc + 4 * k, "add"))
                        break
                elif (w2 & 0xFFC00000) == 0xF9400000:        # ldr x, [x, #imm12*8]
                    if (((w2 >> 10) & 0xFFF) * 8) == lo12:
                        hits.append((pc, pc + 4 * k, "ldr"))
                        break
                elif (w2 & 0xFFC00000) == 0xB9400000:        # ldr w, [x, #imm12*4]
                    if (((w2 >> 10) & 0xFFF) * 4) == lo12:
                        hits.append((pc, pc + 4 * k, "ldrw"))
                        break
            if len(hits) >= limit:
                return hits
    return hits


def dump_around(md, data: bytes, segs, addr: int, before: int, after: int):
    start = addr - before * 4          # before 是**指令条数**
    off = None
    for p_off, p_va, p_fsz, _f in segs:
        if p_va <= start < p_va + p_fsz:
            off = p_off + (start - p_va)
            break
    if off is None:
        return
    code = data[off: off + (before + after) * 4]
    for ins in md.disasm(code, start):
        mark = " <<<" if ins.address == addr else ""
        print(f"    0x{ins.address:08x}  {ins.mnemonic:<8} {ins.op_str}{mark}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("needle")
    ap.add_argument("--before", type=int, default=14)
    ap.add_argument("--after", type=int, default=18)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--strings", type=int, default=6, help="最多看几处同名字符串")
    args = ap.parse_args()

    data, md = _load()
    segs = segments(data)
    n = args.strings if args.all else min(args.strings, 2)
    strs = find_strings(data, args.needle.encode("utf-8"), segs, n)
    if not strs:
        print("没有该字符串")
        return 1
    print(f"字符串 {args.needle!r} 共 {len(strs)} 处：")
    seen = set()
    for off, va in strs:
        print(f"  file=0x{off:x} va=0x{va:x}")
        if va is None or va in seen:
            continue
        seen.add(va)
        hits = xrefs(data, segs, va, 6 if args.all else 2)
        if not hits:
            print("    （无 adrp+add 引用，可能被当作数据/表项使用）")
            continue
        for adrp_pc, use_pc, kind in hits:
            print(f"    引用 {kind} @0x{use_pc:x}:")
            dump_around(md, data, segs, use_pc, args.before, args.after)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
