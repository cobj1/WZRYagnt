# -*- coding: utf-8 -*-
"""第 7 层安全动作单测（ARCH-03）。"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.decision.safety import (  # noqa: E402
    SafetyThresholds, low_hp_fallback_action, tower_escape_action,
)

PASS = []


def check(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print(f"PASS {name}")
    else:
        raise AssertionError(f"FAIL {name} {extra}")


def dist_width(cx, cy):
    return math.hypot(cx - 0.5, (cy - 0.5) * 0.5625)


def away(p):
    return (0.1, 0.1)


def main() -> int:
    th = SafetyThresholds()
    cd = {}
    act = tower_escape_action(
        towers=[], self_pos=(0.5, 0.5), enemies=[], now=100.0,
        match_start_t=0.0, cooldowns=cd, away=away)
    check("无塔不动作", act is None)

    cd = {}
    act = tower_escape_action(
        towers=[(0.5, 0.5)], self_pos=(0.5, 0.5), enemies=[], now=5.0,
        match_start_t=0.0, cooldowns=cd, away=away)
    check("开局 8s 内不逃", act is None)

    cd = {}
    act = tower_escape_action(
        towers=[(0.5, 0.5)], self_pos=(0.54, 0.5), enemies=[], now=20.0,
        match_start_t=0.0, cooldowns=cd, away=away)
    check("塔圈内逃生", act and act["reason"] == "escape_turret")
    check("记录逃生时刻", cd.get("escape_t") == 20.0)
    check("记录逃生塔", cd.get("turret_esc") == (0.5, 0.5))

    cd = {}
    act = tower_escape_action(
        towers=[(0.5, 0.5)], self_pos=(0.55, 0.5), enemies=[(0.5, 0.5)],
        now=20.0, match_start_t=0.0, cooldowns=cd, away=away)
    check("有敌人时威胁圈更宽", act and act["reason"] == "escape_turret")

    cd = {}
    act = tower_escape_action(
        towers=[(0.5, 0.5)], self_pos=(0.7, 0.5), enemies=[(0.5, 0.5)],
        now=20.0, match_start_t=0.0, cooldowns=cd, away=away)
    check("圈外不逃", act is None)

    cd = {}
    act = low_hp_fallback_action(
        hp=0.5, enemies=[], minions=[], monsters=[], dist_width=dist_width,
        recall_clear=True, ready=lambda k, t: True, cooldowns=cd,
        now=100.0, away=away)
    check("血够不兜底", act is None)

    cd = {}
    act = low_hp_fallback_action(
        hp=0.29, enemies=[(0.5, 0.5)], minions=[], monsters=[],
        dist_width=dist_width, recall_clear=True, ready=lambda k, t: True,
        cooldowns=cd, now=100.0, away=away)
    check("低血近敌先撤", act and act["reason"] == "low_hp_fallback_safe")

    cd = {}
    act = low_hp_fallback_action(
        hp=0.29, enemies=[], minions=[], monsters=[],
        dist_width=dist_width, recall_clear=True, ready=lambda k, t: True,
        cooldowns=cd, now=100.0, away=away)
    check("安全时回城", act and act["reason"] == "low_hp_recall_first")
    check("记录回城时刻", cd.get("recall_t") == 100.0)

    cd = {}
    act = low_hp_fallback_action(
        hp=0.29, enemies=[], minions=[], monsters=[],
        dist_width=dist_width, recall_clear=False, ready=lambda k, t: True,
        cooldowns=cd, now=100.0, away=away)
    check("回城门禁不通过", act is None)

    cd = {"recall_t": 95.0}
    act = low_hp_fallback_action(
        hp=0.29, enemies=[(0.5, 0.5)], minions=[], monsters=[],
        dist_width=dist_width, recall_clear=True, ready=lambda k, t: True,
        cooldowns=cd, now=100.0, away=away)
    check("15s 内刚回城不重复", act is None)

    cd = {}
    act = low_hp_fallback_action(
        hp=0.29, enemies=[], minions=[], monsters=[(0.6, 0.6)],
        dist_width=dist_width, recall_clear=True, ready=lambda k, t: True,
        cooldowns=cd, now=100.0, away=away)
    check("野怪也算近处威胁", act and act["reason"] == "low_hp_fallback_safe")

    if len(PASS) != 14:
        raise AssertionError(f"expected 16 checks, got {len(PASS)}")
    print(f"\nALL PASS ({len(PASS)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
