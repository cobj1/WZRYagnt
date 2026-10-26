# -*- coding: utf-8 -*-
"""第 6 层片段：勾中确认连招（从 decide() 抽出，行为等价）。

命中判据：二技能释放后 HOOK_CONFIRM_S 内，最近敌人与自己的屏幕距离
相对释放锚点收缩到 HOOK_DIST_SHRINK 且绝对收缩 > min_delta。
确认后按可用性追加召唤师/大招；原实现无论追加几项都会同时写两个冷却，
这里保持不变（见 test_combo 的断言），避免"顺手修 bug"改变线上行为。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

Point = Sequence[float]


@dataclass(frozen=True)
class HookComboConfig:
    confirm_s: float = 1.0
    shrink: float = 0.72
    min_delta: float = 0.12
    summoner_throttle_s: float = 30.0
    skill3_active_s: float = 3.5


def hook_confirm_action(
    *,
    enemies: Sequence[Point],
    nearest: Callable[[Sequence[Point]], Point | None],
    dist_width: Callable[[float, float], float],
    ready: Callable[[str, float], bool],
    cooldowns: dict,
    now: float,
    cfg: HookComboConfig = HookComboConfig(),
) -> dict | None:
    pending = float(cooldowns.get("hook_pending", 0.0))
    if pending <= 0:
        return None
    if now - pending > cfg.confirm_s:
        cooldowns["hook_pending"] = 0.0
        return None
    anchor = float(cooldowns.get("hook_anchor_dist", 0.0))
    near = nearest(enemies)
    if near is None or anchor <= 0:
        return None
    d = dist_width(near[0], near[1])
    if not (d < anchor * cfg.shrink and (anchor - d) > cfg.min_delta):
        return None

    cooldowns["hook_pending"] = 0.0
    actions: list[dict] = []
    if ready("summoner_t", cfg.summoner_throttle_s):
        actions.append({"type": "summoner"})
    if ready("skill3_t", cfg.skill3_active_s):
        actions.append({"type": "skill", "id": 3, "mode": "tap"})
    if not actions:
        return None
    cooldowns["summoner_t"] = now
    cooldowns["skill3_t"] = now
    cooldowns["skill"] = now
    return {"type": "combo", "actions": actions, "reason": "hook_confirmed"}
