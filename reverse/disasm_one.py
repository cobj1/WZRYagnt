# -*- coding: utf-8 -*-
"""反汇编指定地址起的 N 条指令（RULES_TRIAGE 逐条确认用）。

比 rule_consts.py 更原始：不做筛选，直接按顺序打印，便于判断寄存器语义。
用法：python temp/disasm_one.py 0x27ee08c 30
"""
from __future__ import annotations

import pathlib
import sys

from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM

ROOT = pathlib.Path(__file__).resolve().parents[1]      # 本脚本在 WZRYagnt/reverse/ 下
SO = ROOT / "reverse" / "out" / "il2cpp" / "lib" / "arm64-v8a" / "libGameCore.so"

addr = int(sys.argv[1], 16)
n = int(sys.argv[2]) if len(sys.argv) > 2 else 30

data = SO.read_bytes()
md = Cs(CS_ARCH_ARM64, CS_MODE_ARM)
code = data[addr:addr + n * 4]
for ins in md.disasm(code, addr):
    print(f"0x{ins.address:08x}  {ins.mnemonic:<8s} {ins.op_str}")
