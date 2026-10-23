# -*- coding: utf-8 -*-
"""在二进制容器中按字节搜索关键字，定位资源在包内的物理偏移。

用途：`.db` 资源容器疑似压缩/加密，先确认 AILab AI 配置是否以明文存在于
容器中；若存在则记录偏移，供后续切片提取。
"""
import sys
from pathlib import Path

KEYWORDS = [b"AILab", b"tactics_config", b"game_ai_model", b"ai_config",
            b"obstacle_config", b"skill_manager_config", b"hero_lane",
            b"Blackboard", b"RuleTactics"]


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python find_bytes.py <file> [file...]")
        return 2
    for name in sys.argv[1:]:
        p = Path(name)
        data = p.read_bytes()
        hits = []
        for kw in KEYWORDS:
            pos = data.find(kw)
            hits.append(f"{kw.decode()}@{pos if pos >= 0 else '-'}")
        print(f"{p.name} ({len(data)} bytes): " + "  ".join(hits))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
