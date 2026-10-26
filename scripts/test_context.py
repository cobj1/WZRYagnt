# -*- coding: utf-8 -*-
"""单位视图解析单测（ARCH-03 第 4 层）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.decision.context import parse_units  # noqa: E402

PASS = []


def check(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print(f"PASS {name}")
    else:
        raise AssertionError(f"FAIL {name} {extra}")


def u(cls, x, y, trusted=True):
    return {"cls": cls, "screen": [x, y, 0.1, 0.1], "trusted": trusted}


def main() -> int:
    units = [
        u("enemy_hero", 0.5, 0.5),          # 正常敌人
        u("enemy_hero", 0.9, 0.1),          # 右上头像区 -> 丢弃
        u("enemy_hero", 0.4, 0.5, trusted=False),  # 不可信 -> 丢弃
        u("ally_hero", 0.55, 0.5),          # 友方，未给血条 -> 丢弃
        u("enemy_minion", 0.45, 0.6),       # 挡钩物，不进 minions
        u("enemy_turret", 0.5, 0.7),        # 塔不参与
        u("enemy_crystal", 0.5, 0.8),       # 水晶不参与
        u("neutral_monster", 0.6, 0.6),     # 野怪
        u("ally_turret", 0.3, 0.7),         # 友方塔不参与
    ]
    view = parse_units(units, w=1280.0, h=720.0, ally_bars=[],
                       trusted=lambda d: d.get("trusted", True))
    check("敌人过滤 UI 与低可信", len(view.enemies) == 1, f"{view.enemies}")
    check("无敌方兵进决策", view.minions == [])
    check("小兵作为挡钩物", len(view.hook_blockers) == 1)
    check("塔不参与", view.turrets == [])
    check("水晶不参与", all(a not in view.turrets for a in units))
    check("野怪保留", len(view.monsters) == 1)
    check("友方塔不进 allies", view.ally_turrets == [])

    bars = [(0.55 * 1280, 0.5 * 720)]
    view2 = parse_units([u("ally_hero", 0.55, 0.5)], w=1280.0, h=720.0,
                        ally_bars=bars)
    check("友方血条匹配", len(view2.allies) == 1)

    far = [(0.1 * 1280, 0.1 * 720)]
    view3 = parse_units([u("ally_hero", 0.55, 0.5)], w=1280.0, h=720.0,
                        ally_bars=far)
    check("友方血条不匹配则丢弃", view3.allies == [])

    if len(PASS) != 9:
        raise AssertionError(f"expected 9 checks, got {len(PASS)}")
    print(f"\nALL PASS ({len(PASS)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
