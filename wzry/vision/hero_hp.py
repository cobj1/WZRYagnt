# -*- coding: utf-8 -*-
"""敌方血量比例读数（v15.1）：从**场景内**敌方英雄头顶红条量出 HP 比例。

为什么走这条路：
  - 顶部头像条（选中/攻击敌方时出现的"当前目标实时血量"）是**条件性元素**，不锁定
    目标就看不见 —— 只能读到"我正在打的那个人"；
  - 而场景内头顶红条**只要能看见敌人就一直有** —— 能给出"每个可见敌人"的血量比例，
    而且是交战前的决策输入（该不该上/该不该撤）。
  - 判据复用已真机验证过能定位英雄头顶血条的那套（hok_yolov5._hero_bar_probe 的
    逐窄带扫描 + 颜色阈值），这里只多做一件事：**量实心横条的长度**。

口径：ratio = 实心红条长度 / 满条宽（1280 帧空间 FULL_BAR_PX=115，与 self_bars 同源）。
      只是近似值：相机距离会改变屏幕上的像素长度（远小近大），所以它更适合当
      "相对血量/是否残血"特征，**不要当精确血量**。真机验证前先按近似用。

另含 `lock_target(frame)`：顶部"锁定目标条"（选中 / 正在攻击敌方英雄时才出现的实时血量）。
      它与上面两条路互补 —— 上面是"每个可见敌人"，这条是"我正在打的那一个"。
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from wzry.vision import colors   # HSV 判据单一事实来源（v15.3）
from wzry.vision.hok_yolov5 import _bar_color_fracs

ROOT = Path(__file__).resolve().parents[2]
CALIB_PATH = ROOT / "configs" / "calibration_absolute.json"

FULL_BAR_PX = 115.0          # 满条宽（1280x720 帧空间；与 self_bars.FULL_BAR_W 同源）
W_FRAC = 0.70                # 扫描带宽下限 = 框宽 * 该比例
SCAN_W_MIN_PX = None         # 运行时取 max(框宽*W_FRAC, FULL_BAR_PX*1.15)：见下方说明
SCAN_PX0, SCAN_PX1 = 3, 45   # 框顶上方扫描区间（720p 基准，按帧高缩放）
BAND_PX = 4                  # 窄带高度（720p 基准）
MIN_RUN = 12                 # 实心段最小长度：低于它当噪声（避免草地/特效误判）
#   扫描带宽必须 **≥ 满条宽**，否则量长度会系统性低估（框只有 60~90px，而满条 115px；
#   若沿用 hok_yolov5 的 0.70*框宽 就只覆盖血条的一部分）。分类不需要宽度，所以那边不用改。


def _red_mask(bgr):
    # v15.3 阈值集中到 wzry/vision/colors.py（RED_SCENE）
    return colors.mask_u8(cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV), colors.RED_SCENE)


def _max_run(mask):
    """掩码里最长的水平连续段长度（像素）。"""
    best = 0
    for r in range(mask.shape[0]):
        idx = np.nonzero(mask[r])[0]
        if len(idx) == 0:
            continue
        s = p = idx[0]
        for i in idx[1:]:
            if i - p > 2:
                if p - s + 1 > best:
                    best = int(p - s + 1)
                s = i
            p = i
        if p - s + 1 > best:
            best = int(p - s + 1)
    return best


def enemy_hp_ratio(frame, xyxy, full_bar_px=FULL_BAR_PX, scan_px=(SCAN_PX0, SCAN_PX1)):
    """一个敌方英雄框 -> HP 比例(0..1)，读不到返回 None。

    做法：框顶上方逐窄带扫描，取"红色覆盖率最高"的那一带作为血条所在带，
    然后量该带里最长红色连续段 = 实心条长度，除以满条宽。
    """
    try:
        x1, y1, x2, y2 = [float(v) for v in xyxy]
        h, w = frame.shape[:2]
        sc = h / 720.0
        bw = max(1.0, x2 - x1)
        cx = (x1 + x2) / 2.0
        scan_w = max(bw * W_FRAC, full_bar_px * 1.15)
        rx0 = max(0, int(cx - scan_w / 2))
        rx1 = min(w, int(cx + scan_w / 2))
        if rx1 - rx0 < 7:
            return None
        band = max(2, int(round(BAND_PX * sc)))
        step = max(1, band // 2)
        y_lo = max(0, int(y1 - scan_px[1] * sc))
        y_hi = max(1, int(y1 - scan_px[0] * sc))
        best_cov, best_run = 0.0, 0
        for top in range(y_lo, y_hi + 1, step):
            crop = frame[top:top + band, rx0:rx1]
            if crop.size == 0:
                continue
            _g, r_frac, _b = _bar_color_fracs(crop)
            if r_frac < 0.25:            # 该带红色太少 -> 不是血条
                continue
            run = _max_run(_red_mask(crop))
            if run > best_run or (r_frac > best_cov and run >= MIN_RUN):
                best_cov, best_run = max(best_cov, r_frac), max(best_run, run)
        if best_run < MIN_RUN:
            return None
        return round(min(1.0, best_run / float(full_bar_px)), 3)
    except Exception:
        return None


# -------- 顶部锁定目标条（选中 / 正在攻击敌方英雄时才出现的"当前目标实时血量"）--------
# 真机实测（25s 内 19 帧，其中 8 帧有锁定目标 / 11 帧无；1280x720 帧空间）：
#   条体 = x 578→764, y 14→31（187×17px）；条内 3~6px 的断口就是白字 "7928/7928"。
#   空槽**也是暗红**（V≈110~170），所以"是不是红"根本分不开填充与空槽 ——
#   必须按**亮度**分：填充=亮红 V≥180（阈值是扫出来的，见下）。
#   校验（条内文字真值 vs 按亮度量出的填充比例）：
#     F11 7928/7928=1.000 -> 0.995｜F16 4884/7928=0.616 -> 0.620｜F18 4608/7928=0.581 -> 0.588
#   → 误差 ≤0.7pp，比读条内文字靠谱（文字会被数字模板读错：9↔1、0↔1）。
LOCK_X0, LOCK_X1, LOCK_Y0, LOCK_Y1 = 578, 764, 14, 31
LOCK_FILL_V = 180            # 填充亮度下限（V≥150/165 都会把空槽算成填充）
LOCK_PRESENT_MIN = 0.15      # 条体区"红像素占比"达标即有锁定目标（实测 有 0.57~0.94 / 无 0.00~0.01）
DEFAULT_LOCK_ROI = {"x0": LOCK_X0, "x1": LOCK_X1, "y0": LOCK_Y0, "y1": LOCK_Y1}


def load_lock_cfg():
    """读 configs 的 lock_target 段；未标定就用真机实测的默认值。"""
    try:
        d = json.loads(CALIB_PATH.read_text(encoding="utf-8-sig"))
        c = d.get("lock_target") or {}
        if all(k in c for k in ("x0", "x1", "y0", "y1")):
            return c
    except Exception:
        pass
    return dict(DEFAULT_LOCK_ROI)


def lock_target(frame, cfg=None):
    """顶部锁定目标条 -> {"present","ratio","red_frac","reason"}。

    `present=False`（屏幕上没有锁定目标）时 ratio 为 None —— 这时**不要**把旧值当新值用。
    ratio = 填充长度 / 条体全长；这是近似值（相机/皮肤可能微调），实测误差 ≤0.7pp。
    """
    cfg = cfg or load_lock_cfg()
    x0, x1 = int(cfg["x0"]), int(cfg["x1"])
    y0, y1 = int(cfg["y0"]), int(cfg["y1"])
    h, w = frame.shape[:2]
    x1, y1 = min(x1, w), min(y1, h)
    if x1 <= x0 or y1 <= y0 or y0 < 0 or x0 < 0:
        return {"present": False, "ratio": None, "red_frac": 0.0, "reason": "lock_target ROI 越界"}
    box = frame[y0:y1, x0:x1]
    hsv = cv2.cvtColor(box, cv2.COLOR_BGR2HSV)
    V = hsv[..., 2]
    # v15.3 阈值集中到 colors.py（RED_LOCK = 色相+饱和度；亮度按空槽/填充分两支，仍在本处）
    hue_red = colors.mask(hsv, colors.RED_LOCK)
    red_frac = float((hue_red & (V > 90)).mean())        # 含暗红空槽：用来判"有没有目标"
    if red_frac < LOCK_PRESENT_MIN:
        return {"present": False, "ratio": None, "red_frac": round(red_frac, 3),
                "reason": "无锁定目标"}
    fill = hue_red & (V >= LOCK_FILL_V)                  # 只认亮红=填充
    cols = np.nonzero(fill.mean(axis=0) > 0.5)[0]
    if len(cols) == 0:
        return {"present": True, "ratio": 0.0, "red_frac": round(red_frac, 3),
                "reason": "有目标但填充读不到（空槽？）"}
    ratio = round(float(int(cols[-1]) + 1) / float(x1 - x0), 3)
    return {"present": True, "ratio": min(1.0, ratio), "red_frac": round(red_frac, 3),
            "reason": "ok"}


def _get(d, k, default=None):
    return d.get(k, default) if isinstance(d, dict) else getattr(d, k, default)


def enemy_hp_from_dets(frame, dets, full_bar_px=FULL_BAR_PX):
    """一批检测框 -> 敌方 HP 比例列表（只取 enemy_hero）。返回 [ratio, ...]。"""
    out = []
    for d in dets or []:
        if _get(d, "cls") != "enemy_hero":
            continue
        # 不可信框（camp_src=default 的幻影敌人）不参与
        if _get(d, "camp_src") == "default":
            continue
        xyxy = _get(d, "xyxy")
        if xyxy is None:
            continue
        r = enemy_hp_ratio(frame, xyxy, full_bar_px=full_bar_px)
        if r is not None:
            out.append(r)
    return out


if __name__ == "__main__":
    import sys

    import cv2
    p = sys.argv[1] if len(sys.argv) > 1 else None
    if not p:
        print("用法: python -m wzry.vision.hero_hp <帧图>   （会先用检测器找敌方英雄框）")
        raise SystemExit(0)
    img = cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)
    from wzry.vision.hok_yolov5 import HokYoloV5
    dets = HokYoloV5(conf=0.35).detect(img)
    rs = enemy_hp_from_dets(img, dets)
    print("敌方 HP 比例:", rs, "| min =", min(rs) if rs else None)
