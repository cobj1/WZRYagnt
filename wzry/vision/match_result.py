# -*- coding: utf-8 -*-
"""结算页胜负识别（无 OCR 依赖）。

为什么需要：价值模型的唯一监督信号是胜负，而 44 局存量数据 **0 局有 result**。
结算页的「胜利 / 失败」是唯一稳定的胜负来源，人工填不可持续。

判据（**两类都用真机样本标定过**，2026-10-26）：

| 结算页 | 横幅主色 | 青蓝占比(H85-115,S≥90) | 暗红占比(H<12或>165,S≥90) |
|---|---|---:|---:|
| **胜利 VICTORY** | 蓝/银翼 + 金字 | **0.466** | 0.007 |
| **失败 DEFEAT** | 暗红/酒红 | 0.001 | **0.287** |

两类在颜色上完全可分，因此判据是：
  - 青蓝 ≥ 0.10 → win
  - 暗红 ≥ 0.06 → loss
  - 两者都够时取占比大的；都不够 → 返回 None（不是结算页 / 判读不出，**不猜**）

⚠️ 早期版本按"金色=胜利"猜（**错的**：胜利横幅是蓝色，金色只占 1.7%）。
拿到真机胜利样本后已纠正 —— 这是"没有样本就不要启用分支"的实例。

用法：
    from wzry.vision.match_result import classify
    res, conf, detail = classify(frame)     # ('win'|'loss'|None, 0~1, {...})
"""
from __future__ import annotations

from typing import Optional, Tuple

import cv2
import numpy as np

# 横幅区域（归一化）
BANNER = (0.18, 0.15, 0.82, 0.62)
# 颜色阈值
CRIMSON = {"h_lo": 165, "h_hi": 12, "s_min": 90, "v_lo": 55, "v_hi": 150}
CYAN = {"h_lo": 85, "h_hi": 115, "s_min": 90, "v_min": 90}
# 占比阈值（占横幅区像素）——由两个真机样本标定，留足余量
MIN_CRIMSON = 0.06
MIN_CYAN = 0.10
# 结构门：结算页整屏压暗。真机实测 底/顶亮度 58/40 与 44/50；对局帧 99/95~115/99。
DARK_BOT_V = 80.0
DARK_TOP_V = 80.0
# 置信度映射：占比 → 置信度
_CONF_HI = 0.25


def _crop(frame):
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = BANNER
    return frame[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]


def _fracs(roi) -> Tuple[float, float]:
    """返回 (暗红占比, 青蓝占比)，分母是 ROI 像素数。"""
    if roi.size == 0:
        return 0.0, 0.0
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    n = float(h.size)
    crimson = (((h <= CRIMSON["h_hi"]) | (h >= CRIMSON["h_lo"]))
               & (s >= CRIMSON["s_min"])
               & (v >= CRIMSON["v_lo"]) & (v <= CRIMSON["v_hi"])).sum() / n
    cyan = ((h >= CYAN["h_lo"]) & (h <= CYAN["h_hi"])
            & (s >= CYAN["s_min"]) & (v >= CYAN["v_min"])).sum() / n
    return float(crimson), float(cyan)


def _conf(frac: float, lo: float) -> float:
    if frac <= lo:
        return 0.0
    x = (frac - lo) / max(1e-6, _CONF_HI - lo)
    return float(min(1.0, 0.65 + 0.3 * min(1.0, x)))


def _dark_border(frame) -> Tuple[float, float]:
    """返回 (底部亮度, 顶部亮度)。

    结算页整屏压暗（真机实测 底/顶 = 58/40 与 44/50），对局帧是亮的
    （底/顶 = 99/95 ~ 115/99）。这一条把"画面里恰好有一片青蓝/暗红"的
    对局帧和随机噪声挡在外面 —— 只靠颜色占比会误报（噪声 cyan=0.144）。
    """
    h = frame.shape[0]
    v = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)[..., 2]
    bot = float(v[int(0.90 * h):, :].mean())
    top = float(v[:max(1, int(0.05 * h)), :].mean())
    return bot, top


def classify(frame, *, allow_unverified_win: bool = True
             ) -> Tuple[Optional[str], float, dict]:
    """判定结算页胜负。

    返回 `(result, conf, detail)`：
      result ∈ {'win', 'loss', None}；None 表示"不是结算页/判读不出"。
      conf   0..1，仅在 result 非 None 时有意义。
      detail 原始占比，便于落盘审计与阈值回标。

    `allow_unverified_win=False` 可临时关掉 win 分支（早期无胜利样本时的保守开关；
    现在两类都有真机样本，默认允许）。
    """
    if frame is None or getattr(frame, "size", 0) == 0:
        return None, 0.0, {"reason": "no_frame"}
    roi = _crop(frame)
    crimson, cyan = _fracs(roi)
    bot_v, top_v = _dark_border(frame)
    detail = {"crimson_frac": round(crimson, 4), "cyan_frac": round(cyan, 4),
              "bot_v": round(bot_v, 1), "top_v": round(top_v, 1),
              "banner": list(BANNER),
              "min_crimson": MIN_CRIMSON, "min_cyan": MIN_CYAN}

    # 结构门：结算页整屏压暗；对局帧/噪声都亮得多
    if bot_v >= DARK_BOT_V or top_v >= DARK_TOP_V:
        detail["reason"] = "frame_not_dark"
        return None, 0.0, detail

    win_ok = bool(allow_unverified_win) and cyan >= MIN_CYAN
    loss_ok = crimson >= MIN_CRIMSON

    if win_ok and cyan >= crimson:
        detail["rule"] = "cyan_banner"
        return "win", _conf(cyan, MIN_CYAN), detail
    if loss_ok and crimson > cyan:
        detail["rule"] = "crimson_banner"
        return "loss", _conf(crimson, MIN_CRIMSON), detail
    detail["reason"] = "no_banner_color"
    return None, 0.0, detail


def is_settlement(frame) -> bool:
    """只回答"是不是结算页"，不判胜负。"""
    r, _, _ = classify(frame)
    return r is not None
