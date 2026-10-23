#!/usr/bin/env python
"""逻辑单元数组（0xD0）的离线回归：不需要设备。

用假内存对象喂构造好的字节，验证：
  T1 合法数组能被 `_check` 接受，条数正确
  T2 步长不符 / 条数越界 / 字段非法一律拒绝
  T3 `read()` 解析出的 id/camp/type/xyz 与写入一致
  T4 `verify()`：英雄坐标对得上 → 通过；整体平移 → 拒绝（核心安全阀）
  T5 `index()` 按 (id, camp) 分组

跑法：venv/Scripts/python.exe -X utf8 scripts/test_logic_units.py
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision import logic_units as LU   # noqa: E402

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


class FakeMem:
    """只有 read(addr,size) 的假内存。"""

    def __init__(self, blobs: dict):
        self.blobs = blobs          # {addr: bytes}

    def read(self, addr: int, size: int) -> bytes:
        for base, buf in self.blobs.items():
            if base <= addr < base + len(buf):
                s = addr - base
                return buf[s:s + size]
        return b""


def rec(rid, camp, typ, x, y, z):
    buf = bytearray(LU.STRIDE)
    struct.pack_into("<3i", buf, LU.OFF_ID, rid, 0, 0)
    struct.pack_into("<i", buf, LU.OFF_CAMP, camp)
    struct.pack_into("<i", buf, LU.OFF_TYPE, typ)
    struct.pack_into("<3i", buf, LU.OFF_X, x, y, z)
    return bytes(buf)


BASE = 0x50000000


def build(n_good=6, dx=0, dz=0):
    rows = []
    for i in range(n_good):
        rows.append(rec(1690 + i, 1 + (i % 2), 11,
                        -50000 + i * 1000 + dx, 0, -50000 + i * 900 + dz))
    return b"".join(rows)


def main() -> int:
    # ---- T1 合法数组 ----
    buf = build(6)
    lu = LU.LogicUnits(FakeMem({BASE: buf}))
    n = lu._check(BASE, BASE + len(buf))
    check("T1 合法数组被接受且条数正确", n == 6, f"n={n}")

    # ---- T2 非法一律拒绝 ----
    bad_stride = lu._check(BASE, BASE + len(buf) - 8)
    one = lu._check(BASE, BASE + LU.STRIDE)
    bad_field = bytearray(build(3))
    struct.pack_into("<i", bad_field, LU.OFF_CAMP, 7)      # 阵营非法
    bad = lu._check(BASE, BASE + len(bad_field))
    lu_bad = LU.LogicUnits(FakeMem({BASE: bytes(bad_field)}))
    bad2 = lu_bad._check(BASE, BASE + len(bad_field))
    check("T2 步长不符 / 单条 / 字段非法都被拒",
          bad_stride is None and one is None and bad2 is None,
          f"stride={bad_stride} one={one} field={bad2}")

    # ---- T3 解析正确 ----
    lu.begin, lu.count = BASE, 6
    recs = lu.read()
    r0 = recs[0]
    check("T3 read() 字段与写入一致",
          len(recs) == 6 and r0["id"] == 1690 and r0["camp"] == 1
          and r0["type"] == 11 and r0["x"] == -50000 and r0["z"] == -50000,
          f"{r0}")

    # ---- T4 交叉验证（安全阀） ----
    heroes = [{"id": 1690 + i, "x": -50000 + i * 1000, "z": -50000 + i * 900}
              for i in range(4)]
    ok_hit = lu.verify(heroes)
    lu_far = LU.LogicUnits(FakeMem({BASE: build(6, dx=9000, dz=9000)}))
    lu_far.begin, lu_far.count = BASE, 6
    ok_far = lu_far.verify(heroes)
    check("T4 verify()：坐标吻合通过、整体平移拒绝",
          ok_hit and not ok_far, f"hit={ok_hit} far={ok_far} | {lu.reason}")

    # ---- T5 索引 ----
    idx = lu.index()
    check("T5 index() 按 (id,camp) 分组",
          idx.get((1690, 1)) and len(idx[(1690, 1)]) == 1,
          f"keys={list(idx)[:3]}")

    print(f"\n结果: {PASS}/{PASS + FAIL} 通过")
    if FAILS:
        print("失败项:", ", ".join(FAILS))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
