# -*- coding: utf-8 -*-
"""反汇编地址口径回归守卫（防"VA 当文件偏移"复发）。

背景：`reverse/disasm_one.py` 早期版本用 `data[addr:]` 直接读，等于把 VA 当文件偏移。
libGameCore.so 的 `.text` 两者相差 0x1000，于是整体读偏 256 条指令，
把 52 条函数入口全看成"函数中段"，据此得出的"语义不可确认"结论全部作废
（见 `docs/反汇编地址口径勘误-2026-10-28.md`）。

本测试不依赖 capstone（项目 venv 未装），用 ARM64 指令编码掩码判"首条是否做栈分配"：
  ① STP pre-index 64bit : 0xA9800000 掩 0xFFC00000
  ② STR pre-index 64bit : 0xF8000C00 掩 0xFFE00C00
  ③ SUB sp, sp, #imm    : 0xD10003FF 掩 0xFFC003FF

三条断言：
  T1 VA→文件偏移换算正确（.text 差 0x1000）
  T2 表内地址在 VA 映射下 ≥90% 落在函数序言
  T3 把地址当文件偏移时 ≤10% 落在序言（这条专抓"回退到裸偏移读法"）

运行：venv/Scripts/python.exe -X utf8 scripts/test_disasm_addr.py
"""
from __future__ import annotations

import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "reverse"))

SO = ROOT / "reverse" / "out" / "il2cpp" / "lib" / "arm64-v8a" / "libGameCore.so"
TRIAGE = ROOT / "docs" / "RULES_TRIAGE.md"

_PASS, _FAIL, _SKIP = [], [], []


def check(name, cond, extra=""):
    if cond:
        _PASS.append(name)
        print(f"PASS {name}")
    else:
        _FAIL.append(name)
        print(f"FAIL {name} {extra}")


def skip(name, why):
    _SKIP.append(name)
    print(f"SKIP {name} {why}")


def _is_stack_alloc(word: int) -> bool:
    """首条指令是否做栈分配（= 函数序言特征），纯编码掩码，不依赖 capstone。"""
    if (word & 0xFFC00000) == 0xA9800000:      # stp xN, xM, [sp, #-N]!
        return True
    if (word & 0xFFC00000) == 0x6D800000:      # stp dN, dM, [sp, #-N]!
        return True
    if (word & 0xFFE00C00) == 0xF8000C00:      # str xN, [sp, #-N]!
        return True
    if (word & 0xFFE00C00) == 0xFC000C00:      # str dN, [sp, #-N]!
        return True
    if (word & 0xFFC003FF) == 0xD10003FF:      # sub sp, sp, #imm
        return True
    return False


def _parse_triage_addrs() -> list[int]:
    pat = re.compile(r"^\|\s*\d+\s*\|\s*\[\d+\]\s*\|\s*(0x[0-9a-fA-F]+)\s*\|\s*\d+\s*\|")
    out = []
    for line in TRIAGE.read_text(encoding="utf-8").splitlines():
        m = pat.match(line.strip())
        if m:
            out.append(int(m.group(1), 16))
    return out


def main() -> int:
    if not SO.exists():
        skip("all", f"so 不存在：{SO}")
        print("\nSKIP (0 pass / 0 fail)")
        return 0
    data = SO.read_bytes()

    import elf_va                                        # reverse/elf_va.py（无 capstone 依赖）

    # T1：VA → 文件偏移
    secs = _text_section(data)
    check("T1_text_section_mapping",
          secs is not None and secs[0] != secs[1],
          f"text va/off={secs}")
    if secs:
        va, off = secs
        check("T1b_va_to_off_matches_section",
              elf_va.va_to_off(data, va) == off,
              f"va=0x{va:x} expect 0x{off:x} got 0x{elf_va.va_to_off(data, va):x}")
        check("T1c_va_to_off_delta_is_0x1000",
              va - off == 0x1000, f"delta=0x{va - off:x}")

    addrs = _parse_triage_addrs()
    if len(addrs) < 20:
        skip("T2/T3", f"RULES_TRIAGE 只解析出 {len(addrs)} 条地址")
    else:
        hit_va = hit_raw = 0
        miss = []
        for a in addrs:
            off = elf_va.va_to_off(data, a)
            w_va = struct.unpack_from("<I", data, off)[0] if off >= 0 else 0
            w_raw = struct.unpack_from("<I", data, a)[0]
            if _is_stack_alloc(w_va):
                hit_va += 1
            else:
                miss.append(hex(a))
            if _is_stack_alloc(w_raw):
                hit_raw += 1
        n = len(addrs)
        r_va, r_raw = hit_va / n, hit_raw / n
        print(f"  序言命中：VA 映射 {hit_va}/{n}={r_va:.1%}  裸偏移 {hit_raw}/{n}={r_raw:.1%}")
        check("T2_triage_addrs_are_function_entries", r_va >= 0.90,
              f"{r_va:.1%} < 90%；未命中：{miss[:5]}")
        check("T3_raw_offset_is_not_entries", r_raw <= 0.10,
              f"裸偏移命中 {r_raw:.1%} > 10% —— 疑似有人回退到 data[addr:] 读法")

    # T4：工具源码不得再出现裸偏移读法
    src = (ROOT / "reverse" / "disasm_one.py").read_text(encoding="utf-8")
    check("T4_disasm_one_uses_va_to_off",
          "va_to_off" in src and "data[addr:" not in src,
          "disasm_one.py 仍含 data[addr:] 裸偏移读法")
    check("T4b_elf_va_is_shared_module",
          (ROOT / "reverse" / "elf_va.py").exists(),
          "缺少 reverse/elf_va.py（地址换算公共模块）")

    print(f"\n结果: {len(_PASS)} PASS / {len(_FAIL)} FAIL / {len(_SKIP)} SKIP")
    if _FAIL:
        print("失败: " + ", ".join(_FAIL))
        return 1
    print("ALL PASS")
    return 0


def _text_section(data: bytes):
    """.text 的 (vaddr, file offset)；读 ELF 段表。"""
    e_shoff, = struct.unpack_from("<Q", data, 0x28)
    e_shentsize, = struct.unpack_from("<H", data, 0x3A)
    e_shnum, = struct.unpack_from("<H", data, 0x3C)
    e_shstrndx, = struct.unpack_from("<H", data, 0x3E)
    base = struct.unpack_from("<IIQQQQIIQQ", data, e_shoff + e_shstrndx * e_shentsize)[4]
    for i in range(e_shnum):
        o = e_shoff + i * e_shentsize
        nm, t, f, a, off, sz, lk, inf, al, es = struct.unpack_from("<IIQQQQIIQQ", data, o)
        end = data.find(b"\0", base + nm)
        if data[base + nm:end] == b".text":
            return a, off
    return None


if __name__ == "__main__":
    raise SystemExit(main())
