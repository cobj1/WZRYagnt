#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LiveMem 安全读回归（不需要设备）。

覆盖：
  T1 is_mapped：连续段可跨、gap/非法地址/GPU 段拒绝
  T2 read_checked：正常/短读/超预算/未映射
  T3 read_ptr_checked：PAC 去 tag
  T4 resolve：重复映射时优先 exec 段

跑法：python scripts/test_livemem_safe.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "reverse"))

from livemem import LiveMem  # noqa: E402

PASS = FAIL = 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS {name}" + (f"  {extra}" if extra else ""))
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"FAIL {name}  {extra}")


def main() -> int:
    m = LiveMem()
    m.maps = [(0x1000, 0x2000, 0, "[anon:test]"),
              (0x2000, 0x3000, 0, "[anon:more]"),
              (0x4000, 0x5000, 0, "[anon:gpu]")]
    m.map_perms = {0x1000: "r--p", 0x2000: "rw-p", 0x4000: "rw-p"}

    check("mapped basic", m.is_mapped(0x1000, 0x500))
    check("mapped across contiguous", m.is_mapped(0x1F00, 0x200))
    check("gap rejected", not m.is_mapped(0x1F00, 0x1200))
    check("gpu denied", not m.is_mapped(0x4000, 0x10))
    check("zero addr denied", not m.is_mapped(0, 8))
    check("negative denied", not m.is_mapped(-1, 8))

    m.read = lambda addr, size: b"\x11" * size
    check("read ok", m.read_checked(0x1000, 16) == b"\x11" * 16)
    check("read ok stat", m.read_stats["ok"] == 1)

    m.read = lambda addr, size: b"\x11" * (size - 1)
    check("short rejected", m.read_checked(0x1000, 16) is None)
    check("short stat", m.read_stats["short"] == 1)

    check("over budget rejected",
          m.read_checked(0x1000, 8 * 1024 * 1024) is None)
    check("over budget stat", m.read_stats["denied"] == 1)

    m.read = lambda addr, size: (0x0000_1234_5678_9ABC).to_bytes(8, "little")
    check("pac stripped", m.read_ptr_checked(0x1000) == 0x1234_5678_9ABC)

    m.read_spans = lambda spans: [(s, b"\x00" * n) for s, n in spans]
    good = [(0x1000, 0x100), (0x2000, 0x100)]
    check("span ok", len(m.read_spans_checked(good)) == 2)
    check("span over budget",
          m.read_spans_checked([(0x1000, 5 * 1024 * 1024)]) == [])
    check("span unmapped",
          m.read_spans_checked([(0x3500, 0x100)]) == [])

    class FakeElf:
        def load_of(self, vaddr):
            return {"off": 0, "vaddr": 0, "filesz": 0x1000, "memsz": 0x1000}

    class FakeMem(LiveMem):
        def elf(self, name):
            return FakeElf()

        def segments(self, name):
            return [(0x1000, 0x2000, 0, "dup"), (0x2000, 0x3000, 0, "exec")]

    f = FakeMem()
    f.maps = [(0x1000, 0x2000, 0, "dup"), (0x2000, 0x3000, 0, "exec")]
    f.map_perms = {0x1000: "r--p", 0x2000: "r-xp"}
    check("exec preferred on duplicate", f.resolve("x", 0) == 0x2000,
          hex(f.resolve("x", 0)))
    check("active exec start", f.active_exec_start("x") == 0x2000)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
