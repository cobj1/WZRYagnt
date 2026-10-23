# -*- coding: utf-8 -*-
"""规则函数常量提取：把"判据相关"的指令筛出来，便于人工读阈值。

不打印全部指令（几百条看不清），只留：
  - `cmp` / `cmn` / `subs`（带立即数）
  - `tbz` / `tbnz` / `b.<cond>`（分支条件）
  - 浮点比较 `fcmp` 与紧邻的 `fmov` 立即数
  - 通过 `adrp+ldr` 从 .rodata 取到的**浮点字面量**（解析成实际数值）

用法：
  D:/app/python/python.exe reverse/rule_consts.py 0x27dcc5c 0x27df040 0x27e766c
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM

ROOT = Path(__file__).resolve().parents[1]
SO = ROOT / "reverse" / "out" / "il2cpp" / "lib" / "arm64-v8a" / "libGameCore.so"
MAX_INS = 420


def _load():
    data = SO.read_bytes()
    md = Cs(CS_ARCH_ARM64, CS_MODE_ARM)
    md.detail = True
    return data, md


def _va_to_off(data: bytes, va: int) -> int:
    """ELF 里 VA -> 文件偏移（用 program headers 的 PT_LOAD 映射）。"""
    e_phoff = struct.unpack_from("<Q", data, 0x20)[0]
    e_phentsize = struct.unpack_from("<H", data, 0x36)[0]
    e_phnum = struct.unpack_from("<H", data, 0x38)[0]
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        p_type = struct.unpack_from("<I", data, off)[0]
        if p_type != 1:                     # PT_LOAD
            continue
        p_offset = struct.unpack_from("<Q", data, off + 0x08)[0]
        p_vaddr = struct.unpack_from("<Q", data, off + 0x10)[0]
        p_filesz = struct.unpack_from("<Q", data, off + 0x20)[0]
        if p_vaddr <= va < p_vaddr + p_filesz:
            return p_offset + (va - p_vaddr)
    return -1


def dump(data, md, addr: int, limit: int = MAX_INS):
    off = _va_to_off(data, addr)
    if off < 0:
        print(f"  无法映射地址 0x{addr:x}")
        return
    code = data[off: off + limit * 4]
    page_base, page_lit = None, {}
    for ins in md.disasm(code, addr):
        m = ins.mnemonic
        ops = ins.op_str
        interesting = False
        note = ""
        if m in ("cmp", "cmn", "subs", "adds", "ccmp") and "#" in ops:
            interesting = True
        elif m in ("tbz", "tbnz", "cbz", "cbnz"):
            interesting = True
        elif m.startswith("b."):
            interesting = True
        elif m == "fcmp":
            interesting = True
        elif m == "fmov" and "#" in ops:
            interesting = True
        elif m == "adrp":
            try:
                page_base = int(ops.split(",")[1].strip().lstrip("#"), 16)
            except Exception:
                page_base = None
        elif m == "ldr" and page_base is not None and "[x" in ops and "#" in ops:
            # 解析 ldr s0, [x8, #0x123]  -> 读 .rodata 里的 float
            try:
                reg = ops.split(",")[0].strip()
                imm = int(ops.split("#")[-1].rstrip("]"), 16)
                voff = _va_to_off(data, page_base + imm)
                if voff > 0 and reg.startswith("s"):
                    val = struct.unpack_from("<f", data, voff)[0]
                    if -1e6 < val < 1e6 and abs(val) > 1e-6:
                        interesting = True
                        note = f"   ; float={val:.6g}"
                elif voff > 0 and reg.startswith(("w", "x")):
                    iv = struct.unpack_from("<i", data, voff)[0]
                    if abs(iv) < 10_000_000:
                        interesting = True
                        note = f"   ; int={iv}"
            except Exception:
                pass
        if interesting:
            print(f"  0x{ins.address:08x}  {m:<7} {ops}{note}")
        if m == "ret":
            break


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    data, md = _load()
    for a in sys.argv[1:]:
        addr = int(a, 16)
        print(f"\n===== 0x{addr:x} =====")
        dump(data, md, addr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
