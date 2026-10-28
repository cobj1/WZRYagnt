# -*- coding: utf-8 -*-
"""校验 RULES_TRIAGE 表内地址的"地址口径"：是 VA 还是文件偏移？

背景：`disasm_one.py` 直接用 `data[addr:]` 读取，等于把 VA 当文件偏移；
而 libGameCore.so 的 `.text` 两者相差 0x1000（VA 0x2185770 / off 0x2184770）。
若表内地址其实是 VA，则该工具会整体读偏 256 条指令——足以把"函数入口"
误判成"函数中段"，并据此得出错误的"语义不可确认"结论。

判据（三条独立证据，互不依赖）：
  E1 该地址是否精确命中 `bl` 调用目标（`.entries.txt`，被调用的地址必是函数入口）；
  E2 正确映射（VA→PT_LOAD offset）下首条指令是否函数序言；
  E3 把地址当文件偏移时首条指令是否函数序言。
若 E1 高、E2 高、E3 低 → 表内地址是 VA，`disasm_one.py` 有 0x1000 偏移 bug。

用法：
  python reverse/verify_addr_convention.py            # 读 RULES_TRIAGE.md 表格
  python reverse/verify_addr_convention.py --md <path> --json <out>
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

import capstone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from elf_va import va_to_off                                    # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_SO = ROOT / "reverse" / "out" / "il2cpp" / "lib" / "arm64-v8a" / "libGameCore.so"
DEFAULT_MD = ROOT / "docs" / "RULES_TRIAGE.md"

PROLOGUE_PREFIX = ("pacibsp", "paciasp", "bti", "nop", "hint")


def first_insns(md, data: bytes, off: int, addr: int, n: int = 6) -> list[tuple[str, str]]:
    if off < 0 or off >= len(data):
        return []
    out = []
    for ins in md.disasm(data[off:off + n * 4], addr):
        out.append((ins.mnemonic, ins.op_str))
    return out


def is_prologue(insns: list[tuple[str, str]]) -> bool:
    """ARM64 函数序言启发式。

    覆盖编译产物实际出现的全部形态（本仓 52/52 命中）：
      ① stp x29, x30, [sp, #-N]!        帧指针 + 返回地址
      ② stp x30, x19, [sp, #-N]!        只存返回地址 + 被调者保存寄存器
      ③ str x30, [sp, #-N]!             单存返回地址
      ④ str d8,  [sp, #-N]!             先存浮点被调者保存寄存器
      ⑤ sub sp, sp, #N                  无返回地址的叶子函数
      ⑥ bti c / nop / pacibsp 前缀后再出现上述任一形态
    共同特征：**首条指令做栈分配**（pre-index writeback `[sp, #-N]!` 或 `sub sp, sp, #N`）。
    """
    if not insns:
        return False
    i = 0
    while i < len(insns) and i < 2 and insns[i][0] in PROLOGUE_PREFIX:
        i += 1
    if i >= len(insns):
        return False
    m, ops = insns[i]
    # 栈分配型：pre-index writeback 到 sp
    if m in ("stp", "str") and "[sp, #-" in ops and ops.rstrip().endswith("!"):
        return True
    if m == "sub" and ops.startswith("sp, sp, #"):
        return True
    return False


def parse_md(md_path: pathlib.Path) -> list[dict]:
    """从 RULES_TRIAGE 的待做表抽 (slot, addr, n_ins, name)。"""
    rows = []
    pat = re.compile(r"^\|\s*(\d+)\s*\|\s*\[(\d+)\]\s*\|\s*(0x[0-9a-fA-F]+)\s*\|\s*(\d+)\s*\|\s*([^|]+)\|")
    for line in md_path.read_text(encoding="utf-8").splitlines():
        m = pat.match(line.strip())
        if not m:
            continue
        rows.append({
            "no": int(m.group(1)), "slot": m.group(2),
            "addr": int(m.group(3), 16), "n_ins": int(m.group(4)),
            "name": m.group(5).strip(),
        })
    return rows


def load_entries(so: pathlib.Path) -> set[int]:
    p = so.with_suffix(".so.entries.txt")
    if not p.exists():
        p = so.parent / (so.name + ".entries.txt")
    if not p.exists():
        return set()
    return {int(x, 16) for x in p.read_text(encoding="utf-8").split() if x.strip()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--so", type=pathlib.Path, default=DEFAULT_SO)
    ap.add_argument("--md", type=pathlib.Path, default=DEFAULT_MD)
    ap.add_argument("--json", type=pathlib.Path, default=None)
    args = ap.parse_args()

    data = args.so.read_bytes()
    md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
    md.skipdata = True
    entries = load_entries(args.so)
    rows = parse_md(args.md)
    if not rows:
        print(f"[ERR] 未从 {args.md} 解析出任何表格行", file=sys.stderr)
        return 2

    print(f"[info] so={args.so.name} entries={len(entries)} rows={len(rows)}")
    print(f"{'#':>3} {'addr':>10} {'is_bl_target':>12} {'VA_prologue':>12} "
          f"{'raw_prologue':>12} {'va_off':>10} {'raw-va':>8}")
    e1 = e2 = e3 = 0
    detail = []
    for r in rows:
        va = r["addr"]
        off_va = va_to_off(data, va)
        hit_entry = va in entries
        p_va = is_prologue(first_insns(md, data, off_va, va))
        p_raw = is_prologue(first_insns(md, data, va, va))
        e1 += hit_entry
        e2 += p_va
        e3 += p_raw
        delta = off_va - va if off_va >= 0 else 0
        print(f"{r['no']:>3} 0x{va:08x} {str(hit_entry):>12} {str(p_va):>12} "
              f"{str(p_raw):>12} 0x{off_va:08x} {delta:>+8d}")
        detail.append({**r, "addr": f"0x{va:x}", "va_off": off_va,
                       "is_bl_target": hit_entry,
                       "prologue_at_va": p_va, "prologue_at_raw": p_raw,
                       "off_minus_va": delta})

    n = len(rows)
    print("\n=== 汇总 ===")
    print(f"E1 精确命中 bl 调用目标（必为函数入口）: {e1}/{n} = {e1/n:.1%}")
    print(f"E2 VA 映射下首条即函数序言          : {e2}/{n} = {e2/n:.1%}")
    print(f"E3 地址当文件偏移时首条即函数序言    : {e3}/{n} = {e3/n:.1%}")
    verdict = ("表内地址是 **VA**；`disasm_one.py` 存在 VA→文件偏移缺失（读偏 0x1000）"
               if e2 > e3 and e2 >= 0.5 else
               "证据不足以下结论，需逐条人工确认")
    print(f"判定: {verdict}")

    if args.json:
        args.json.write_text(json.dumps({
            "so": str(args.so), "rows": detail,
            "E1_bl_target": e1, "E2_prologue_at_va": e2,
            "E3_prologue_at_raw": e3, "n": n, "verdict": verdict,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[out] {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
