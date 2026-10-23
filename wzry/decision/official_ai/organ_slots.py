# -*- coding: utf-8 -*-
"""22 个具名建筑槽位：把"一坨塔"变成"按语义索引的固定表"。

官方做法（`docs/wzry-ai-data-inputs.md` §4.3）不是遍历所有塔再判断，而是**按固定语义取塔**：

    friend/enemy_crystal, friend/enemy_spring_tower,
    friend/enemy_{mid,top,bottom}_high_tower,
    friend/enemy_{mid,top,bottom}_norm_tower_1st,
    friend/enemy_{mid,top,bottom}_norm_tower_2nd          → 11 × 2 = 22

我们的困难是：内存里建筑只有 `cid 92/95` 两种取值（见 §8.4 缺口），**cid 无法区分分路与层级**。
本模块用"位置拓扑"补齐这一维，完全不依赖 cid：

  1. 分路：从本方基地角落出发，比较单位方向与三条分路方向的夹角（top/bottom 为正交边，mid 为对角线）；
  2. 层级：在已标定的该分路 3 个点位里取最近的一个（1st 最远、2nd 居中、high 最近基地）；
  3. 基地：距角落 < BASE_RADIUS 的单位判为 crystal / spring_tower（spring 更靠角落）。

标定点位来自 `wzry/vision/mm_rules_v7.py` 的 `BLUE_TOWER_PTS` / `RED_TOWER_PTS`——
那是 s01–s12 真机帧聚类得到的**固定地图拓扑**（塔不会移动），不是本轮新造的数值。

定位/校验：
  - 静态自检：`python -c "from wzry.decision.official_ai import organ_slots as o; print(o.self_check())"`
  - 真机校验：`scripts/verify_organ_slots.py --live`（一次性，确认 22 槽位全部命中、无冲突）
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

from . import geo

Pt = Tuple[float, float]

# ---- 标定点位（小地图归一化；来源见模块 docstring）--------------------------
# 每侧 11 个：crystal + spring_tower + 3 分路 × (1st / 2nd / high)
# 分路顺序固定为 [1st, 2nd, high]：越靠后离本方基地越近。
LANE_PTS: Dict[int, Dict[str, Tuple[Pt, Pt, Pt]]] = {
    # camp1 = 蓝方 = 小地图左下：(0,1)
    1: {
        "top": ((0.032, 0.300), (0.031, 0.535), (0.045, 0.717)),
        "mid": ((0.366, 0.545), (0.301, 0.661), (0.169, 0.783)),
        "bottom": ((0.695, 0.906), (0.450, 0.906), (0.222, 0.907)),
    },
    # camp2 = 红方 = 小地图右上：(1,0)
    2: {
        "top": ((0.293, 0.051), (0.500, 0.042), (0.711, 0.042)),
        "mid": ((0.578, 0.390), (0.653, 0.277), (0.779, 0.168)),
        "bottom": ((0.907, 0.701), (0.916, 0.415), (0.905, 0.223)),
    },
}

# 水晶点位（同上，取自标定集里最靠近基地角落的那个点）
CRYSTAL_PTS: Dict[int, Pt] = {1: (0.082, 0.847), 2: (0.869, 0.092)}

# 泉水塔：标定集里没有独立点位（被基地建筑遮挡），按"水晶再向角落内推 35%"给出**近似**值。
# 只用于"回城/泉水停留"这类方向性判断，不用于精确距离。`approx=True` 显式标注。
SPRING_FACTOR = 0.35
BASE_RADIUS = 0.20          # 距角落小于此值 -> crystal / spring 候选（水晶实测 0.16~0.17，
                            #   最近的高地塔 0.24，取中间值留余量）
MAX_SLOT_ERR = 0.10         # 到最近槽位超过此距离 -> 判定"不属于这 22 栋"，不入表
                            #   （归一化 0.10 ≈ 地图宽度 10%）。⚠️ 2026-10-21 真机实测：
                            #   静态表与真实世界坐标偏差 6k~18k 世界单位（0.05~0.15），
                            #   所以**不要靠放宽这个门限来兼容真机** —— 那会把地图中央的
                            #   建筑也硬塞进槽位（回归 T39 就是防这个）。
                            #   真机走 `assign_measured()`（cid 定层级 + z 序定分路，精确）。
TIER_NAMES = ("norm_tower_1st", "norm_tower_2nd", "high_tower")
LANES = ("top", "mid", "bottom")


def _spring_pt(camp: int) -> Pt:
    c = geo.CAMP_CORNER[camp]
    x, y = CRYSTAL_PTS[camp]
    return (x + (c[0] - x) * SPRING_FACTOR, y + (c[1] - y) * SPRING_FACTOR)


SPRING_PTS: Dict[int, Pt] = {1: _spring_pt(1), 2: _spring_pt(2)}


def lane_of(pt: Pt, camp: int) -> str:
    """点位所属分路（top/mid/bottom）——只看方向，不看层级。诊断/日志用。"""
    corner = geo.CAMP_CORNER[camp]
    if camp == 1:
        dirs = {"top": (0.0, -1.0), "bottom": (1.0, 0.0)}
    else:
        dirs = {"top": (-1.0, 0.0), "bottom": (0.0, 1.0)}
    dirs["mid"] = ((dirs["top"][0] + dirs["bottom"][0]) / math.sqrt(2.0),
                   (dirs["top"][1] + dirs["bottom"][1]) / math.sqrt(2.0))
    vx, vy = pt[0] - corner[0], pt[1] - corner[1]
    n = math.hypot(vx, vy) or 1.0
    vx, vy = vx / n, vy / n
    return max(dirs, key=lambda k: vx * dirs[k][0] + vy * dirs[k][1])


# 槽位顺序：与 schema.ORGAN_SLOTS 逐字一致（官方 .rodata 顺序）
SLOT_ORDER: Tuple[str, ...] = (
    "friend_crystal", "enemy_crystal",
    "friend_spring_tower", "enemy_spring_tower",
    "friend_mid_high_tower", "enemy_mid_high_tower",
    "friend_top_high_tower", "enemy_top_high_tower",
    "friend_bottom_high_tower", "enemy_bottom_high_tower",
    "friend_mid_norm_tower_1st", "enemy_mid_norm_tower_1st",
    "friend_top_norm_tower_1st", "enemy_top_norm_tower_1st",
    "friend_bottom_norm_tower_1st", "enemy_bottom_norm_tower_1st",
    "friend_mid_norm_tower_2nd", "enemy_mid_norm_tower_2nd",
    "friend_top_norm_tower_2nd", "enemy_top_norm_tower_2nd",
    "friend_bottom_norm_tower_2nd", "enemy_bottom_norm_tower_2nd",
)


def slot_name(camp: int, kind: str, lane: str = "", tier: int = -1,
              self_camp: int = 1) -> str:
    """(阵营, 类型, 分路, 层级) -> 官方槽位名（friend_/enemy_ 相对 self_camp）。"""
    side = "friend" if camp == self_camp else "enemy"
    if kind == "crystal":
        return f"{side}_crystal"
    if kind == "spring":
        return f"{side}_spring_tower"
    return f"{side}_{lane}_{TIER_NAMES[tier]}"


def table(self_camp: int = 1) -> Dict[str, Pt]:
    """返回 22 个槽位名 -> 归一化点位（按 self_camp 决定 friend/enemy 前缀）。"""
    out: Dict[str, Pt] = {}
    for camp in (1, 2):
        out[slot_name(camp, "crystal", self_camp=self_camp)] = CRYSTAL_PTS[camp]
        out[slot_name(camp, "spring", self_camp=self_camp)] = SPRING_PTS[camp]
        for lane in LANES:
            for i, p in enumerate(LANE_PTS[camp][lane]):
                out[slot_name(camp, "tower", lane, i, self_camp)] = p
    return out


def geo_table(self_camp: int = 1) -> Dict[str, Pt]:
    """返回 22 个槽位名 -> **世界坐标**（引擎单位，可直接与单位坐标比较）。

    与 `table()` 的区别：`table()` 给的是小地图归一化坐标（视觉/标定用），
    `geo_table()` 给的是世界坐标（规则/价值场用）。

    ⚠️ 几何是**静态地图数据**，任何时候都有 22 项；它**不含**血量/存活。
    实测（2026-10-21，真机）：建筑的 `pos` 组件指针为 0（`ACTOR_POS_OFF` 处 `0x0`），
    属性组件的回指校验也不通过 —— 所以在拿到建筑坐标之前，槽位只能提供几何，
    不能提供 `hp_rate/is_alive`。见 docs/AI_UPGRADE_ALIGN_OFFICIAL.md §10.4。
    """
    out: Dict[str, Pt] = {}
    for name, (nx, ny) in table(self_camp).items():
        out[name] = geo.norm_to_world(nx, ny)
    return out


def alive_from_vision(self_camp: int, towers: Optional[dict],
                      tol: float = 0.035) -> Dict[str, bool]:
    """用视觉小地图检测结果给槽位打存活标记。

    `towers` 形如 `{"ally": [(nx,ny)...], "enemy": [...]}`（`mm_rules_v7` 的输出，
    只含**检测为存活**的塔）。返回 {槽位名: 是否存活}，只覆盖有检测输入的阵营；
    没检测到任何塔时不返回该阵营的键（= 未知，而不是"全灭"）。
    """
    out: Dict[str, bool] = {}
    if not towers:
        return out
    tab = table(self_camp)
    groups = []
    if isinstance(towers, dict):
        for key in ("ally", "enemy"):
            pts = towers.get(key) or []
            if pts:
                groups.append(pts)
    elif isinstance(towers, (list, tuple)):
        # 旧录制格式：一个扁平点列（不分阵营），按位置就近归属即可
        groups.append(list(towers))
    for pts in groups:
        for name, sp in tab.items():
            hit = any(geo.dist_norm((float(p[0]), float(p[1])), sp) <= tol
                      for p in pts if p is not None and len(p) >= 2)
            if hit:
                out[name] = True
    # 只对"有检测输入"的阵营补 False（没输入 = 未知，不写 False）
    if isinstance(towers, dict):
        for side, key in (("friend", "ally"), ("enemy", "enemy")):
            pts = towers.get(key) or []
            if not pts:
                continue
            for name in tab:
                if name.startswith(side):
                    out.setdefault(name, False)
    return out


def classify(pt: Pt, camp: int) -> Tuple[str, str, int]:
    """归一化点位 -> (kind, lane, tier)。kind ∈ {crystal, spring, tower}。

    先判基地（角落附近只有水晶/泉水两栋），再对 9 个塔位做**最近邻**。

    为什么不用"分路夹角"：`bottom_high_tower` 与 `mid_high_tower` 的方向余弦只差 0.003
    （高地塔本来就挤在基地区域），夹角判据会在这一带随机翻转。
    9 个塔位彼此最小间距 0.135（归一化），最近邻没有歧义，且塔位是固定拓扑、不随局变。
    """
    corner = geo.CAMP_CORNER[camp]
    d_corner = geo.dist_norm(pt, corner)
    if d_corner <= BASE_RADIUS:
        # 谁更靠角落谁是泉水塔
        d_spr = geo.dist_norm(pt, SPRING_PTS[camp])
        d_cry = geo.dist_norm(pt, CRYSTAL_PTS[camp])
        return ("spring" if d_spr < d_cry else "crystal"), "", -1

    best_key, best_d = (LANES[0], 0), float("inf")
    for lane in LANES:
        for i, p in enumerate(LANE_PTS[camp][lane]):
            d = geo.dist_norm(pt, p)
            if d < best_d:
                best_key, best_d = (lane, i), d
    return "tower", best_key[0], best_key[1]


def slot_of(pt: Pt, camp: int, self_camp: int = 1) -> str:
    kind, lane, tier = classify(pt, camp)
    return slot_name(camp, kind, lane, tier, self_camp)


def _unit_pt(u) -> Optional[Pt]:
    """单位 -> 归一化点位（内存世界坐标优先，视觉归一化坐标兜底）。"""
    if u.x is not None and u.z is not None:
        return geo.world_to_norm(u.x, u.z)
    pt = getattr(u, "coord_norm", None)
    return tuple(pt) if pt else None


# 真机实测（2026-10-21，对局内 24 栋 type=2 建筑）得到的 cid 语义。
# 依据是"到本方泉水角的距离"严格单调：泉水 0 → 水晶 5.3k → 基地塔 20k
# → 高地塔 32k → 二塔 53k → 一塔 70~85k；血量也按层级递增（6000/10000/9000/8500/7500）。
CID_SPRING = {44, 46}
CID_CRYSTAL = {21, 35}
CID_BASE_TOWER = {92, 95}          # 基地塔：不在 22 槽位表内（表里没有这个槽位）
CID_TIER = {
    1690: 0, 1693: 0,              # 一塔
    1691: 1, 1694: 1,              # 二塔
    1696: 2, 1697: 2, 1698: 2,     # 高地塔（蓝）
    1699: 2, 16100: 2, 16101: 2,   # 高地塔（红）
}
LANE_BY_Z = ("bottom", "mid", "top")   # 同层三个实例按 z 升序 -> 下/中/上（两阵营一致）


def assign_measured(units: Sequence, self_camp: int = 1
                    ) -> Tuple[Dict[str, object], List[str]]:
    """按真机实测的 cid 语义分配槽位（不依赖静态表的坐标精度）。

    层级由 cid 决定（见 `CID_TIER`），分路由同层三个实例的 **z 坐标升序**决定
    （下/中/上，两阵营一致，实测 12 栋建筑验证）。
    返回 (槽位名 -> 单位, 说明)；基地塔不在表内，进说明列表而不是硬塞。
    """
    by_group: Dict[tuple, list] = {}
    mapping: Dict[str, object] = {}
    notes: List[str] = []
    for u in units:
        if getattr(u, "kind", "") != "organ":
            continue
        if u.x is None or u.z is None:
            continue
        cid = int(getattr(u, "cid", 0) or 0)
        camp = u.camp if getattr(u, "camp", None) in (1, 2) else None
        if camp is None:
            continue
        if cid in CID_SPRING:
            mapping[slot_name(camp, "spring", self_camp=self_camp)] = u
            continue
        if cid in CID_CRYSTAL:
            mapping[slot_name(camp, "crystal", self_camp=self_camp)] = u
            continue
        if cid in CID_BASE_TOWER:
            notes.append(f"base_tower:cid={cid} camp={camp} 不在 22 槽位表内")
            continue
        tier = CID_TIER.get(cid)
        if tier is None:
            notes.append(f"unknown_cid:{cid} camp={camp}")
            continue
        by_group.setdefault((camp, tier), []).append(u)

    for (camp, tier), rows in by_group.items():
        rows.sort(key=lambda u: u.z)
        if len(rows) != 3:
            notes.append(f"tier{tier} camp={camp} 实例数={len(rows)}（期望 3）")
            lanes = LANE_BY_Z[:len(rows)]
        else:
            lanes = LANE_BY_Z
        for u, lane in zip(rows, lanes):
            mapping[slot_name(camp, "tower", lane, tier, self_camp)] = u
    return mapping, notes


def assign(units: Sequence, self_camp: int = 1,
           cands_only: bool = True) -> Tuple[Dict[str, object], List[str]]:
    """把建筑单位按位置分配到 22 个槽位。

    返回 (槽位名 -> 单位, 冲突说明)。同一槽位出现多个单位时**保留离标定基准更近的那个**，
    其余进冲突列表（用于真机校验，不静默吞掉）。
    """
    mapping: Dict[str, object] = {}
    errors: List[str] = []
    best: Dict[str, Tuple[float, object]] = {}

    for u in units:
        if cands_only and getattr(u, "kind", "") != "organ":
            continue
        pt = _unit_pt(u)
        if pt is None:
            continue
        camp = u.camp if u.camp in (1, 2) else None
        if camp is None:
            continue
        kind, lane, tier = classify(pt, camp)
        name = slot_name(camp, kind, lane, tier, self_camp)
        ref = (CRYSTAL_PTS[camp] if kind != "tower"
               else LANE_PTS[camp][lane][tier])
        d = geo.dist_norm(pt, ref)
        if d > MAX_SLOT_ERR:
            # 离所有已知槽位都太远 -> 不是这 22 栋里的任何一栋（或者标定漂了）。
            # 宁可留空让上层用占位单位，也不要硬塞到某个槽位造成"语义错位"。
            errors.append(f"far:{kind}{lane or ''}{tier if tier >= 0 else ''} "
                          f"cid={getattr(u, 'cid', '?')} d={d:.3f}")
            continue
        if name in best and best[name][0] <= d:
            errors.append(f"conflict:{name} extra_cid={getattr(u, 'cid', '?')} d={d:.3f}")
            continue
        if name in best:
            errors.append(f"conflict:{name} displaced d={best[name][0]:.3f}->{d:.3f}")
        best[name] = (d, u)

    for k, (_, u) in best.items():
        mapping[k] = u
    return mapping, errors


def ordered_units(snap, self_camp: Optional[int] = None) -> List[object]:
    """按官方槽位顺序返回 22 个建筑（缺失槽位用占位单位，`alive=False`）。

    这是"槽位化"的关键：`VEC_ORGAN` 的第 k 个单位必须始终是同一个语义建筑，
    否则特征向量的第 k 段会随帧漂移，模型学不到东西。
    """
    from .snapshot import Unit          # 延迟导入，避免循环
    self_camp = self_camp if self_camp in (1, 2) else (snap.self_camp or 1)
    out: List[object] = []
    for name in SLOT_ORDER:
        u = snap.organ_slots.get(name)
        if u is None:
            out.append(Unit(kind="organ", cid=0, camp=None, x=0.0, z=0.0,
                            hp=0.0, hp_max=1.0, alive=False, slot=name))
        else:
            if getattr(u, "slot", "") != name:
                u.slot = name
            out.append(u)
    return out


def self_check() -> Dict[str, object]:
    """静态自检：22 槽位齐全 / 每槽位可反解回自身 / 层级距离单调 / 两侧旋转对称。

    返回 {ok, n_slots, dup, monotonic, symmetry, max_err}。
    """
    t1 = table(1)
    n = len(t1)
    dup = n - len(set(t1.keys()))
    # 反解：每个标定点 classify 回自身
    bad: List[str] = []
    for name, pt in t1.items():
        kind, lane, tier = classify(pt, 1 if name.startswith("friend") else 2)
        back = slot_name(1 if name.startswith("friend") else 2, kind, lane, tier, 1)
        if back != name:
            bad.append(f"{name}->{back}")
    # 层级单调：同分路到本方角落的距离 1st > 2nd > high
    mono = True
    for camp in (1, 2):
        corner = geo.CAMP_CORNER[camp]
        for lane in LANES:
            d = [geo.dist_norm(p, corner) for p in LANE_PTS[camp][lane]]
            if not (d[0] > d[1] > d[2]):
                mono = False
    # 旋转对称：camp1 的 top_lane 与 camp2 的 bottom_lane 应互为 180° 旋转
    err = 0.0
    for i in range(3):
        a = LANE_PTS[1]["top"][i]
        b = LANE_PTS[2]["bottom"][i]
        err = max(err, geo.dist_norm((1 - a[0], 1 - a[1]), b))
    return {"ok": (n == 22 and dup == 0 and not bad and mono and err < 0.10),
            "n_slots": n, "dup": dup, "roundtrip_bad": bad,
            "monotonic": mono, "symmetry_max_err": round(err, 3)}
