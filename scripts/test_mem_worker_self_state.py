# -*- coding: utf-8 -*-
"""MemWorker.apply() 的 canonical self_state 与单位合并回归。

背景：录制语料 self_state 0/15929，hok_bridge 只能拿 ui.hp 合成；
且 apply() 里曾用未定义的 self.src，异常被主循环吞掉，导致 apply 后半段
（mem_units / 野怪）静默跳过。这里用假 MemBattle 直接验证两条路径。

运行：venv/Scripts/python.exe -X utf8 scripts/test_mem_worker_self_state.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision.mem_battle import MemWorker  # noqa: E402

_PASS, _FAIL = [], []


def check(name, cond, extra=""):
    if cond:
        _PASS.append(name)
        print(f"PASS {name}")
    else:
        _FAIL.append(name)
        print(f"FAIL {name} {extra}")


class _FakeSrc:
    def read_hero_base_stats(self):
        return {}


class _FakeMB:
    def __init__(self):
        self.src = _FakeSrc()


HEROES = [
    {
        "id": 106, "camp": "blue", "hp": 800, "hp_max": 1000,
        "ep": 300, "ep_max": 500, "gold": 1234, "gold_earned": 4567,
        "gold_spent": 3333, "level": 7, "exp": 2800,
        "kills": 2, "deaths": 1, "assists": 3,
        "atk_range": 6500, "equip_ids": [1337, 1234],
        "respawn_s": 0, "dead": False, "x": 100.0, "z": 200.0,
        "skills": None, "summoner": None, "heal": None, "recall": None,
    },
    {
        "id": 131, "camp": "red", "hp": 500, "hp_max": 900,
        "level": 6, "dead": False,
    },
]

UNITS = [
    {"cid": 16100, "kind": "monster", "hp": 1200, "dead": False},
    {"cid": 16101, "kind": "monster", "hp": 0, "dead": True},
]


def main() -> int:
    worker = MemWorker(_FakeMB(), self_id=106)
    worker.latest = {"clock": 600, "heroes": HEROES, "units": UNITS}
    worker.ts = time.time()

    state = {}
    summary = worker.apply(state)
    self_state = state.get("self_state") or {}

    check("T1_self_state_written", self_state.get("cid") == 106, f"self={self_state}")
    check("T2_self_state_camp_hp",
          self_state.get("camp") == "blue"
          and self_state.get("hp") == 800
          and self_state.get("hp_max") == 1000,
          f"self={self_state}")
    check("T3_self_state_economy",
          self_state.get("gold") == 1234
          and self_state.get("gold_total") == 4567
          and self_state.get("gold_spent") == 3333
          and self_state.get("level") == 7,
          f"self={self_state}")
    check("T4_self_state_source",
          (state.get("sources") or {}).get("self") == "memory",
          f"sources={state.get('sources')}")
    check("T5_ui_hp_from_mem", state.get("ui", {}).get("hp") == 0.8,
          f"ui={state.get('ui')}")

    # apply 后半段（units 合并）必须真的执行：self.src 未定义时这里会是空的
    extra = state.get("extra") or {}
    check("T6_mem_units_merged", len(extra.get("mem_units") or []) == 2,
          f"extra={list(extra)}")
    check("T7_monster_alive_filter",
          extra.get("mem_monsters_alive") == [16100],
          f"alive={extra.get('mem_monsters_alive')}")
    check("T8_units_summary", summary.get("n_units") == 2, f"summary={summary}")

    # stale 快照不写：宁可缺，不给过期值
    worker.ts = time.time() - 999
    stale = {}
    out = worker.apply(stale)
    check("T9_stale_is_noop", out == {} and "self_state" not in stale,
          f"out={out} stale={stale}")

    print(f"\n{len(_PASS)} PASS / {len(_FAIL)} FAIL")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
