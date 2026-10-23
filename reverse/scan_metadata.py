# -*- coding: utf-8 -*-
"""从 IL2CPP global-metadata.dat 抽取可打印字符串，建立可搜索符号索引。

用途：王者荣耀是 Unity IL2CPP 构建且未随包附带 C# DLL，因此类名/方法名
只能从 global-metadata.dat 的字符串表恢复。本脚本只做字符串抽取（不解析
元数据结构），产出 metadata_strings.txt 供后续按关键字定位 AI/离线对局逻辑。
"""
import re
import sys
from pathlib import Path

KEYWORDS = (
    "AI", "Bot", "Robot", "Offline", "Agent", "Behavi", "Decision", "Strategy",
    "Tactic", "Skill", "Match", "Command", "Nav", "State", "Level", "Surrender",
)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python scan_metadata.py <global-metadata.dat> [out.txt]")
        return 2
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_name("metadata_strings.txt")
    data = src.read_bytes()

    magic = int.from_bytes(data[0:4], "little")
    version = int.from_bytes(data[4:8], "little")
    print(f"file      : {src}")
    print(f"size      : {len(data)} bytes")
    print(f"magic     : 0x{magic:08X} (expect 0xFAB11BAF)")
    print(f"version   : {version}")

    raw = re.findall(rb"[\x20-\x7e]{4,240}", data)
    uniq = sorted({s.decode("ascii") for s in raw})
    print(f"strings   : {len(raw)} occurrences / {len(uniq)} unique")

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(uniq), encoding="utf-8")
    print(f"written   : {dst}")

    low = [(s, s.lower()) for s in uniq]
    print("--- keyword hits (unique) ---")
    for kw in KEYWORDS:
        k = kw.lower()
        hits = [s for s, l in low if k in l]
        print(f"{kw:>10}: {len(hits)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
