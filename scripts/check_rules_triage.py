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

impl_addrs = {int(a, 16) for a in re.findall(r"0x[0-9a-fA-F]{6,9}", src)}
done_funcs = set(re.findall(r"^def r_([a-z0-9_]+)\(", src, re.M))
orl_funcs = set(re.findall(r"^def ([a-z0-9_]+)\(", orl, re.M))
doc_names = set(re.findall(r"RuleTactics::([A-Za-z0-9_]+)", src))


def snake(label: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", label)
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", s)
    return s.strip("_").lower()


implemented, missing = [], []
for t in todo:
    sn = snake(t["label"])
    hit = (int(t["addr"], 16) in impl_addrs or sn in done_funcs
           or sn in orl_funcs or t["label"] in doc_names)
    (implemented if hit else missing).append((t, sn))

print(f"待做表: {len(todo)} 条")
print(f"已实现（地址或名字命中）: {len(implemented)} 条")
for t, sn in implemented:
    print(f"  #{t['idx']:<3} {t['addr']}  {t['label']}")
print()
print(f"**真实未实现: {len(missing)} 条**")
print()
print("最短的 12 条：")
for t, sn in sorted(missing, key=lambda x: x[0]["ins"])[:12]:
    print(f"  #{t['idx']:<3} [{t['ins']:>3}条] {t['addr']}  {t['label']}")
