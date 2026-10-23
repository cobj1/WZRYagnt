# -*- coding: utf-8 -*-
"""Dump 金币差分候选地址周边，定位承载结构与英雄字段。

用法：
  venv/Scripts/python.exe -X utf8 reverse/dump_gold_candidates.py \
      --addrs 0x724c547e9b3c,0x724c547e9d20,0x724c6083d3a0,0x7250fe584ebc
"""
import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from livemem import LiveMem  # noqa: E402


def is_ptr(v):
    return 0x1000000000 < v < 0x800000000000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addrs", required=True)
    ap.add_argument("--before", type=lambda s: int(s, 0), default=0x80)
    ap.add_argument("--after", type=lambda s: int(s, 0), default=0x200)
    a = ap.parse_args()
    cands = [int(x, 0) for x in a.addrs.split(",") if x.strip()]

    m = LiveMem()
    m.find_pid()
    m.refresh_maps()
    print(f"pid={m.pid}")
    for c in cands:
        raw = m.read(c - a.before, a.before + a.after)
        print(f"\n===== 0x{c:x}  read={len(raw)}")
        for off in range(0, len(raw) - 4, 4):
            addr = c - a.before + off
            u = struct.unpack_from("<I", raw, off)[0]
            i = struct.unpack_from("<i", raw, off)[0]
            f = struct.unpack_from("<f", raw, off)[0]
            marks = []
            if addr == c:
                marks.append("GOLD")
            if u == 106:
                marks.append("ID106")
            if 1 <= u <= 12:
                marks.append("lvl?")
            if 1500 <= u <= 3500:
                marks.append("econ?")
            if off % 8 == 0 and off + 8 <= len(raw):
                if is_ptr(struct.unpack_from("<Q", raw, off)[0]):
                    marks.append("ptr?")
            if not marks and u == 0:
                continue
            fs = f"{f:.2f}" if 0.001 < abs(f) < 1e6 else ""
            print(f"  +0x{addr - c:+05x} 0x{addr:x} u32={u:<12} i32={i:<12} "
                  f"f32={fs:<12} {' '.join(marks)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
