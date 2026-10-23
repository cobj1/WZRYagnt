# -*- coding: utf-8 -*-
"""动作空间：复刻官方的「双手动作」标签体系。

官方证据（.rodata 标签串 + 日志）：
    [SelectTactics] label type is grid action / hero action /
                    multi task two hand action / two hand action
    [UnpredictProcess] choose target type:%d / choose skill slot:%d
    目标手：AtkHero / AtkSoldier / AtkTower / AtkCrystal / AtkMonster / AtkNorm / NoneAction
    技能手：AtkSkill1..4（另按目标细分 AtkHeroSkill1..3 / AtkSoldierSkill1..3 / AtkTowerSkill1..3）

所以官方不是"选一个高层战术"，而是**两手同时出**：打谁 × 用哪个技能槽。
走位是另一个 label 类型（grid action），本模块把它保留为 `MoveAction`。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from .features import skill_range
from .snapshot import Snapshot, Unit

# 目标手（官方拼写）
T_HERO = "Hero"
T_SOLDIER = "Soldier"
T_TOWER = "Tower"
T_CRYSTAL = "Crystal"
T_MONSTER = "Monster"
T_NONE = "None"

TARGET_CN = {T_HERO: "敌方英雄", T_SOLDIER: "敌方小兵", T_TOWER: "防御塔",
             T_CRYSTAL: "水晶", T_MONSTER: "野怪", T_NONE: "无目标"}


@dataclass
class Action:
    label: str                       # 官方风格标签，如 AtkHeroSkill1
    kind: str                        # attack / skill / move / none
    target_kind: str = T_NONE
    target: Optional[Unit] = None
    skill_slot: int = 0              # 0=普攻，1..3=技能槽
    point: Optional[Tuple[float, float]] = None   # 走位目标（世界坐标）
    meta: dict = field(default_factory=dict)

    @property
    def is_attack(self) -> bool:
        return self.kind in ("attack", "skill")

    def __str__(self) -> str:
        if self.kind == "move":
            return f"{self.label}->({self.point[0]:.0f},{self.point[1]:.0f})"
        if self.target is not None:
            return f"{self.label}[cid={self.target.cid} hp={self.target.hp_rate:.2f}]"
        return self.label


def _label(tk: str, slot: int) -> str:
    """官方标签拼接规则：目标手 + （技能手）。普攻不带后缀。"""
    if tk == T_NONE:
        return "NoneAction"
    if slot <= 0:
        return f"Atk{tk}"
    return f"Atk{tk}Skill{slot}"


def _skill_ready(u: Unit, slot: int) -> Tuple[bool, float]:
    """返回 (是否可用, 剩余比例)。本地英雄 level/state 恒 0，只能按 cd 判。"""
    for s in u.skills or []:
        if int(s.get("slot") or 0) == slot:
            left = int(s.get("cd_left_ms") or 0)
            total = int(s.get("cd_total_ms") or 0)
            return left <= 0, (left / total if total else 0.0)
    return False, 1.0


def enumerate_actions(snap: Snapshot, max_hero: int = 3, max_soldier: int = 3,
                      max_tower: int = 2, max_monster: int = 1,
                      allow_skills: Sequence[int] = (1, 2, 3)) -> List[Action]:
    """枚举可行双手动作（对齐官方"先过滤可行性，再交给模型打分"）。"""
    out: List[Action] = []
    me = snap.self_unit
    if me is None:
        return [Action("NoneAction", "none")]

    # 官方语义：标签 = "打哪类目标 + 用哪个技能槽"，**具体打哪一个由锁敌决定**
    # （AITargetSearcher::GetNearestEnemy 先筛出目标）。所以同一 (目标类型, 技能槽)
    # 只保留最近的那个单位 —— 既对齐语义，又天然保证标签唯一。
    best: Dict[tuple, tuple] = {}

    def consider(tk: str, u: Unit, rng: float, slot: int) -> None:
        # 无坐标单位不可作为攻击目标：`snap.dist` 会返回 MAP_DIAG 从而被 rng 过滤，
        #   但那是"碰巧对"，一旦有人改 dist 的缺失值语义就会漏；这里显式拒绝。
        if u.x is None or u.z is None:
            return
        # 官方锁敌过滤器（`targeting.TargetFilter`）：不可攻击 / 泉水内 一律不进候选。
        #   对应 IsTargetCanBeAttacked 与官方 tower-save/spring 判定。
        try:
            from . import targeting as _tg
            if not _tg._attackable(u) or _tg._in_spring(snap, u):
                return
        except Exception:
            pass
        d = snap.dist(u)
        if d > rng:
            return
        k = (tk, slot)
        if k not in best or d < best[k][0]:
            best[k] = (d, u, rng)

    def add(tk: str, u: Unit, rng: float) -> None:
        consider(tk, u, rng, 0)
        for slot in allow_skills:
            ok, ratio = _skill_ready(me, slot)
            if not ok:
                continue
            consider(tk, u, skill_range(me), slot)

    foes = sorted([h for h in snap.enemies() if h.alive], key=snap.dist)[:max_hero]
    for u in foes:
        add(T_HERO, u, float(me.atk_range) * 1.15)

    mins = sorted([m for m in snap.minions if m.alive and
                   (m.camp is None or m.camp != snap.self_camp)], key=snap.dist)[:max_soldier]
    for u in mins:
        add(T_SOLDIER, u, float(me.atk_range) * 1.15)

    towers = sorted([o for o in snap.organs if o.alive and
                     (o.camp is None or o.camp != snap.self_camp)], key=snap.dist)[:max_tower]
    for u in towers:
        # 塔的判定半径用塔自身攻击距离（官方 AttackEnemyTower 用 real_tower_atk_range）
        add(T_TOWER if u.cid in (92, 95) else T_CRYSTAL, u,
            float(me.atk_range) * 1.15 + float(u.atk_range) * 0.15)

    mons = sorted([m for m in snap.monsters if m.alive], key=snap.dist)[:max_monster]
    for u in mons:
        add(T_MONSTER, u, float(me.atk_range) * 1.15)

    for (tk, slot), (d, u, rng) in sorted(best.items()):
        ok, ratio = _skill_ready(me, slot) if slot else (True, 0.0)
        out.append(Action(_label(tk, slot), "attack" if slot == 0 else "skill",
                          tk, u, slot,
                          meta={"dist": d, "cd_ratio": ratio, "range": rng}))
    out.append(Action("NoneAction", "none"))
    return out


def enumerate_moves(snap: Snapshot, retreat: bool = False) -> List[Action]:
    """网格动作（官方 grid action）：8 向微操 + 撤退点 + 原地。"""
    out: List[Action] = []
    if snap.self_x is None:
        return [Action("Stand", "move", point=None)]
    step = 900.0
    for ang in range(8):
        a = ang * math.pi / 4
        out.append(Action(f"Move{ang}", "move", point=(
            snap.self_x + math.cos(a) * step, snap.self_z + math.sin(a) * step),
            meta={"ang": ang}))
    if retreat:
        d = snap.enemy_hero_min_dist()
        # 撤退必须真的往后走（旧实现 point=self，等于原地站着但标签叫 Retreat）
        out.append(Action("Retreat", "move", point=snap.retreat_point(),
                          meta={"retreat": True, "enemy_min_dis": d}))
    out.append(Action("Stand", "move", point=(snap.self_x, snap.self_z),
                      meta={"stand": True}))
    return out


def describe(a: Action) -> str:
    if a.kind == "move":
        return a.label
    if a.target is not None:
        return (f"{a.label}({TARGET_CN.get(a.target_kind, a.target_kind)} "
                f"cid={a.target.cid} hp={a.target.hp_rate:.2f} d={a.meta.get('dist', 0):.0f})")
    return a.label
