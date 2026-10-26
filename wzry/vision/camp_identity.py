# -*- coding: utf-8 -*-
"""阵营无关的自我定位与敌我判定。

为什么不能直接信 camp 或 slot0：
- 上游 TrueVision 在 11.3.1.1 上发现红方开局会"红蓝翻转"，读者不能假设自己=蓝方；
- `next(u for u in units if u.cid == self_cid)` 在同 cid 多实例（镜像/傀儡类）时会认错人；
- 内存英雄数组的 slot 与本地玩家无关，旧代码"slot0=自己"是未验证假设。

本模块只做纯计算：输入单位序列（MemUnit 或 dict）+ 自己的 cid，可选自己的世界坐标
（来自 self 的 C# 状态对象，是最可靠的身份锚点），输出自己/阵营/敌我划分与判定来源。
同 cid 多实例时按与 self 坐标的距离消歧；消不了就明确报 ambiguous，不猜。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Tuple

BLUE = "blue"
RED = "red"
_CAMP_BY_ID = {1: BLUE, 2: RED}
_ID_BY_CAMP = {BLUE: 1, RED: 2}
_OTHER = {BLUE: RED, RED: BLUE}

# 同 cid 多实例消歧参数（世界坐标单位，地图约 ±60000）
DEFAULT_MAX_DIST = 6000.0   # self 状态对象与 actor 行允许的最大偏差
NEAR_EXACT = 400.0          # 这么近直接认定，不再要求与第二候选拉开差距
DEFAULT_MIN_GAP = 600.0     # 否则要求最近与次近至少差这么多

# 泉水坐标（与 official_ai/organ_slots.SPRING_PTS 同源；地图固定）。
# 只有"站在泉水附近"才能由位置反推阵营：英雄可以越界到对面半场，
# 用 x 符号判阵营在中期会把入侵者判反，故不采用。
BLUE_SPRING = (-52474.56, -44193.63)
RED_SPRING = (48154.48, 53081.14)
BASE_INFER_RADIUS = 9000.0


def camp_name(v: Any) -> Optional[str]:
    """'blue'/'red'/1/2 -> 规范阵营名；其它值返回 None。"""
    if isinstance(v, str):
        s = v.strip().lower()
        return s if s in _OTHER else None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return _CAMP_BY_ID.get(int(v))
    return None


def camp_id(v: Any) -> Optional[int]:
    name = camp_name(v)
    return _ID_BY_CAMP[name] if name else None


def enemy_camp(camp: Any) -> Optional[str]:
    name = camp_name(camp)
    return _OTHER[name] if name else None


def infer_camp_from_base(pos: Optional[Tuple[float, float]]
                         ) -> Optional[str]:
    """由"是否站在某方泉水附近"推断阵营（仅作兜底，来源须标注）。

    不用 x 符号判阵营：英雄可以越界到对面半场，中期会把入侵者判反。
    只有在某一方泉水半径内，位置才等价于阵营。
    """
    if not pos:
        return None
    x, z = float(pos[0]), float(pos[1])
    db = ((x - BLUE_SPRING[0]) ** 2 + (z - BLUE_SPRING[1]) ** 2) ** 0.5
    dr = ((x - RED_SPRING[0]) ** 2 + (z - RED_SPRING[1]) ** 2) ** 0.5
    if min(db, dr) > BASE_INFER_RADIUS:
        return None
    return BLUE if db <= dr else RED


# ---------- 单位访问器：同时兼容 MemUnit 与 dict ----------
def _field(u: Any, *names: str) -> Any:
    for name in names:
        if isinstance(u, dict):
            if u.get(name) is not None:
                return u[name]
        else:
            v = getattr(u, name, None)
            if v is not None:
                return v
    return None


def cid_of(u: Any) -> int:
    v = _field(u, "config_id", "cid", "hero_cid", "id")
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def camp_of(u: Any) -> Optional[str]:
    return camp_name(_field(u, "camp"))


def pos_of(u: Any) -> Optional[Tuple[float, float]]:
    if isinstance(u, dict):
        p = u.get("pos_world")
        if isinstance(p, (list, tuple)) and len(p) == 2:
            return float(p[0]), float(p[1])
    x = _field(u, "x")
    z = _field(u, "z")
    if x is None or z is None:
        return None
    return float(x), float(z)


def is_hero(u: Any) -> bool:
    if isinstance(u, dict):
        kind = str(u.get("kind") or "")
        if kind:
            return kind == "hero"
        return u.get("actor_type") in (None, 0)
    kind = getattr(u, "kind", None)
    if kind:
        return kind == "hero"
    return getattr(u, "is_hero", False)


def _dist(a: Optional[Tuple[float, float]], b: Optional[Tuple[float, float]]) -> Optional[float]:
    if a is None or b is None:
        return None
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


@dataclass
class SelfResolution:
    unit: Optional[Any]          # 定位到的自己（None = ambiguous/missing）
    match: str                   # cid / cid+pos / ambiguous / missing
    camp: Optional[str]
    camp_src: str                # unit / unit_x / cid_consensus / self_x / unknown
    candidates: int = 0

    @property
    def ok(self) -> bool:
        return self.unit is not None


def pick_self(units: Iterable[Any], self_cid: int,
              self_pos: Optional[Tuple[float, float]] = None,
              heroes_only: bool = True,
              max_dist: float = DEFAULT_MAX_DIST,
              min_gap: float = DEFAULT_MIN_GAP
              ) -> Tuple[Optional[Any], str, int]:
    """按 cid 找自己；同 cid 多实例时用 self 坐标消歧。

    返回 (unit, match, n_candidates)；match ∈ cid / cid+pos / ambiguous / missing。
    """
    cands = [u for u in units
             if cid_of(u) == int(self_cid) and (is_hero(u) or not heroes_only)]
    n = len(cands)
    if n == 0:
        return None, "missing", 0
    if n == 1:
        return cands[0], "cid", 1
    if self_pos is None:
        return None, "ambiguous", n

    scored = []
    for u in cands:
        d = _dist(pos_of(u), self_pos)
        if d is not None:
            scored.append((d, u))
    if not scored:
        return None, "ambiguous", n
    scored.sort(key=lambda t: t[0])
    best_d, best = scored[0]
    if best_d > max_dist:
        return None, "ambiguous", n
    if best_d <= NEAR_EXACT:
        return best, "cid+pos", n
    if len(scored) > 1 and scored[1][0] - best_d < min_gap:
        return None, "ambiguous", n
    return best, "cid+pos", n


def resolve_self(units: Iterable[Any], self_cid: int,
                 self_pos: Optional[Tuple[float, float]] = None,
                 heroes_only: bool = True,
                 max_dist: float = DEFAULT_MAX_DIST,
                 min_gap: float = DEFAULT_MIN_GAP) -> SelfResolution:
    """定位自己并给出自身阵营（带来源）。

    阵营优先级：自己单位的 camp > 自己单位是否在泉水（位置反推）> 同 cid 候选的
    阵营共识 > 传入 self_pos 是否在泉水 > unknown。镜像局（同 cid 双阵营且无坐标）
    会得到 camp=None，调用方不得默认"我方=蓝"。
    """
    units = list(units)
    unit, match, n = pick_self(units, self_cid, self_pos, heroes_only,
                               max_dist, min_gap)
    camp = None
    src = "unknown"
    if unit is not None:
        camp = camp_of(unit)
        if camp:
            src = "unit"
        else:
            camp = infer_camp_from_base(pos_of(unit))
            if camp:
                src = "unit_base"
    if camp is None and n:
        cands = [u for u in units
                 if cid_of(u) == int(self_cid) and (is_hero(u) or not heroes_only)]
        camps = {c for c in (camp_of(u) for u in cands) if c}
        if len(camps) == 1:
            camp = camps.pop()
            src = "cid_consensus"
    if camp is None and self_pos is not None:
        camp = infer_camp_from_base(self_pos)
        if camp:
            src = "self_base"
    return SelfResolution(unit=unit, match=match, camp=camp, camp_src=src,
                          candidates=n)


def classify(u: Any, self_camp: Optional[str]) -> str:
    """单位相对自己的敌我：ally / enemy / unknown（阵营缺失时不猜）。"""
    camp = camp_name(self_camp)
    other = camp_of(u)
    if camp is None or other is None:
        return "unknown"
    return "ally" if other == camp else "enemy"


def split_teams(units: Iterable[Any], self_camp: Optional[str],
                heroes_only: bool = False
                ) -> Tuple[List[Any], List[Any], List[Any]]:
    """按自身阵营分三组：(allies, enemies, unknown)。"""
    allies: List[Any] = []
    enemies: List[Any] = []
    unknown: List[Any] = []
    for u in units:
        if heroes_only and not is_hero(u):
            continue
        side = classify(u, self_camp)
        (allies if side == "ally" else enemies if side == "enemy"
         else unknown).append(u)
    return allies, enemies, unknown
