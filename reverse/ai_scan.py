# -*- coding: utf-8 -*-
"""libGameCore.so 全量数据流扫描：把「哪个函数读了哪块数据」抽成表。

产出三类事件（每条都带归属函数）：
  1) string  : ADRP+ADD/LDR 还原出的字符串引用（数据节点名、日志标签、字段名）
  2) slot    : `ldr xN, [page, #off]` 形式加载的全局槽位（运行期由 reloc 填充的函数指针表）
  3) icall   : `blr xN`，且 xN 来自上面的 slot（= 通过函数指针表调用的 API，可反查实现地址）

函数归属：用 <elf>.entries.txt（由 func_dump.py 生成，含全部 bl 目标）做最近入口查找。

用法：python ai_scan.py <elf> [out_prefix]
"""
import bisect
import re
import struct
import sys
from pathlib import Path

import capstone

STR_MIN, STR_MAX = 3, 160

RE_ADRP = re.compile(r"^([xw]\d+), #(0x[0-9a-f]+)$")
RE_ADD = re.compile(r"^([xw]\d+), ([xw]\d+), #(0x[0-9a-f]+)$")
RE_LDR = re.compile(r"^([xw]\d+), \[([xw]\d+)(?:, #(0x[0-9a-f]+))?\]$")


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


def build_strmap(d: bytes, secs):
    """把只读段里所有可打印 NUL 结尾串建成 addr -> text 表。"""
    m = {}
    for s in secs:
        if not s["size"] or s["type"] == 8 or s["sname"] not in (
                ".rodata", ".data.rel.ro", ".data", ".rodata.1"):
            continue
        blob = d[s["off"]:s["off"] + s["size"]]
        i, n = 0, len(blob)
        while i < n:
            j = blob.find(b"\0", i)
            if j < 0:
                break
            if STR_MIN <= j - i <= STR_MAX:
                seg = blob[i:j]
                if all(32 <= c < 127 for c in seg):
                    m[s["addr"] + i] = seg.decode("ascii")
            i = j + 1
    return m


def main() -> int:
    elf = Path(sys.argv[1])
    pref = Path(sys.argv[2]) if len(sys.argv) > 2 else elf.with_suffix("")
    d = elf.read_bytes()
    secs = load_sections(d)
    text = next(s for s in secs if s["sname"] == ".text")
    print(f".text 0x{text['addr']:x} size 0x{text['size']:x}")

    strmap = build_strmap(d, secs)
    print(f"string table: {len(strmap)} entries")

    cache = elf.with_name(elf.name + ".entries.txt")
    entries = [int(x, 16) for x in cache.read_text().split()]
    entries.sort()
    print(f"function entries: {len(entries)}")

    def owner(a):
        i = bisect.bisect_right(entries, a) - 1
        if i < 0:
            return 0
        f = entries[i]
        return f if a - f <= 0x3000 else 0

    md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_LITTLE_ENDIAN)
    md.skipdata = True

    pages = {}      # reg -> adrp page
    vals = {}       # reg -> 绝对地址（由 adrp+add 得到）
    slot_regs = {}  # reg -> slot 地址（由 ldr [page,#off] 得到）
    cur = 0
    fstr, fslot, ficall = [], [], []

    code = d[text["off"]:text["off"] + text["size"]]
    total = 0
    for ins in md.disasm(code, text["addr"]):
        total += 1
        a, m, ops = ins.address, ins.mnemonic, ins.op_str
        if a - cur > 0x3000 or cur == 0:
            cur = owner(a)
        if m == "adrp":
            mm = RE_ADRP.match(ops)
            if mm:
                r = mm.group(1)
                pages[r] = int(mm.group(2), 0)
                vals.pop(r, None)
                slot_regs.pop(r, None)
            continue
        if m == "add":
            mm = RE_ADD.match(ops)
            if mm:
                dst, src, imm = mm.group(1), mm.group(2), int(mm.group(3), 0)
                if src in pages:
                    t = pages[src] + imm
                    vals[dst] = t
                    if t in strmap:
                        fstr.append((a, cur, strmap[t]))
            continue
        if m == "ldr":
            mm = RE_LDR.match(ops)
            if mm:
                dst, base = mm.group(1), mm.group(2)
                imm = int(mm.group(3), 0) if mm.group(3) else 0
                if base in pages:
                    slot = pages[base] + imm
                    slot_regs[dst] = slot
                    fslot.append((a, cur, slot))
                    vals.pop(dst, None)
                    continue
                if base in vals:
                    t = vals[base] + imm
                    if t in strmap:
                        fstr.append((a, cur, strmap[t]))
                vals.pop(dst, None)
                slot_regs.pop(dst, None)
            continue
        if m == "blr":
            r = ops.strip()
            if r in slot_regs:
                ficall.append((a, cur, slot_regs[r]))
            continue
        # 任何写寄存器的指令都会让缓存失效（保守：只清 dst）
        if m.startswith(("mov", "ldp", "adr", "ldur", "ldrb", "ldrh", "ldrsw")):
            dst = ops.split(",")[0].strip()
            if dst.startswith(("x", "w")):
                vals.pop(dst, None)
                slot_regs.pop(dst, None)

    print(f"instructions scanned: {total}")
    print(f"string refs: {len(fstr)}   slot loads: {len(fslot)}   icalls: {len(ficall)}")

    with open(f"{pref}.strrefs.tsv", "w", encoding="utf-8", newline="\n") as f:
        for a, fn, s in fstr:
            f.write(f"0x{a:08x}\t0x{fn:08x}\t{s}\n")
    with open(f"{pref}.slots.tsv", "w", encoding="utf-8", newline="\n") as f:
        for a, fn, s in fslot:
            f.write(f"0x{a:08x}\t0x{fn:08x}\t0x{s:08x}\n")
    with open(f"{pref}.icalls.tsv", "w", encoding="utf-8", newline="\n") as f:
        for a, fn, s in ficall:
            f.write(f"0x{a:08x}\t0x{fn:08x}\t0x{s:08x}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
