# -*- coding: utf-8 -*-
"""转储 .data/.got 中的函数指针表：把规则分发表还原成函数地址清单。

背景：libGameCore.so 的 RuleTactics 通过固定基址 0x509c000 的全局函数指针
      表逐条调用规则（adrp x8,#0x509c000; ldr x8,[x8,#0xNNN]; blr x8）。
      该表在文件里存的是 R_AARCH64_RELATIVE 的 addend（即目标地址本身），
      因此可直接读出每个规则函数的实际地址。

用法：python dump_ptrtable.py <elf> <base_vaddr> <count> [out.txt]
"""
import struct
import sys
from pathlib import Path


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
    elf = Path(sys.argv[1])
    base = int(sys.argv[2], 0)
    count = int(sys.argv[3])
    out = Path(sys.argv[4]) if len(sys.argv) > 4 else elf.with_name(f"ptrtable_{base:x}.txt")

    d = elf.read_bytes()
    secs = load_sections(d)
    text = next(s for s in secs if s["sname"] == ".text")
    t0, t1 = text["addr"], text["addr"] + text["size"]

    rows, in_text = [], 0
    for i in range(count):
        va = base + i * 8
        sec = next((s for s in secs if s["addr"] <= va < s["addr"] + s["size"]), None)
        if sec is None:
            rows.append(f"[{i:04d}] 0x{va:x}  <no section>")
            continue
        off = sec["off"] + (va - sec["addr"])
        ptr, = struct.unpack_from("<Q", d, off)
        tag = "TEXT" if t0 <= ptr < t1 else (sec["sname"] if ptr else "-")
        if t0 <= ptr < t1:
            in_text += 1
        rows.append(f"[{i:04d}] slot=0x{va:x} -> 0x{ptr:016x}  {tag}")

    out.write_text("\n".join(rows), encoding="utf-8", newline="\n")
    print(f"slots={count}  in_text={in_text}  -> {out}")
    print("--- first 40 ---")
    for r in rows[:40]:
        print(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
