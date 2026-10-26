# -*- coding: utf-8 -*-
"""走位点生成（对应官方 `RuleTactics::GenMovePos @0x027ecc5c`）。

反汇编给出的结构（日志原文 + 立即数）：

```
multi_task_vec_model_result[%d].size():%d < 144        ← 模型输出是 **144 维**，少于此数直接跳过
top1:%d top2:%d current pos loc:%d,%d (%d,%d) dist:%d  ← 取 top1/top2 两个格子，换算成世界坐标
top1:%d top2:%d current 6s pos loc:%d,%d (%d,%d) dist:%d
too far or in obstacle pos:%d,%d                        ← 距离过远 / 在障碍里 一律丢弃
```

144 = **12×12 网格**。所以官方的走位不是"连续点"，而是"英雄周围 12×12 个候选格子里
挑模型分最高的两个，再按距离/障碍做二次校验"。我们拿不到模型，就把格子分数换成
`field.field_value`（价值场），**其余流程逐字保留**：top-k → 距离校验 → 障碍校验 → 输出。

立即数：`1000`（格距）、`10000`/`20000`（距离上下限，超出即 `too far`）。
"""
from __future__ import annotations

import math
from typing import List, Optional, Tuple

from . import field as vfield
from .snapshot import WORLD_X, WORLD_Z, Snapshot

GRID_N = 12                  # 144 = 12×12（官方 `size() < 144` 直接跳过）
GRID_STEP = 1000.0           # 格距（立即数 1000）
MAX_MOVE_DIST = 20000.0      # `too far` 上限（立即数 20000）
MIN_MOVE_DIST = 150.0        # 与 QuitMovePosWhenArrive 的到达阈值同源
TOPK = 2                     # 官方只取 top1/top2


def cells(snap: Snapshot, step: float = GRID_STEP, n: int = GRID_N) -> List[Tuple[int, float, float]]:
    """英雄周围的 n×n 个候选格子（世界坐标，已裁掉出界点）。"""
    if snap.self_x is None or snap.self_z is None:
        return []
    half = (n - 1) / 2.0
    out = []
    idx = 0
    for i in range(n):
        for j in range(n):
            x = snap.self_x + (i - half) * step
            z = snap.self_z + (j - half) * step
            if not (WORLD_X[0] <= x <= WORLD_X[1] and WORLD_Z[0] <= z <= WORLD_Z[1]):
                idx += 1
                continue
            out.append((idx, x, z))
            idx += 1
    return out


def decode(snap: Snapshot, scores: Optional[List[float]] = None,
           topk: int = TOPK) -> List[dict]:
    """解码 144 格 → top-k 候选。`scores` 缺省时用价值场打分（替代模型）。"""
    cs = cells(snap)
    if not cs:
        return []
    if scores is not None and len(scores) >= GRID_N * GRID_N:
        val = {i: float(scores[i]) for i, _, _ in cs}
    else:
        val = {i: vfield.field_value(snap, x, z) for i, x, z in cs}
    ranked = sorted(cs, key=lambda t: val[t[0]], reverse=True)[:topk]
    return [{"idx": i, "x": x, "z": z, "score": val[i]} for i, x, z in ranked]


def in_obstacle(snap: Snapshot, x: float, z: float) -> bool:
    """障碍判定。

    ⚠️ 我们没有导航网格（navmesh 不随包），这里只能做**代理判定**：
      - 出界 → 算障碍；
      - 落在**敌方**塔/水晶攻击圈内且自己血量偏低 → 算"不该去的点"。
    真实的 `in obstacle` 需要地图寻路数据，见 docs 已知缺口。
    """
    if not (WORLD_X[0] <= x <= WORLD_X[1] and WORLD_Z[0] <= z <= WORLD_Z[1]):
        return True
    me = snap.self_unit
    if me is None:
        return False
    if me.hp_rate >= 0.45:
        return False
    for tx, tz in vfield.enemy_tower_pts(snap):
        if math.hypot(tx - x, tz - z) < vfield.TOWER_ZONE:
            return True
    return False


def gen_move_pos(snap: Snapshot, scores: Optional[List[float]] = None,
                 topk: int = TOPK) -> Optional[dict]:
    """官方 `GenMovePos` 的等价实现：top-k 解码 + 距离/障碍校验，返回第一个通过的点。

    返回 `{point, idx, score, dist, rejected}`；全部被拒返回 None。
    """
    if snap.self_x is None:
        return None
    rejected = []
    for cand in decode(snap, scores, topk=topk):
        d = math.hypot(cand["x"] - snap.self_x, cand["z"] - snap.self_z)
        if d > MAX_MOVE_DIST:
            rejected.append((cand["idx"], "too_far"))
            continue
        if d < MIN_MOVE_DIST:
            rejected.append((cand["idx"], "too_close"))
            continue
        if in_obstacle(snap, cand["x"], cand["z"]):
            rejected.append((cand["idx"], "obstacle"))
            continue
        return {"point": (cand["x"], cand["z"]), "idx": cand["idx"],
                "score": cand["score"], "dist": d, "rejected": rejected}
    return None
