#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""镜像局（双方同英雄）英雄状态回归：不需要设备。

覆盖：
  T1 10 个 slot 中有重复 cid 时，read_hero_states 仍返回 10 人（旧版按 cid 合并只剩 9）
  T2 主副本缺槽时，用同多重集的副副本补齐，并按 (exp,gold) 取新
  T3 副副本 cid 多重集不一致时拒绝按 slot 混拼接
  T4 _locate_hero_arrays 用 10 个 cid 的多重集定位，重复 cid 不再被去重拒绝

跑法：python scripts/test_hero_states_dupe.py
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision.mem_source import (  # noqa: E402
    HERO_ARR_COUNT, HERO_ARR_STRIDE, SELF_OBJ_ID, MemSource, MemUnit,
)

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


IDS = [106, 106, 107, 108, 109, 110, 111, 112, 113, 114]


def row(slot, cid, gold=100, exp=0):
    return {
        "id": cid, "slot": slot, "level": 1, "gold": gold,
        "gold_earned": gold + 50, "hp": 100, "hp_max": 100,
        "kills": 0, "deaths": 0, "assists": 0, "exp": exp,
    }


def make_src(bases, reader):
    src = MemSource()
    src.pid = "123"
    src._hero_array_pid = "123"
    src._hero_arrays = list(bases)
    src._hero_id_counts = Counter(IDS)
    src._hero_camp = {cid: ("blue" if cid % 2 else "red") for cid in IDS}
    src._hero_camp[106] = None
    src._hero_camp_ambiguous = {106}
    src._read_hero_array_at = reader
    return src


def main() -> int:
    primary = [row(i, cid) for i, cid in enumerate(IDS)]
    src = make_src([0x1000], lambda base: primary)
    states = src.read_hero_states()
    check("duplicate cid returns 10", len(states) == HERO_ARR_COUNT,
          f"got {len(states)}")
    check("duplicate cid preserved",
          Counter(r["id"] for r in states) == Counter(IDS))
    check("camp ambiguous marked",
          all(r["camp"] is None and r["camp_ambiguous"]
              for r in states if r["id"] == 106))
    check("slots complete", sorted(r["slot"] for r in states) == list(range(10)))

    # T2: 主副本缺 slot9，副副本同多重集，slot0 金币更新
    primary9 = primary[:9]
    secondary = [row(i, cid, gold=200 if i == 0 else 100)
                 for i, cid in enumerate(IDS)]

    def reader2(base):
        return primary9 if base == 0x1000 else secondary

    src2 = make_src([0x1000, 0x2000], reader2)
    states2 = src2.read_hero_states()
    check("secondary fills missing slot", len(states2) == HERO_ARR_COUNT,
          f"got {len(states2)}")
    check("secondary newer gold wins",
          next(r for r in states2 if r["slot"] == 0)["gold"] == 200)

    # T3: 副副本 ID 多重集不一致 -> 不混拼
    bad = [dict(r) for r in secondary]
    bad[9] = row(9, 999)

    def reader3(base):
        return primary9 if base == 0x1000 else bad

    src3 = make_src([0x1000, 0x2000], reader3)
    states3 = src3.read_hero_states()
    check("mismatched secondary rejected", len(states3) == 9,
          f"got {len(states3)}")
    check("no foreign id mixed in",
          all(r["id"] != 999 for r in states3))

    # T4: _locate_hero_arrays 用多重集定位，重复 cid 不被去重
    units = []
    for i, cid in enumerate(IDS):
        camp = "blue" if i < 5 else "red"
        units.append(MemUnit(actor=0x3000 + i * 0x100, config_id=cid,
                             actor_type=0, camp=camp, kind="hero"))
    src4 = MemSource()
    src4.pid = "123"
    src4.snapshot = lambda: units
    src4._read_hero_array_at = lambda base: (
        primary if base == 0x2000 else [])
    src4._u32 = lambda addr: 0
    src4.m._run = lambda *a, **k: b"base=0x216c\n"
    bases = src4._locate_hero_arrays()
    check("duplicate ids locate array", bases == [0x2000], str(bases))
    check("named constant sanity",
          SELF_OBJ_ID == 0x16C and HERO_ARR_STRIDE == 0x2AD8)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
