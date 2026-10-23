# -*- coding: utf-8 -*-
"""施法 + 英雄对象时间序列探针。

定位 C# 英雄状态对象（memscan herostate，scudo-only），读 MP/HP 作为施法硬判据，
同时转储整条 0x2AD8 记录，用于找技能冷却字段。

用法：
  python reverse/probe_skill_cd.py --id 106 --swipe 1740 640 1650 520 --dur 400
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
OFF_MP = 0x1A0
OFF_HP = 0x1A4
OFF_HPM = 0x1A8
RECORD = 0x2AD8


def fields(raw):
    def i(off):
        return struct.unpack_from("<i", raw, off)[0]

    return dict(id=i(OFF_ID), x=i(OFF_X), z=i(OFF_Z), level=i(OFF_LEVEL),
                mp=i(OFF_MP), hp=i(OFF_HP), hpm=i(OFF_HPM))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, default=106)
    ap.add_argument("--swipe", nargs=4, type=int, metavar=("X1", "Y1", "X2", "Y2"))
    ap.add_argument("--tap", nargs=2, type=int, metavar=("X", "Y"))
    ap.add_argument("--motion", nargs=4, type=int, metavar=("X1", "Y1", "X2", "Y2"))
    ap.add_argument("--hold", type=int, default=300)
    ap.add_argument("--dur", type=int, default=400)
    ap.add_argument("--samples", default="0.4,1.0,2.0,3.0,5.0")
    ap.add_argument("--out", default="reverse/out/mem/cd_probe")
    a = ap.parse_args()

    src = MemSource()
    src.connect()
    m = LiveMem()
    m.find_pid()
    m.refresh_maps()

    x = z = None
    for u in src.snapshot():
        if u.is_hero and u.config_id == a.id and u.x is not None:
            x, z = u.x, u.z
            break
    if x is None:
        print("actor pos not found")
        return 1
    print(f"pid={m.pid} actor pos=({x},{z})")

    cmd = (f"MEMSCAN_ONLY=scudo /data/local/tmp/memscan {m.pid} "
           f"herostate {a.id} {x} {z}")
    out = m._run(["shell", cmd]).decode("utf-8", "replace")
    print(out.strip())
    objs = [int(v, 16) for v in re.findall(r"obj=0x([0-9a-f]+)", out)]
    if not objs:
        print("herostate not found")
        return 2

    o = objs[0]
    raw0 = m.read(o, RECORD)
    f0 = fields(raw0)
    print(f"obj=0x{o:x} t0 {f0}")
    if f0["id"] != a.id:
        for cand in objs[1:]:
            raw = m.read(cand, RECORD)
            f = fields(raw)
            if f["id"] == a.id:
                o, raw0, f0 = cand, raw, f
                print(f"pick obj=0x{o:x} {f0}")
                break

    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "rec_t0.bin").write_bytes(raw0)

    if a.motion:
        x1, y1, x2, y2 = a.motion
        xm, ym = (x1 + x2) // 2, (y1 + y2) // 2
        print(f"motion cast ({x1},{y1})->({x2},{y2}) hold={a.hold}ms")
        m._run(["shell", f"input motionevent DOWN {x1} {y1}"])
        time.sleep(a.hold / 1000)
        m._run(["shell", f"input motionevent MOVE {xm} {ym}"])
        time.sleep(0.1)
        m._run(["shell", f"input motionevent MOVE {x2} {y2}"])
        time.sleep(0.1)
        t_cast = time.time()
        m._run(["shell", f"input motionevent UP {x2} {y2}"])
        m._run(["shell", "screencap -p /sdcard/skillcast.png"])
    elif a.swipe:
        x1, y1, x2, y2 = a.swipe
        act = f"input swipe {x1} {y1} {x2} {y2} {a.dur}"
        print(f"cast swipe ({x1},{y1})->({x2},{y2}) dur={a.dur}ms")
        t_cast = time.time()
        m._run(["shell", act])
    elif a.tap:
        x, y = a.tap
        act = f"input tap {x} {y}"
        print(f"cast tap ({x},{y})")
        t_cast = time.time()
        m._run(["shell", act])
    else:
        print("need --motion/--swipe/--tap")
        return 3

    samples = [float(v) for v in a.samples.split(",")]
    meta = []
    for k, dt in enumerate(samples):
        wait = t_cast + dt - time.time()
        if wait > 0:
            time.sleep(wait)
        raw = m.read(o, RECORD)
        f = fields(raw)
        actual = time.time() - t_cast
        name = f"rec_{k:02d}.bin"
        (outdir / name).write_bytes(raw)
        meta.append(f"{name},{actual:.3f}")
        print(f"t+{dt:<4g} {f}  dt={actual:.2f}s")
    (outdir / "meta.csv").write_text("\n".join(meta), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
