# -*- coding: utf-8 -*-
"""校准文件加载：统一输出【归一化坐标】键值表。

支持两种现有格式：
  - configs/calibration.json             （已归一化：{"minimap_center": [nx, ny], ...}）
  - configs/calibration_absolute.json    （绝对像素：{"screen_width": W, "screen_height": H, "points": {...}}）
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CALIB = ROOT / "configs" / "calibration.json"
DEFAULT_CALIB_ABS = ROOT / "configs" / "calibration_absolute.json"


def load_calibration(path=None):
    """返回 (归一化点表, source_path)。

    优先级：显式 path -> calibration_absolute.json（全量 20 点）-> calibration.json（旧 6 点）。
    旧文件的 `minimap_center` 会合并进 absolute 结果，避免切换源后丢键。
    """
    if path:
        p = Path(path)
        if not p.is_absolute():
            p = ROOT / p
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "move_stick_center" in data:
            return data, p
        if "points" in data:
            w, h = data["screen_width"], data["screen_height"]
            return {k: [x / w, y / h] for k, (x, y) in data["points"].items()}, p
        raise ValueError(f"校准文件 {p} 格式不符")

    legacy = {}
    if DEFAULT_CALIB.exists():
        with open(DEFAULT_CALIB, "r", encoding="utf-8") as f:
            legacy = json.load(f)
    if DEFAULT_CALIB_ABS.exists():
        with open(DEFAULT_CALIB_ABS, "r", encoding="utf-8") as f:
            data = json.load(f)
        w, h = data["screen_width"], data["screen_height"]
        norm = {k: [x / w, y / h] for k, (x, y) in data["points"].items()}
        if "minimap_center" in legacy and "minimap_center" not in norm:
            norm["minimap_center"] = legacy["minimap_center"]
        return norm, DEFAULT_CALIB_ABS

    if legacy and "move_stick_center" in legacy:
        return legacy, DEFAULT_CALIB
    raise FileNotFoundError(
        "未找到校准文件，请先运行 scripts/control/calibrate.py 或 calibrate_absolute.py"
    )


def point_norm(calib, label, default=None):
    """取归一化点，缺失时返回 default。"""
    return calib.get(label, default)
