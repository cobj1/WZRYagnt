# -*- coding: utf-8 -*-
"""self_bars 分辨率缩放回归（S1-3）。

生产帧 1280x576（20:9）曾套用 720p 绝对像素常量，导致血条排除区/宽度阈值系统性偏移。
用法：
    venv\\Scripts\\python.exe scripts/test_self_bars_scaling.py
"""
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision import self_bars  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  {extra}" if extra else ""))


def frame_720() -> np.ndarray:
    return np.zeros((720, 1280, 3), dtype=np.uint8)


def frame_576() -> np.ndarray:
    return np.zeros((576, 1280, 3), dtype=np.uint8)


def add_green_bar(frame, y: int, width: int = 100, height: int = 6) -> None:
    x0 = (frame.shape[1] - width) // 2
    cv2.rectangle(frame, (x0, y), (x0 + width, y + height), (0, 255, 0), -1)


# 1. 缩放因子
check("R1 720p 缩放为 1.0", self_bars._scale(frame_720()) == (1.0, 1.0))
check("R2 576p 垂直缩放为 0.8", self_bars._scale(frame_576()) == (1.0, 0.8))

# 2. 空白帧不误检、不抛异常
for name, frame in (("720p", frame_720()), ("576p", frame_576())):
    bars = self_bars.detect_all_bars(frame)
    check(f"R3 {name} 空白帧无血条", not bars["self"] and not bars["allies"] and not bars["enemies"])
    hp, mp, pos = self_bars.self_hp_mp(frame)
    check(f"R4 {name} 空白帧 self_hp_mp 返回空", hp is None and mp is None and pos is None)
    check(f"R5 {name} death_banner_px 为 0", self_bars.death_banner_px(frame) == 0)
    check(f"R6 {name} hero_bar_check 无条", self_bars.hero_bar_check(frame, 640, 300) is None)

# 3. 合成绿条在两种分辨率下都能检出，且 HP 比例一致
bar_w = 100
frame = frame_720()
add_green_bar(frame, y=400, width=bar_w)
bars = self_bars.detect_all_bars(frame)
hp, _, pos = self_bars.self_hp_mp(frame)
check("R7 720p 合成绿条可检出", len(bars["self"]) >= 1 and hp is not None)
check("R8 720p HP 比例按满条 115px 计算", abs(hp - bar_w / 115.0) < 0.02, f"hp={hp}")
check("R9 720p 英雄位置为条中心下方", pos is not None and pos[1] > 400)

frame = frame_576()
add_green_bar(frame, y=int(400 * 0.8), width=bar_w)
bars = self_bars.detect_all_bars(frame)
hp576, _, pos576 = self_bars.self_hp_mp(frame)
check("R10 576p 合成绿条可检出", len(bars["self"]) >= 1 and hp576 is not None)
check("R11 576p HP 比例与 720p 同逻辑宽度一致", abs(hp576 - bar_w / 115.0) < 0.02, f"hp={hp576}")
check("R12 576p 英雄 gap 按 0.8 缩放",
      pos576 is not None and pos576[1] < pos[1],
      f"720p_y={pos[1]} 576p_y={pos576[1]}")

# 4. 1920x1080 也按基准缩放，不因绝对 y 阈值漏检
frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
add_green_bar(frame, y=600, width=150, height=9)
bars = self_bars.detect_all_bars(frame)
hp1080, _, _ = self_bars.self_hp_mp(frame)
check("R13 1080p 合成绿条可检出", len(bars["self"]) >= 1 and hp1080 is not None)
check("R14 1080p HP 比例按 1.5x 满条 172.5px 计算",
      hp1080 is not None and abs(hp1080 - 150.0 / 172.5) < 0.02, f"hp={hp1080}")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", ", ".join(FAIL))
    raise SystemExit(1)
print("SELF_BARS_SCALING_OK")
