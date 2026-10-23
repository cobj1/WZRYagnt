# -*- coding: utf-8 -*-
"""列出 ELF64 的段表与符号表存在性（判断原生引擎是否被 strip）。

用法：python elf_sections.py <elf>
用途：libGameCore.so 若保留 .symtab/.dynsym，Ghidra 可直接看到函数名，
否则只能靠字符串交叉引用来定位 RuleTactics 等决策函数。
"""
import struct
import sys
from pathlib import Path


def main() -> int:
    p = Path(sys.argv[1])
    d = p.read_bytes()
    if d[:4] != b"\x7fELF" or d[4] != 2:
        print("not ELF64")
        return 1
    e_shoff, = struct.unpack_from("<Q", d, 0x28)
    e_shentsize, = struct.unpack_from("<H", d, 0x3A)
    e_shnum, = struct.unpack_from("<H", d, 0x3C)
    e_shstrndx, = struct.unpack_from("<H", d, 0x3E)

    def sh(i):
        o = e_shoff + i * e_shentsize
        name, typ, flags, addr, off, size, link, info, align, entsize = struct.unpack_from(
            "<IIQQQQIIQQ", d, o)
        return dict(name=name, type=typ, flags=flags, addr=addr, off=off,
                    size=size, entsize=entsize)

    strtab = sh(e_shstrndx)
    base = strtab["off"]

    def sname(idx):
        e = d.find(b"\0", base + idx)
        return d[base + idx:e].decode("ascii", "replace")

    print(f"{p.name}  sections={e_shnum}")
    for i in range(e_shnum):
        h = sh(i)
        n = sname(h["name"])
        if n in ("", ".text", ".rodata", ".data", ".dynsym", ".symtab",
                 ".strtab", ".dynstr", ".rela.dyn", ".rela.plt", ".got", ".plt"):
            extra = ""
            if h["type"] == 2 and h["entsize"]:  # SYMTAB
                extra = f"  symbols={h['size'] // h['entsize']}"
            print(f"  {n:<14} type={h['type']} addr=0x{h['addr']:x} "
                  f"off=0x{h['off']:x} size=0x{h['size']:x}{extra}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
