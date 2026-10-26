# -*- coding: utf-8 -*-
"""结算页胜负识别回归（`wzry/vision/match_result.py`）。

两类判据都用**真机样本**标定并锁在测试里（样本已入库 `docs/samples/`）：
  - `settlement_win.png`  胜利 VICTORY：青蓝翼 + 金字
  - `settlement_loss.png` 失败 DEFEAT：暗红/酒红

覆盖：
  T1  合成暗红横幅 → loss
  T2  合成青蓝横幅 → win
  T3  纯色/随机帧 → None（普通画面不得误报）
  T4  真机胜利样本 → win（早期"金色=胜利"的假设就是被这条纠正的）
  T5  真机失败样本 → loss
  T6  真机对局帧 → None
  T7  allow_unverified_win=False → win 分支可关（保守开关仍可用）
  T8  is_settlement 与 classify 一致

运行：python -X utf8 scripts/test_match_result.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision.match_result import (BANNER, classify,  # noqa: E402
                                      is_settlement)

PASS, FAIL = [], []
SAMPLES = ROOT / "docs" / "samples"


def check(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print(f"PASS {name}")
    else:
        FAIL.append(name)
        print(f"FAIL {name} {extra}")


def synth(bgr, size=(720, 1280)):
    """整屏暗底 + 横幅区填色。"""
    h, w = size
    img = np.full((h, w, 3), 18, dtype=np.uint8)
    x0, y0, x1, y1 = BANNER
    img[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)] = bgr
    return img


def main() -> int:
    # 合成：暗红 = 失败（BGR）
    r, c, d = classify(synth((40, 30, 120)))
    check("T1_crimson_banner_is_loss", r == "loss" and c > 0.6, f"{r}/{c:.2f} {d}")

    # 合成：青蓝 = 胜利（BGR：B 高、R 低）
    r2, c2, d2 = classify(synth((200, 120, 40)))
    check("T2_cyan_banner_is_win", r2 == "win" and c2 > 0.6, f"{r2}/{c2:.2f} {d2}")

    # 普通画面不得误报
    flat = np.full((720, 1280, 3), 90, dtype=np.uint8)
    check("T3_flat_frame_no_result", classify(flat)[0] is None)
    rng = np.random.default_rng(7)
    noise = rng.integers(0, 256, (720, 1280, 3), dtype=np.uint8)
    check("T3b_noise_frame_no_result", classify(noise)[0] is None)

    # 真机样本（已入库）
    win_p = SAMPLES / "settlement_win.png"
    loss_p = SAMPLES / "settlement_loss.png"
    if win_p.is_file():
        img = cv2.imread(str(win_p))
        r3, c3, d3 = classify(img)
        check("T4_real_victory_is_win", r3 == "win" and c3 > 0.8, f"{r3}/{c3:.2f} {d3}")
        check("T8_victory_is_settlement", is_settlement(img) is True)
        r3b, _, _ = classify(img, allow_unverified_win=False)
        check("T7_win_branch_can_be_disabled", r3b is None, f"{r3b}")
    else:
        print("SKIP T4/T8/T7（缺 docs/samples/settlement_win.png）")

    if loss_p.is_file():
        img = cv2.imread(str(loss_p))
        r4, c4, d4 = classify(img)
        check("T5_real_defeat_is_loss", r4 == "loss" and c4 > 0.6, f"{r4}/{c4:.2f} {d4}")
    else:
        print("SKIP T5（缺 docs/samples/settlement_loss.png）")

    mdir = ROOT / "data" / "matches"
    frames = sorted(mdir.glob("*/frames/*.png"))[:5] if mdir.is_dir() else []
    if frames:
        bad = [p.name for p in frames if classify(cv2.imread(str(p)))[0] is not None]
        check("T6_in_match_frames_no_result", not bad, f"{bad}")
    else:
        print("SKIP T6（没有 data/matches 帧）")

    print(f"\n结果: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
    if FAIL:
        print("失败项:", ", ".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
