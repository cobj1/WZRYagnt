# -*- coding: utf-8 -*-
"""战斗动作生成单测（ARCH-03）。"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.decision.combat import CombatConfig, combat_action  # noqa: E402

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


def run(cd=None, *, near=(0.5, 0.5), enemies=None, engage=True,
        ready_keys=("skill2_t", "skill1_t", "attack_t"), mp=1.0,
        blocked=False, has_yolo=True, states=None, now=10.0, cfg=None):
    cd = {} if cd is None else cd
    if enemies is None:
        enemies = [near] if near else []
    def ready(k, t):
        if k not in ready_keys:
            return False
        return now - float(cd.get(k, 0.0)) > t

    return combat_action(
        enemies=enemies, enemy_bars=[], nearest=nearest, dist_width=dist_width,
        hook_path_blocked=lambda p: blocked,
        ready=ready, cooldowns=cd, now=now,
        engage_ok=engage, engage_reason="engage_enemy",
        skill_states=states or {}, mp=mp, has_yolo_enemy=has_yolo,
        cfg=cfg or CombatConfig())


def main() -> int:
    check("无敌人证据不动作", run(near=None) is None)

    cd = {}
    check("二技能优先", run(cd)["id"] == 2)
    check("二技能记录 pending", cd.get("hook_pending") == 10.0)
    check("二技能记录锚距", cd.get("hook_anchor_dist") == 0.0)

    cd = {"skill2_t": 9.0}
    check("二技能冷却时一技能", run(cd)["id"] == 1)
    check("一技能写冷却", cd.get("skill1_t") == 10.0)

    cd = {"skill2_t": 9.0, "skill1_t": 9.0}
    check("技能都冷却时普攻", run(cd)["type"] == "attack")
    check("普攻写冷却", cd.get("attack_t") == 10.0)

    cd = {}
    check("未接战不动作", run(cd, engage=False) is None)

    cd = {}
    check("蓝量低省蓝", run(cd, near=(0.7, 0.5), mp=0.1) is None)

    cd = {}
    check("钩路径被挡不放二技能",
          run(cd, blocked=True, ready_keys=("skill2_t",)) is None)

    cd = {"skill": 9.9}
    act = run(cd, now=10.0)
    check("防抖命中", act == {"type": "none", "reason": "skill_debounce"})

    cd = {}
    act = run(cd, states={"2": {"unlocked": False}})
    check("二技能未解锁跳过", act is None or act.get("id") != 2)

    cd = {}
    act = run(cd, mp=0.5, states={"1": {"unlocked": False}},
              ready_keys=("skill1_t",))
    check("一技能未解锁不普攻", act is None)

    far = (0.9, 0.5)
    cd = {"skill2_t": 9.5, "skill1_t": 9.5}
    act = run(cd, near=far)
    check("超距不普攻", act is None)

    cd = {"skill2_t": 9.5, "skill1_t": 9.5}
    act = run(cd, near=(0.52, 0.5))
    check("近战普攻", act and act["type"] == "attack")

    if len(PASS) != 16:
        raise AssertionError(f"expected 16 checks, got {len(PASS)}")
    print(f"\nALL PASS ({len(PASS)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
