# -*- coding: utf-8 -*-
"""走位价值场（对应官方 `grid action` 的打分）。

官方把走位也交给模型（`[SelectTactics] label type is grid action`），我们没有模型，
所以用一个**显式价值场**代替：对任意点算一个标量，走位候选按"到达后的价值增量"打分。

场的构成（每项都对应官方特征里的一个量）：
  1. 交战项   ：离敌越近越值钱，但按"战力差"加权（残血时反向）
  2. 危险项   ：敌人在 301 危险半径内且自身血量低 → 强负
  3. 协同项   ：离队友越近越值钱（官方 `nearby_frd_hero_num` 的连续版）
  4. 塔区项   ：进入敌塔攻击圈 → 负（官方 `KeepAwayFromEnemyTower`）
  5. 推进项   ：朝敌方基地方向推进有正收益（官方 `arrived_strategy_target` / grid_tactics_state）
  6. 距离成本 ：越远越贵（避免无意义长途跋涉）

坐标一律**世界坐标**，与官方 301 危险半径同单位。
"""
from __future__ import annotations

import math
from typing import Optional

from .. import official_rules as orl
from .snapshot import MAP_DIAG, WORLD_X, WORLD_Z, Snapshot

# 权重（工程标定；改这里等于改"性格"，与官方配置表无关）
W_ENGAGE = 26.0
W_RISK = 55.0
W_COOP = 10.0
W_TOWER = 22.0
W_PUSH = 26.0
W_DIST = 0.5        # 必须小于推进项的斜率，否则"原地"永远最优
DANGER = float(orl.DANGER_DIST)      # 301
TOWER_ZONE = 850.0


def enemy_base(snap: Snapshot) -> tuple:
    """敌方基地方向（活体槽位 > 静态几何 > 按阵营取对角）。"""
    for name in ("enemy_crystal", "enemy_spring_tower"):
        u = snap.organ_slots.get(name)
        if u is not None and u.x is not None:
            return u.x, u.z
        g = snap.organ_geo.get(name)
        if g is not None:
            return g
    x = WORLD_X[1] if (snap.self_camp or 1) == 1 else WORLD_X[0]
    z = WORLD_Z[1] if (snap.self_camp or 1) == 1 else WORLD_Z[0]
    return x, z


def own_base(snap: Snapshot) -> tuple:
    for name in ("friend_crystal", "friend_spring_tower"):
        u = snap.organ_slots.get(name)
        if u is not None and u.x is not None:
            return u.x, u.z
        g = snap.organ_geo.get(name)
        if g is not None:
            return g
    x = WORLD_X[0] if (snap.self_camp or 1) == 1 else WORLD_X[1]
    z = WORLD_Z[0] if (snap.self_camp or 1) == 1 else WORLD_Z[1]
    return x, z


def enemy_tower_pts(snap: Snapshot) -> list:
    """敌方塔/水晶的位置：活体优先，缺活体数据时退回 22 槽位静态几何。

    静态几何只给位置、不给血量 —— 对"塔区惩罚"这个用途足够（宁可保守一点），
    但不要拿它当"塔还活着"的证据。
    """
    pts = [(o.x, o.z) for o in snap.organs
           if o.alive and o.x is not None and o.camp is not None
           and o.camp != snap.self_camp]
    if pts:
        return pts
    out = []
    for name, (gx, gz) in (snap.organ_geo or {}).items():
        if not name.startswith("enemy_"):
            continue
        if snap.organ_alive.get(name) is False:
            continue                    # 视觉明确说没了
        out.append((gx, gz))
    return out


def field_value(snap: Snapshot, x: float, z: float) -> float:
    """点 (x,z) 的价值。正值=好位置。"""
    me = snap.self_unit
    hp = me.hp_rate if me else 1.0
    v = 0.0

    foes = [h for h in snap.enemies() if h.alive and h.x is not None]
    if foes:
        d = [math.hypot(h.x - x, h.z - z) for h in foes]
        dmin = min(d)
        # 战力差：我方血量比例 - 敌方平均（近似官方 situation assessment）
        ehp = sum(h.hp_rate for h in foes) / len(foes)
        adv = hp - ehp
        # 交战：能打（adv>=0）才靠近；打不过就躲
        engage = math.exp(-dmin / 1200.0)
        v += W_ENGAGE * engage * (1.0 if adv >= -0.05 else -1.2)
        # 危险：301 内 + 血量低 -> 强负
        if dmin < DANGER:
            v -= W_RISK * (1.0 - hp) * (1.0 - dmin / DANGER) * 3.0
        # 血量低时整体回避敌人
        if hp < 0.45:
            v -= W_RISK * 0.6 * engage

    allies = [a for a in snap.allies() if a.alive and a.x is not None]
    if allies:
        da = min(math.hypot(a.x - x, a.z - z) for a in allies)
        v += W_COOP * math.exp(-da / 1000.0)

    towers = enemy_tower_pts(snap)
    if towers:
        dt = min(math.hypot(tx - x, tz - z) for tx, tz in towers)
        if dt < TOWER_ZONE:
            # 塔下惩罚：有兵线（我方兵在附近）时减轻
            near_min = any(m.alive and m.camp == snap.self_camp
                           and math.hypot((m.x or 0) - x, (m.z or 0) - z) < 600
                           for m in snap.minions)
            v -= W_TOWER * (0.35 if near_min else 1.0) * (1.0 - dt / TOWER_ZONE)

    bx, bz = enemy_base(snap)
    d_base = math.hypot(bx - x, bz - z)
    v += W_PUSH * (1.0 - min(1.0, d_base / MAP_DIAG))

    if snap.self_x is not None:
        v -= W_DIST * min(1.0, math.hypot(x - snap.self_x, z - snap.self_z) / 4000.0)
    return v


def move_delta(snap: Snapshot, x: float, z: float) -> float:
    """到达该点相对当前点的价值增量（走位打分就用它）。"""
    if snap.self_x is None or snap.self_z is None:
        return 0.0
    return field_value(snap, x, z) - field_value(snap, snap.self_x, snap.self_z)


def best_point(snap: Snapshot, radius: float = 2500.0, step: float = 500.0) -> Optional[tuple]:
    """在自身周围做粗网格搜索，返回价值最高的点（官方 grid action 的近似）。"""
    if snap.self_x is None:
        return None
    best, bestv = None, -1e9
    n = int(radius // step)
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            x = snap.self_x + i * step
            z = snap.self_z + j * step
            if not (WORLD_X[0] <= x <= WORLD_X[1] and WORLD_Z[0] <= z <= WORLD_Z[1]):
                continue
            v = field_value(snap, x, z)
            if v > bestv:
                best, bestv = (x, z), v
    return best
