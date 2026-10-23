# -*- coding: utf-8 -*-
"""英雄状态记录四点转储：找施法时跳变、冷却期间恒定的 CD 字段。"""
import argparse
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wzry.vision.mem_source import MemSource  # noqa: E402
from livemem import LiveMem  # noqa: E402

REC = 0x2AD8
OUT = Path("reverse/out/mem/rec_cd2")


def locate(src, m, cid):
    x = z = None
    for u in src.snapshot():
        if u.is_hero and u.config_id == cid and u.x is not None:
            x, z = u.x, u.z
            break
    if x is None:
        return []
    out = m._run(["shell",
                  f"MEMSCAN_ONLY=scudo /data/local/tmp/memscan {m.pid} "
                  f"herostate {cid} {x} {z}"]).decode("utf-8", "replace")
    objs = [int(v, 16) for v in __import__("re").findall(r"obj=0x([0-9a-f]+)", out)]
    good = []
    for o in objs:
        raw = m.read(o, 0x1B0)
        if len(raw) >= 0x1B0 and struct.unpack_from("<i", raw, 0x16C)[0] == cid:
            good.append(o)
    return good


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, default=106)
    ap.add_argument("--wait", type=float, default=20)
    a = ap.parse_args()

    src = MemSource()
    src.connect()
    m = src.m
    objs = locate(src, m, a.id)
    if not objs:
        print("locate failed")
        return 1
    o = objs[0]
    print(f"pid={m.pid} rec=0x{o:x}")

    OUT.mkdir(parents=True, exist_ok=True)
    if a.wait:
        print(f"wait {a.wait:.0f}s")
        time.sleep(a.wait)

    def dump(tag):
        raw = m.read(o, REC)
        (OUT / f"{tag}.bin").write_bytes(raw)
        lvl = struct.unpack_from("<i", raw, 0x19C)[0] if len(raw) > 0x1A0 else -1
        print(f"{tag} len={len(raw)} level={lvl} t={time.time():.2f}")
        return raw

    def cast():
        t = time.time()
        m._run(["shell",
                "input motionevent DOWN 1780 640; sleep 0.35; "
                "input motionevent MOVE 1715 580; sleep 0.1; "
                "input motionevent MOVE 1650 520; sleep 0.1; "
                "input motionevent UP 1650 520; "
                "screencap -p /sdcard/rc_" + str(int(t)) + ".png"])
        print(f"cast at {t:.2f} wall={time.time() - t:.2f}s")

    dump("d0_pre")
    cast()
    time.sleep(1.2)
    dump("d1_cd")
    time.sleep(36)
    dump("d2_after")
    cast()
    time.sleep(1.2)
    dump("d3_cd2")
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
