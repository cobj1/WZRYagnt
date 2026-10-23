# -*- coding: utf-8 -*-
"""当前法力（ep）定位实验：施法差分。

思路：法力是"施法瞬间下降、之后缓慢回升"的唯一量。只读扫描定不了它
（12 秒采样里没有明显的"消耗+上限"对），必须制造一次消耗事件。

步骤：
  1. 施法前：dump 本地英雄记录（0x2AD8）+ attr 组件（0x300）
  2. 点一次技能（坐标来自 configs/calibration_absolute.json，1280x720 基准 × 缩放）
  3. 在 +0.2/+0.5/+1.0/+2.0/+4.0s 各 dump 一次
  4. 输出：先降后升的字段（法力候选）、纯下降字段（CD/消耗）

用法：
  venv/Scripts/python.exe -X utf8 reverse/probe_ep_cast.py --id 106 --slot 1
  venv/Scripts/python.exe -X utf8 reverse/probe_ep_cast.py --id 106 --slot 1 --dry-run   # 只看不点
"""
import argparse
import json
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wzry.vision.mem_source import (ACTOR_ATTR_OFF, HERO_ARR_COUNT,  # noqa: E402
                                    HERO_ARR_STRIDE, MemSource)

CALIB = ROOT / "configs" / "calibration_absolute.json"
CALIB_BASE = (1280, 720)          # 标定基准分辨率


def skill_point(slot: int, screen: tuple) -> tuple:
    pts = (json.loads(CALIB.read_text(encoding="utf-8")).get("points") or {})
    p = pts.get(f"skill{slot}") or pts.get("attack")
    sx = screen[0] / CALIB_BASE[0]
    sy = screen[1] / CALIB_BASE[1]
    return int(p[0] * sx), int(p[1] * sy)


def dump(src, base, slot_idx, actor):
    """返回 {名字: 字节串}。"""
    out = {}
    out["record"] = src.m.read(base + slot_idx * HERO_ARR_STRIDE, HERO_ARR_STRIDE)
    if actor:
        out["actor"] = src.m.read(actor, 0x400)
        try:
            attr = struct.unpack("<Q", src.m.read(actor + ACTOR_ATTR_OFF, 8))[0]
            if 0x1000000000 < attr < 0x7FFFFFFFFFFF:
                out["attr"] = src.m.read(attr, 0x400)
        except Exception:
            pass
    return out


def diff(a, b):
    """返回 [(区域, 偏移, 旧, 新, 方向)]，只看 i32。"""
    out = []
    for k in a:
        if k not in b:
            continue
        x, y = a[k], b[k]
        n = min(len(x), len(y)) - 4
        for off in range(0, n, 4):
            v0 = struct.unpack_from("<i", x, off)[0]
            v1 = struct.unpack_from("<i", y, off)[0]
            if v0 != v1:
                out.append((k, off, v0, v1, "down" if v1 < v0 else "up"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, default=106)
    ap.add_argument("--slot", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--serial", default="127.0.0.1:16384")
    a = ap.parse_args()

    src = MemSource(serial=a.serial)
    src.connect()
    hs = src.read_hero_states()
    if not hs:
        print("未读到英雄数组：可能不在对局中")
        return 1
    base = src._hero_arrays[0]
    slot_idx = next((i for i, h in enumerate(sorted(hs, key=lambda r: r["slot"]))
                     if h["id"] == a.id), None)
    # 用 slot 字段定位更稳
    me = next((h for h in hs if h["id"] == a.id), None)
    if me is None:
        print(f"英雄 {a.id} 不在本局")
        return 1
    slot_idx = me["slot"]

    units = [u for u in src.snapshot() if u.is_hero and u.config_id == a.id]
    actor = units[0].actor if units else None
    w, h = 1920, 1080
    try:
        out = src.m._run(["shell", "wm size"]).decode()
        if "Override size" in out or "Physical size" in out:
            line = [ln for ln in out.splitlines() if "size" in ln][-1]
            w, h = [int(v) for v in line.split(":")[-1].strip().split("x")]
            if w < h:
                w, h = h, w
    except Exception:
        pass
    px, py = skill_point(a.slot, (w, h))
    print(f"id={a.id} slot_idx={slot_idx} actor=0x{(actor or 0):x} "
          f"screen={w}x{h} skill{a.slot}@({px},{py})")

    s0 = dump(src, base, slot_idx, actor)
    print(f"pre: record={len(s0.get('record', b''))} attr={len(s0.get('attr', b''))}")

    if a.dry_run:
        print("dry-run: 不发送输入")
        return 0

    t0 = time.monotonic()
    src.m._run(["shell", f"input tap {px} {py}"])
    print(f"tap sent at t=0")

    times = (0.2, 0.5, 1.0, 2.0, 4.0)
    samples = []
    for t in times:
        while time.monotonic() - t0 < t:
            time.sleep(0.01)
        samples.append((round(time.monotonic() - t0, 2), dump(src, base, slot_idx, actor)))

    print("\n=== 相对施法前的变化 ===")
    for t, s in samples:
        d = diff(s0, s)
        if not d:
            print(f"t+{t:.2f}s: 无变化")
            continue
        print(f"t+{t:.2f}s: {len(d)} 处")
        for area, off, v0, v1, dirn in d[:14]:
            print(f"   {area} +0x{off:04x}  {v0} -> {v1}  ({dirn})")

    # 先降后升 = 法力候选：对每个变化过的字段，拼出完整序列（含基线）再看形状
    print("\n=== 法力候选（低于基线后又回升） ===")
    keys = set()
    for _t, s in samples:
        for area, off, _v0, _v1, _d in diff(s0, s):
            keys.add((area, off))
    cand = []
    for area, off in sorted(keys):
        base_v = struct.unpack_from("<i", s0[area], off)[0]
        series = [base_v]
        for _t, s in samples:
            series.append(struct.unpack_from("<i", s[area], off)[0])
        if min(series) < base_v and series[-1] > min(series):
            cand.append((area, off, series))
    for area, off, series in cand[:15]:
        print(f"   {area} +0x{off:04x}: {series}")
    if not cand:
        print("   （未找到；该技能可能不耗蓝，或施法未成功）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
