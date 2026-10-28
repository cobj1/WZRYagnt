# -*- coding: utf-8 -*-
"""反汇编指定地址起的 N 条指令（RULES_TRIAGE 逐条确认用）。

比 rule_consts.py 更原始：不做筛选，直接按顺序打印，便于判断寄存器语义。

⚠️ 地址口径（2026-10-28 修正，务必先看）
  RULES_TRIAGE 表内地址是 **VA**（虚拟地址），不是文件偏移。
  libGameCore.so 的 `.text` 二者相差 0x1000（VA 0x2185770 / off 0x2184770），
  早期版本直接把 VA 当文件偏移读，**整体读偏 0x1000 = 256 条指令**，
  足以把函数入口误判成"函数中段"。已改为走 PT_LOAD 映射。
  校验脚本：`reverse/verify_addr_convention.py`（52/52 条地址在 VA 映射下精确落在函数序言）。

用法：
  python reverse/disasm_one.py 0x27ee08c 30
  python reverse/disasm_one.py 0x27ee08c 30 --prologue   # 额外回显函数序言判定
"""
from __future__ import annotations

import pathlib
import sys

from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from elf_va import va_to_off                                    # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]      # 本脚本在 WZRYagnt/reverse/ 下
SO = ROOT / "reverse" / "out" / "il2cpp" / "lib" / "arm64-v8a" / "libGameCore.so"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    addr = int(sys.argv[1], 16)
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    want_prologue = "--prologue" in sys.argv

    data = SO.read_bytes()
    off = va_to_off(data, addr)
    if off < 0:
        print(f"[ERR] VA 0x{addr:x} 不在任何 PT_LOAD 段内", file=sys.stderr)
        return 1
    if off != addr:
        print(f"[info] VA 0x{addr:x} -> file off 0x{off:x} (差 {off - addr:+d})")

    md = Cs(CS_ARCH_ARM64, CS_MODE_ARM)
    md.skipdata = True
    code = data[off:off + n * 4]
    insns = list(md.disasm(code, addr))
    for ins in insns:
        print(f"0x{ins.address:08x}  {ins.mnemonic:<8s} {ins.op_str}")

    if want_prologue:
        head = [(i.mnemonic, i.op_str) for i in insns[:2]]
        stack_alloc = any(
            (m in ("stp", "str") and "[sp, #-" in o and o.rstrip().endswith("!"))
            or (m == "sub" and o.startswith("sp, sp, #"))
            for m, o in head
        )
        print(f"\n[prologue] {'YES —— 该地址是函数入口' if stack_alloc else 'NO —— 可能位于函数中段'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
