# -*- coding: utf-8 -*-
"""技能冷却组件探针：施法后扫描 actor 组件及其一级子对象，找递减字段。

用法：
  python reverse/probe_comp_cd.py --id 106 --motion 1780 640 1650 520
"""
import argparse
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wzry.vision.mem_source import MemSource  # noqa: E402

COMP_BASE = 0x110
COMP_STRIDE = 0x18
COMP_SCAN = 0x300
CHILD_SCAN = 0x200


def comps_of(m, actor):
    raw = m.read(actor, COMP_BASE + COMP_STRIDE * 20 + 0x10)
    out = []
    for k in range(20):
        o = COMP_BASE + k * COMP_STRIDE
        if o + 16 > len(raw):
            break
        p = struct.unpack_from("<Q", raw, o)[0]
        sid = struct.unpack_from("<I", raw, o + 8)[0]
        if not (0x1000000000 < p < 0x7FFFFFFFFFFF):
            continue
        comp = m.read(p, 0x20)
        if len(comp) < 0x18:
            continue
        csid, back = struct.unpack_from("<IQ", comp, 0xC)
        if back == actor and csid == sid:
            out.append((o, p))
    return out


def heap_ptrs(m, base, buf):
    maps = m.maps

    def mapped(x):
        return any(a <= x < b for a, b, _, _ in maps)

    out = []
    for i in range(0, len(buf) - 8, 8):
        v = struct.unpack_from("<Q", buf, i)[0]
        if 0x1000000000 < v < 0x7FFFFFFFFFFF and mapped(v):
            out.append(v)
    return out


def grab(m, comps):
    spans = [(p, COMP_SCAN) for _, p in comps]
    bufs = dict(m.read_spans(spans))
    objs = {}
    for p in list(bufs):
        objs[p] = bufs[p]
    children = set()
    for p, buf in bufs.items():
        children.update(heap_ptrs(m, p, buf))
    children = [c for c in children if c not in objs]
    cb = dict(m.read_spans([(c, CHILD_SCAN) for c in children]))
    for c, b in cb.items():
        objs[c] = b
    return objs


def diff_scan(s1, s2, dt):
    hits = []
    for base, b1 in s1.items():
        b2 = s2.get(base)
        if not b2:
            continue
        n = min(len(b1), len(b2))
        for off in range(0, n - 4, 4):
            i1 = struct.unpack_from("<i", b1, off)[0]
            i2 = struct.unpack_from("<i", b2, off)[0]
            f1 = struct.unpack_from("<f", b1, off)[0]
            f2 = struct.unpack_from("<f", b2, off)[0]
            d = i1 - i2
            if 20 <= i1 <= 40 and 8 <= d <= 18:
                hits.append((base, off, "i", i1, i2))
            fd = f1 - f2
            if 20 <= f1 <= 40 and 8 <= fd <= 18:
                hits.append((base, off, "f", round(f1, 2), round(f2, 2)))
            dms = i1 - i2
            if 20000 <= i1 <= 40000 and 8000 <= dms <= 18000:
                hits.append((base, off, "ms", i1, i2))
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, default=106)
    ap.add_argument("--motion", nargs=4, type=int, default=[1780, 640, 1650, 520])
    ap.add_argument("--hold", type=int, default=350)
    ap.add_argument("--wait", type=float, default=0)
    ap.add_argument("--out", default="reverse/out/mem/comp_cd")
    a = ap.parse_args()

    src = MemSource()
    src.connect()
    src.m.refresh_maps()
    src.snapshot()
    units = [u for u in src.snapshot() if u.is_hero and u.config_id == a.id]
    if not units:
        print("hero not found")
        return 1
    u = units[0]
    m = src.m
    comps = comps_of(m, u.actor)
    print(f"actor=0x{u.actor:x} comps={len(comps)} " +
          " ".join(f"0x{o:x}:0x{p:x}" for o, p in comps))

    if a.wait:
        print(f"wait {a.wait:.0f}s for cd reset")
        time.sleep(a.wait)

    x1, y1, x2, y2 = a.motion
    xm, ym = (x1 + x2) // 2, (y1 + y2) // 2
    t0 = time.time()
    m._run(["shell",
            f"input motionevent DOWN {x1} {y1}; sleep {a.hold / 1000}; "
            f"input motionevent MOVE {xm} {ym}; sleep 0.1; "
            f"input motionevent MOVE {x2} {y2}; sleep 0.1; "
            f"input motionevent UP {x2} {y2}; "
            f"screencap -p /sdcard/compcast.png"])
    t_cast = time.time()
    print(f"cast wall={t_cast - t0:.2f}s")

    t1 = t_cast + 0.5
    while time.time() < t1:
        time.sleep(0.02)
    s1 = grab(m, comps)
    a1 = time.time() - t_cast
    t2 = t_cast + 13.0
    while time.time() < t2:
        time.sleep(0.02)
    s2 = grab(m, comps)
    a2 = time.time() - t_cast
    print(f"s1 t={a1:.2f}s objs={len(s1)}  s2 t={a2:.2f}s objs={len(s2)} dt={a2-a1:.2f}s")

    hits = diff_scan(s1, s2, a2 - a1)
    print(f"hits={len(hits)}")
    compmap = {p: o for o, p in comps}
    for base, off, kind, v1, v2 in hits:
        tag = f"comp@0x{compmap[base]:x}" if base in compmap else "child"
        print(f"0x{base:x} ({tag}) +0x{off:03x} {kind} {v1} -> {v2}")

    uniq = []
    seen = set()
    for base, off, kind, _v1, _v2 in hits:
        if kind == "ms":
            continue
        key = (base, off)
        if key not in seen:
            seen.add(key)
            uniq.append(key)
    uniq = uniq[:40]
    if uniq:
        pages = {(b + o) & ~0xFFF for b, o in uniq}

        def sample():
            pd = m.read_pages(pages)
            out = []
            for b, o in uniq:
                a = b + o
                pg = a & ~0xFFF
                buf = pd.get(pg, b"")
                if len(buf) < (a & 0xFFF) + 4:
                    out.append(None)
                else:
                    out.append(struct.unpack_from("<f", buf, a & 0xFFF)[0])
            return out

        print("--- series (float view) ---")
        for k in range(5):
            print(f"t+{time.time() - t_cast:5.2f}", [None if v is None else round(v, 2) for v in sample()])
            time.sleep(0.9)
        print("addrs", [f"0x{b + o:x}" for b, o in uniq])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
