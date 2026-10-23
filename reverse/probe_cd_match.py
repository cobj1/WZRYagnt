# -*- coding: utf-8 -*-
"""对表探针：施法后同时采集候选字段与屏幕倒计时截图，找出真正的 CD 字段。"""
import argparse
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wzry.vision.mem_source import MemSource  # noqa: E402

CANDS = [
    ("childA", 0x724C6FD69020 + 0x4EC, "ms"),
    ("childB", 0x724C6D783CE0 + 0x3B8, "ms"),
    ("childC", 0x724C6F2143F8 + 0x400, "int"),
]


def read_cands(m):
    vals = []
    for name, addr, kind in CANDS:
        raw = m.read(addr, 4)
        v = struct.unpack("<I", raw)[0]
        if kind == "ms":
            vals.append((name, v))
        else:
            vals.append((name, struct.unpack("<i", raw)[0]))
    return vals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=float, default=15)
    ap.add_argument("--cycles", type=int, default=16)
    ap.add_argument("--gap", type=float, default=0.6)
    a = ap.parse_args()

    src = MemSource()
    src.connect()
    m = src.m
    units = [u for u in src.snapshot() if u.is_hero and u.config_id == 106]
    if not units:
        print("hero not found")
        return 1
    print("actor", hex(units[0].actor))
    if a.wait:
        print(f"wait {a.wait:.0f}s")
        time.sleep(a.wait)

    m._run(["shell", "screencap -p /sdcard/m0.png"])
    t0 = time.time()
    m._run(["shell",
            "input motionevent DOWN 1780 640; sleep 0.35; "
            "input motionevent MOVE 1715 580; sleep 0.1; "
            "input motionevent MOVE 1650 520; sleep 0.1; "
            "input motionevent UP 1650 520; "
            "screencap -p /sdcard/m1.png"])
    t_cast = time.time()
    print(f"cast wall={t_cast - t0:.2f}s")
    for k in range(a.cycles):
        if k == a.cycles // 2:
            m._run(["shell", "screencap -p /sdcard/m2.png"])
            print(f"shot2 t+{time.time() - t_cast:.2f}")
        print(f"t+{time.time() - t_cast:5.2f}", read_cands(m), flush=True)
        time.sleep(a.gap)
    m._run(["shell", "screencap -p /sdcard/m3.png"])
    print(f"shot3 t+{time.time() - t_cast:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
