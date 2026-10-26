# -*- coding: utf-8 -*-
"""召唤师技能识别（官方 `SUMMON_SKILL_*_CONFIG_ID` 的本地替代）。

**为什么需要单独一层**：官方按 `skill_config_id` 精确判断"这是不是惩击/寒冰惩击/传送"
（日志原文 `[UseChengji] not use, not hanbing chengji, skill_config_id:%d`）。
这个 id → 名字的映射表**不随包**，静态拿不到全量。所以这里分两条路：

1. **实测映射**（`KNOWN_IDS`）：真机读到的 id 直接登记，可信。
2. **CD 窗口推断**（`CD_WINDOWS`）：惩击总 CD 30s，是所有召唤师技能里唯一落在这个窗口的，
   用它做兜底判别。**只用于 30s 这一档**，其它技能一律不猜。

未识别的槽位 `find()` 返回 `None`，调用方（规则）就保持惰性 ——
宁可这条规则不触发，也不要把闪现当惩击交出去。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional, Tuple

# 实测登记（2026-10-21 真机，技能区 stride 0x78，见 docs/wzry-ai-data-inputs.md §11）
#   80115 → slot6，总 CD 120s（闪现量级）
#   90003 → slot5，总 CD  60s（治疗）
#   90005 → slot7，总 CD   0  （回城）
KNOWN_IDS = {
    80115: "flash",
    90003: "heal",
    90005: "recall",
}

# CD 窗口兜底：惩击 30s。区间取 ±5s 容忍配置差异。
#   为什么只做这一档：其它召唤师技能的 CD 会随版本调整，猜错代价是"把闪现当惩击交了"。
CD_WINDOWS: List[Tuple[int, int, str]] = [
    (25_000, 35_000, "smite"),
]

# 官方配置项名（日志里出现过，供将来灌表时对齐）
CONFIG_KEY = "summoner_skill_ids"
CONFIG_PATH = Path("configs/summoner_skills.json")


def _load_override() -> dict:
    """读 `configs/summoner_skills.json`（可选）。格式：{"80115": "flash", ...}"""
    for base in (Path.cwd(), Path(__file__).resolve().parents[3]):
        p = base / CONFIG_PATH
        if not p.exists():
            continue
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if isinstance(raw, dict):
            out = {}
            for k, v in (raw.get(CONFIG_KEY) or raw).items():
                try:
                    out[int(k)] = str(v)
                except Exception:
                    continue
            return out
    return {}


def _table() -> dict:
    t = dict(KNOWN_IDS)
    t.update(_load_override())
    return t


def role_of(skill_id: int, cd_total_ms: Optional[int] = None) -> Optional[str]:
    """技能 id（+ 可选总 CD）→ 语义角色。未知返回 None。"""
    if not skill_id:
        return None
    r = _table().get(int(skill_id))
    if r:
        return r
    if cd_total_ms:
        for lo, hi, name in CD_WINDOWS:
            if lo <= int(cd_total_ms) <= hi:
                return name
    return None


def slots_of(me) -> List[dict]:
    """英雄身上所有召唤师类槽位（含内存给的 role），统一成 dict 列表。

    每项：`{slot, id, role, cd_left_ms, cd_total_ms, ready}`

    **三重排除**（顺序很重要，否则会把英雄技能当召唤师技能）：
      1. 槽位 1–3 恒为英雄技能（内存 `role=skill1..3`；离线夹具可能没有 role 字段）；
      2. id 落在英雄技能区间（`id // 100 == 英雄 id`）；
      3. id 不在召唤师技能号段（80000–90000）且内存没标 `summoner` 的，不做 CD 兜底推断 ——
         否则"某个 30s CD 的普通技能"会被误判成惩击。
    """
    self_cid = int(getattr(me, "cid", 0) or 0)
    out = []
    for s in (getattr(me, "skills", None) or []):
        sid = int(s.get("id") or 0)
        total = int(s.get("cd_total_ms") or 0)
        slot = int(s.get("slot") or 0)
        mem_role = str(s.get("role") or "")
        if mem_role in ("skill1", "skill2", "skill3"):
            continue
        if slot and slot <= 3 and mem_role not in ("heal", "recall", "summoner"):
            continue                                   # 槽位 1-3 恒为英雄技能
        if self_cid and sid and sid // 100 == self_cid:
            continue                                   # 英雄技能 id 形如 10610/10620
        in_summoner_band = 80_000 <= sid < 90_000
        if mem_role in ("heal", "recall"):
            role = mem_role
        elif mem_role == "summoner" or in_summoner_band:
            role = role_of(sid, total) or "summoner"
        else:
            role = None                                # 号段外且没标注 -> 不猜
        if role is None:
            continue
        out.append({
            "slot": slot,
            "id": sid,
            "role": role,
            "cd_left_ms": int(s.get("cd_left_ms") or 0),
            "cd_total_ms": total,
            "ready": bool(s.get("ready")) or int(s.get("cd_left_ms") or 0) <= 0,
        })
    return out


def find(me, role: str) -> Optional[dict]:
    """按角色找槽位；找不到返回 None（调用方应保持惰性）。"""
    for s in slots_of(me):
        if s["role"] == role:
            return s
    return None


def is_smite(me) -> bool:
    return find(me, "smite") is not None


def ready(me, role: str) -> bool:
    s = find(me, role)
    return bool(s and s["ready"] and s["cd_left_ms"] <= 0)
