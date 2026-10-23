# -*- coding: utf-8 -*-
"""英雄记录字段时序探针（**只读**，不发任何输入）。

目的：在一局进行中，按固定间隔重复读 10 条英雄记录，把"会变的字段"筛出来，
      再按变化形态给候选语义（法力/护盾/连杀/计时器…）。

为什么只读就够：对局里 10 个英雄的经济、血量、蓝量都在持续变化，
不需要施法；施法只适合定位"只在施法瞬间变化"的字段（那是下一步）。

用法：
  venv/Scripts/python.exe -X utf8 reverse/probe_record_fields.py --secs 30 --gap 0.7
  venv/Scripts/python.exe -X utf8 reverse/probe_record_fields.py --lo 0x180 --hi 0x1d4
"""
import argparse
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wzry.vision.mem_source import MemSource, HERO_ARR_STRIDE, HERO_ARR_COUNT  # noqa: E402


def read_slots(src, base, lo, hi):
    """读一整份英雄数组，返回 {slot: {off: i32}}。"""
    raw = src.m.read(base, HERO_ARR_STRIDE * HERO_ARR_COUNT)
    out = {}
    for slot in range(HERO_ARR_COUNT):
        o = slot * HERO_ARR_STRIDE
        if len(raw) < o + hi:
            continue
        rid = struct.unpack_from("<i", raw, o + 0x16C)[0]
        if not (0 < rid < 10000):
            continue
        vals = {}
        for off in range(lo, hi, 4):
            vals[off] = struct.unpack_from("<i", raw, o + off)[0]
        out[slot] = (rid, vals)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=30.0)
    ap.add_argument("--gap", type=float, default=0.7)
    ap.add_argument("--lo", type=lambda s: int(s, 0), default=0x180)
    ap.add_argument("--hi", type=lambda s: int(s, 0), default=0x1D4)
    ap.add_argument("--min-delta", type=int, default=1)
    a = ap.parse_args()

    src = MemSource()
    src.connect()
    if not src.read_hero_states():
        print("no hero array (not in match?)")
        return 1
    base = src._hero_arrays[0]
    print(f"base=0x{base:x}  window=0x{a.lo:x}..0x{a.hi:x}  {a.secs:.0f}s @{a.gap:.1f}s")

    samples = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < a.secs:
        s = read_slots(src, base, a.lo, a.hi)
        if s:
            samples.append((time.monotonic() - t0, s))
        time.sleep(a.gap)
    print(f"samples={len(samples)}")

    # 每个 (slot, off) 的取值序列
    changed = {}
    for i in range(1, len(samples)):
        _, s0 = samples[i - 1]
        _, s1 = samples[i]
        for slot, (rid, v1) in s1.items():
            v0 = (s0.get(slot) or (None, {}))[1]
            if not v0:
                continue
            for off, x1 in v1.items():
                x0 = v0.get(off)
                if x0 is None:
                    continue
                if abs(x1 - x0) >= a.min_delta:
                    changed.setdefault(off, []).append((i, slot, x0, x1))

    print(f"\n变化字段数: {len(changed)}")
    for off in sorted(changed):
        ev = changed[off]
        slots = sorted({e[1] for e in ev})
        series = []
        for slot in slots[:4]:
            vals = [s[slot][1].get(off) for _, s in samples if slot in s]
            series.append(f"slot{slot}:" + ",".join(str(v) for v in vals[:14]))
        print(f"  +0x{off:03x}  事件{len(ev):3d} 涉及slot{slots[:6]}")
        for line in series:
            print(f"      {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
