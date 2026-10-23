# -*- coding: utf-8 -*-
"""转储二进制文件指定区间的十六进制与可打印字符串。

用途：在资源容器中命中关键字后，查看该偏移附近的字符串表/路径清单。
"""
import re
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 4:
        print("usage: python ctx_dump.py <file> <offset> <length>")
        return 2
    p = Path(sys.argv[1])
    off = int(sys.argv[2], 0)
    ln = int(sys.argv[3], 0)
    data = p.read_bytes()
    win = data[off:off + ln]
    print(f"{p.name} offset={off} len={len(win)}")
    print("--- printable strings (>=4) ---")
    for m in re.finditer(rb"[\x20-\x7e]{4,200}", win):
        print(f"  +{m.start():>6}  {m.group(0).decode('ascii')}")
    print("--- hex ---")
    for i in range(0, len(win), 32):
        chunk = win[i:i + 32]
        hx = " ".join(f"{b:02x}" for b in chunk)
        tx = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"{off + i:08x}  {hx:<96}  {tx}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
