# -*- coding: utf-8 -*-
"""顶部/侧边头像血条读数（v15.0）：敌方与队友血量比例。

问题：self_bars 只读"自己"头顶绿条，且 EXCLUDE_Y_TOP=240 把整个顶部 HUD 排除
      -> 敌方/队友血量比例在整条链路上是缺的（顶部头像条的血条从没被读过）。

设计（配置驱动 + 自标定，不写死像素）：
  - 条带取自 configs/calibration_absolute.json 的 "top_bar"：
        {"x0","x1","y0","y1","slots"}      # 1280x720 帧空间；slots=条带内头像数
  - 在条带内找红/蓝横条 -> 按 x 槽位聚类 -> 每槽 ratio = 实心宽 / (实心+右侧空槽延伸)，
    与 ui_reader.find_hp_bar 同口径
  - 未标定 -> found=False（下游当"无数据"，不猜）

标定：scripts/calibrate_top_bar.py 扫候选条带，写回 "top_bar"。
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
CALIB_PATH = ROOT / "configs" / "calibration_absolute.json"

BAR_H_MIN, BAR_H_MAX = 3, 16
BAR_W_MIN = 10
EMPTY_GRAY = 60          # 空血槽灰度下限（与 ui_reader.find_hp_bar 同口径）


def load_cfg():
    """读 configs/calibration_absolute.json 的 top_bar 段；未标定返回 None。"""
    try:
        d = json.loads(CALIB_PATH.read_text(encoding="utf-8"))
        c = d.get("top_bar")
        if not c:
            return None
        for k in ("x0", "x1", "y0", "y1", "slots"):
            if k not in c:
                return None
        return c
    except Exception:
        return None


def save_cfg(cfg):
    d = json.loads(CALIB_PATH.read_text(encoding="utf-8"))
    d["top_bar"] = cfg
    CALIB_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return CALIB_PATH


def _bar_masks(frame):
    """红=敌 / 蓝=友 的血条掩码（王者血条配色：自己=绿、队友=蓝、敌人=红）。"""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    H, S, V = hsv[..., 0].astype(int), hsv[..., 1].astype(int), hsv[..., 2].astype(int)
    red = (((H <= 15) | (H >= 165)) & (S > 70) & (V > 90)).astype(np.uint8)
    blue = ((H >= 90) & (H <= 135) & (S > 50) & (V > 70)).astype(np.uint8)
    return {"red": red, "blue": blue}


def find_bars_in_strip(mask, y0, y1, x0, x1):
    """条带内逐行找横条 -> 行间堆叠成条。返回 [{x, w, y, h}]（绝对坐标）。"""
    sub = mask[y0:y1, x0:x1]
    rows = []
    for ry in range(sub.shape[0]):
        idx = np.nonzero(sub[ry])[0]
        if len(idx) < BAR_W_MIN:
            continue
        s = p = idx[0]
        for i in idx[1:]:
            if i - p > 3:
                if p - s + 1 >= BAR_W_MIN:
                    rows.append((ry, s, p))
                s = i
            p = i
        if p - s + 1 >= BAR_W_MIN:
            rows.append((ry, s, p))
    bars = []
    for ry, a, b in rows:
        for o in bars:
            if ry - o["yb"] <= 2 and not (b < o["x0"] - 6 or a > o["x1"] + 6):
                o["yb"] = ry
                o["x0"] = min(o["x0"], a)
                o["x1"] = max(o["x1"], b)
                break
        else:
            bars.append({"ry": ry, "yb": ry, "x0": a, "x1": b})
    out = []
    for o in bars:
        h = o["yb"] - o["ry"] + 1
        if not (BAR_H_MIN <= h <= BAR_H_MAX):
            continue
        out.append({"x": x0 + int(o["x0"]), "w": int(o["x1"] - o["x0"] + 1),
                    "y": y0 + int(o["ry"]), "h": int(h)})
    return out


def _ratio(gray, x, w, y, h):
    """实心宽 / (实心+右侧空槽延伸)：抗"血条被截断/遮挡"的低估（find_hp_bar 同口径）。"""
    if y + h // 2 >= gray.shape[0]:
        return 1.0
    row = gray[y + h // 2]
    end = x + w
    while end < row.shape[0] and row[end] > EMPTY_GRAY:
        end += 1
    total = max(w, end - x)
    return round(float(w) / float(max(total, 1)), 3)


def read(frame, cfg=None):
    """条带 -> 血量比例读数。

    返回 {"found", "bars":[{slot,x,w,ratio,color}], "n",
          "enemy_hp_min/mean", "ally_hp_min/mean"}
    """
    cfg = cfg or load_cfg()
    if not cfg:
        return {"found": False, "reason": "top_bar 未标定"}
    x0, x1, y0, y1 = int(cfg["x0"]), int(cfg["x1"]), int(cfg["y0"]), int(cfg["y1"])
    slots = max(1, int(cfg.get("slots", 5)))
    h, w = frame.shape[:2]
    x1, y1 = min(x1, w), min(y1, h)
    if x1 <= x0 or y1 <= y0:
        return {"found": False, "reason": "top_bar 条带越界"}
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    items = []
    for color, m in _bar_masks(frame).items():
        for b in find_bars_in_strip(m, y0, y1, x0, x1):
            b["color"] = color
            b["ratio"] = _ratio(gray, b["x"], b["w"], b["y"], b["h"])
            items.append(b)
    if not items:
        return {"found": False, "reason": "条带内无血条"}
    pitch = float(x1 - x0) / slots
    best = {}
    for b in items:
        slot = min(slots - 1, max(0, int((b["x"] - x0) / pitch)))
        cur = best.get(slot)
        if cur is None or b["w"] > cur["w"]:
            best[slot] = b
    bars = [{"slot": s, "x": best[s]["x"], "w": best[s]["w"],
             "ratio": best[s]["ratio"], "color": best[s]["color"]} for s in sorted(best)]
    en = [b["ratio"] for b in bars if b["color"] == "red"]
    al = [b["ratio"] for b in bars if b["color"] == "blue"]
    return {
        "found": bool(bars), "bars": bars, "n": len(bars),
        "enemy_hp_min": min(en) if en else None,
        "enemy_hp_mean": round(sum(en) / len(en), 3) if en else None,
        "ally_hp_min": min(al) if al else None,
        "ally_hp_mean": round(sum(al) / len(al), 3) if al else None,
    }


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print("用法: python -m wzry.vision.top_status_bars <帧图>")
        print("当前标定:", load_cfg())
        raise SystemExit(0)
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        print("读不到图片:", path)
        raise SystemExit(1)
    print(read(img))
