# -*- coding: utf-8 -*-
"""唯一坐标口径：世界坐标 <-> 小地图归一化坐标。

**为什么单独一个模块**：这套换算原先在三个地方各写了一遍，且 z（纵向）轴方向不一致：

| 位置 | nx | ny | 结果 |
| --- | --- | --- | --- |
| `wzry/vision/mem_source.py::MemUnit.minimap_norm` | `(x-X_MIN)/W` | `(Z_MAX-z)/H` | ✅ 与画面一致（ny 向下） |
| `official_ai/bridge.py::world_to_norm` | `(x-X_MIN)/W` | `(z-Z_MIN)/H` | ❌ z 轴反向 |
| `official_ai/snapshot.py::_screen_to_world` | 反算 | `z = Z_MIN+ny*H` | ❌ z 轴反向（与上行互为逆，闭环自洽但整体镜像） |

标定依据（`mem_source.py` 注释，真机实测）：

    蓝方出生点 (-56379, -53367) ≈ 小地图 (0.02, 0.98)   ← 左下
    红方出生点 ( 55757,  57652) ≈ 小地图 (0.98, 0.02)   ← 右上

即世界坐标 x 向右、z 向上；小地图 ny 向右下。所以 **ny 必须由 z 反序得到**。
桥接层（决策 -> `map_move` 的 nx/ny）与视觉兜底（屏幕点 -> 世界）必须共用本模块，
否则"往左下撤退"会被下成"往右上"。
"""
from __future__ import annotations

from typing import Optional, Tuple

# 世界坐标范围（来自 mem_source 标定；不要在此之外另立常量）
MAP_X_MIN, MAP_X_MAX = -58700.0, 58100.0
MAP_Z_MIN, MAP_Z_MAX = -55700.0, 60000.0

MAP_W = MAP_X_MAX - MAP_X_MIN          # 116800
MAP_H = MAP_Z_MAX - MAP_Z_MIN          # 115700

# 阵营基地角落（小地图归一化）：camp1 左下、camp2 右上
CAMP_CORNER = {1: (0.0, 1.0), 2: (1.0, 0.0)}


def clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else float(v))


def world_to_norm(x: float, z: float) -> Tuple[float, float]:
    """世界坐标 -> 小地图归一化 (nx, ny)，ny 向下（与画面/小地图图像一致）。"""
    nx = (float(x) - MAP_X_MIN) / MAP_W
    ny = (MAP_Z_MAX - float(z)) / MAP_H
    return clamp01(nx), clamp01(ny)


def norm_to_world(nx: float, ny: float) -> Tuple[float, float]:
    """小地图归一化 (nx, ny) -> 世界坐标 (x, z)。`world_to_norm` 的严格逆。"""
    x = MAP_X_MIN + clamp01(nx) * MAP_W
    z = MAP_Z_MAX - clamp01(ny) * MAP_H
    return x, z


def norm_or_none(x: Optional[float], z: Optional[float]) -> Optional[Tuple[float, float]]:
    if x is None or z is None:
        return None
    return world_to_norm(x, z)


def dist_norm(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """归一化平面上的欧氏距离（0..~1.41）。"""
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def dist_world(x1: float, z1: float, x2: float, z2: float) -> float:
    return ((float(x1) - float(x2)) ** 2 + (float(z1) - float(z2)) ** 2) ** 0.5
