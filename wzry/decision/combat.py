# -*- coding: utf-8 -*-
"""第 6 层片段：战斗动作生成（从 decide() 抽出，行为等价）。

输入只有"接战是否允许 + 目标距离 + 技能可用性"，输出技能/普攻动作；
不做目标选择、不评估接战门槛（那些在 context/engage 层）。
优先级保持线上顺序：二技能（勾）→ 一技能 → 普攻。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

Point = Sequence[float]


@dataclass(frozen=True)
class CombatConfig:
    skill2_range: float = 0.45
    skill1_range: float = 0.18
    melee_frac: float = 0.10
    skill2_throttle_s: float = 3.0
    skill1_throttle_s: float = 1.5
    attack_throttle_s: float = 0.6
    skill_debounce_s: float = 0.35
    mp_save_below: float = 0.20
    ult_active_s: float = 3.5


def combat_action(*, enemies: Sequence[Point], enemy_bars: Sequence[Point],
                  nearest: Callable[[Sequence[Point]], Point | None],
                  dist_width: Callable[[float, float], float],
                  hook_path_blocked: Callable[[Point], bool],
                  ready: Callable[[str, float], bool], cooldowns: dict,
                  now: float, engage_ok: bool, engage_reason: str,
                  skill_states: dict, mp: float, has_yolo_enemy: bool,
                  cfg: CombatConfig = CombatConfig()) -> dict | None:
    if not (has_yolo_enemy or enemy_bars):
        return None
    near = nearest(enemies) if enemies else None
    d = dist_width(near[0], near[1]) if near else 0.90
    s1 = skill_states.get("1") or {}
    s2 = skill_states.get("2") or {}
    in_ult = now - float(cooldowns.get("skill3_t", 0.0)) <= cfg.ult_active_s
    mp_saving = float(mp) < cfg.mp_save_below
    blocked = near is not None and hook_path_blocked(near)

    if now - float(cooldowns.get("skill", -9.0)) < cfg.skill_debounce_s:
        return {"type": "none", "reason": "skill_debounce"}

    if (engage_ok and near is not None and d <= cfg.skill2_range
            and ready("skill2_t", cfg.skill2_throttle_s)
            and not mp_saving and not blocked
            and s2.get("unlocked") is not False):
        cooldowns["skill2_t"] = now
        cooldowns["skill"] = now
        cooldowns["hook_pending"] = now
        cooldowns["hook_anchor_dist"] = d
        return {"type": "skill", "id": 2, "mode": "tap",
                "reason": "enemy_in_skill2_range"}

    if (engage_ok and not in_ult and not mp_saving
            and d <= cfg.skill1_range
            and ready("skill1_t", cfg.skill1_throttle_s)
            and s1.get("unlocked") is not False and has_yolo_enemy):
        cooldowns["skill1_t"] = now
        cooldowns["skill"] = now
        return {"type": "skill", "id": 1, "mode": "tap",
                "reason": "enemy_hero_near"}

    if engage_ok and d <= cfg.melee_frac and ready("attack_t", cfg.attack_throttle_s) \
            and has_yolo_enemy:
        cooldowns["attack_t"] = now
        return {"type": "attack", "priority": "free", "reason": engage_reason}
    return None
