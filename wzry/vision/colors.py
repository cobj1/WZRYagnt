# -*- coding: utf-8 -*-
"""HSV 颜色判据的**单一事实来源**（v15.3）。

## 为什么需要

视觉层有 8+ 个模块各自内联"蓝/红/绿/黄"的 HSV 阈值，实测存在 **30 组不同数值**：
仅"蓝"就有 10 组（`H70-135/S>60` 用于框内像素判阵营，`H90-135/S>60` 用于头顶血条，
`H85-140/S>35/V>35` 用于放宽的自血条…）。散落的结果是**改一处漏两处**，已经出过事故：

  - `top_status_bars` 的注释自述：真机上"扫不到成排血条"，根因就是掩码色域口径漂移（缺 green 掩码）；
  - `hero_hp` / `hok_yolov5` 的"红条"阈值分别是 `H<=15|H>=168` 与 `H<=10|H>=170`，
    同一根血条在两处判据不同。

## 本模块的立场（重要）

**只做"同一数值收敛到一处"，不强行统一不同数值。** 那些不同数值是各自 ROI / 光照 /
降采样路径下调出来的，统一它们会改变行为，必须逐处用真机帧验证后才能做（本项目铁律：
变更不得影响功能）。本模块把 30 组数值**集中命名并标注出处**，让"哪些是真重复、哪些是
真差异"一眼可见，后续统一才有依据。

## 规格格式

`spec = (h_lo, h_hi, s_lo, s_hi, v_lo, v_hi)`，`None` 表示该通道不约束（0/255 全开）。
**H 环绕**：当 `h_lo > h_hi` 时按红色环绕处理 -> `(H <= h_hi) | (H >= h_lo)`（如 `(165, 15)` = `H<=15 | H>=165`）。
严格不等号用 ±1 表达（`S > 60` -> `s_lo = 61`；`V >= 80` -> `v_lo = 80`）。

## 尚未接入（非生产路径，暂不动）

`wzry/vision/minimap.py` 的 `COLOR_THRESH`（盘式小地图，仅 `understanding.py` / 脚本使用）。

## 合并可行性实测（2026-10-16，`temp/_colors_ab.py`，请勿凭直觉合并）

在 160 个真实 ROI（80 检测框 / 60 头顶血条带 / 10 顶部头像条 / 10 小地图 / 全帧）上逐对比较掩码：
**31 组预设里没有任何一对能产生相同掩码** —— 所以"统一不同数值"**必然改变行为**，不是重构。
按差异从小到大排序，只有下面几对还算接近，其余是结构性不同（如蓝 `BLUE_BAR` vs `BLUE_MM` 差 13.2%）：

| 对 | 差异像素占比 | 差异来源 |
|---|---|---|
| `RED_CAMP` vs `RED_SEG` | 0.093% | 仅 V 门（无 vs `V>60`） |
| `RED_CAMP` vs `RED_LOCK` | 0.214% | H 上界 165 vs 168 + S 门 60 vs 80 |
| `RED_MM` vs `RED_TOWER` | 0.268% | 仅 S 门 40 vs 50 |
| `BLUE_SEG` vs `BLUE_MP` | 0.373% | 仅 S 门 55 vs 60 |
| `BLUE_MP` vs `BLUE_HUD` | 0.470% | H 上界 135 vs 140 + S 门 60 vs 60(≥) |

**结论**：这 5 对是"只差一个门限"的候选，若将来要合并，必须**先做真机 A/B 确认该门限对下游
指标无影响**；其余（尤其蓝 10 组、绿 7 组）是各自 ROI/光照下调出来的结构性差异，不要合并。
注：真正的重复（`detector.camp_blue_red_fracs` 与 `hok_yolov5.camp_blue_red_fracs` 逐字相同）
已在收敛时合并为 `BLUE_CAMP` / `RED_CAMP` 共用。
"""
from __future__ import annotations

import cv2
import numpy as np

# =====================================================================
# 绿（自己 / 血条底 / 小地图底）
# =====================================================================
GREEN_SCAN = (35, 90, 51, None, None, None)        # H35-90,S>50          self_bars.detect_all_bars
GREEN_SELF_LOOSE = (35, 90, 31, None, 46, None)    # H35-90,S>30,V>45     self_bars.self_hp_mp（放宽）
GREEN_MM = (35, 90, 41, None, None, None)          # H35-90,S>40          mm_rules_v7.color_masks.self
GREEN_PHASE = (35, 90, 81, None, 91, None)         # H35-90,S>80,V>90     match_state 阶段判定
GREEN_BAR = (40, 85, 80, None, 80, None)           # H40-85,S>=80,V>=80   hok_yolov5._bar_color_fracs
GREEN_TOP = (40, 85, 91, None, 71, None)           # H40-85,S>90,V>70     top_status_bars
GREEN_HUD = (35, 90, 60, None, 70, None)           # cv2.inRange(35,60,70)-(90,255,255)  ui_reader

# =====================================================================
# 蓝（队友 / 我方单位 / 蓝量）
# =====================================================================
BLUE_CAMP = (70, 135, 61, None, None, None)        # H70-135,S>60         detector / hok_yolov5 框内判阵营
BLUE_SCAN = (90, 135, 61, None, None, None)        # H90-135,S>60         self_bars.detect_all_bars
BLUE_SELF_LOOSE = (85, 140, 36, None, 36, None)    # H85-140,S>35,V>35    self_bars.self_hp_mp（放宽）
BLUE_SEG = (90, 135, 56, None, 61, None)           # H90-135,S>55,V>60    self_bars.find_ally_bars
BLUE_MP = (90, 135, 61, None, 61, None)            # H90-135,S>60,V>60    self_bars 自蓝量条
BLUE_TOP = (90, 135, 51, None, 71, None)           # H90-135,S>50,V>70    top_status_bars
BLUE_BAR = (90, 140, 80, None, 80, None)           # H90-140,S>=80,V>=80  hok_yolov5._bar_color_fracs
BLUE_HUD = (85, 140, 60, None, 60, None)           # cv2.inRange(85,60,60)-(140,255,255)  ui_reader
BLUE_MM = (85, 150, 41, None, None, None)          # H85-150,S>40         mm_rules_v7.color_masks.ally
BLUE_TOWER = (85, 135, 51, None, None, None)       # H85-135,S>50         mm_rules_v7._tower_alive(ally)

