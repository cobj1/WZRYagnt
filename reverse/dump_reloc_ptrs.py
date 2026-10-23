# -*- coding: utf-8 -*-
"""解析 .rela.dyn 重定位表，还原运行时才填充的函数指针表。

背景：RuleTactics 通过固定基址 0x509c000 的全局函数指针表逐条调用规则。
      该表在文件中全为 0，其值由加载器的 R_AARCH64_RELATIVE 重定位写入，
      目标地址存在重定位项的 r_addend 里。因此解析 .rela.dyn 即可拿到
      每条规则函数的实际地址（无需运行游戏）。

用法：python dump_reloc_ptrs.py <elf> <base_vaddr> <count> [out.txt]
"""
import struct
import sys
from pathlib import Path

R_AARCH64_RELATIVE = 1027


def load_sections(d: bytes):
    e_shoff, = struct.unpack_from("<Q", d, 0x28)
    es, = struct.unpack_from("<H", d, 0x3A)
    num, = struct.unpack_from("<H", d, 0x3C)
    shstr, = struct.unpack_from("<H", d, 0x3E)
    secs = []
    for i in range(num):
        o = e_shoff + i * es
        v = struct.unpack_from("<IIQQQQIIQQ", d, o)
        secs.append(dict(name=v[0], type=v[1], addr=v[3], off=v[4], size=v[5],
                         link=v[6], entsize=v[9]))
    base = secs[shstr]["off"]
    for s in secs:
        e = d.find(b"\0", base + s["name"])
        s["sname"] = d[base + s["name"]:e].decode("ascii", "replace")
    return secs


def main() -> int:
    elf = Path(sys.argv[1])
    base = int(sys.argv[2], 0)
    count = int(sys.argv[3])
    out = Path(sys.argv[4]) if len(sys.argv) > 4 else elf.with_name(f"reloc_{base:x}.txt")

    d = elf.read_bytes()
    secs = load_sections(d)
    text = next(s for s in secs if s["sname"] == ".text")
    t0, t1 = text["addr"], text["addr"] + text["size"]

    rela = next((s for s in secs if s["sname"] in (".rela.dyn", ".rela.plt")), None)
    if rela is None:
        print("no .rela.dyn")
        return 1
    n = rela["size"] // 24
    relocs = {}
    for i in range(n):
        o = rela["off"] + i * 24
        r_off, r_info, r_add = struct.unpack_from("<QQq", d, o)
        if (r_info & 0xFFFFFFFF) == R_AARCH64_RELATIVE and base <= r_off < base + count * 8:
            relocs[r_off] = r_add
    print(f".rela.dyn entries={n}  matched slots in range={len(relocs)}")

    rows, in_text = [], 0
    for i in range(count):
        va = base + i * 8
        tgt = relocs.get(va)
        if tgt is None:
            sec = next((s for s in secs if s["addr"] <= va < s["addr"] + s["size"]), None)
            raw, = struct.unpack_from("<Q", d, sec["off"] + (va - sec["addr"])) if sec else (0,)
            tgt = raw
        tag = "TEXT" if t0 <= tgt < t1 else ("sym?" if tgt else "-")
        if t0 <= tgt < t1:
            in_text += 1
        rows.append(f"[{i:04d}] slot=0x{va:x} -> 0x{tgt:08x}  {tag}")

    out.write_text("\n".join(rows), encoding="utf-8", newline="\n")
    print(f"resolved in_text={in_text}  -> {out}")
    for r in rows:
        if "TEXT" in r:
            print(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
