# -*- coding: utf-8 -*-
"""核对 RULES_TRIAGE「待做」里的规则，有多少其实已经在 rules.py 里实现了。

待做表由 `reverse/gen_rules_triage.py` 从注册表生成，**不会随代码更新**：
rules.py 新增了 `r_xxx` 之后，表里那一行仍留在「待做」。

比对口径（按可靠性排序）：
  1. **地址**：rules.py 的 docstring 都写 `RuleTactics::Xxx @0x027e....`，与待做表地址比对；
  2. 名字：CamelCase / snake_case 归一后比对（次选，因为存在近义词改写，
     例如 wait_resurrection_armor 实现成 r_wait_revive_armor）。
"""
from __future__ import annotations

import pathlib
import re

WG = pathlib.Path(__file__).resolve().parents[1]      # 本脚本在 WZRYagnt/scripts/ 下
doc = (WG / "docs" / "RULES_TRIAGE.md").read_text(encoding="utf-8")
src = (WG / "wzry" / "decision" / "official_ai" / "rules.py").read_text(encoding="utf-8")
orl = (WG / "wzry" / "decision" / "official_rules.py").read_text(encoding="utf-8")

todo_part = doc.split("## 二、待做", 1)[1]
row = r"\|\s*(\d+)\s*\|\s*\[(\d+)\]\s*\|\s*(0x[0-9a-fA-F]+)\s*\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|"
todo = [{"idx": int(m.group(1)), "addr": m.group(3).lower(),
         "ins": int(m.group(4)), "label": m.group(5).strip()}
        for m in re.finditer(row, todo_part)]

# ⚠️ 地址只能从 **Rule(...) 注册行**取：全文 grep 会把「注释里提到过这个地址」
#   也当成已实现（实测 StayInSpring @0x027f2a5c 只在常量注释里出现，根本没注册）。
#   名字同理：`r_use_chuansong_skill` 对应的是 0x027f3f00，与待做表的 0x027f3ea4 不是同一条。
rule_lines = "\n".join(l for l in src.splitlines() if re.match(r"\s*Rule\(", l))
impl_addrs = {int(a, 16) for a in re.findall(r"0x0?([0-9a-fA-F]{6,9})", rule_lines)}

# 名字命中也要求**地址一致**：注册行可能跨行（addr= 在下一行），
# 且同一个 r_xxx 可能被别名的规则复用（实测 r_use_chuansong_skill -> 0x027f3f00，
# 与待做表的 0x027f3ea4 不是同一条）。
src_lines = src.splitlines()
fn_addr: dict[str, int] = {}
for i, line in enumerate(src_lines):
    if not re.match(r"\s*Rule\(", line):
        continue
    m = re.search(r"Rule\(\s*\"[^\"]+\"\s*,\s*r_([a-z0-9_]+)", line)
    if not m:
        continue
    blob = line + " " + (src_lines[i + 1] if i + 1 < len(src_lines) else "")
    a = re.search(r"\"0x0?([0-9a-fA-F]{6,9})\"", blob)
    if a:
        fn_addr.setdefault(m.group(1), int(a.group(1), 16))

orl_funcs = set(re.findall(r"^def ([a-z0-9_]+)\(", orl, re.M))
doc_names = set(re.findall(r"RuleTactics::([A-Za-z0-9_]+)", src))


def snake(label: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", label)
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", s)
    return s.strip("_").lower()


implemented, missing, name_only = [], [], []
for t in todo:
    sn = snake(t["label"])
    addr = int(t["addr"], 16)
    if addr in impl_addrs:
        implemented.append((t, "addr"))
    elif sn in fn_addr:
        # 同名但地址不同 -> 不是同一条规则，归入待确认
        (implemented if fn_addr[sn] == addr else name_only).append(
            (t, "name" if fn_addr[sn] == addr else f"name@{hex(fn_addr[sn])}"))
    else:
        missing.append((t, ""))

print(f"待做表: {len(todo)} 条")
print(f"已实现（地址一致）: {len(implemented)} 条")
for t, how in implemented:
    print(f"  #{t['idx']:<3} {t['addr']}  {t['label']}  [{how}]")
if name_only:
    print(f"同名但地址不同（待人工确认，暂计未实现）: {len(name_only)} 条")
    for t, how in name_only:
        print(f"  #{t['idx']:<3} {t['addr']}  {t['label']}  [{how}]")
print()
print(f"**真实未实现: {len(missing) + len(name_only)} 条**")
print()
print("最短的 12 条：")
for t, how in sorted(missing + name_only, key=lambda x: x[0]["ins"])[:12]:
    print(f"  #{t['idx']:<3} [{t['ins']:>3}条] {t['addr']}  {t['label']}")
