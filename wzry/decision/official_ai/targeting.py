# -*- coding: utf-8 -*-
"""锁敌：复刻官方 `AITargetSearcher::GetNearestEnemy` 的过滤器语义。

官方证据（`docs/wzry-ai-reverse.md` §9.3）：
  - 外壳 `0x34e037c` 打日志后，用 setter 表（基址 `0x508f000`）灌 ~10 个过滤参数；
  - `+0x250` / `+0x260` 设整型过滤参数，`+0x2d0` 设布尔，`+0x300` 提交检索；
  - 真身 `0x34de91c` 是"多过滤器检索"（6 个寄存器参数 + 9 个栈上标志）；
  - 执行函数 `0x2593820` 把参数**序列化成查询缓冲**（`strb #0xc`、`str w8,[sp,#4]`…），
    说明每个参数是枚举而不是自由数值。

我们能确定的部分来自引擎自己的行为树节点名（`docs/wzry-ai-data-inputs.md` §4）：
    GetNearestEnemyWithPriorityWithoutJungleMonster / ...WithoutActor /
    GetNearestEnemyIgnoreVisible / GetNearestEnemyInGivenTargetList /
    GetNearestEnemyWithFilterByIncludeDying / IsActorFilterVisible /
    IsTargetCanBeAttacked / IsActorInBattle / IsActorForbidSelect
→ 归纳出官方至少支持这几类过滤：**类型 / 距离 / 可见性 / 可攻击性 / 是否在战斗 /
  是否允许残血目标 / 白名单 / 黑名单 / 优先级排序**。

**证据分级**：过滤器"存在"是 name 级（节点名 + setter 表）；具体阈值是工程值。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

from .actions import T_CRYSTAL, T_HERO, T_MONSTER, T_SOLDIER, T_TOWER
from .snapshot import Snapshot, Unit

# 单位类别 -> 官方目标手标签
KIND_LABEL = {"hero": T_HERO, "minion": T_SOLDIER, "organ": None, "monster": T_MONSTER}
# 建筑再按 cid 分塔/水晶
TOWER_CIDS = (92, 95)


def kind_of(u: Unit) -> str:
    if u.kind == "organ":
        return "tower" if u.cid in TOWER_CIDS else "crystal"
    return u.kind


@dataclass
class TargetFilter:
    """一次锁敌查询。字段名尽量贴官方过滤器语义。"""
    kinds: Tuple[str, ...] = ("hero", "tower", "crystal", "minion", "monster")
    max_dist: float = 3500.0
    require_visible: bool = True       # 对应 IsActorFilterVisible / GetNearestEnemyIgnoreVisible 的反面
    require_attackable: bool = True    # 对应 IsTargetCanBeAttacked
    in_battle_only: bool = False       # 对应 IsActorInBattle
    include_dying: bool = False        # 对应 GetNearestEnemyWithFilterByIncludeDying
    min_hp_rate: float = 0.0
    exclude_in_spring: bool = True     # 泉水里的目标不可选（官方 tower save / spring 判定）
    priority: Tuple[str, ...] = ("hero", "tower", "crystal", "minion", "monster")
    blacklist: Tuple[int, ...] = ()    # cid 黑名单
    whitelist: Tuple[int, ...] = ()    # 非空时只允许这些 cid


def in_main_camp(snap: Snapshot, x: Optional[float] = None) -> bool:
    """是否在己方半场。对应 `RuleTactics::IsInMainHeroCamp`（0x027e287c）。

    官方日志同时打 `IsInEnemyHeroCamp` / `IsInMainHeroCamp`，函数内立即数 10000
    （与地图半场尺度同量级）。蓝方基地在负 x 侧、红方在正 x 侧，故按符号判。
    """
    if x is None:
        x = snap.self_x
    if x is None:
        return True
    return (x < 0) if (snap.self_camp or 1) == 1 else (x > 0)


def _visible(snap: Snapshot, u: Unit) -> bool:
    """可见性：官方有视野/草丛两套判据；我们只有"距离 + 是否在草丛"的近似。"""
    d = snap.dist(u)
    if d <= 1200:
        return True
    # 远处只信"在视野半径内"的估计（工程近似，官方是服务端可见性）
    return d <= 2000


def _attackable(u: Unit) -> bool:
    if not u.alive or (u.hp is not None and u.hp <= 0):
        return False
    return True


def _in_spring(snap: Snapshot, u: Unit) -> bool:
    """是否在（任一方）泉水框内。复用 rules 的泉水方框常量。"""
    from .rules import SPRING_BOX_HALF_X, SPRING_BOX_HALF_Z
    for name in ("friend_spring_tower", "enemy_spring_tower"):
        s = (snap.organ_slots or {}).get(name)
        g = (snap.organ_geo or {}).get(name)
        cx, cz = (s.x, s.z) if (s is not None and s.x is not None) else (g if g else (None, None))
        if cx is None:
            continue
        if abs((u.x or 0) - cx) <= SPRING_BOX_HALF_X and abs((u.z or 0) - cz) <= SPRING_BOX_HALF_Z:
            return True
    return False


def candidates(snap: Snapshot, f: TargetFilter) -> List[Unit]:
    """按过滤器筛出候选，并按 (优先级, 距离) 排序。"""
    out: List[Unit] = []
    for u in list(snap.heroes) + list(snap.minions) + list(snap.organs) + list(snap.monsters):
        if u.cid == snap.self_cid:
            continue
        if u.camp is not None and u.camp == snap.self_camp:
            continue                      # 只选敌方
        k = kind_of(u)
        if k not in f.kinds:
            continue
        if f.whitelist and u.cid not in f.whitelist:
            continue
        if u.cid in f.blacklist:
            continue
        if f.require_attackable and not _attackable(u):
            continue
        if not f.include_dying and u.hp is not None and 0 < u.hp <= 0:
            continue
        if f.min_hp_rate > 0 and u.hp_rate < f.min_hp_rate:
            continue
        if f.require_visible and not _visible(snap, u):
            continue
        if f.exclude_in_spring and _in_spring(snap, u):
            continue
        d = snap.dist(u)
        if d > f.max_dist:
            continue
        out.append(u)

    prio = {k: i for i, k in enumerate(f.priority)}
    out.sort(key=lambda u: (prio.get(kind_of(u), 99), snap.dist(u)))
    return out


def select_target(snap: Snapshot, f: Optional[TargetFilter] = None) -> Optional[Unit]:
    """返回唯一主目标（官方 `GetNearestEnemy` 的返回值）。"""
    f = f or TargetFilter()
    c = candidates(snap, f)
    return c[0] if c else None


def per_kind_best(snap: Snapshot, f: Optional[TargetFilter] = None) -> dict:
    """每类目标各取一个最近的（动作空间用：每类只保留一个，标签天然唯一）。"""
    f = f or TargetFilter()
    out = {}
    for u in candidates(snap, f):
        k = kind_of(u)
        if k not in out:
            out[k] = u
    return out
