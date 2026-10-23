# -*- coding: utf-8 -*-
"""在候选地址区间里寻找英雄状态表记录（+0x08 = 英雄 config id）。

用法：
  venv/Scripts/python.exe -X utf8 reverse/probe_state_table.py \
      --base 0x724c3d070000 --size 0x40000 --ids 106,167,112
"""
import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from livemem import LiveMem  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", type=lambda s: int(s, 0), required=True)
    ap.add_argument("--size", type=lambda s: int(s, 0), default=0x40000)
    ap.add_argument("--ids", default="106,167,112,184,166")
    ap.add_argument("--step", type=lambda s: int(s, 0), default=0x40)
    a = ap.parse_args()

    ids = {int(x) for x in a.ids.split(",") if x.strip()}
    m = LiveMem()
    m.find_pid()
    m.refresh_maps()
    raw = m.read(a.base, a.size)
    print(f"pid={m.pid} base=0x{a.base:x} read={len(raw)} ids={sorted(ids)}")
    hits = []
    for off in range(0, len(raw) - 4, 4):
        v = struct.unpack_from("<I", raw, off)[0]
        if v in ids:
            hits.append(off)
    print(f"raw id hits={len(hits)}")
    for off in hits:
        addr = a.base + off
        rec = struct.unpack_from("<16I", raw, off - 8) if off >= 8 else None
        if rec is None:
            continue
        gold, lvl, cid, unk, x, y, z, u3, hp, hpmax = rec[:10]
        # 保留看起来像一条记录的位置：id 在 +0x08，且等级/HP 合理
        ok = (0 <= lvl <= 30 and 0 <= hp <= 40000 and 0 <= hpmax <= 40000
              and hp <= hpmax and -90000 <= x <= 90000 and -90000 <= z <= 90000)
        flag = "OK " if ok else "   "
        print(f"{flag}0x{addr:x}  rec@0x{addr - 8:x}: gold={gold} lvl={lvl} "
              f"id={cid} unk={unk} pos=({x},{y},{z}) hp={hp}/{hpmax} "
              f"u={rec[10:16]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
