# -*- coding: utf-8 -*-
"""第 5 层：接战评估（从 decide() 抽出，纯计算 + 有界缓存）。

只回答三个问题：现在能不能打、要不要先撤、回城是否安全。
不生成技能候选，也不改移动目标——那些仍属于规则层。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Sequence

Point = Sequence[float]


@dataclass(frozen=True)
class EngageThresholds:
    hp_min: float = 0.60
    max_enemies: int = 2
    kill_hp_max: float = 0.30
    kill_self_hp_min: float = 0.35
    press_enemy_hp_max: float = 0.35
    press_min_low_enemies: int = 2
    press_self_hp_min: float = 0.50
    press_stale_s: float = 0.6
    skill2_range_frac: float = 0.45
    recall_enemy_clear_frac: float = 0.65


@dataclass(frozen=True)
class EngageResult:
    hp: float
    nearest: Point | None
    nearest_dist: float
    kill_confirm: bool
    press: bool
    engage_floor: float
    engage_reason: str
    engage_ok: bool
    recall_clear: bool
    retreat_needed: bool
    n_low: int
    kill_new: bool
    press_new: bool


def _read_hp(ui: dict) -> float:
    hp = ui.get("hp")
    if hp is None:
        return 0.6
    try:
        hp = float(hp)
    except (TypeError, ValueError):
        return 0.6
    if hp != hp or not 0.0 <= hp <= 1.0:
        return 0.6
    return hp


def evaluate(*, ui: dict, enemies: Sequence[Point], enemy_bars: Sequence[Point],
             enemy_hp_scene: Sequence[float] | None, cooldowns: dict, now: float,
             w: float, h: float, aspect: float,
             nearest: Callable[[Sequence[Point]], Point | None],
             dist_width: Callable[[float, float], float],
             thresholds: EngageThresholds = EngageThresholds()) -> EngageResult:
    hp = _read_hp(ui)
    near = nearest(enemies)
    d_near = dist_width(near[0], near[1]) if near else 9.9

    lock_hp = ui.get("lock_target_hp")
    kill_confirm = bool(ui.get("lock_target") and lock_hp is not None
                        and float(lock_hp) <= thresholds.kill_hp_max
                        and hp >= thresholds.kill_self_hp_min)

    n_low = 0
    if enemy_hp_scene:
        n_low = sum(1 for r in enemy_hp_scene
                    if r is not None and float(r) <= thresholds.press_enemy_hp_max)
        cooldowns["n_low_enemies"], cooldowns["n_low_t"] = n_low, now
    else:
        cached = int(cooldowns.get("n_low_enemies") or 0)
        fresh = now - float(cooldowns.get("n_low_t", -9e9)) <= thresholds.press_stale_s
        n_low = cached if fresh else 0

    press = bool((not kill_confirm) and n_low >= thresholds.press_min_low_enemies
                 and hp >= thresholds.press_self_hp_min)
    kill_new = kill_confirm and (now - float(cooldowns.get("kill_log_t", -9e9)) > 5.0)
    if kill_new:
        cooldowns["kill_log_t"] = now
    press_new = press and (now - float(cooldowns.get("press_log_t", -9e9)) > 5.0)
    if press_new:
        cooldowns["press_log_t"] = now

    floor = (thresholds.kill_self_hp_min if kill_confirm
             else thresholds.press_self_hp_min if press
             else thresholds.hp_min)
    reason = ("kill_confirm_attack" if kill_confirm
              else "press_advantage_attack" if press else "engage_enemy")
    engage_ok = hp >= floor and len(enemies) <= thresholds.max_enemies
    recall_clear = (d_near > thresholds.recall_enemy_clear_frac
                    and not any(math.hypot(b[0] / w - 0.5, (b[1] / h - 0.5) * aspect)
                                <= thresholds.recall_enemy_clear_frac
                                for b in enemy_bars))
    retreat_needed = (not engage_ok) and near is not None \
        and d_near <= thresholds.skill2_range_frac
    return EngageResult(
        hp=hp, nearest=near, nearest_dist=d_near,
        kill_confirm=kill_confirm, press=press, engage_floor=floor,
        engage_reason=reason, engage_ok=engage_ok, recall_clear=recall_clear,
        retreat_needed=retreat_needed, n_low=n_low,
        kill_new=kill_new, press_new=press_new)
