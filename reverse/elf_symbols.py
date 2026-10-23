# -*- coding: utf-8 -*-
"""导出 ELF64 动态符号表（.dynsym）到文本，便于按关键字检索函数。

用法：python elf_symbols.py <elf> [out.txt]
"""
import struct
import sys
from pathlib import Path


def main() -> int:
    p = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else p.with_suffix(".dynsym.txt")
    d = p.read_bytes()
    e_shoff, = struct.unpack_from("<Q", d, 0x28)
    e_shentsize, = struct.unpack_from("<H", d, 0x3A)
    e_shnum, = struct.unpack_from("<H", d, 0x3C)
    e_shstrndx, = struct.unpack_from("<H", d, 0x3E)

    def sh(i):
        o = e_shoff + i * e_shentsize
        n, t, f, a, off, sz, lk, inf, al, es = struct.unpack_from("<IIQQQQIIQQ", d, o)
        return dict(t=t, addr=a, off=off, size=sz, link=lk, entsize=es)

    secs = [sh(i) for i in range(e_shnum)]
    base = secs[e_shstrndx]["off"]

    def sname(i):
        e = d.find(b"\0", base + i)
        return d[base + i:e].decode("ascii", "replace")

    names = {sname(s["name"] if "name" in s else 0): s for s in secs}  # placeholder
    dynsym = next((s for i, s in enumerate(secs)
                   if s["t"] == 11), None)
    # 重新取名字需带 name 字段，这里二次扫描
    entries = []
    for i in range(e_shnum):
        o = e_shoff + i * e_shentsize
        nm, t, f, a, off, sz, lk, inf, al, es = struct.unpack_from("<IIQQQQIIQQ", d, o)
        if sname(nm) != ".dynsym":
            continue
        strtab = secs[lk]
        n_entries = sz // es
        for k in range(n_entries):
            eo = off + k * es
            st_name, st_info, st_other, st_shndx, st_value, st_size = struct.unpack_from(
                "<IBBHQQ", d, eo)
            e = d.find(b"\0", strtab["off"] + st_name)
            nm2 = d[strtab["off"] + st_name:e].decode("ascii", "replace")
            if nm2:
                entries.append((st_value, st_size, st_shndx, st_info, nm2))
        break

    entries.sort(key=lambda x: x[0])
    out.write_text("\n".join(f"0x{v:08x} sz={s:>6} shndx={si:<3} info={i:<3} {n}"
                             for v, s, si, i, n in entries),
                   encoding="utf-8", newline="\n")
    print(f"{p.name}: {len(entries)} dynamic symbols -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
