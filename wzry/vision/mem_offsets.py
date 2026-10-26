# -*- coding: utf-8 -*-
"""内存偏移注册表（MEM-01）。

偏移的唯一可变来源是 configs/mem_offsets.json：带版本、验证日期和证据路径。
代码里的常量必须与该文件一致 —— test_mem_offsets.py 会在漂移时报错，避免
"改了一处忘了另一处"导致读错字段却仍返回看似合法的值。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "configs" / "mem_offsets.json"
REQUIRED = {
    "battle_global", "actor_mgr_off", "actor_attr_off", "actor_pos_off",
    "attr_max_hp", "attr_hp", "pos_x", "pos_y", "pos_z",
    "self_obj_id", "ep_cur", "ep_max", "hero_arr_stride", "hero_arr_count",
}


class OffsetRegistryError(ValueError):
    pass


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        raise OffsetRegistryError(f"偏移不能是布尔值: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 0)
    raise OffsetRegistryError(f"无法解析偏移: {value!r}")


def load(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else DEFAULT_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OffsetRegistryError(f"缺少偏移注册表: {p}") from exc
    except json.JSONDecodeError as exc:
        raise OffsetRegistryError(f"偏移注册表不是合法 JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise OffsetRegistryError("偏移注册表顶层必须是对象")
    offsets = raw.get("offsets")
    if not isinstance(offsets, dict):
        raise OffsetRegistryError("缺少 offsets 对象")
    missing = REQUIRED - set(offsets)
    if missing:
        raise OffsetRegistryError(f"缺少必需偏移: {sorted(missing)}")
    parsed = {k: _as_int(v) for k, v in offsets.items()}
    for key in ("version", "game", "module", "validated_at"):
        if not raw.get(key):
            raise OffsetRegistryError(f"缺少元数据字段: {key}")
    if not isinstance(raw.get("evidence"), list) or not raw["evidence"]:
        raise OffsetRegistryError("必须至少给一条 evidence")
    return {
        "schema": int(raw.get("schema", 1)),
        "version": str(raw["version"]),
        "game": str(raw["game"]),
        "module": str(raw["module"]),
        "validated_at": str(raw["validated_at"]),
        "evidence": [str(x) for x in raw["evidence"]],
        "offsets": parsed,
    }


def drift(module_constants: dict[str, int], registry: dict[str, Any] | None = None) -> dict[str, tuple]:
    """返回 {名字: (代码值, 注册表值)}；空 dict 表示一致。"""
    reg = (registry or load())["offsets"]
    out = {}
    for name, want in reg.items():
        if name not in module_constants:
            continue
        got = module_constants[name]
        if got != want:
            out[name] = (got, want)
    return out
