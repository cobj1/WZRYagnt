# -*- coding: utf-8 -*-
"""勾中连招单测（ARCH-03 抽层）。"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.decision.combo import HookComboConfig, hook_confirm_action  # noqa: E402

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


def run(cd, enemies=((0.5, 0.5),), now=10.0, ready_keys=("summoner_t", "skill3_t")):
    def ready(key, thr):
        return key in ready_keys
    return hook_confirm_action(
        enemies=list(enemies), nearest=nearest, dist_width=dist_width,
        ready=ready, cooldowns=cd, now=now)


def main() -> int:
    cd = {}
    check("无 pending 不动作", run(cd) is None)

    cd = {"hook_pending": 5.0, "hook_anchor_dist": 0.5}
    check("过期清理", run(cd, now=7.0) is None and cd["hook_pending"] == 0.0)

    cd = {"hook_pending": 9.5, "hook_anchor_dist": 0.5}
    check("无敌人不动作", run(cd, enemies=(), now=10.0) is None)
    check("无敌人保留 pending", cd["hook_pending"] == 9.5)

    cd = {"hook_pending": 9.5, "hook_anchor_dist": 0.0}
    check("无锚点不动作", run(cd, now=10.0) is None)

    cd = {"hook_pending": 9.5, "hook_anchor_dist": 0.22}
    check("距离未收缩不动作", run(cd, enemies=((0.68, 0.5),), now=10.0) is None)
    check("未确认保留 pending", cd["hook_pending"] == 9.5)

    cd = {"hook_pending": 9.5, "hook_anchor_dist": 0.5,
          "summoner_t": 0.0, "skill3_t": 0.0}
    act = run(cd, now=10.0, ready_keys=())
    check("技能都不可用不返回", act is None)
    check("无可用项仍清 pending", cd["hook_pending"] == 0.0)

    cd = {"hook_pending": 9.5, "hook_anchor_dist": 0.5,
          "summoner_t": 0.0, "skill3_t": 0.0}
    act = run(cd, now=10.0)
    check("确认返回 combo", act and act["type"] == "combo")
    check("combo 两个动作", len(act["actions"]) == 2)
    check("combo reason", act["reason"] == "hook_confirmed")
    check("写 summoner 冷却", cd["summoner_t"] == 10.0)
    check("写大招冷却", cd["skill3_t"] == 10.0)
    check("写技能防抖", cd["skill"] == 10.0)

    cd = {"hook_pending": 9.5, "hook_anchor_dist": 0.5, "skill3_t": 0.0}
    act = run(cd, now=10.0, ready_keys=("skill3_t",))
    check("单项也返回", act and len(act["actions"]) == 1)
    check("单项仍写两项冷却", cd["summoner_t"] == 10.0 and cd["skill3_t"] == 10.0)

    cfg = HookComboConfig(confirm_s=0.2)
    cd = {"hook_pending": 9.5, "hook_anchor_dist": 0.5}
    act = hook_confirm_action(enemies=[(0.5, 0.5)], nearest=nearest,
                              dist_width=dist_width, ready=lambda k, t: False,
                              cooldowns=cd, now=10.0, cfg=cfg)
    check("配置缩短确认窗口", act is None and cd["hook_pending"] == 0.0)

    if len(PASS) != 18:
        raise AssertionError(f"expected 17 checks, got {len(PASS)}")
    print(f"\nALL PASS ({len(PASS)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
