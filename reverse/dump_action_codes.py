# -*- coding: utf-8 -*-
"""抽取官方「动作标签串 → 动作码」对照表。

背景：C 组规则落地前唯一的硬缺口是**官方动作码与我们 label 体系无对照表**。
反汇编发现 `libGameCore.so` 里有一个标签→码的转换函数（`0x4a4a6b0` 起）：
对候选标签串按**长度递增**做前缀 `strncmp`，命中后 `mov w8, #<code>` 并跳到公共出口。

    0x4a4a730  adrp x1, #0x11d7000
    0x4a4a738  add  x1, x1, #0xdca        ; "AtkNorm"
    0x4a4a740  bl   #0x4b400d0           ; strncmp
    0x4a4a744  cbnz w0, <next>
    0x4a4a748  cmp  x21, #6
    0x4a4a74c  b.ls <next>
    0x4a4a750  mov  w8, #1               ; ← 动作码 1
    0x04a4a754  b    #0x4a4aac0

本工具把这类 (label, code) 对全量抽出来，输出 TSV。

用法：
  python reverse/dump_action_codes.py
  python reverse/dump_action_codes.py --func 0x4a4a6b0 --window 400
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

import capstone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from elf_va import sections, va_to_off                         # noqa: E402
from resolve_got import cstr                                   # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
SO = ROOT / "reverse" / "out" / "il2cpp" / "lib" / "arm64-v8a" / "libGameCore.so"
DEFAULT_OUT = ROOT / "reverse" / "out" / "diag" / "action_codes.tsv"
DEFAULT_FUNC = 0x4A4A6B0

LABEL_RE = re.compile(r"^(Atk|NoneAction|Move|Grid|Skill|Defend|Return|Recall)")
JOIN_RE = re.compile(r"^w8, \[x20, #0x1c\]$")


def ops_join(op_str: str) -> bool:
    """是否落在本函数的公共出口 `str w8, [x20, #0x1c]`（动作码写回结构体）。"""
    return bool(JOIN_RE.match(op_str.strip()))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--func", default=hex(DEFAULT_FUNC), help="转换函数 VA")
    ap.add_argument("--window", type=int, default=400, help="反汇编指令数")
    ap.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    func = int(args.func, 16)
    data = SO.read_bytes()
    rodata = sections(data)[".rodata"]
    off = va_to_off(data, func)
    if off < 0:
        print(f"[ERR] 0x{func:x} 不在 PT_LOAD 内", file=sys.stderr)
        return 1

    md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
    md.skipdata = True
    insns = list(md.disasm(data[off:off + args.window * 4], func))

    pages: dict[str, int] = {}
    cur_label: str | None = None
    pairs: list[tuple[str, int, int]] = []       # (label, code, code_addr)
    for i, ins in enumerate(insns):
        m, ops = ins.mnemonic, ins.op_str
        if m == "adrp":
            reg, imm = ops.split(", ")
            pages[reg] = int(imm.lstrip("#"), 16)
        elif m == "add":
            parts = [p.strip() for p in ops.split(",")]
            if len(parts) == 3 and parts[1] in pages and parts[2].startswith("#"):
                s = cstr(data, pages[parts[1]] + int(parts[2][1:], 0), rodata)
                if s and LABEL_RE.match(s):
                    cur_label = s
        elif m == "mov" and ops.startswith("w8, #") and cur_label:
            # 命中分支有两种收尾：`b <公共出口>`（长度分组内的 strncmp）
            # 或直接落到公共出口 `str w8, [x20, #0x1c]`（最后一个 strcmp 分支）
            nxt = insns[i + 1] if i + 1 < len(insns) else None
            if nxt is not None and (nxt.mnemonic == "b"
                                    or (nxt.mnemonic == "str"
                                        and ops_join(nxt.op_str))):
                pairs.append((cur_label, int(ops.split("#")[1], 0), ins.address))
                cur_label = None

    # 去重（同一标签可能出现在多个分支里，取首次出现）
    seen: dict[str, int] = {}
    for lab, code, _ in pairs:
        seen.setdefault(lab, code)

    print(f"[func] 0x{func:x}  {len(insns)} 条指令  抽出 {len(pairs)} 处，去重 {len(seen)} 个标签")
    for lab, code in sorted(seen.items(), key=lambda kv: kv[1]):
        print(f"  {code:>3} (0x{code:02x})  {lab}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="\n") as f:
        f.write("code_hex\tcode_dec\tlabel\n")
        for lab, code in sorted(seen.items(), key=lambda kv: kv[1]):
            f.write(f"0x{code:x}\t{code}\t{lab}\n")
    print(f"[out] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
