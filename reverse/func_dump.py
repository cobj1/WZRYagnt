# -*- coding: utf-8 -*-
"""ARM64 函数级反汇编 + 注解（函数入口由调用图反推）。

libGameCore.so 的 .symtab 已剥离，也没有全局 bti 边界。做法：
  1) 全量扫一遍 .text，把所有 `bl #target` 的目标地址收集为"函数入口集合"
     （覆盖率高的启发式：被调用的地址必然是函数入口），结果缓存到
     <elf>.entries.txt，重复运行不再重扫；
  2) 对给定地址，取不大于它的最近入口作为函数起点；
  3) 反汇编并注解：ADRP+ADD/LDR 还原的 .rodata 地址标注为字符串内容，
     bl 目标标注为调用地址。

用法：python func_dump.py <elf> <vaddr> [max_ins] [out.txt]
"""
import re
import struct
import sys
from pathlib import Path

import capstone

ADRP_RE = re.compile(r"^([xw]\d+), #(0x[0-9a-f]+)$")
ADD_RE = re.compile(r"^([xw]\d+), ([xw]\d+), #(0x[0-9a-f]+)$")
LDR_RE = re.compile(r"^([xw]\d+), \[([xw]\d+), #(0x[0-9a-f]+)\]$")
REG_RE = re.compile(r"^[xw](\d+)$")


def rnum(reg: str):
    m = REG_RE.match(reg)
    return int(m.group(1)) if m else None


def load_sections(d: bytes):
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
    return secs


def build_entries(elf: Path, d: bytes, text) -> list:
    cache = elf.with_name(elf.name + ".entries.txt")
    if cache.exists():
        return [int(x, 16) for x in cache.read_text().split()]
    code = d[text["off"]:text["off"] + text["size"]]
    md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_LITTLE_ENDIAN)
    md.skipdata = True
    entries = set()
    for ins in md.disasm(code, text["addr"]):
        if ins.mnemonic in ("bl", "blr") and ins.op_str.startswith("#"):
            entries.add(int(ins.op_str[1:], 0))
    lst = sorted(entries)
    cache.write_text("\n".join(f"{a:x}" for a in lst), encoding="utf-8", newline="\n")
    print(f"[entries] built {len(lst)} entries -> {cache}")
    return lst


def main() -> int:
    elf, vaddr = Path(sys.argv[1]), int(sys.argv[2], 0)
    max_ins = int(sys.argv[3]) if len(sys.argv) > 3 else 400
    out = Path(sys.argv[4]) if len(sys.argv) > 4 else elf.with_suffix(f".{vaddr:x}.asm.txt")

    d = elf.read_bytes()
    secs = load_sections(d)
    text = next(s for s in secs if s["sname"] == ".text")
    rodata = next((s for s in secs if s["sname"] == ".rodata"), None)

    entries = build_entries(elf, d, text)
    exact = len(sys.argv) > 5 and sys.argv[5] == "exact"
    if exact:
        start, mode = vaddr, "exact"
    else:
        # 只信任近距离的入口（同一函数内），否则退回"目标前 N 字节"窗口
        cands = [a for a in entries if a <= vaddr and vaddr - a <= 0x400]
        start = cands[-1] if cands else max(text["addr"], vaddr - 0x800)
        mode = "call-entry" if cands else "back-window"
    print(f"target 0x{vaddr:x} -> window start 0x{start:x} "
          f"(offset +0x{vaddr - start:x}, {mode})")

    off = text["off"] + (start - text["addr"])
    code = d[off:off + max_ins * 4]
    md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_LITTLE_ENDIAN)
    md.skipdata = True

    def cstr(addr, limit=72):
        if rodata and rodata["addr"] <= addr < rodata["addr"] + rodata["size"]:
            o = rodata["off"] + (addr - rodata["addr"])
            e = d.find(b"\0", o, o + limit)
            if e > o:
                s = d[o:e]
                if all(32 <= c < 127 for c in s) and len(s) >= 3:
                    return s.decode("ascii")
        return None

    pages, lines = {}, []
    for ins in md.disasm(code, start):
        note = ""
        m, ops = ins.mnemonic, ins.op_str
        if m == "adrp":
            r = ADRP_RE.match(ops)
            if r:
                k = rnum(r.group(1))
                if k is not None:
                    pages[k] = int(r.group(2), 0)
        elif m in ("add", "ldr"):
            r = ADD_RE.match(ops) or LDR_RE.match(ops)
            if r:
                k = rnum(r.group(2))
                if k is not None and k in pages:
                    t = pages[k] + int(r.group(3), 0)
                    s = cstr(t)
                    if s:
                        note = f'; "{s}"'
                    elif rodata and rodata["addr"] <= t < rodata["addr"] + rodata["size"]:
                        note = f"; .rodata+0x{t - rodata['addr']:x}"
        if m in ("bl", "b") and ops.startswith("#"):
            note = (note + " " if note else "") + f"; -> 0x{int(ops[1:], 0):x}"
        mark = "   <<< TARGET" if ins.address == vaddr else ""
        lines.append(f"0x{ins.address:08x}  {m:<8} {ops:<44}{note}{mark}")

    out.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"disassembled {len(lines)} instructions -> {out}")
    print(f"string refs={sum(1 for x in lines if '; \"' in x)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
