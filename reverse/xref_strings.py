# -*- coding: utf-8 -*-
"""ARM64 字符串交叉引用定位器（带诊断计数）。

AArch64 用 ADRP+ADD 构造 PC 相对地址引用 .rodata 常量串。扫描 .text，
跟踪每个寄存器编号的 ADRP 页地址（capstone 直接给出绝对页地址），
遇到同编号寄存器的 ADD/LDR 立即数即还原目标地址；命中目标串地址则记录引用点。

用法：python xref_strings.py <elf> <targets.txt> [out.txt]
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


def main() -> int:
    elf, targets_file = Path(sys.argv[1]), Path(sys.argv[2])
    out = Path(sys.argv[3]) if len(sys.argv) > 3 else elf.with_suffix(".xref.txt")
    d = elf.read_bytes()
    secs = load_sections(d)

    targets = [t.strip() for t in targets_file.read_text(encoding="utf-8").splitlines()
               if t.strip() and not t.startswith("#")]
    tgt = {}
    for t in targets:
        b = t.encode()
        for s in secs:
            if not s["size"] or not s["addr"] or s["type"] == 8:
                continue
            i = d[s["off"]:s["off"] + s["size"]].find(b)
            if i >= 0:
                tgt[s["addr"] + i] = t
                break
    print(f"resolved target strings: {len(tgt)}/{len(targets)}")

    text = next(s for s in secs if s["sname"] == ".text")
    code = d[text["off"]:text["off"] + text["size"]]
    md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_LITTLE_ENDIAN)
    md.skipdata = True   # 遇无法解码的字节继续前进，否则 capstone 会提前终止整个反汇编

    pages, hits = {}, {}
    n_adrp = n_chain = n_ins = 0
    last_addr = 0
    in_rodata = sum(1 for a in tgt if 0xfbe3a0 <= a < 0x121c1cc)
    for ins in md.disasm(code, text["addr"]):
        n_ins += 1
        last_addr = ins.address
        m, ops = ins.mnemonic, ins.op_str
        if m == "adrp":
            r = ADRP_RE.match(ops)
            if r:
                k = rnum(r.group(1))
                if k is not None:
                    pages[k] = int(r.group(2), 0)
                    n_adrp += 1
            continue
        if m in ("add", "ldr"):
            r = ADD_RE.match(ops) or LDR_RE.match(ops)
            if not r:
                continue
            k = rnum(r.group(2))
            if k is not None and k in pages:
                n_chain += 1
                t = pages[k] + int(r.group(3), 0)
                if t in tgt:
                    hits.setdefault(tgt[t], []).append(ins.address)

    print(f"adrp={n_adrp}  chained_add/ldr={n_chain}  instructions={n_ins} "
          f"last=0x{last_addr:x}  targets_in_rodata={in_rodata}")
    print("--- xrefs ---")
    lines = []
    for name, addrs in sorted(hits.items(), key=lambda kv: -len(kv[1])):
        line = f"{name}: {len(addrs)} refs  " + " ".join(f"0x{a:x}" for a in addrs[:10])
        lines.append(line)
        print(line)
    out.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
