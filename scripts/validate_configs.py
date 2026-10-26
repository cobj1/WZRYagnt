# -*- coding: utf-8 -*-
"""配置/标定一致性校验（无设备可跑，CI 入口）。

用法：
    python -X utf8 scripts/validate_configs.py
    python -X utf8 scripts/validate_configs.py --strict   # warning 也失败
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIGS = ROOT / "configs"
REQUIRED_POINTS = {
    "move_stick_center", "dir_up", "dir_down", "dir_left", "dir_right",
    "dir_left_up", "dir_right_up", "dir_left_down", "dir_right_down",
    "skill1", "skill2", "skill3", "attack", "recall", "restore", "summoner",
    "active1", "active2", "attack_minion", "attack_tower",
}
OPTIONAL_CONFIGS = [
    "paths.json", "knowledge.json", "lane_priority.json", "tactics.json",
    "equip_ids.json", "value_weights.json", "policy_tune.json",
]


def read_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)
    errors, warnings = [], []

    classes_path = CONFIGS / "classes.json"
    if not classes_path.exists():
        errors.append("missing configs/classes.json")
    else:
        classes = read_json(classes_path)
        if not isinstance(classes, list) or not classes:
            errors.append("classes.json 必须是非空 list")
        elif len(classes) != len(set(classes)):
            errors.append("classes.json 存在重复类名")

    abs_path = CONFIGS / "calibration_absolute.json"
    if not abs_path.exists():
        errors.append("missing configs/calibration_absolute.json")
    else:
        cal = read_json(abs_path)
        for key in ("screen_width", "screen_height", "points", "ui_taps", "panel_rois",
                    "top_bar", "digit_rois", "minimap_box"):
            if key not in cal:
                errors.append(f"calibration_absolute.json 缺少 {key}")
        pts = set((cal.get("points") or {}).keys())
        missing = REQUIRED_POINTS - pts
        if missing:
            errors.append(f"calibration_absolute.points 缺少 {sorted(missing)}")
        mb = cal.get("minimap_box") or {}
        for size in ("1280x720", "1280x576"):
            if size not in mb:
                errors.append(f"minimap_box 缺少 {size}")

    legacy_path = CONFIGS / "calibration.json"
    if legacy_path.exists() and abs_path.exists():
        legacy = read_json(legacy_path)
        cal = read_json(abs_path)
        w, h = cal.get("screen_width", 1280), cal.get("screen_height", 720)
        for key, val in (cal.get("points") or {}).items():
            if key not in legacy:
                continue
            old_px = (legacy[key][0] * w, legacy[key][1] * h)
            drift = math.hypot(old_px[0] - val[0], old_px[1] - val[1])
            if drift > 1.0:
                warnings.append(f"calibration.json {key} 与 absolute 漂移 {drift:.1f}px")

    for name in OPTIONAL_CONFIGS:
        if not (CONFIGS / name).exists():
            warnings.append(f"configs/{name} 不存在（相关功能会降级）")

    e_drive = re.compile(r"""["']E:[\\/]""")
    hits = []
    for base in (ROOT / "wzry", ROOT / "scripts"):
        for p in base.rglob("*.py"):
            if "venv" in p.parts:
                continue
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if e_drive.search(txt):
                hits.append(str(p.relative_to(ROOT)))
    if hits:
        errors.append("硬编码 E: 路径: " + ", ".join(sorted(hits)[:10]))

    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    bad = errors or (args.strict and warnings)
    print(f"configs: {len(errors)} errors, {len(warnings)} warnings")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
