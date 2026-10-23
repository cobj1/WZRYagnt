# -*- coding: utf-8 -*-
"""验证：定位 C# 英雄状态对象并连续读金币/HP/经验。

定位用设备端 memscan herostate（按 config_id + 已知坐标），
之后只读 obj+0x200(gold)/+0x1a4(hp)/+0x1bc(exp)，不再全扫。

用法：
  venv/Scripts/python.exe -X utf8 reverse/read_hero_gold.py --id 106 --n 5 --gap 2
"""
import argparse
import re
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wzry.vision.mem_source import MemSource  # noqa: E402
from livemem import LiveMem  # noqa: E402

OFF_ID = 0x16C
OFF_X = 0x184
OFF_Z = 0x18C
OFF_LEVEL = 0x19C
OFF_HP = 0x1A4
OFF_HPM = 0x1A8
OFF_EXP = 0x1BC
OFF_GOLD = 0x200


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, default=106)
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--gap", type=float, default=2.0)
    ap.add_argument("--x", type=int, default=None)
    ap.add_argument("--z", type=int, default=None)
    a = ap.parse_args()

    src = MemSource()
    src.connect()
    m = LiveMem()
    m.find_pid()
    m.refresh_maps()

    # 己方英雄坐标：先由 actor 层给出，避免把英雄 ID 硬编码到坐标
    x, z = a.x, a.z
    if x is None or z is None:
        for u in src.snapshot():
            if u.is_hero and u.config_id == a.id and u.x is not None:
                x, z = u.x, u.z
                break
    if x is None:
        print("actor pos not found")
        return 1
    print(f"actor pos=({x},{z})")

    cmd = (f"MEMSCAN_ONLY=scudo /data/local/tmp/memscan {m.pid} "
           f"herostate {a.id} {x} {z}")
    out = m._run(["shell", cmd]).decode("utf-8", "replace")
    objs = [int(v, 16) for v in re.findall(r"obj=0x([0-9a-f]+)", out)]
    print(out.strip())
    if not objs:
        print("herostate not found")
        return 2
    print(f"objects: {[hex(o) for o in objs]}")

    for k in range(a.n):
        row = []
        for o in objs:
            raw = m.read(o + OFF_ID, 0x9C)
            cid = struct.unpack_from("<i", raw, 0)[0]
            gx = struct.unpack_from("<i", raw, OFF_X - OFF_ID)[0]
            gz = struct.unpack_from("<i", raw, OFF_Z - OFF_ID)[0]
            hp = struct.unpack_from("<i", raw, OFF_HP - OFF_ID)[0]
            hpm = struct.unpack_from("<i", raw, OFF_HPM - OFF_ID)[0]
            lvl = struct.unpack_from("<i", raw, OFF_LEVEL - OFF_ID)[0]
            exp = struct.unpack_from("<i", raw, OFF_EXP - OFF_ID)[0]
            gold = struct.unpack_from("<i", raw, OFF_GOLD - OFF_ID)[0]
            row.append(f"0x{o:x} id={cid} lvl={lvl} gold={gold} hp={hp}/{hpm} "
                       f"exp={exp} pos=({gx},{gz})")
        print(f"[{k}] " + " | ".join(row))
        time.sleep(a.gap)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
