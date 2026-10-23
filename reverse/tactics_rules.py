# -*- coding: utf-8 -*-
"""枚举 RuleTactics 真正使用的规则条目，产出"调用序 ↔ 槽位 ↔ 地址 ↔ 标签/阈值"表。

背景：0x509c000 是一张混合注册表（含 JS 引擎/测试框架/AITrial 等条目），
      只有被调度器以 `adrp x8,#0x509c000` + `ldr x8,[x8,#imm]` 引用到的槽位
      才属于规则流程。因此先全量扫 .text 找出这类引用对，再按 .rela.dyn
      还原目标地址，最后对每个唯一规则函数反汇编抽取标签与阈值常量。

用法：python tactics_rules.py <elf> [base_vaddr] [out.tsv]
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
           "and", "ands", "orr", "eor", "tst", "cmn"}


def rnum(reg):
    m = REG_RE.match(reg.strip())
    return int(m.group(1)) if m else None


def sections(d):
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
    out = Path(sys.argv[3]) if len(sys.argv) > 3 else elf.with_name("tactics_rules.tsv")
    site_lo = int(sys.argv[4], 0) if len(sys.argv) > 4 else 0
    site_hi = int(sys.argv[5], 0) if len(sys.argv) > 5 else 0xFFFFFFFFFFFFFFFF

    d = elf.read_bytes()
    secs = sections(d)
    text = next(s for s in secs if s["sname"] == ".text")
    md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_LITTLE_ENDIAN)
    md.skipdata = True

    # 1) 全量扫 .text：找出 adrp base + 紧随其后的 ldr [reg,#imm]
    slots = {}   # slot_offset -> list(code_addr)
    pages, code = {}, d[text["off"]:text["off"] + text["size"]]
    for ins in md.disasm(code, text["addr"]):
        m, ops = ins.mnemonic, ins.op_str
        if m == "adrp":
            r = ADRP_RE.match(ops)
            if r and rnum(r.group(1)) is not None:
                pages[rnum(r.group(1))] = int(r.group(2), 0)
            continue
        if m == "ldr":
            r = LDR_MEM_RE.match(ops)
            if r:
                k = rnum(r.group(2))
                imm = int(r.group(3), 0)
                if k is not None and pages.get(k) == base and imm <= 0x2000 \
                        and site_lo <= ins.address < site_hi:
                    slots.setdefault(imm, []).append(ins.address)
    print(f"referenced slots on table 0x{base:x}: {len(slots)}")

    # 2) 用重定位把槽位还原为函数地址
    rela = next(s for s in secs if s["sname"] == ".rela.dyn")
    relocs = {}
    targets = {base + off for off in slots}
    for i in range(rela["size"] // 24):
        o = rela["off"] + i * 24
        r_off, r_info, r_add = struct.unpack_from("<QQq", d, o)
        if (r_info & 0xFFFFFFFF) == R_AARCH64_RELATIVE and r_off in targets:
            relocs[r_off] = r_add

    def sec_of(va):
        return next((s for s in secs if s["addr"] <= va < s["addr"] + s["size"]), None)

    def cstr(va, limit=72):
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
        if f != f or abs(f) in (float("inf"),) or not (1e-6 <= abs(f) <= 1e6):
            return None
        return round(f, 6)

    def analyse(tgt):
        off = text["off"] + (tgt - text["addr"])
        blk = d[off:off + 1600]
        pages2, tags, consts, floats = {}, [], [], []
        n = 0
        for ins in md.disasm(blk, tgt):
            n += 1
            m, ops = ins.mnemonic, ins.op_str
            if m == "ret":
                break
            if m == "adrp":
                r = ADRP_RE.match(ops)
                if r and rnum(r.group(1)) is not None:
                    pages2[rnum(r.group(1))] = int(r.group(2), 0)
                continue
            if m in ("add", "ldr", "ldrb", "ldrh", "ldrsw"):
                r = ADD_RE.match(ops) if m == "add" else LDR_MEM_RE.match(ops)
                if r:
                    k = rnum(r.group(2))
                    if k is not None and k in pages2:
                        t = pages2[k] + int(r.group(3), 0)
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
        return n, tags, floats, consts

    cache = {}
    lines = ["order\tslot\toff\taddr\tins\ttags\tfloats\tconsts\tsites"]
    for order, off in enumerate(sorted(slots)):
        va = base + off
        tgt = relocs.get(va)
        if tgt is None or not (text["addr"] <= tgt < text["addr"] + text["size"]):
            lines.append(f"{order}\t[{off:#x}]\t{off:#x}\tn/a\t0\t\t\t\t"
                         f"{'|'.join(hex(a) for a in slots[off])}")
            continue
        if tgt not in cache:
            cache[tgt] = analyse(tgt)
        n, tags, floats, consts = cache[tgt]
        lines.append(f"{order}\t[{off:#x}]\t{off:#x}\t0x{tgt:x}\t{n}\t"
                     f"{'|'.join(dict.fromkeys(tags))}\t"
                     f"{'|'.join(str(x) for x in dict.fromkeys(floats))}\t"
                     f"{'|'.join(str(c) for c in dict.fromkeys(consts))}\t"
                     f"{'|'.join(hex(a) for a in slots[off][:4])}")
    out.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    resolved = sum(1 for l in lines[1:] if "\tn/a\t" not in l)
    print(f"resolved rule functions: {resolved} / {len(slots)} slots")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