# =====================================================================
# 红（敌人 / 血条 / 死亡横幅）
# =====================================================================
RED_CAMP = (165, 15, 61, None, None, None)         # H<=15|H>=165,S>60    detector / hok_yolov5 框内 + self_bars.is_red
RED_SEG = (165, 15, 61, None, 61, None)            # H<=15|H>=165,S>60,V>60  self_bars.find_enemy_bars
RED_SCENE = (168, 15, 51, None, 81, None)          # H<=15|H>=168,S>50,V>80  hero_hp._red_mask（场景头顶条）
RED_LOCK = (168, 15, 81, None, None, None)         # H<=15|H>=168,S>80    hero_hp.lock_target（V 另有分支）
RED_TOP = (165, 15, 71, None, 91, None)            # H<=15|H>=165,S>70,V>90  top_status_bars
RED_BAR = (170, 10, 80, None, 80, None)            # H<=10|H>=170,S>=80,V>=80  hok_yolov5._bar_color_fracs
RED_DEATH = (165, 15, 81, None, 40, 150)           # H<=15|H>=165,S>80,V40-150  self_bars.death_banner
RED_MM = (160, 20, 41, None, None, None)           # H<=20|H>=160,S>40    mm_rules_v7.color_masks.enemy
RED_TOWER = (160, 20, 51, None, None, None)        # H<=20|H>=160,S>50    mm_rules_v7._tower_alive(enemy)

# =====================================================================
# 黄 / 品红 / 金 / 白
# =====================================================================
YELLOW_MARKER = (15, 38, 80, None, 90, None)       # H15-38,S>=80,V>=90   mm_rules_v7._marker_evidence
YELLOW_KILL = (15, 40, 121, None, 141, None)       # H15-40,S>120,V>140   self_bars.kill_banner（击杀金条）
MAGENTA_MARKER = (140, 170, 70, None, 80, None)    # H140-170,S>=70,V>=80 mm_rules_v7._marker_evidence
GOLD_NAME = (12, 45, 91, None, 151, None)          # H12-45,S>90,V>150    camp_detect.gold_name_row（金色名字）
WHITE_VS = (None, None, None, 59, 201, None)       # S<60,V>200           camp_detect.detect_show_stage


def mask_channels(H, S, V, spec):
    """按已拆分的 H/S/V 三个通道数组求掩码（供 `_find_bars` 这类回调式调用点复用）。"""
    h_lo, h_hi, s_lo, s_hi, v_lo, v_hi = spec
    if h_lo is None:
        m = np.ones(H.shape, dtype=bool)
    elif h_lo > h_hi:                      # 环绕（红）
        m = (H <= h_hi) | (H >= h_lo)
    else:
        m = (H >= h_lo) & (H <= h_hi)
    if s_lo is not None:
        m &= (S >= s_lo)
    if s_hi is not None:
        m &= (S <= s_hi)
    if v_lo is not None:
        m &= (V >= v_lo)
    if v_hi is not None:
        m &= (V <= v_hi)
    return m


def mask(hsv, spec):
    """HSV 图（或 ROI）-> bool 掩码。逐像素等价于各模块原先的内联表达式。"""
    return mask_channels(hsv[..., 0], hsv[..., 1], hsv[..., 2], spec)


def cond(spec):
    """colors 规格 -> `hue_cond(H, S, V)` 回调（self_bars.find_color_bars / _find_bars 用）。"""
    def _f(H, S, V):
        return mask_channels(H, S, V, spec)
    return _f


def frac(bgr_or_hsv, spec, is_hsv=False):
    """像素占比。`is_hsv=False` 时先做 BGR->HSV 转换。"""
    if bgr_or_hsv is None or getattr(bgr_or_hsv, "size", 0) == 0:
        return 0.0
    hsv = bgr_or_hsv if is_hsv else cv2.cvtColor(bgr_or_hsv, cv2.COLOR_BGR2HSV)
    return float(mask(hsv, spec).mean())


def mask_u8(hsv, spec):
    """uint8 的 0/1 掩码（供 np.sum 计数或 astype(np.uint8) 场合）。"""
    return mask(hsv, spec).astype(np.uint8)


def count(hsv, spec):
    """掩码像素数（int）。等价于 `int(np.sum(...))`。"""
    return int(mask(hsv, spec).sum())


def in_range_bounds(spec):
    """-> ((h_lo,s_lo,v_lo), (h_hi,s_hi,v_hi))，供 cv2.inRange 使用。

    仅支持**非环绕**规格；环绕（红）请改用 `mask()` 或两次 inRange 取并。
    """
    h_lo, h_hi, s_lo, s_hi, v_lo, v_hi = spec
    if h_lo is None or h_lo > h_hi:
        raise ValueError(f"该规格不能用单次 inRange 表达: {spec}")
    return ((h_lo, 0 if s_lo is None else s_lo, 0 if v_lo is None else v_lo),
            (h_hi, 255 if s_hi is None else s_hi, 255 if v_hi is None else v_hi))
