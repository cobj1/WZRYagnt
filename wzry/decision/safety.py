# -*- coding: utf-8 -*-
"""第 7 层安全动作（从 decide() 抽出，行为等价）。

两部分：
  - 塔圈逃生：开局 8s 后，距敌塔 < escape（或敌人在场且 < threat）先撤；
  - 低血兜底：HP < 30% 且 15s 内没刚回城过，近处有敌人/兵/野 -> 反向撤，
    否则满足回城门禁就回城。

只做"保命动作"，不生成技能；阈值由调用方传入，避免把 m2 常量搬进库层。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Sequence

Point = Sequence[float]


@dataclass(frozen=True)
class SafetyThresholds:
    turret_escape: float = 0.06
    turret_threat: float = 0.09
    min_match_age_s: float = 8.0
    low_hp: float = 0.30
    recall_holdoff_s: float = 15.0
    near_frac: float = 0.42
    escape_k: float = 0.22
    recall_throttle_s: float = 20.0


def tower_escape_action(*, towers: Sequence[Point], self_pos: Point,
                        enemies: Sequence[Point], now: float,
                        match_start_t: float, cooldowns: dict,
                        away: Callable[[Point], Point],
                        thresholds: SafetyThresholds = SafetyThresholds()) -> dict | None:
    if not towers:
        return None
    if now - float(match_start_t) <= thresholds.min_match_age_s:
        return None
    sx, sy = float(self_pos[0]), float(self_pos[1])

    def dist(t: Point) -> float:
        return math.hypot(t[0] - sx, t[1] - sy)

    near = min(towers, key=dist)
    d = dist(near)
    if not (d < thresholds.turret_escape
            or (enemies and d < thresholds.turret_threat)):
        return None
    cooldowns["escape_t"] = now
    cooldowns["turret_esc"] = (near[0], near[1])
    ax, ay = away(near)
    return {"type": "map_move", "nx": ax, "ny": ay, "reason": "escape_turret"}


def low_hp_fallback_action(*, hp: float, enemies: Sequence[Point],
                           minions: Sequence[Point], monsters: Sequence[Point],
                           dist_width: Callable[[float, float], float],
                           recall_clear: bool,
                           ready: Callable[[str, float], bool],
                           cooldowns: dict, now: float,
                           away: Callable[[Point], Point],
                           thresholds: SafetyThresholds = SafetyThresholds()) -> dict | None:
    hp = hp if 0 <= hp <= 1 else 1.0
    if hp >= thresholds.low_hp:
        return None
    if now - float(cooldowns.get("recall_t", 0.0)) <= thresholds.recall_holdoff_s:
        return None
    near = [s for s in list(enemies) + list(minions) + list(monsters)
            if dist_width(s[0], s[1]) < thresholds.near_frac]
    if near:
        esc = min(near, key=lambda s: dist_width(s[0], s[1]))
        ax, ay = away(esc)
        return {"type": "map_move", "nx": ax, "ny": ay,
                "reason": "low_hp_fallback_safe"}
    if recall_clear and ready("recall_t", thresholds.recall_throttle_s):
        cooldowns["recall_t"] = now
        return {"type": "recall", "reason": "low_hp_recall_first"}
    return None
