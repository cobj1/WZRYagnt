# -*- coding: utf-8 -*-
"""执行通道健康单测（EXEC-01）：半开 adb 的判定与恢复。"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.control import executor as exmod  # noqa: E402
from wzry.control.executor import AdbExecutor  # noqa: E402

PASS = []


def check(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print(f"PASS {name}")
    else:
        raise AssertionError(f"FAIL {name} {extra}")


class _R:
    def __init__(self, out="", rc=0):
        self.stdout = out
        self.stderr = ""
        self.returncode = rc


def make_exec():
    ex = object.__new__(AdbExecutor)
    ex.serial = "127.0.0.1:16384"
    ex._lock = threading.Lock()
    ex._event_fp = None
    ex.event_log = None
    ex.last_cmd_ms = 0.0
    ex.cmd_count = 0
    ex._online = None
    ex._online_ts = 0.0
    ex.health_checks = 0
    ex.recoveries = 0
    ex.last_error = ""
    return ex


def main() -> int:
    check("离线文本 1", AdbExecutor.looks_offline("error: device offline"))
    check("离线文本 2", AdbExecutor.looks_offline("adb: no devices/emulators found"))
    check("正常文本不误判", not AdbExecutor.looks_offline("device"))

    calls = []
    seq = ["offline", "device"]

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if "get-state" in args:
            return _R(seq.pop(0) if seq else "device")
        if "connect" in args:
            return _R("connected")
        return _R("error: device offline")

    orig = exmod.subprocess.run
    exmod.subprocess.run = fake_run
    try:
        ex = make_exec()
        check("离线后重连成功", ex.health(force=True) is True, f"{calls}")
        check("记录一次恢复", ex.recoveries == 1)
        n = len(calls)
        check("TTL 内不再探测", ex.health() is True and len(calls) == n)
        check("强制探测会再查", ex.health(force=True) is True and len(calls) > n)

        ex2 = make_exec()
        check("半开命令标记离线", ex2._run(["shell", "input", "tap", "1", "2"]) is not None)
        # 第一次 health -> device；命令本身返回 device offline -> 标记离线
        check("_run 识别 offline", ex2._online is False and "offline" in ex2.last_error)
    finally:
        exmod.subprocess.run = orig

    if len(PASS) != 9:
        raise AssertionError(f"expected 9 checks, got {len(PASS)}")
    print(f"\nALL PASS ({len(PASS)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
