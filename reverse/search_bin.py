# -*- coding: utf-8 -*-
"""在二进制中按任意关键字列表搜索，统计出现次数并给出前若干偏移。

用法：python search_bin.py <file> <kw1> [kw2 ...]
"""
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: python search_bin.py <file> <kw1> [kw2 ...]")
        return 2
    p = Path(sys.argv[1])
    data = p.read_bytes()
    print(f"{p.name} size={len(data)}")
    for kw in sys.argv[2:]:
        pat = kw.encode("ascii")
        total, first, pos = 0, [], 0
        while True:
            pos = data.find(pat, pos)
            if pos < 0:
                break
            total += 1
            if len(first) < 5:
                first.append(pos)
            pos += len(pat)
        print(f"  {kw:<16} count={total}  first={first}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
