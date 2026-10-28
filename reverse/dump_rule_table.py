# -*- coding: utf-8 -*-
"""从 `.rela.dyn` 重建 RuleTactics 规则函数表（地址 + 名字 + 引用串）。

原理：`.data` 里的函数指针由 `R_AARCH64_RELATIVE` 重定位在加载时填充，
addend 就是真实函数 VA。规则函数集中在 `0x27c0000–0x2800000`，
统计该区间的重定位即可得到**完整的规则函数表**（不依赖任何手工登记表）。

每个函数再反汇编一段窗口，抽它引用的 `.rodata` 字符串 —— 其中 `rule_*` 前缀的
就是官方规则名（`RULES_TRIAGE.md` 的 tags 列即由此而来），其余是判据/日志线索。

输出 TSV：`slot_va  func_va  rule_name  other_strings`

用法：
  python reverse/dump_rule_table.py                       # 全表 → out/diag/rule_table_reloc.tsv
  python reverse/dump_rule_table.py --out x.tsv --window 300
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

import capstone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from elf_va import sections, va_to_off                         # noqa: E402
from resolve_got import build_reloc_map, collect_strings, cstr  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
SO = ROOT / "reverse" / "out" / "il2cpp" / "lib" / "arm64-v8a" / "libGameCore.so"
DEFAULT_OUT = ROOT / "reverse" / "out" / "diag" / "rule_table_reloc.tsv"

RULE_LO, RULE_HI = 0x27C0000, 0x2800000
RULE_NAME_RE = re.compile(r"^rule_|^RuleTactics::")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    ap.add_argument("--window", type=int, default=260, help="每函数反汇编指令数")
    args = ap.parse_args()

    data = SO.read_bytes()
    secs = sections(data)
    rodata = secs[".rodata"]
    text = secs[".text"]
    rel = build_reloc_map(data)
    md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
    md.skipdata = True

    entries = sorted((o, t) for o, t in rel.items()
                     if RULE_LO <= t < RULE_HI)
    print(f"[info] 指向规则区的重定位 {len(entries)} 条；"
          f"槽位区间 0x{entries[0][0]:x}–0x{entries[-1][0]:x}")

    rows = []
    for slot, func in entries:
        if not (text["addr"] <= func < text["addr"] + text["size"]):
            continue
        strs = collect_strings(md, data, func, rodata, args.window)
        names = [s for s in strs if RULE_NAME_RE.match(s)]
        others = [s for s in strs if s not in names]
        rows.append({
            "slot": slot, "func": func,
            "name": names[0] if names else "",
            "others": " | ".join(others[:6]),
        })

    named = sum(1 for r in rows if r["name"])
    print(f"[info] 函数 {len(rows)} 个，其中带规则名 {named} 个")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="\n") as f:
        f.write("slot_va\tfunc_va\trule_name\tother_strings\n")
        for r in rows:
            f.write(f"0x{r['slot']:x}\t0x{r['func']:x}\t{r['name']}\t{r['others']}\n")
    print(f"[out] {args.out}")

    # 与 RULES_TRIAGE 对账：表内地址有多少出现在本表里
    triage = ROOT / "docs" / "RULES_TRIAGE.md"
    if triage.exists():
        pat = re.compile(r"^\|\s*(\d+)\s*\|\s*\[(\d+)\]\s*\|\s*(0x[0-9a-fA-F]+)\s*\|")
        want = {}
        for line in triage.read_text(encoding="utf-8").splitlines():
            m = pat.match(line.strip())
            if m:
                want[int(m.group(3), 16)] = (int(m.group(1)), m.group(2))
        have = {r["func"] for r in rows}
        hit = [a for a in want if a in have]
        print(f"[对账] RULES_TRIAGE {len(want)} 条 → 本表命中 {len(hit)} 条")
        miss = sorted(set(want) - set(hit))
        if miss:
            print(f"       未命中 {len(miss)} 条（可能是子函数或不在规则区）："
                  + ", ".join(hex(a) for a in miss[:8]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
