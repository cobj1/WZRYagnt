# -*- coding: utf-8 -*-
"""桥接层：把复刻版决策转成 m2 主循环认识的 action dict。

m2 的动作格式（执行层既有契约，不改）：
    {"type": "map_move", "nx": 0..1, "ny": 0..1, "reason": str}
    {"type": "skill", "id": 1..3, "mode": "tap"|"hold", "reason": str}
    {"type": "attack", "priority": "free"|"hero"|..., "reason": str}
    {"type": "none", "reason": str}

官方是"打哪个单位 + 用哪个技能"，我们执行层只有"技能槽"和"进攻姿态"，
所以攻击类动作**丢掉具体单位、保留目标类型**（与官方标签的语义一致：
标签本身就是"打哪类目标"，具体目标由锁敌在帧内决定）。
"""
from __future__ import annotations

from typing import Optional, Tuple

from .actions import Action, T_CRYSTAL, T_HERO, T_MONSTER, T_SOLDIER, T_TOWER
from .policy import Decision, OfficialAI
from . import geo
from .snapshot import Snapshot, from_state_dict

_TARGET_PRIORITY = {T_HERO: "hero", T_SOLDIER: "minion", T_TOWER: "tower",
                    T_CRYSTAL: "tower", T_MONSTER: "monster"}


def world_to_norm(x: float, z: float) -> Tuple[float, float]:
    """世界 -> 小地图归一化（下发给 `map_move` 的 nx/ny）。

    ⚠️ 必须与视觉侧同一口径：小地图 ny 向下、世界 z 向上，**反序**。见 `geo.py`。
    旧实现在此处按 z 正序换算，导致 official 架构的走位目标整体沿对角镜像 —— 
    追人变成逃、撤退变成进攻。
    """
    return geo.world_to_norm(x, z)


def to_legacy_action(act: Action, snap: Snapshot) -> dict:
    if act.kind == "move":
        if act.point is None:
            return {"type": "none", "reason": act.label}
        nx, ny = world_to_norm(act.point[0], act.point[1])
        return {"type": "map_move", "nx": nx, "ny": ny, "reason": act.label}
    if act.kind == "summoner":
        # 召唤师技能（治疗/闪现/惩击）：执行层点 `summoner` 标定按钮（id=5）
        # 用 operate 层的规范名（`OperateType.SUMMONER_SKILL.value`）；
        # `normalize_command` 同时接受小写别名 "summoner"。
        meta = act.meta or {}
        mode = str(meta.get("mode") or "tap")
        out = {"type": "SummonerSkill", "mode": mode, "reason": act.label}
        # ⚠️ 坐标空间：`skill_cast(drag)` 要的是**帧坐标(1280x720)**，
        #   而规则层算夹角用的是**世界坐标**。两者不能混用，所以分开两个字段：
        #     meta["target"]        世界坐标 —— 只给规则判方向（shanxian 89° 那条）
        #     meta["target_frame"]  帧坐标   —— 只给执行层拖拽
        #   只有世界坐标时退化为 tap（宁可不拖，也不拖到错的地方）。
        if meta.get("target_frame") is not None:
            out["target"] = meta["target_frame"]
        elif meta.get("target") is not None:
            out["mode"] = "tap"
            out["reason"] = f"{act.label}:no_frame_target"
        return out
    if act.kind == "skill" and act.skill_slot > 0:
        return {"type": "skill", "id": int(act.skill_slot), "mode": "tap",
                "reason": act.label}
    if act.kind == "attack":
        return {"type": "attack",
                "priority": _TARGET_PRIORITY.get(act.target_kind, "free"),
                "reason": act.label}
    return {"type": "none", "reason": act.label}


def decide_legacy(state: dict, ai: Optional[OfficialAI] = None,
                  self_id: Optional[int] = None,
                  atk_range: float = 650.0) -> Tuple[dict, Decision]:
    """一步到位：state_dict -> (legacy action, Decision)。"""
    ai = ai or OfficialAI(level="hard")
    snap = from_state_dict(state, self_id=self_id, atk_range=atk_range)
    dec = ai.decide(snap)
    return to_legacy_action(dec.action, snap), dec
