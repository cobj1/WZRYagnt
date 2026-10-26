# -*- coding: utf-8 -*-
"""第 4 层：单位视图解析（从 decide() 抽出）。

保持 v8.0 起的既有语义：
  - 小兵/塔/水晶不参与决策；enemy_minion 只作为钩子路径遮挡物；
  - 右上角头像区、左上角小地图区的框视为 UI 误检；
  - enemy_hero 需通过可信度过滤（幻影敌人/低置信框不进技能候选）；
  - ally_hero 需与友方血条位置匹配。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Sequence

Point = Sequence[float]


@dataclass
class UnitView:
    enemies: list[Point] = field(default_factory=list)
    minions: list[Point] = field(default_factory=list)
    turrets: list[Point] = field(default_factory=list)
    allies: list[Point] = field(default_factory=list)
    ally_minions: list[Point] = field(default_factory=list)
    monsters: list[Point] = field(default_factory=list)
    ally_turrets: list[Point] = field(default_factory=list)
    hook_blockers: list[Point] = field(default_factory=list)
    skipped: int = 0


def parse_units(units: Sequence[dict], *, w: float, h: float,
                ally_bars: Sequence[Sequence[float]] = (),
                trusted: Callable[[dict], bool] | None = None) -> UnitView:
    view = UnitView()
    for u in units:
        cls = str(u.get("cls", ""))
        scr = u.get("screen") or [0.5, 0.5, 0.0, 0.0]
        cx, cy = scr[0], scr[1]
        if cls in ("enemy_turret", "ally_turret", "enemy_hero", "ally_hero") \
                and cx > 0.72 and cy < 0.20:
            view.skipped += 1
            continue
        if cls in ("enemy_turret", "ally_turret") and cx < 0.18 and cy < 0.32:
            view.skipped += 1
            continue
        if cls == "enemy_minion":
            view.hook_blockers.append(scr)
        if cls in ("enemy_turret", "ally_turret", "enemy_minion", "ally_minion",
                   "enemy_crystal", "ally_crystal"):
            continue
        if cls == "enemy_hero":
            if trusted is None or trusted(u):
                view.enemies.append(scr)
        elif cls == "ally_hero":
            view.allies.append(scr)
        elif cls == "neutral_monster":
            view.monsters.append(scr)

    if view.allies and ally_bars:
        view.allies = [a for a in view.allies if any(
            math.hypot(b[0] / w - float(a[0]), b[1] / h - float(a[1])) < 0.20
            for b in ally_bars)]
    return view
