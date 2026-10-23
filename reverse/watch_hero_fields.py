# -*- coding: utf-8 -*-
"""在己方英雄 actor / attr 组件里找金币等实时字段。

做法：锁定 config_id 106 的 actor，采样两到三次，
打印数值变化的 u32 及其地址偏移；再 dump 候选附近。
用法：
  venv/Scripts/python.exe -X utf8 reverse/watch_hero_fields.py [--id 106] [--gap 2]
"""
import argparse
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wzry.vision.mem_source import MemSource  # noqa: E402
from livemem import LiveMem  # noqa: E402

ATTR_OFF = 0x1D0
POS_OFF = 0x230


class Probe:
    def __init__(self):
        self.src = MemSource()
        self.src.connect()
        self.m = LiveMem()
        self.m.find_pid()
        self.m.refresh_maps()

    def u64(self, a):
        b = self.m.read(a, 8)
        return int.from_bytes(b, "little") if len(b) == 8 else None

    def find_actor(self, cid):
        mgr = self.src.actor_manager()
        arr = self.u64(mgr + 0x30)
        cnt = struct.unpack_from("<I", self.m.read(mgr + 0x4C, 4))[0]
        sel = struct.unpack_from("<I", self.m.read(mgr + 0x70, 4))[0]
        if sel:
            cnt = struct.unpack_from("<I", self.m.read(mgr + 0x6C, 4))[0]
        raw = self.m.read(arr, cnt * 0x18)
        for i in range(cnt):
            a = struct.unpack_from("<Q", raw, i * 0x18)[0]
            if not a or not (0x1000000000 < a < 0x800000000000):
                continue
            h = self.m.read(a + 0x28, 4)
            if len(h) == 4 and struct.unpack("<i", h)[0] == cid:
                return a
        return None

    def read_region(self, base, size):
        return self.m.read(base, size)

    def snap(self, base, size):
        raw = self.read_region(base, size)
        out = {}
        for off in range(0, len(raw) - 4, 4):
            out[off] = struct.unpack_from("<I", raw, off)[0]
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, default=106)
    ap.add_argument("--gap", type=float, default=2.0)
    ap.add_argument("--size", type=lambda s: int(s, 0), default=0x400)
    a = ap.parse_args()

    p = Probe()
    actor = p.find_actor(a.id)
    if not actor:
        print(f"actor id={a.id} not found")
        return 1
    attr = p.u64(actor + ATTR_OFF)
    pos = p.u64(actor + POS_OFF)
    print(f"pid={p.src.pid} actor=0x{actor:x} attr=0x{attr:x} pos=0x{pos:x}")
    print(f"actor+0x110={p.u64(actor + 0x110):#x} "
          f"actor+0x108={p.u64(actor + 0x108):#x}")

    targets = [("actor", actor, a.size)]
    if attr:
        targets.append(("attr", attr, a.size))
    if pos:
        targets.append(("pos", pos, 0x200))

    s0 = {name: p.snap(base, size) for name, base, size in targets}
    time.sleep(a.gap)
    s1 = {name: p.snap(base, size) for name, base, size in targets}

    for name, base, size in targets:
        diffs = []
        for off, v1 in s0[name].items():
            v2 = s1[name].get(off)
            if v2 is None or v1 == v2:
                continue
            d = v2 - v1
            if abs(d) < 100000:
                diffs.append((off, v1, v2, d))
        print(f"\n== {name} 0x{base:x} changed={len(diffs)} (gap {a.gap}s)")
        for off, v1, v2, d in diffs[:120]:
            mark = ""
            if 100 <= v1 <= 100000 and -1000 < d < 1000:
                mark = " *"
            print(f"  +0x{off:03x}: {v1} -> {v2}  (d={d}){mark}")

    x = p.u64(actor + 0x108)
    if x:
        print(f"\n== handle? 0x{x:x}")
        h0 = p.snap(x, 0x100)
        time.sleep(a.gap)
        h1 = p.snap(x, 0x100)
        for off in h0:
            if h0[off] != h1.get(off):
                print(f"  +0x{off:03x}: {h0[off]} -> {h1[off]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
