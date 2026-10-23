# -*- coding: utf-8 -*-
"""枚举己方英雄 actor 的 15 个组件槽，找持有金币字段的对象。

已知金币字段地址（本轮实测）可通过 --gold-addr 传入；
脚本会检查每个槽对象范围内是否覆盖该地址，并打印对象里的
金币值/坐标/106 等命中。

用法：
  venv/Scripts/python.exe -X utf8 reverse/probe_actor_slots.py \
      --id 106 --gold-addr 0x724c6083d3a0
"""
import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wzry.vision.mem_source import MemSource  # noqa: E402
from livemem import LiveMem  # noqa: E402

SLOTS = 15
SLOT_BASE = 0x110
SLOT_STRIDE = 0x18


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, default=106)
    ap.add_argument("--gold-addr", type=lambda s: int(s, 0), default=None)
    ap.add_argument("--size", type=lambda s: int(s, 0), default=0x800)
    a = ap.parse_args()

    src = MemSource()
    src.connect()
    m = LiveMem()
    m.find_pid()
    m.refresh_maps()

    actor = None
    mgr = src.actor_manager()
    arr = int.from_bytes(m.read(mgr + 0x30, 8), "little")
    cnt = struct.unpack_from("<I", m.read(mgr + 0x4C, 4))[0]
    sel = struct.unpack_from("<I", m.read(mgr + 0x70, 4))[0]
    if sel:
        cnt = struct.unpack_from("<I", m.read(mgr + 0x6C, 4))[0]
    raw = m.read(arr, cnt * 0x18)
    for i in range(cnt):
        p = struct.unpack_from("<Q", raw, i * 0x18)[0]
        if struct.unpack_from("<i", m.read(p + 0x28, 4))[0] == a.id:
            actor = p
            break
    if not actor:
        print("actor not found")
        return 1
    print(f"actor 0x{actor:x}  gold_addr=0x{a.gold_addr:x}" if a.gold_addr
          else f"actor 0x{actor:x}")
    gold = None
    if a.gold_addr:
        gold = struct.unpack_from("<I", m.read(a.gold_addr, 4))[0]
        print(f"gold now = {gold}")

    for i in range(SLOTS):
        slot = actor + SLOT_BASE + i * SLOT_STRIDE
        b = m.read(slot, SLOT_STRIDE)
        ptr, sid, extra = struct.unpack_from("<QII", b)
        if not ptr:
            continue
        head = m.read(ptr, 0x20)
        vt = int.from_bytes(head[:8], "little") if len(head) >= 8 else 0
        contains = ""
        if a.gold_addr and ptr <= a.gold_addr < ptr + a.size:
            contains = f" <== CONTAINS gold+0x{a.gold_addr - ptr:x}"
        print(f"\nslot{i:02d} @0x{slot:x} ptr=0x{ptr:x} sid={sid} extra={extra} "
              f"vt=0x{vt:x}{contains}")
        obj = m.read(ptr, a.size)
        hits = []
        for off in range(0, len(obj) - 4, 4):
            u = struct.unpack_from("<I", obj, off)[0]
            s = struct.unpack_from("<i", obj, off)[0]
            tag = None
            if gold is not None and gold - 40 <= u <= gold + 40:
                tag = f"gold?={u}"
            elif u == 106:
                tag = "id106"
            elif s == -56379:
                tag = "x=-56379"
            elif s == -53367:
                tag = "z=-53367"
            elif u in (0x0A89,):
                tag = "2697"
            if tag:
                hits.append((off, tag))
        for off, tag in hits[:40]:
            print(f"    +0x{off:03x} {tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
