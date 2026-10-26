# -*- coding: utf-8 -*-
"""UI 区判据：把"落在 HUD 图标区的检测框"识别出来并剔除。

背景（2026-10-26 golden 核验实测）：71 帧里 **13 帧**存在小地图/头像/技能按钮区的
误检框，占全部误检的 **56%**（25 个 FP 中 14 个）。决策层
（`wzry/decision/context.py:parse_units`）早就按坐标过滤了这些框，但**录制与评估
链路没有** —— 于是 `states.jsonl` 与 golden 候选都带着它们。

在 golden（59 帧人工标注）上量化（imgsz=640 / threads=8，微平均）：

| 过滤 | P | R | F1 | TP | FP | FN |
|---|---:|---:|---:|---:|---:|---:|
| 不过滤 | 0.788 | 0.979 | 0.873 | 93 | 25 | 2 |
| 仅决策层判据 | 0.793 | 0.968 | 0.872 | 92 | 24 | 3 |
| **本模块（all）** | **0.893** | **0.968** | **0.929** | 92 | 11 | 3 |

代价是 1 个真框：`enemy_turret @ (0.82, 0.16)`（塔身从右上头像区后面露出来）。
净收益 +0.056 F1，因此**录制侧默认开启**。

⚠️ 只对"有 `screen` 归一化坐标"的单位生效；内存单位（`coord_src=memory`）没有
screen 坐标，不参与过滤。
"""
from __future__ import annotations

from typing import Iterable, Optional

# (名称, x0, y0, x1, y1) —— 归一化，判据是**框中心**是否落在区内
ZONES = [
    ("minimap", 0.000, 0.000, 0.185, 0.300),   # 左上小地图
    ("topbar", 0.000, 0.000, 1.000, 0.055),    # 顶部计分条
    ("portrait", 0.720, 0.000, 1.000, 0.200),  # 右上头像/经济条
    ("skillbar", 0.000, 0.800, 1.000, 1.000),  # 底部技能/摇杆带
]

# 只对这些类做 UI 过滤：小兵/野怪很少出现在 HUD 上，且它们不参与战斗决策
FILTER_CLASSES = {
    "enemy_hero", "ally_hero", "enemy_turret", "ally_turret",
    "enemy_crystal", "ally_crystal", "enemy_minion", "ally_minion",
}


def zone_of(cls: str, cx: float, cy: float) -> Optional[str]:
    """框中心落在哪个 UI 区；不在任何区则 None。"""
    if cls not in FILTER_CLASSES:
        return None
    for name, x0, y0, x1, y1 in ZONES:
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            return name
    return None


def filter_units(units: Iterable[dict]) -> tuple[list[dict], dict]:
    """剔除 UI 区单位，返回 (保留列表, {区名: 剔除数})。

    没有 `screen`（内存单位/视觉缺失）的原样保留——不可判就不动。
    """
    keep: list[dict] = []
    dropped: dict[str, int] = {}
    for u in units or []:
        scr = u.get("screen")
        if not scr or len(scr) < 2:
            keep.append(u)
            continue
        z = zone_of(str(u.get("cls") or ""), float(scr[0]), float(scr[1]))
        if z:
            dropped[z] = dropped.get(z, 0) + 1
            continue
        keep.append(u)
    return keep, dropped
