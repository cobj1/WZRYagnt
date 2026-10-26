# -*- coding: utf-8 -*-
"""接战层单测（ARCH-03 抽层后的行为契约）。"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.decision.engage import evaluate  # noqa: E402

PASS = []


def check(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print(f"PASS {name}")
    else:
        raise AssertionError(f"FAIL {name} {extra}")


def dist_width(cx, cy):
    return math.hypot(cx - 0.5, (cy - 0.5) * 0.5625)


def nearest(lst):
    return min(lst, key=lambda p: dist_width(p[0], p[1])) if lst else None


def run(ui=None, enemies=(), bars=(), scene=None, now=10.0, cd=None):
    cd = {} if cd is None else cd
    return evaluate(ui=ui or {}, enemies=list(enemies), enemy_bars=list(bars),
                    enemy_hp_scene=scene, cooldowns=cd, now=now,
                    w=1280.0, h=720.0, aspect=0.5625,
                    nearest=nearest, dist_width=dist_width)


def main() -> int:
    r = run(ui={"hp": 0.8})
    check("无敌人可接战", r.engage_ok and not r.retreat_needed)
    check("无敌人默认距离", r.nearest_dist == 9.9)
    check("无敌人回城安全", r.recall_clear)
    check("常规门槛", r.engage_floor == 0.60 and r.engage_reason == "engage_enemy")

    r = run(ui={"hp": 0.3}, enemies=[(0.5, 0.5)])
    check("残血不接战", not r.engage_ok)
    check("近敌要求撤退", r.retreat_needed)
    check("撤退不改层", r.engage_reason == "engage_enemy")

    r = run(ui={"hp": 0.4, "lock_target": True, "lock_target_hp": 0.2},
            enemies=[(0.5, 0.5)])
    check("收人头放宽门槛", r.kill_confirm and r.engage_floor == 0.35)
    check("收人头可接战", r.engage_ok and not r.retreat_needed)
    check("收人头 reason", r.engage_reason == "kill_confirm_attack")
    check("收人头只记一次", r.kill_new is True)

    r = run(ui={"hp": 0.52}, enemies=[(0.5, 0.5)], scene=[0.1, 0.2, 0.9])
    check("占优放宽门槛", r.press and r.engage_floor == 0.50)
    check("占优残血计数", r.n_low == 2)
    check("占优可接战", r.engage_ok)

    cd = {}
    run(ui={"hp": 0.52}, enemies=[(0.5, 0.5)], scene=[0.1, 0.2, 0.9], cd=cd, now=10)
    r = run(ui={"hp": 0.52}, enemies=[(0.5, 0.5)], cd=cd, now=10.3)
    check("残血读数有界缓存", r.press and r.n_low == 2)
    r = run(ui={"hp": 0.52}, enemies=[(0.5, 0.5)], cd=cd, now=11.0)
    check("缓存过期归零", not r.press and r.n_low == 0)

    r = run(ui={"hp": 0.8}, bars=[(640.0, 360.0)])
    check("敌方血条挡回城", not r.recall_clear)

    r = run(ui={"hp": 0.8}, enemies=[(0.55, 0.55), (0.56, 0.55), (0.57, 0.55)])
    check("敌多不接战", not r.engage_ok)
    check("敌多且近要撤", r.retreat_needed)

    if len(PASS) != 19:
        raise AssertionError(f"expected 19 checks, got {len(PASS)}")
    print(f"\nALL PASS ({len(PASS)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
