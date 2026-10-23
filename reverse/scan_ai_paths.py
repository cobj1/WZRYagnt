# -*- coding: utf-8 -*-
"""枚举容器中出现的 AILab 配置路径，摸清 AI 配置清单范围。"""
import re
import sys
from pathlib import Path

PAT = re.compile(rb"AILab/[A-Za-z0-9_./-]{3,120}")


def main() -> int:
    p = Path(sys.argv[1])
    data = p.read_bytes()
    found = set()
    for m in PAT.finditer(data):
        found.add(m.group(0).decode("ascii").rstrip("."))
    print(f"{p.name}: {len(found)} unique AILab paths")
    for s in sorted(found):
        print("  ", s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
