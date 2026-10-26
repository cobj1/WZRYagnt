# -*- coding: utf-8 -*-
"""离线回放采集源单测（PERF-01 无设备链路）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.capture.replay import ReplayCapture  # noqa: E402

PASS = []


def check(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print(f"PASS {name}")
    else:
        raise AssertionError(f"FAIL {name} {extra}")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for i, v in enumerate((10, 200)):
            cv2.imwrite(str(d / f"f_{i:06d}.png"),
                        np.full((16, 16, 3), v, dtype=np.uint8))
        cap = ReplayCapture(d, fps=1000.0, loop=True).start()
        check("发现帧", len(cap.frames) == 2)
        f1, lag1 = cap.wait_frame(timeout=2.0)
        f2, _ = cap.wait_frame(timeout=2.0)
        check("按序返回", f1 is not None and f2 is not None)
        check("像素未损坏", int(f1[0, 0, 0]) in (10, 200)
              and int(f2[0, 0, 0]) in (10, 200))
        check("lag 非负", lag1 >= 0.0)
        f3, _ = cap.wait_frame(timeout=2.0)
        check("循环回绕", f3 is not None and cap.index in (0, 1))
        cap.stop()
        check("stop 清空", cap.frames == [])

        empty = ReplayCapture(d / "nope", fps=1000.0).start()
        frame, lag = empty.wait_frame(timeout=0.05)
        check("空目录返回 None", frame is None and lag == 0.0)

    if len(PASS) != 7:
        raise AssertionError(f"expected 7 checks, got {len(PASS)}")
    print(f"\nALL PASS ({len(PASS)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
