# -*- coding: utf-8 -*-
"""分路优先级：复刻官方 `ssg` 脚本层的 `taowa_lane_priority`。

官方证据：
  - `ssg::SSGScrMg::InitTaowaLanePriority`（`0x273b088`）加载分路优先级表；
  - 日志 `Load PriorityConfig [%s] ERROR: wrong lane %s` / `[%s].size = %d (!= 5)`；
  - 配置键 `taowa_lane_priority`、`ssg_script_taowa_config`；
  - 另有 `GetHeroBranchRoadType` / `GetActorSoldierRegionInfo` 提供"英雄该走哪条路"。

`.size != 5` 说明每条分路优先级是**长度 5 的数组**（5v5 五个位置：对抗/中/发育/游走/打野）。
官方按"位置 -> 分路偏好"查表；我们拿不到那张表，所以用**职业-分路先验**近似，
并把权重做成可调（`configs/lane_priority.json` 存在时优先读它）。

用途：给走位价值场加一项"本路推进"偏置，避免全队挤在一条路上。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[3]
CFG = ROOT / "configs" / "lane_priority.json"

LANES = ("top", "mid", "bottom")

# 职业 -> 分路先验（官方表是"位置 0..4"，我们只有英雄 cid，先用通用先验）
#   发育路(bottom) 给射手，中路给法师，对抗路(top) 给战士/坦克，打野/游走居中。
ROLE_LANE = {
    "marksman": ("bottom", "mid", "top"),
    "mage": ("mid", "bottom", "top"),
    "fighter": ("top", "mid", "bottom"),
    "tank": ("top", "mid", "bottom"),
    "assassin": ("mid", "bottom", "top"),
    "support": ("bottom", "mid", "top"),
}
DEFAULT_PRIORITY = ("bottom", "mid", "top")


@dataclass
class LanePlan:
    order: Tuple[str, ...] = DEFAULT_PRIORITY
    weights: Dict[str, float] = None

    def __post_init__(self):
        if self.weights is None:
            # 第一位权重 1.0，依次递减（官方表是离散优先级，这里做连续化）
            n = max(1, len(self.order))
            self.weights = {ln: 1.0 - i / (n + 1) for i, ln in enumerate(self.order)}


def load_config() -> Optional[dict]:
    if CFG.exists():
        try:
            return json.loads(CFG.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def plan_for(role: Optional[str] = None, hero_cid: int = 0) -> LanePlan:
    """按职业给分路计划；配置里有按 cid 的覆盖表时优先用它。"""
    cfg = load_config() or {}
    by_cid = (cfg.get("by_cid") or {}).get(str(hero_cid))
    if by_cid:
        order = tuple(by_cid)
        return LanePlan(order=order, weights=cfg.get("weights"))
    order = ROLE_LANE.get((role or "").lower(), DEFAULT_PRIORITY)
    return LanePlan(order=order, weights=cfg.get("weights"))


def lane_anchor(snap, lane: str) -> Optional[Tuple[float, float]]:
    """分路锚点：优先用该路一塔的活体/静态位置。"""
    camp = "friend" if (snap.self_camp or 1) == 1 else "enemy"
    for tier in ("norm_tower_1st", "high_tower"):
        name = f"{camp}_{lane}_{tier}"
        u = (snap.organ_slots or {}).get(name)
        if u is not None and u.x is not None:
            return u.x, u.z
        g = (snap.organ_geo or {}).get(name)
        if g:
            return g[0], g[1]
    return None


def lane_bonus(snap, point: Tuple[float, float], plan: Optional[LanePlan] = None) -> float:
    """某个点相对"本路推进"的加分（0..1），供价值场使用。"""
    plan = plan or plan_for(hero_cid=snap.self_cid)
    best = 0.0
    for lane, w in plan.weights.items():
        a = lane_anchor(snap, lane)
        if a is None:
            continue
        d = ((point[0] - a[0]) ** 2 + (point[1] - a[1]) ** 2) ** 0.5
        best = max(best, w * max(0.0, 1.0 - d / 12000.0))
    return best
