# -*- coding: utf-8 -*-
"""HUD 数字读数器（无 OCR 依赖）：模板匹配版。

用途：等级 / 金币 / 技能剩余 CD 秒数 / 技能点 等 HUD 上的数字。

本机没装 paddleocr / easyocr / pytesseract（也不打算为此引新依赖），所以走模板匹配：
  1) build_templates(): 用 PIL 字体渲染 0-9（多种字号），存 configs/digit_templates.npz
  2) read_number(img): 连通域切字符 -> 归一化到模板尺寸 -> matchTemplate 取最优
  3) read_roi(frame, roi): 先按 configs/calibration_absolute.json 的 digit_rois 取块再读

⚠️ 精度前提：游戏 HUD 用的是自有字体，首版模板是系统字体**近似**。要可信必须替换成
从真机帧里抠出的真实数字模板 —— 见 scripts/calibrate_digits.py。
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
TPL_PATH = ROOT / "configs" / "digit_templates.npz"
CALIB_PATH = ROOT / "configs" / "calibration_absolute.json"

TPL_W, TPL_H = 16, 24          # 模板归一化尺寸
FONT_CANDIDATES = [
    "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]
FONT_SIZES = (16, 20, 24, 28)
THR = 150                       # 数字为亮字（HUD 常见），按灰度阈值切字符


def build_templates(out=TPL_PATH, fonts=FONT_CANDIDATES, sizes=FONT_SIZES):
    """渲染 0-9 多字号模板并落盘。返回 {"tpl": (N,TPL_H,TPL_W) float32, "labels": (N,)}。"""
    from PIL import Image, ImageDraw, ImageFont
    tpls, labels = [], []
    for fp in fonts:
        for sz in sizes:
            try:
                font = ImageFont.truetype(fp, sz)
            except Exception:
                continue
            for d in range(10):
                im = Image.new("L", (sz * 2, sz * 2), 0)
                dr = ImageDraw.Draw(im)
                dr.text((sz // 2, sz // 2), str(d), fill=255, font=font)
                a = np.array(im)
                ys, xs = np.nonzero(a > 40)
                if len(xs) == 0:
                    continue
                crop = a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
                tpls.append(cv2.resize(crop, (TPL_W, TPL_H), interpolation=cv2.INTER_AREA))
                labels.append(d)
    if not tpls:
        raise RuntimeError("没有任何字体可用，无法生成数字模板")
    tpl = np.stack(tpls).astype(np.float32) / 255.0
    lab = np.array(labels, dtype=np.int32)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, tpl=tpl, labels=lab)
    print(f"数字模板已写出: {out}  {len(lab)} 条（{len(set(labels))} 个数字）")
    return {"tpl": tpl, "labels": lab}


def load_templates(path=TPL_PATH):
    p = Path(path)
    if not p.exists():
        build_templates(p)
    d = np.load(p)
    return {"tpl": d["tpl"].astype(np.float32), "labels": d["labels"]}


def _blobs(img_gray):
    """数字字符连通域 -> 合并"竖直重叠且横向大幅重叠"的碎片 -> [{x,y,w,h}]（按 x 排序）。

    ⚠️ 合并条件必须严格：并排的两个数字在 x 上只是相邻（重叠很小），
    而同一字符的碎片（如被反锯齿切断的笔画）横向重叠很大。
    早前用"x 距离 <= 2px 就合并"会把 '1234' 合成 '13'（实测踩到）。
    """
    m = (img_gray > THR).astype(np.uint8)
    n, lab, st, cent = cv2.connectedComponentsWithStats(m, 8)
    boxes = []                              # [x0, y0, x1, y1]
    for i in range(1, n):
        x, y, w, h, area = st[i]
        if h < 5 or w < 2 or area < 6:
            continue
        boxes.append([int(x), int(y), int(x + w), int(y + h)])
    if not boxes:
        return []
    boxes.sort(key=lambda b: b[0])
    merged = []
    for b in boxes:
        for o in merged:
            ov = min(o[2], b[2]) - max(o[0], b[0])
            wmin = min(o[2] - o[0], b[2] - b[0])
            vo = min(o[3], b[3]) - max(o[1], b[1])
            if wmin > 0 and vo > 0 and ov > 0.5 * wmin:
                o[0], o[1] = min(o[0], b[0]), min(o[1], b[1])
                o[2], o[3] = max(o[2], b[2]), max(o[3], b[3])
                break
        else:
            merged.append(list(b))
    merged.sort(key=lambda b: b[0])
    return [{"x": b[0], "y": b[1], "w": b[2] - b[0], "h": b[3] - b[1]} for b in merged]


def read_number(img, tpl=None, max_digits=6):
    """一张裁图 -> (数值, 逐位置信度) 或 (None, [])。"""
    if img is None or img.size == 0:
        return None, []
    if img.ndim == 3:
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        g = img
    tpl = tpl or load_templates()
    T, L = tpl["tpl"], tpl["labels"]
    digits, confs = [], []
    for b in _blobs(g)[:max_digits]:
        crop = g[b["y"]:b["y"] + b["h"], b["x"]:b["x"] + b["w"]]
        if crop.size == 0:
            continue
        c = cv2.resize(crop, (TPL_W, TPL_H), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
        scores = []
        for i in range(T.shape[0]):
            r = float(cv2.matchTemplate(c, T[i], cv2.TM_CCOEFF_NORMED)[0, 0])
            scores.append(r)
        j = int(np.argmax(scores))
        digits.append(int(L[j]))
        confs.append(round(float(scores[j]), 3))
    if not digits:
        return None, []
    return int("".join(str(d) for d in digits)), confs


def read_k_gold(crop, tpl=None, min_conf=0.5, per_unit=100, max_blobs=4):
    """面板经济 "XX.Yk" -> **绝对金币**整数。返回 (gold, 逐位置信度) 或 (None, [])。

    为什么不能用 read_number：战绩面板的经济固定写成 "XX.Yk"（一位小数 + 千位后缀），
      而 read_number 会把小数点整个丢掉、还把尾随的 'k' 当成数字 —— 真机实测
      "33.7k" -> 3378（差两个数量级），"12.5k" -> 1258，直接拿去用会毒化状态层。
    做法：逐字符块读，只保留**高置信**字符。'k' 被误认成 8 时置信度只有 ~0.19，
      而真数字稳定在 0.85~0.97（两个真机帧都复现）-> 滤掉低置信就只剩 "337"。
    面板固定一位小数 -> abs_gold = int("XXY") * per_unit（33.7k = 337 * 100 = 33700）。
    """
    try:
        if crop is None or crop.size == 0:
            return None, []
        g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
        tpl = tpl or load_templates()
        digits, confs = [], []
        for b in _blobs(g)[:max_blobs]:
            v, cf = read_number(g[b["y"]:b["y"] + b["h"], b["x"]:b["x"] + b["w"]], tpl=tpl)
            if v is None or not cf or max(cf) < min_conf:
                continue                 # 低置信 -> 更像 'k'/'.' 这类非数字字符，丢掉
            digits.append(str(v))
            confs += list(cf)
        if not digits:
            return None, []
        return int("".join(digits)) * int(per_unit), confs
    except Exception:
        return None, []


def load_digit_rois():
    """configs/calibration_absolute.json 的 digit_rois：{name: [x0,y0,x1,y1]}（1280 帧空间）。"""
    try:
        d = json.loads(CALIB_PATH.read_text(encoding="utf-8"))
        return d.get("digit_rois") or {}
    except Exception:
        return {}


def read_roi(frame, name, rois=None, tpl=None):
    """按名字读 HUD 数字（'level' / 'gold' / 'skill_pt' ...）。未标定 -> None。"""
    rois = rois if rois is not None else load_digit_rois()
    roi = rois.get(name)
    if not roi:
        return None, []
    x0, y0, x1, y1 = [int(v) for v in roi]
    h, w = frame.shape[:2]
    x0, x1 = max(0, x0), min(w, x1)
    y0, y1 = max(0, y0), min(h, y1)
    if x1 <= x0 or y1 <= y0:
        return None, []
    return read_number(frame[y0:y1, x0:x1], tpl=tpl)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        build_templates()
        raise SystemExit(0)
    if len(sys.argv) > 2:
        img = cv2.imdecode(np.fromfile(sys.argv[1], dtype=np.uint8), cv2.IMREAD_COLOR)
        print("ROI:", read_roi(img, sys.argv[2]))
        raise SystemExit(0)
    print("用法: python -m wzry.vision.digit_reader build | <帧图> <roi名字>")
    print("已标定 ROI:", list(load_digit_rois().keys()))
