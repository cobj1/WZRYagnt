# -*- coding: utf-8 -*-
"""校验坐标命中是否落在英雄状态表里。

状态表（结算页已观察）步长 0x40：
  +0x00 gold, +0x04 level, +0x08 config_id, +0x0c unk,
  +0x10 x, +0x14 y, +0x18 z, +0x1c hp, +0x20 hp_max
本脚本按“坐标 x 的命中地址 H”反推 rec=H-0x10，
检查 id/level/z/hp/邻接记录。

用法：
  venv/Scripts/python.exe -X utf8 reverse/check_pos_hits.py \
      --hits reverse/out/mem/pos_x_hits.txt --x -56379 --z -53367 --id 106
"""
import argparse
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from livemem import LiveMem  # noqa: E402

HERO_IDS = {106, 149, 152, 153, 167, 171, 112, 166, 184, 111}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hits", required=True)
    ap.add_argument("--x", type=int, default=-56379)
    ap.add_argument("--z", type=int, default=-53367)
    ap.add_argument("--id", type=int, default=106)
    ap.add_argument("--step", type=lambda s: int(s, 0), default=0x40)
    a = ap.parse_args()

    addrs = []
    for line in Path(a.hits).read_text(encoding="utf-8").splitlines():
        m = re.match(r"0x([0-9a-f]+)", line.strip())
        if m:
            addrs.append(int(m.group(1), 16))
    print(f"hits={len(addrs)}")

    m = LiveMem()
    m.find_pid()
    m.refresh_maps()
    z32 = a.z & 0xFFFFFFFF
    exact, near = [], []
    for h in addrs:
        rec = h - 0x10
        raw = m.read(rec - 0x10, 0x80)
        if len(raw) < 0x80:
            continue
        vals = struct.unpack_from("<32I", raw)
        gold, lvl, cid, unk = vals[4], vals[5], vals[6], vals[7]
        x, y, z, hp, hpmax = vals[8], vals[9], vals[10], vals[11], vals[12]
        ok = (cid == a.id and x == (a.x & 0xFFFFFFFF) and z == z32
              and 0 < lvl <= 30 and 0 < hp <= 40000 and hp <= hpmax)
        near_ok = (cid == a.id and x == (a.x & 0xFFFFFFFF))
        if ok:
            exact.append((rec, gold, lvl, cid, unk, x, y, z, hp, hpmax))
        elif near_ok:
            near.append((rec, gold, lvl, cid, unk, x, y, z, hp, hpmax))

    print(f"exact={len(exact)} near={len(near)}")
    for label, rows in (("EXACT", exact), ("NEAR", near)):
        for rec, gold, lvl, cid, unk, x, y, z, hp, hpmax in rows:
            print(f"{label} rec=0x{rec:x} gold={gold} lvl={lvl} id={cid} "
                  f"unk={unk} pos=({x},{y},{z}) hp={hp}/{hpmax}")
            # 前后 3 条记录
            for k in range(-3, 4):
                r2 = rec + k * a.step
                b = m.read(r2 - 0x10, 0x40)
                if len(b) < 0x40:
                    continue
                v = struct.unpack_from("<16I", b)
                g2, l2, c2, u2 = v[4], v[5], v[6], v[7]
                x2, y2, z2, h2, hm2 = v[8], v[9], v[10], v[11], v[12]
                tag = ">>" if k == 0 else "  "
                sx = struct.unpack_from("<i", b, 0x20)[0]
                sz = struct.unpack_from("<i", b, 0x28)[0]
                sh = struct.unpack_from("<i", b, 0x2c)[0]
                shm = struct.unpack_from("<i", b, 0x30)[0]
                print(f"   {tag}[{k:+d}] 0x{r2:x} gold={g2} lvl={l2} id={c2} "
                      f"unk={u2} pos=({sx},{struct.unpack_from('<i', b, 0x24)[0]},{sz}) "
                      f"hp={sh}/{shm} rawid={c2 in HERO_IDS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
