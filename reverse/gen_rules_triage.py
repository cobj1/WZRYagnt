# -*- coding: utf-8 -*-
"""规则长尾的分诊表：把 141 条具名规则按"实现状态 + 反汇编难度"排开。

数据源全部是已有产物，不重新扫 .so：
  `reverse/out/il2cpp/rules_table.tsv`（slot / addr / ins / tags / consts / floats / mem_offsets）

产出：`docs/RULES_TRIAGE.md`
  - 已实现：名称、证据等级、地址
  - 待做：按指令数升序（先做短的），带首个日志串与候选常量，便于 `rule_consts.py` 定点取阈值

用法：
  D:/app/python/python.exe reverse/gen_rules_triage.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TABLE = ROOT / "reverse" / "out" / "il2cpp" / "rules_table.tsv"
OUT = ROOT / "docs" / "RULES_TRIAGE.md"

NAME_RE = re.compile(r"(?:RuleTactics::|rule_)([A-Za-z][A-Za-z0-9_]{3,})")


def load_table():
    rows = []
    for line in TABLE.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 6 or not parts[0].startswith("["):
            continue
        slot, addr, ins, tags, consts = parts[0], parts[1], parts[2], parts[3], parts[4]
        name = ""
        for tag in tags.split("|"):
            m = NAME_RE.search(tag)
            if m:
                name = m.group(1)
                break
        rows.append({
            "slot": slot, "addr": addr,
            "ins": int(ins) if ins.isdigit() else 0,
            "name": name,
            "first_tag": (tags.split("|")[0] if tags else "")[:90],
            "consts": (consts.split("|")[:8] if consts else []),
        })
    return rows


def main() -> int:
    from wzry.decision.official_ai import rules as R

    done = {r.name: r for r in R.CHAIN if r.implemented}
    rows = load_table()
    # 去重（同一函数可能被多个槽位引用）+ 丢掉 2~6 条指令的桩函数（不是规则本体）
    seen, uniq = set(), []
    for r in sorted(rows, key=lambda x: -(x["ins"] or 0)):
        if not r["name"] or r["addr"] in seen or r["ins"] < 8:
            continue
        seen.add(r["addr"])
        uniq.append(r)
    rows = uniq
    todo = [r for r in rows if r["name"] not in done]
    todo.sort(key=lambda r: (r["ins"] or 10 ** 6))

    lines = [
        "# 规则长尾分诊表（141 条）",
        "",
        "> 数据源：`reverse/out/il2cpp/rules_table.tsv`（不重新扫 `.so`）。",
        "> 生成：`D:/app/python/python.exe reverse/gen_rules_triage.py`。",
        f"> 现状：已实现 **{len(done)}** 条；表内可识别的具名函数（去重、去桩）**{len(rows)}** 个，"
        f"待做 **{len(todo)}** 条。",
        ">",
        "> 说明：§8.2 的 141 是 RuleTactics 调度表里的具名规则数；本表按"
        "`RuleTactics::` / `rule_` 前缀从 260 行原始注册表里抽，含部分重载与子函数，"
        "所以条数会多于 141 —— 以本表为**工作清单**，以 141 为**对账口径**。",
        "",
        "工作流（每条 10~20 分钟）：",
        "1. `D:/app/python/python.exe reverse/rule_consts.py <addr>` 取判据附近的 `cmp/fcmp/fmov/tbz`；",
        "2. 用日志串确认分支语义（`rules_table.tsv` 的 tags 列就是该函数引用的日志）；",
        "3. 常量写进 `rules.py` 并标 `evidence=\"disasm\"` + `addr`；",
        "4. 在 `test_official_ai_replica.py` 加一条边界测试（阈值两侧各一例）。",
        "",
        "## 一、已实现",
        "",
        "| 规则 | 证据 | 地址 |",
        "| --- | --- | --- |",
    ]
    for r in R.CHAIN:
        if not r.implemented:
            continue
        lines.append(f"| {r.name} | {r.evidence} | {r.addr or '-'} |")
    lines += [
        "",
        "## 二、待做（按指令数升序 = 先做短的）",
        "",
        "| # | slot | 地址 | 指令数 | 规则名/首日志串 | 候选常量（前 8） |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for i, r in enumerate(todo, 1):
        label = r["name"] or r["first_tag"]
        lines.append(f"| {i} | {r['slot']} | {r['addr']} | {r['ins']} | {label} | "
                     f"{' / '.join(r['consts'])} |")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写 {OUT}：已实现 {len(done)}，待做 {len(todo)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
