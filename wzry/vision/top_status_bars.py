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

from wzry.vision import colors   # HSV 判据单一事实来源（v15.3）

ROOT = Path(__file__).resolve().parents[2]
CALIB_PATH = ROOT / "configs" / "calibration_absolute.json"

BAR_H_MIN, BAR_H_MAX = 3, 16
BAR_W_MIN = 10
EMPTY_GRAY = 60          # 空血槽灰度下限（与 ui_reader.find_hp_bar 同口径）
# 行间堆叠时允许的 x 容差。**真机校正（2026-10-15）**：顶部头像行 5 条血条 pitch≈36px、
# 条宽≈29px -> 相邻条之间只有 7~11px 空隙；旧值 6 会把相邻两条粘成一条
# （实测 289/325 两条被合成 w=66 的怪条，导致槽位数变 3、等距判据失效）。
MERGE_TOL = 2


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
    """血条掩码（三色）。

    真机校正（2026-10-15，Redmi K60 5v5 对局帧 3200x1440 -> 1280x720 生产帧空间）：
      顶部头像行**只有己方 5 人**（敌方头像不显示），成排的血条实测是**蓝色**，
      自己那一格是**绿色**；红色在顶部 HUD 里**没有**（敌人血量只能从场景红条 /
      战绩面板拿）。旧版只有 red/blue 两色掩码，导致真机帧上永远扫不到成排血条
      （`calibrate_top_bar.py` 报"未找到候选条带"）。
    语义约定：
      red   -> 敌方（`enemy_hp_*`）
      blue  -> 己方（`ally_hp_*`）
      green -> 己方（`ally_hp_*`，自己那格）
    """
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    # v15.3 阈值集中到 wzry/vision/colors.py（RED_TOP / BLUE_TOP / GREEN_TOP）
    return {"red": colors.mask_u8(hsv, colors.RED_TOP),
            "blue": colors.mask_u8(hsv, colors.BLUE_TOP),
            "green": colors.mask_u8(hsv, colors.GREEN_TOP)}


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
            # 真机校正：相邻两条血条之间只有 7~11px 间隙，且中间常有 2px 抗锯齿"桥梁"
            # （实测顶部 HUD y=42：289-317 | 319-320 | 325-353）。旧阈值 >3 会把
            # 319-320 这座桥当条内间隙，把两条粘成一条 w=66 的怪条 -> 槽位数掉一。
            if i - p > 1:
                if p - s + 1 >= BAR_W_MIN:
                    rows.append((ry, s, p))
                s = i
            p = i
        if p - s + 1 >= BAR_W_MIN:
            rows.append((ry, s, p))
    bars = []
    for ry, a, b in rows:
        for o in bars:
            if ry - o["yb"] <= 2 and not (b < o["x0"] - MERGE_TOL
                                          or a > o["x1"] + MERGE_TOL):
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


def _ratio(gray, x, w, y, h, full_w=None):
    """实心宽 / (实心+右侧空槽延伸)：抗"血条被截断/遮挡"的低估（find_hp_bar 同口径）。

    ⚠️ 真机校正（2026-10-15）：顶部 HUD 的空槽背景**很暗**（灰度 < EMPTY_GRAY），
    延伸恒为 0 -> 旧口径会把任何血量都算成 1.0（实测满条/残条全是 1.0，读数失效）。
    所以这里识别出"暗空槽"情况后改用 `w / full_w` 折算（full_w = 标定的**满条像素宽**，
    由 calibrate_top_bar.py 实测写入 cfg）。
    """
    if y + h // 2 >= gray.shape[0]:
        return 1.0
    row = gray[y + h // 2]
    end = x + w
    while end < row.shape[0] and row[end] > EMPTY_GRAY:
        end += 1
    total = max(w, end - x)
    if total > w:
        return round(float(w) / float(total), 3)
    if full_w:
        return round(min(max(float(w) / float(full_w), 0.0), 1.0), 3)
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
    # v15.2 性能（逐字段等价）：血条掩码只可能落在条带内 —— find_bars_in_strip 本就是
    #   `sub = mask[y0:y1, x0:x1]`，故先在条带裁剪上做色彩空间转换，避免对整帧(1280x720)
    #   跑 HSV + 三个全帧布尔掩码（实测 read() 26.7ms，其中掩码转换 ~29ms 级）。
    #   gray 仍取**整帧**：_ratio 的"空槽向右延伸"要按整行判断，保持原语义不变。
    strip = frame[y0:y1, x0:x1]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    items = []
    pitch = float(x1 - x0) / slots
    # full_w：满条像素宽（帧空间）。暗空槽 HUD 用它把"实心宽"折算成血量比例；
    # 未标定时退回 pitch*0.75（真机实测满条≈28px / pitch≈38px）。
    full_w = cfg.get("full_w") or (pitch * 0.75)
    for color, m in _bar_masks(strip).items():
        for b in find_bars_in_strip(m, 0, y1 - y0, 0, x1 - x0):
            b["color"] = color
            b["ratio"] = _ratio(gray, b["x"] + x0, b["w"], b["y"] + y0, b["h"], full_w)
            b["x"] += x0
            b["y"] += y0
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
    # 若标定里指定了 color，只认该颜色（避免自己那格的绿条和队友的蓝条混在同一槽位）
    only = cfg.get("color")
    if only:
        best = {s: b for s, b in best.items() if b["color"] == only}
    bars = [{"slot": s, "x": best[s]["x"], "w": best[s]["w"],
             "ratio": best[s]["ratio"], "color": best[s]["color"]} for s in sorted(best)]
    # ally_only：该条带里只有己方头像（王者荣耀顶部 HUD 不放敌头像）。此时条带内偶发的
    # 红色元素（低血条/头像美术色）**不能**当成敌人 —— 会把"队友残血"读成"敌人残血"
    # 并直接喂给收人头/占优推进闸门（m2 消费 ui.enemy_hp_min）。故显式关掉 enemy 口径。
    ally_only = bool(cfg.get("ally_only"))
    en = [] if ally_only else [b["ratio"] for b in bars if b["color"] == "red"]
    al = [b["ratio"] for b in bars if b["color"] in ("blue", "green")]
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
