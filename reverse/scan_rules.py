# -*- coding: utf-8 -*-
"""批量扫描规则函数表，产出"规则名 ↔ 地址 ↔ 阈值常量"对照表。

做法：先用 .rela.dyn 还原 0x509c000 的规则函数指针表，再对每个规则函数
      做有界反汇编（遇到首个 ret 即停），抽取：
        - 引用的 .rodata 字符串（规则标签，如 kMoveStopWhenMonsterNotBorn）
        - 立即数常量（cmp/mov/add/sub/and 的 #imm）
        - 引用的 .rodata 浮点常量（阈值），以及 ctx 结构偏移

用法：python scan_rules.py <elf> [base_vaddr] [count] [out.tsv]
"""
import re
import struct
import sys
from pathlib import Path

import capstone

R_AARCH64_RELATIVE = 1027
ADRP_RE = re.compile(r"^([xw]\d+), #(0x[0-9a-f]+)$")
ADD_RE = re.compile(r"^([xw]\d+), ([xw]\d+), #(0x[0-9a-f]+)$")
LDR_MEM_RE = re.compile(r"^([xw]\d+), \[([xw]\d+), #(0x[0-9a-f]+)\]$")
IMM_END_RE = re.compile(r"#(-?0x[0-9a-f]+)$")
MEM_OFS_RE = re.compile(r"\[([xw]\d+), #(0x[0-9a-f]+)\]$")
REG_RE = re.compile(r"^[xw](\d+)$")
CMPLIKE = {"cmp", "mov", "movz", "movk", "add", "adds", "sub", "subs",
           "and", "ands", "orr", "eor", "tst", "cmn", "lsl", "lsr", "asr"}


def rnum(reg):
    m = REG_RE.match(reg.strip())
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
    b = secs[shstr]["off"]
    for s in secs:
        e = d.find(b"\0", b + s["name"])
        s["sname"] = d[b + s["name"]:e].decode("ascii", "replace")
    return secs


def main() -> int:
    elf = Path(sys.argv[1])
    base = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0x509C000
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 512
    out = Path(sys.argv[4]) if len(sys.argv) > 4 else elf.with_name("rules_table.tsv")

    d = elf.read_bytes()
    secs = load_sections(d)
    text = next(s for s in secs if s["sname"] == ".text")
    rodata = [s for s in secs if s["type"] == 1 and s["addr"] and s["size"]]

    rela = next(s for s in secs if s["sname"] == ".rela.dyn")
    relocs = {}
    for i in range(rela["size"] // 24):
        o = rela["off"] + i * 24
        r_off, r_info, r_add = struct.unpack_from("<QQq", d, o)
        if (r_info & 0xFFFFFFFF) == R_AARCH64_RELATIVE and base <= r_off < base + count * 8:
            relocs[r_off] = r_add

    def sec_of(va):
        return next((s for s in secs if s["addr"] <= va < s["addr"] + s["size"]), None)

    def cstr(va, limit=64):
        s = sec_of(va)
        if s is None or s["type"] == 8:
            return None
        o = s["off"] + (va - s["addr"])
        e = d.find(b"\0", o, o + limit)
        if e > o:
            b = d[o:e]
            if len(b) >= 3 and all(32 <= c < 127 for c in b):
                return b.decode("ascii")
        return None

    def cfloat(va):
        s = sec_of(va)
        if s is None:
            return None
        o = s["off"] + (va - s["addr"])
        if o + 4 > len(d):
            return None
        f, = struct.unpack_from("<f", d, o)
        if f != f or f in (float("inf"), float("-inf")):
            return None
        a = abs(f)
        if 1e-6 <= a <= 1e6:
            return round(f, 6)
        return None

    md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_LITTLE_ENDIAN)
    md.skipdata = True

    rows, seen = [], {}
    for i in range(count):
        va = base + i * 8
        tgt = relocs.get(va)
        if tgt is None or not (text["addr"] <= tgt < text["addr"] + text["size"]):
            continue
        if tgt in seen:          # 同一函数被多个槽位引用
            seen[tgt].append(i)
            continue
        seen[tgt] = [i]
        off = text["off"] + (tgt - text["addr"])
        code = d[off:off + 1600]
        pages, tags, consts, floats, mems = {}, [], [], [], []
        n = 0
        for ins in md.disasm(code, tgt):
            n += 1
            m, ops = ins.mnemonic, ins.op_str
            if m == "ret":
                break
            if m == "adrp":
                r = ADRP_RE.match(ops)
                if r and rnum(r.group(1)) is not None:
                    pages[rnum(r.group(1))] = int(r.group(2), 0)
                continue
            if m in ("add", "ldr", "ldrb", "ldrh", "ldrsw", "str", "strb"):
                r = ADD_RE.match(ops) if m == "add" else LDR_MEM_RE.match(ops)
                if r:
                    k = rnum(r.group(2))
                    if k is not None and k in pages:
                        t = pages[k] + int(r.group(3), 0)
                        s = cstr(t)
                        if s:
                            tags.append(s)
                        else:
                            f = cfloat(t)
                            if f is not None:
                                floats.append(f)
            if m in CMPLIKE:
                r = IMM_END_RE.search(ops)
                if r:
                    consts.append(int(r.group(1), 0))
            else:
                r = MEM_OFS_RE.search(ops)
                if r:
                    mems.append(int(r.group(2), 0))
        rows.append((i, tgt, n, tags, consts, floats, mems))

    with out.open("w", encoding="utf-8", newline="\n") as f:
        f.write("slot\taddr\tins\ttags\tconsts\tfloats\tmem_offsets\n")
        for i, tgt, n, tags, consts, floats, mems in rows:
            f.write(f"[{i:04d}]\t0x{tgt:x}\t{n}\t"
                    f"{'|'.join(dict.fromkeys(tags))}\t"
                    f"{'|'.join(str(c) for c in dict.fromkeys(consts))}\t"
                    f"{'|'.join(str(x) for x in dict.fromkeys(floats))}\t"
                    f"{'|'.join(hex(x) for x in dict.fromkeys(mems))}\n")
    n_tagged = sum(1 for r in rows if r[3])
    n_float = sum(1 for r in rows if r[5])
    print(f"rule functions scanned={len(rows)}  with_tag={n_tagged}  with_float={n_float}")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
