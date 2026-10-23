# -*- coding: utf-8 -*-
"""encode_state 白名单过滤回归（S1-3）。

白名单外类别（grass/未知类）不能占 mask，否则会以全零 one-hot 污染训练特征。
用法：
    venv\\Scripts\\python.exe scripts/test_encoding_filter.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.train.encoding import CLASSES, CLS2ID, encode_state  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  {extra}" if extra else ""))


state = {
    "t": 1.0,
    "units": [
        {"cls": "grass", "screen": [0.1, 0.1, 0.1, 0.1], "conf": 0.9},
        {"cls": "enemy_hero", "screen": [0.5, 0.4, 0.1, 0.2], "conf": 0.8,
         "camp_src": "bar", "dead": False},
        {"cls": "not_a_real_class", "screen": [0.2, 0.2, 0.1, 0.1], "conf": 0.1},
    ],
    "minimap": {"found": False},
    "ui": {},
}
encoded = encode_state(state)
mask = encoded["unit_mask"]
units = encoded["units"]

check("E1 白名单外类别不占 mask", float(mask.sum()) == 1.0, f"mask_sum={mask.sum()}")
check("E2 enemy_hero 被放到第一个有效槽位",
      units[0, CLS2ID["enemy_hero"]] == 1.0 and mask[0] == 1.0)
check("E3 grass/未知类槽位保持全零",
      not units[1].any() and mask[1] == 0.0 and not units[2].any())
check("E4 classes 表包含 enemy_hero 且不含 grass",
      "enemy_hero" in CLASSES and "grass" not in CLS2ID)
check("E5 编码形状不变", units.shape == (20, len(CLASSES) + 7) and mask.shape == (20,))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", ", ".join(FAIL))
    raise SystemExit(1)
print("ENCODING_FILTER_OK")
