# -*- coding: utf-8 -*-
"""解析 `blr` 虚拟调用：GOT 槽位 → 真实目标 → 目标函数引用的字符串。

背景：RuleTactics 的规则函数核心判据都是
    adrp x8, <page> ; ldr x8, [x8, #off] ; blr x8
即通过 `.data` 里的**函数指针**间接调用。`.data` 在文件里是 0（加载时由重定位填充），
所以「谓词语义」必须靠 `.rela.dyn` 的 `R_AARCH64_RELATIVE` addend 解出真实目标地址，
再去反汇编目标函数、读它引用的 `.rodata` 字符串。

这是 C 组规则落地的第一道前置（见 `docs/反汇编地址口径勘误-2026-10-28.md` §五）。

用法：
  python reverse/resolve_got.py 0x27eb7d8            # 解该函数内所有 blr 站点
  python reverse/resolve_got.py 0x27eb7d8 --ins 160  # 加大扫描窗口
  python reverse/resolve_got.py --slot 0x517f2d8     # 直接解某个槽位
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

import capstone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from elf_va import sections, va_to_off                         # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
SO = ROOT / "reverse" / "out" / "il2cpp" / "lib" / "arm64-v8a" / "libGameCore.so"

R_AARCH64_RELATIVE = 1027
LDR_RE = re.compile(r"^(\w+), \[(\w+), #(0x[0-9a-f]+)\]$")


def build_reloc_map(data: bytes) -> dict[int, int]:
    """{r_offset: addend}，只收 R_AARCH64_RELATIVE（其余需符号表，本 so 已剥离）。"""
    out = {}
    for sec in (".rela.dyn", ".rela.plt"):
        s = sections(data).get(sec)
        if not s:
            continue
        for i in range(s["size"] // 24):
            o = s["off"] + i * 24
            r_off = int.from_bytes(data[o:o + 8], "little")
            r_info = int.from_bytes(data[o + 8:o + 16], "little")
            r_add = int.from_bytes(data[o + 16:o + 24], "little", signed=True)
            if (r_info & 0xFFFFFFFF) == R_AARCH64_RELATIVE:
                out[r_off] = r_add
    return out


def cstr(data: bytes, va: int, rodata: dict, limit: int = 96) -> str | None:
    if not (rodata["addr"] <= va < rodata["addr"] + rodata["size"]):
        return None
    o = rodata["off"] + (va - rodata["addr"])
    e = data.find(b"\0", o, o + limit)
    if e <= o:
        return None
    s = data[o:e]
    if len(s) >= 3 and all(32 <= c < 127 for c in s):
        return s.decode("ascii")
    return None


def collect_strings(md, data: bytes, start: int, rodata: dict, max_ins: int = 220) -> list[str]:
    """目标函数窗口内 adrp+add 还原出的 .rodata 字符串（去重保序）。"""
    off = va_to_off(data, start)
    if off < 0:
        return []
    insns = list(md.disasm(data[off:off + max_ins * 4], start))
    pages: dict[str, int] = {}
    out: list[str] = []
    for ins in insns:
        m, ops = ins.mnemonic, ins.op_str
        if m == "adrp":
            reg, imm = ops.split(", ")
            pages[reg] = int(imm.lstrip("#"), 16)
        elif m == "add":
            parts = [p.strip() for p in ops.split(",")]
            if len(parts) == 3 and parts[1] in pages and parts[2].startswith("#"):
                s = cstr(data, pages[parts[1]] + int(parts[2][1:], 0), rodata)
                if s and s not in out:
                    out.append(s)
        if m in ("ret",) and ins.address > start + 4 and len(out) >= 2:
            break
    return out


def find_blr_sites(md, data: bytes, func_va: int, max_ins: int) -> list[dict]:
    """在函数窗口内找 (adrp page → ldr [page+off] → blr reg) 三元组。"""
    off = va_to_off(data, func_va)
    if off < 0:
        return []
    insns = list(md.disasm(data[off:off + max_ins * 4], func_va))
    pages: dict[str, int] = {}
    slots: dict[str, int] = {}
    out = []
    for ins in insns:
        m, ops = ins.mnemonic, ins.op_str
        if m == "adrp":
            reg, imm = ops.split(", ")
            pages[reg] = int(imm.lstrip("#"), 16)
        elif m == "ldr":
            mm = LDR_RE.match(ops)
            if mm:
                reg, base, disp = mm.group(1), mm.group(2), int(mm.group(3), 0)
                if base in pages:
                    slots[reg] = pages[base] + disp
        elif m == "blr":
            reg = ops.strip()
            if reg in slots:
                out.append({"site": ins.address, "slot": slots[reg]})
            else:
                out.append({"site": ins.address, "slot": None})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("func", nargs="?", help="函数 VA（十六进制）")
    ap.add_argument("--slot", help="直接解析某个槽位 VA")
    ap.add_argument("--ins", type=int, default=120, help="扫描指令数（默认 120）")
    ap.add_argument("--depth", type=int, default=220, help="目标函数反汇编指令数")
    args = ap.parse_args()

    data = SO.read_bytes()
    secs = sections(data)
    rodata = secs[".rodata"]
    text = secs[".text"]
    relocs = build_reloc_map(data)
    md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
    md.skipdata = True

    def report(slot: int, site: int | None) -> None:
        tgt = relocs.get(slot)
        where = f"site 0x{site:x}" if site else "slot"
        if tgt is None:
            print(f"  {where}: slot 0x{slot:x} → 无 R_AARCH64_RELATIVE 重定位（可能是外部导入）")
            return
        in_text = text["addr"] <= tgt < text["addr"] + text["size"]
        print(f"  {where}: slot 0x{slot:x} → target 0x{tgt:x}"
              f"{' [.text]' if in_text else ' [非 .text]'}")
        if in_text:
            for s in collect_strings(md, data, tgt, rodata, args.depth)[:12]:
                print(f"      · {s}")

    if args.slot:
        report(int(args.slot, 16), None)
        return 0

    if not args.func:
        ap.print_help()
        return 2
    func = int(args.func, 16)
    print(f"[func] 0x{func:x} 扫描 {args.ins} 条指令")
    sites = find_blr_sites(md, data, func, args.ins)
    if not sites:
        print("  未发现 blr 站点")
        return 0
    for s in sites:
        if s["slot"] is None:
            print(f"  site 0x{s['site']:x}: blr 来源未识别（寄存器来自参数或返回值）")
        else:
            report(s["slot"], s["site"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
