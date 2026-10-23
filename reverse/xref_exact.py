# -*- coding: utf-8 -*-
"""精确字符串（含结尾 NUL）交叉引用。

与 xref_strings.py 的区别：只匹配完整字符串 "name\\0"，且对**所有**出现位置
做 adrp+add 引用扫描（不只看第一处）。

用法：python xref_exact.py <elf> <name1> [name2 ...]
"""
import re
import struct
import sys
from pathlib import Path

import capstone

ADRP_RE = re.compile(r"^([xw]\d+), #(0x[0-9a-f]+)$")
ADD_RE = re.compile(r"^([xw]\d+), ([xw]\d+), #(0x[0-9a-f]+)$")


def load_sections(d):
    e_shoff, = struct.unpack_from("<Q", d, 0x28)
    es, = struct.unpack_from("<H", d, 0x3A)
    num, = struct.unpack_from("<H", d, 0x3C)
    secs = []
    for i in range(num):
        v = struct.unpack_from("<IIQQQQIIQQ", d, e_shoff + i * es)
        secs.append(dict(name=v[0], type=v[1], addr=v[3], off=v[4], size=v[5]))
    return secs


def main():
    elf = Path(sys.argv[1])
    names = sys.argv[2:]
    d = elf.read_bytes()
    secs = load_sections(d)
    text = next(s for s in secs if s["addr"] <= 0x2185770 < s["addr"] + s["size"])
    # 只在 rodata 中找字符串
    ro = next(s for s in secs if s["addr"] <= 0xfbe3a0 < s["addr"] + s["size"])
    tgt = {}
    for n in names:
        pat = n.encode() + b"\0"
        i = 0
        while True:
            i = d.find(pat, i)
            if i < 0:
                break
            if ro["off"] <= i < ro["off"] + ro["size"]:
                va = ro["addr"] + (i - ro["off"])
                tgt[va] = n
            i += 1
    print("targets:", {hex(k): v for k, v in tgt.items()})

    md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_LITTLE_ENDIAN)
    md.skipdata = True
    pages, hits = {}, {}
    for ins in md.disasm(d[text["off"]:text["off"] + text["size"]], text["addr"]):
        m, o = ins.mnemonic, ins.op_str
        if m == "adrp":
            r = ADRP_RE.match(o)
            if r:
                pages[int(r.group(1)[1:])] = int(r.group(2), 0)
            continue
        if m in ("add", "ldr"):
            r = ADD_RE.match(o) if m == "add" else None
            if r and int(r.group(2)[1:]) in pages:
                t = pages[int(r.group(2)[1:])] + int(r.group(3), 0)
                if t in tgt:
                    hits.setdefault(t, []).append(ins.address)
        pages_keep = pages
    print("--- hits ---")
    for t, addrs in hits.items():
        print(f"{tgt[t]} @0x{t:x}: " + " ".join(f"0x{a:x}" for a in addrs))


if __name__ == "__main__":
    raise SystemExit(main())
