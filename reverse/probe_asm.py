# -*- coding: utf-8 -*-
"""打印 ELF 某段前 N 条 ARM64 指令，用于确认 capstone 的 ADRP/ADD 操作数格式。"""
import struct
import sys
from pathlib import Path

import capstone


def main() -> int:
    elf = Path(sys.argv[1])
    off = int(sys.argv[2], 0)
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 40
    d = elf.read_bytes()
    e_shoff, = struct.unpack_from("<Q", d, 0x28)
    es, = struct.unpack_from("<H", d, 0x3A)
    num, = struct.unpack_from("<H", d, 0x3C)
    shstr, = struct.unpack_from("<H", d, 0x3E)
    secs = []
    for i in range(num):
        o = e_shoff + i * es
        v = struct.unpack_from("<IIQQQQIIQQ", d, o)
        secs.append(dict(name=v[0], type=v[1], addr=v[3], off=v[4], size=v[5]))
    base = secs[shstr]["off"]
    for s in secs:
        e = d.find(b"\0", base + s["name"])
        s["sname"] = d[base + s["name"]:e].decode("ascii", "replace")
    text = next(s for s in secs if s["sname"] == ".text")
    code = d[text["off"] + off: text["off"] + off + n * 4]
    md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_LITTLE_ENDIAN)
    for ins in md.disasm(code, text["addr"] + off):
        print(f"0x{ins.address:08x}  {ins.mnemonic:<8} {ins.op_str}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
