# -*- coding: utf-8 -*-
"""结算页胜负识别回归（`wzry/vision/match_result.py`）。

覆盖：
  T1  合成暗红横幅 → loss（真机样本已验，这里锁阈值）
  T2  合成金黄横幅 → 默认返回 None（win 待真机样本，不猜）
  T3  同上 + allow_unverified_win=True → win
  T4  纯色/随机帧 → None（不得在普通画面上误报）
  T5  真机结算页样本（若存在 temp/probe/live.png）→ loss
  T6  真机对局帧（若存在 data/matches）→ None
  T7  is_settlement 与 classify 一致

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
    # 失败横幅：暗红（BGR）
    loss_img = synth((40, 30, 120))
    r, c, d = classify(loss_img)
    check("T1_crimson_banner_is_loss", r == "loss" and c > 0.6, f"{r}/{c:.2f} {d}")

    # 胜利横幅：金黄（BGR）
    win_img = synth((60, 190, 235))
    r2, c2, d2 = classify(win_img)
    check("T2_gold_banner_awaits_sample", r2 is None and d2.get("rule") == "gold_banner",
          f"{r2} {d2}")
    r3, c3, _ = classify(win_img, allow_unverified_win=True)
    check("T3_gold_banner_win_when_allowed", r3 == "win" and c3 > 0.5, f"{r3}/{c3:.2f}")

    # 普通画面不得误报
    flat = np.full((720, 1280, 3), 90, dtype=np.uint8)
    check("T4_flat_frame_no_result", classify(flat)[0] is None)
    rng = np.random.default_rng(7)
    noise = rng.integers(0, 256, (720, 1280, 3), dtype=np.uint8)
    check("T5_noise_frame_no_result", classify(noise)[0] is None)

    # 真机样本（有则验，无则跳过）
    live = ROOT / "temp" / "probe" / "live.png"
    if live.is_file():
        img = cv2.imread(str(live))
        r4, c4, d4 = classify(img)
        check("T6_real_settlement_is_loss", r4 == "loss" and c4 > 0.6,
              f"{r4}/{c4:.2f} {d4}")
        check("T7_is_settlement_matches", is_settlement(img) is True)
    else:
        print("SKIP T6/T7（没有 temp/probe/live.png 真机样本）")

    mdir = ROOT / "data" / "matches"
    frames = sorted(mdir.glob("*/frames/*.png"))[:5] if mdir.is_dir() else []
    if frames:
        bad = [p.name for p in frames if classify(cv2.imread(str(p)))[0] is not None]
        check("T8_in_match_frames_no_result", not bad, f"{bad}")
    else:
        print("SKIP T8（没有 data/matches 帧）")

    print(f"\n结果: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
    if FAIL:
        print("失败项:", ", ".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
