# -*- coding: utf-8 -*-
"""从 ELF/二进制中抽取可打印字符串，供后续按关键字检索。

用途：判断王者荣耀的战斗决策（走位/锁敌/技能时机）到底落在 C#(IL2CPP) 层
还是原生战斗引擎 libGameCore.so 里。产出 <name>.strings.txt。
"""
import re
import sys
from pathlib import Path

KEYWORDS = ("AI", "Bot", "Robot", "JueWu", "Behavior", "Behavio", "Offline",
            "Commander", "Hero", "Skill", "Tactic", "Target", "Blackboard",
            "Decision", "State", "Path", "Move")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python so_strings.py <binary> [out.txt]")
        return 2
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(src.suffix + ".strings.txt")
    data = src.read_bytes()
    raw = re.findall(rb"[\x20-\x7e]{6,300}", data)
    uniq = sorted({s.decode("ascii") for s in raw})
    dst.write_text("\n".join(uniq), encoding="utf-8", newline="\n")
    print(f"file     : {src.name}")
    print(f"size     : {len(data)}")
    print(f"strings  : {len(raw)} occurrences / {len(uniq)} unique")
    print(f"written  : {dst}")
    low = [(s, s.lower()) for s in uniq]
    print("--- keyword counts ---")
    for k in KEYWORDS:
        kl = k.lower()
        print(f"{k:>12}: {sum(1 for _, l in low if kl in l)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
