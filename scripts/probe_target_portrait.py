# -*- coding: utf-8 -*-
"""取证分析：在已落的对局帧里
  (1) 验证"场景内敌方头顶红条"血量比例读数（wzry.vision.hero_hp）
  (2) 定位"条件性顶部目标头像"——用 A/B：有敌人的帧 vs 没有敌人的帧，
      看顶部 HUD 带里是否**只在有敌人时**出现一条新的红/血条。

用法：
    venv\\Scripts\\python.exe scripts\\probe_target_portrait.py <会话帧目录>
"""
import glob
import os
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision.hero_hp import enemy_hp_from_dets  # noqa: E402
from wzry.vision.hok_yolov5 import HokYoloV5  # noqa: E402

Y_MAX_FRAC = 0.12       # 顶部 HUD 带（与 calibrate_top_bar 同口径）
MIN_W = 25              # 候选红条最小宽度（头像血条 ~30-80px）


def top_hud_bars(frame, y_max_frac=Y_MAX_FRAC, min_w=MIN_W):
    """顶部 HUD 带里的红/蓝横条 -> [(x0,x1,w,y,h,color)]（跨行合并）。"""
    h, w = frame.shape[:2]
    band = frame[0:int(h * y_max_frac)]
    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
    H, S, V = hsv[..., 0].astype(int), hsv[..., 1].astype(int), hsv[..., 2].astype(int)
    masks = {"red": (((H <= 15) | (H >= 168)) & (S > 50) & (V > 80)).astype(np.uint8),
             "blue": ((H >= 95) & (H <= 135) & (S > 45) & (V > 70)).astype(np.uint8)}
    out = []
    for color, m in masks.items():
        rows = []
        for y in range(m.shape[0]):
            idx = np.nonzero(m[y])[0]
            if len(idx) == 0:
                continue
            s = p = idx[0]
            for i in idx[1:]:
                if i - p > 3:
                    if p - s + 1 >= min_w:
                        rows.append((y, int(s), int(p)))
                    s = i
                p = i
            if p - s + 1 >= min_w:
                rows.append((y, int(s), int(p)))
        merged = []
        for y, x0, x1 in rows:
            for o in merged:
                if y - o[3] <= 2 and not (x1 < o[0] - 6 or x0 > o[1] + 6):
                    o[3] = y
                    o[0], o[1] = min(o[0], x0), max(o[1], x1)
                    break
            else:
                merged.append([x0, x1, y, y])
        for x0, x1, y0, y1 in merged:
            out.append((x0, x1, x1 - x0 + 1, y0, y1 - y0 + 1, color))
    return sorted(out, key=lambda t: (t[3], t[0]))


def main():
    if len(sys.argv) < 2:
        print("用法: python scripts/probe_target_portrait.py <会话帧目录>")
        return 1
    d = sys.argv[1]
    ef = sorted(glob.glob(os.path.join(d, "enemy_*.png")))
    pf = sorted(glob.glob(os.path.join(d, "frame_*.png")))
    print(f"敌人帧={len(ef)}  普通帧={len(pf)}")
    if not ef:
        print("没有 enemy_*.png（说明该局内检测器没看到敌方英雄）")
    det = HokYoloV5(conf=0.35)

    def analyze(paths, tag):
        n_enemy, n_hud, hp_vals = 0, 0, []
        hud_shapes = []
        for f in paths:
            img = cv2.imdecode(np.fromfile(f, dtype=np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            ds = det.detect(img)
            eh = [x for x in ds if x.get("cls") == "enemy_hero"]
            if eh:
                n_enemy += 1
            hp = enemy_hp_from_dets(img, ds)
            hp_vals += hp
            bars = top_hud_bars(img)
            if bars:
                n_hud += 1
                hud_shapes.append((os.path.basename(f), bars))
        print(f"-- {tag}: 帧={len(paths)} 含敌方英雄={n_enemy} 顶部HUD有条={n_hud} "
              f"场景敌方HP样本={len(hp_vals)} {hp_vals[:8]}")
        for nm, bars in hud_shapes[:6]:
            print(f"     {nm}: {[(x0, wd, y0, hh, c) for x0, x1, wd, y0, hh, c in bars][:6]}")
        return n_enemy, n_hud, hp_vals

    analyze(ef, "有敌人的帧")
    analyze(pf, "普通帧")
    print("\n判读：若'有敌人的帧'顶部出现而'普通帧'不出现的红/蓝条，"
          "那条就是'锁定/攻击敌方时才出现'的目标头像血条。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
