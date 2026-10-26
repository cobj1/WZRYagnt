# -*- coding: utf-8 -*-
"""结算页胜负识别（无 OCR 依赖）。

为什么需要：价值模型的唯一监督信号是胜负，而 44 局存量数据 **0 局有 result**。
结算页的「胜利 / 失败」是唯一稳定的胜负来源，人工填不可持续。

判据（逐层可验证，不满足就返回 None，**不猜**）：
  1. 结算页横幅在画面中央偏上（x 0.18–0.82, y 0.15–0.62）；
  2. 横幅主色只有两种：
     - **失败**：暗红/酒红（H 0–12 或 165–180，S≥90，V 55–150）
     - **胜利**：金黄（H 16–38，S≥110，V≥150）
  3. 两类占比都低于阈值 → 视为"不是结算页/无法判读"，返回 None。

⚠️ 证据等级：`失败` 已用真机结算页样本验证（2026-10-26）；
`胜利` 只有色相假设，尚无真机样本 —— 因此**默认只自动写 loss**，
win 需要 `allow_unverified_win=True` 才采用（拿到一张胜利结算图后即可升级）。

用法：
    from wzry.vision.match_result import classify
    res, conf, detail = classify(frame)     # ('loss', 0.86, {...}) / (None, 0.0, {...})
"""
from __future__ import annotations

from typing import Optional, Tuple

import cv2
import numpy as np

# 横幅区域（归一化）
BANNER = (0.18, 0.15, 0.82, 0.62)
# 颜色阈值
CRIMSON = {"h_lo": 165, "h_hi": 12, "s_min": 90, "v_lo": 55, "v_hi": 150}
GOLD = {"h_lo": 16, "h_hi": 38, "s_min": 110, "v_min": 150}
# 占比阈值（占横幅区像素）
MIN_FRAC = 0.06
# 置信度映射：占比 → 置信度（线性，0.06→0.6, 0.20→0.95）
_CONF_LO, _CONF_HI = 0.06, 0.20


def _crop(frame):
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = BANNER
    return frame[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]


def _fracs(roi) -> Tuple[float, float]:
    """返回 (暗红占比, 金黄占比)，分母是 ROI 像素数。"""
    if roi.size == 0:
        return 0.0, 0.0
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    n = float(h.size)
    crimson = (((h <= CRIMSON["h_hi"]) | (h >= CRIMSON["h_lo"]))
               & (s >= CRIMSON["s_min"])
               & (v >= CRIMSON["v_lo"]) & (v <= CRIMSON["v_hi"])).sum() / n
    gold = ((h >= GOLD["h_lo"]) & (h <= GOLD["h_hi"])
            & (s >= GOLD["s_min"]) & (v >= GOLD["v_min"])).sum() / n
    return float(crimson), float(gold)


def _conf(frac: float) -> float:
    if frac <= _CONF_LO:
        return 0.0
    x = (frac - _CONF_LO) / (_CONF_HI - _CONF_LO)
    return float(min(1.0, 0.6 + 0.35 * min(1.0, x)))


def classify(frame, *, allow_unverified_win: bool = False
             ) -> Tuple[Optional[str], float, dict]:
    """判定结算页胜负。

    返回 `(result, conf, detail)`：
      result ∈ {'loss', 'win', None}；None 表示"不是结算页/判读不出"。
      conf   0..1，仅在 result 非 None 时有意义。
      detail 原始占比，便于落盘审计与阈值回标。
    """
    if frame is None or getattr(frame, "size", 0) == 0:
        return None, 0.0, {"reason": "no_frame"}
    roi = _crop(frame)
    crimson, gold = _fracs(roi)
    detail = {"crimson_frac": round(crimson, 4), "gold_frac": round(gold, 4),
              "banner": list(BANNER), "min_frac": MIN_FRAC}

    # 红优先：失败横幅的暗红是强特征，且已用真机样本验证
    if crimson >= MIN_FRAC and crimson >= gold:
        detail["rule"] = "crimson_banner"
        return "loss", _conf(crimson), detail
    if gold >= MIN_FRAC:
        detail["rule"] = "gold_banner"
        if not allow_unverified_win:
            detail["reason"] = "win_awaiting_sample"
            return None, 0.0, detail
        return "win", _conf(gold) * 0.9, detail
    detail["reason"] = "no_banner_color"
    return None, 0.0, detail


def is_settlement(frame) -> bool:
    """只回答"是不是结算页"，不判胜负。"""
    r, _, _ = classify(frame, allow_unverified_win=True)
    return r is not None
