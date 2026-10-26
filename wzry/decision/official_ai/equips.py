# -*- coding: utf-8 -*-
"""装备 config_id -> 名称/被动 的查表（数据来自 `configs/equip_ids.json`）。

背景：内存 `equip_ids`（`+0x1928` count / `+0x192C` 数组）已能读出**已购装备**
的 config_id 序列，但 `equip_config_id_file` 不随包，ID→名称只能自建表。
表由 `scripts/build_equip_ids.py` 从腾讯官方赛事数据生成，不手写 ID。

用法：
    from wzry.decision.official_ai import equips
    equips.is_revive_armor([1422, 1337])   # True
    equips.name_of(1337)                   # '贤者的庇护'
    equips.revive_ids()                    # {1337}
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PATH = ROOT / "configs" / "equip_ids.json"

_cache: Optional[dict] = None


def load(path: str | Path | None = None, *, reload: bool = False) -> dict:
    """读表（带缓存）。文件缺失时返回空表而不是抛错——决策层要能降级。"""
    global _cache
    if _cache is not None and not reload and path is None:
        return _cache
    p = Path(path) if path else DEFAULT_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = {"items": {}, "revive_armor_ids": [], "missing": str(p)}
    items = {int(k): v for k, v in (data.get("items") or {}).items()}
    data["items"] = items
    data["revive_armor_ids"] = [int(i) for i in (data.get("revive_armor_ids") or [])]
    if path is None:
        _cache = data
    return data


def revive_ids() -> set[int]:
    return set(load().get("revive_armor_ids") or [])


def is_revive_armor(equip_ids: Iterable[int] | None) -> bool:
    """已购装备里是否含复活甲（贤者的庇护）。

    ⚠️ 语义是"已购买（含合成小件）"；复活甲有 150s 冷却且每局限 2 次，
    仅凭 ID 无法判断**当前是否可用**——需要的话得读装备 CD，这里只回答"有没有"。
    """
    if not equip_ids:
        return False
    rid = revive_ids()
    if not rid:
        return False
    return any(int(e) in rid for e in equip_ids if isinstance(e, (int, float)))


def name_of(equip_id: int) -> Optional[str]:
    rec = (load().get("items") or {}).get(int(equip_id))
    return rec.get("name") if rec else None


def names(equip_ids: Iterable[int] | None) -> list[str]:
    out: list[str] = []
    for e in equip_ids or []:
        n = name_of(int(e))
        out.append(n or f"#{int(e)}")
    return out
