# -*- coding: utf-8 -*-
"""输出后处理：复刻官方 `SetPostOptimze` / `CalMainMoveDegree` / `SceneDeviationDegree`。

官方在模型出动作之后还有一层"人类可执行性"处理（日志与函数名证据）：
  - `ai_tactics::GameTacticsMaster::CalMainMoveDegree`（`dire:%d, cnt:%d`）：
    把目标点换算成**方向角**，并按主导方向做量化；
  - `TacticsMultiTaskTwoHandAction::SceneDeviationDegree`：不同场景允许的**角度偏差**不同；
  - 配置项 `max_single_move_degree` / `min_random_deviation_degree` /
    `max_deviation_degree` / `modify_move_degree`：单步最大转角与随机抖动上下限。

为什么要这一层：模型的输出是连续点，直接下发给摇杆会出现
"走位比人手更平滑"的反常行为，且同一局面每帧微小变化会导致抖动。
量化 + 限幅 + 场景偏差能同时解决这两点，也是官方"看起来像人"的原因之一。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .actions import Action
from .snapshot import Snapshot

# 官方配置项对应的工程默认值
STEP_DEG = 15.0                 # 方向量化粒度（对应 CalMainMoveDegree 的 cnt 分档）
MAX_SINGLE_MOVE = 2500.0        # 单步最大位移（对应 max_single_move_degree 的距离化）
MIN_DEVIATION = 0.0             # 最小随机偏差（度）
MAX_DEVIATION = 6.0             # 最大随机偏差（度）

# 场景偏差上限（对应 SceneDeviationDegree；官方按场景查表，这里给可调档）
SCENE_DEVIATION = {
    "default": 6.0,
    "return_city": 0.0,          # 回城路径不能歪
    "stuck_wall": 25.0,          # 卡墙时需要更大的方向扰动
    "atk_soldier": 8.0,
    "dy": 6.0,
}


def move_degree(snap: Snapshot, point: tuple) -> Optional[float]:
    """目标点相对自身的方位角（0..360，x 正方向为 0，逆时针为正）。"""
    if snap.self_x is None or point is None:
        return None
    dx = point[0] - snap.self_x
    dz = point[1] - snap.self_z
    if abs(dx) < 1e-6 and abs(dz) < 1e-6:
        return None
    return math.degrees(math.atan2(dz, dx)) % 360.0


def quantize_deg(deg: float, step: float = STEP_DEG) -> float:
    """方向量化：吸附到 step 的整数倍（对应官方 `cnt` 分档）。"""
    if step <= 0:
        return deg % 360.0
    return round(deg / step) * step % 360.0


def _deviation(snap: Snapshot, scene: str) -> float:
    """确定性抖动：同一帧同一场景结果一致（可复现），不同帧微变（像人）。"""
    cap = SCENE_DEVIATION.get(scene, SCENE_DEVIATION["default"])
    if cap <= 0:
        return 0.0
    seed = (int(snap.frame) * 2654435761 + hash(scene)) & 0xFFFF
    frac = (seed % 1000) / 1000.0          # 0..1
    return (MIN_DEVIATION + frac * (min(cap, MAX_DEVIATION) - MIN_DEVIATION))


def apply_post(snap: Snapshot, act: Action, scene: str = "default",
               step_deg: float = STEP_DEG,
               max_step: float = MAX_SINGLE_MOVE) -> Action:
    """对移动类动作做"量化 + 限幅 + 场景偏差"；非移动动作原样返回。"""
    if act.kind != "move" or act.point is None or snap.self_x is None:
        return act
    deg = move_degree(snap, act.point)
    if deg is None:
        return act
    d = math.hypot(act.point[0] - snap.self_x, act.point[1] - snap.self_z)
    dist = min(d, max_step)                      # 单步限幅
    deg = (quantize_deg(deg, step_deg) + _deviation(snap, scene)) % 360.0
    rad = math.radians(deg)
    pt = (snap.self_x + math.cos(rad) * dist, snap.self_z + math.sin(rad) * dist)
    meta = dict(act.meta or {})
    meta.update({"post": True, "deg": round(deg, 1), "dist": round(dist, 1),
                 "scene": scene})
    return Action(act.label, "move", point=pt, meta=meta)


@dataclass
class PostParams:
    """可调档（对应官方配置项）。"""
    step_deg: float = STEP_DEG
    max_step: float = MAX_SINGLE_MOVE
    enabled: bool = True
