# -*- coding: utf-8 -*-
"""施法-截图-内存同步探针：一条 shell 完成手势+截图，返回后立刻读候选对象 +0x4ec。"""
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wzry.vision.mem_source import MemSource  # noqa: E402

OBJS = [0x724C6FD69078, 0x724C6FD69BC8]
OFF = 0x4EC


def vals(m):
    out = []
    for o in OBJS:
        raw = m.read(o + OFF - 0x20, 0x40)
        if len(raw) < 0x40:
            out.append(None)
            continue
        i = struct.unpack_from("<i", raw, 0x20)[0]
        f = struct.unpack_from("<f", raw, 0x20)[0]
        near = struct.unpack("<8i", raw[:0x20])
        out.append((i, f, near))
    return out


def main():
    src = MemSource()
    src.connect()
    src.m.refresh_maps()
    units = [u for u in src.snapshot() if u.is_hero and u.config_id == 106]
    if not units:
        print("hero not found")
        return 1
    m = src.m
    print("actor", hex(units[0].actor))

    t0 = time.time()
    m._run(["shell",
            "input motionevent DOWN 1780 640; sleep 0.35; "
            "input motionevent MOVE 1715 580; sleep 0.1; "
            "input motionevent MOVE 1650 520; sleep 0.1; "
            "input motionevent UP 1650 520; "
            "screencap -p /sdcard/cdstart.png"])
    t1 = time.time()
    print(f"gesture+shot wall={t1 - t0:.2f}s")
    print("A", vals(m), f"t+{time.time() - t1:.2f}")
    time.sleep(2.5)
    print("B", vals(m), f"t+{time.time() - t1:.2f}")
    m._run(["shell", "screencap -p /sdcard/cdmid.png"])
    print("C", vals(m), f"t+{time.time() - t1:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
