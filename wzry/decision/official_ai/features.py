# -*- coding: utf-8 -*-
"""特征抽取：对齐官方 `feature::Vec*::Process()`。

官方流程（字符串证据）：
    key:%d start_idx %d, feature_size:%d     ← 每个键在向量里的位置
    more than units limitation, pass         ← 超上限直接丢
    local_vec_feature:%d                     ← 本帧拼出来的长度
本模块逐条照做：按距离排序 → 取前 N → 按键序拼定长向量 → 归一化 → 补零到固定长度。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .schema import (NORM_NONE, NORM_ONE_HOT, NORM_RATIO, SPECS, VEC_GLOBAL,
                     VEC_SELF, Feat, VecSpec, dims, layout)
from .snapshot import MAP_DIAG, SKILL_RANGE_FACTOR, Snapshot, Unit

ORGAN_SOLDIER_RADIUS = 700.0     # 判定"塔下有没有兵"的半径（工程近似：塔攻击圈量级）


@dataclass
class FeaturePack:
    """一帧的特征包：扁平向量 + 索引 + 元信息。"""
    vec: Dict[str, List[float]] = field(default_factory=dict)
    index: Dict[str, List[Tuple[str, int, int]]] = field(default_factory=dict)
    n_units: Dict[str, int] = field(default_factory=dict)
    culled: Dict[str, int] = field(default_factory=dict)
    frame: int = 0
    clock_s: float = 0.0
    notes: List[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(len(v) for v in self.vec.values())

    def flat(self) -> List[float]:
        out: List[float] = []
        for k in ("self", "global", "hero", "soldier", "organ", "monster", "cake"):
            out.extend(self.vec.get(k) or [])
        return out

    def get(self, cat: str, name: str, unit_idx: int = 0) -> Optional[float]:
        """按官方键名取值（调试/断言用）。"""
        for n, start, size in self.index.get(cat) or []:
            if n == name:
                base = unit_idx * self._unit_size(cat) + start
                v = self.vec.get(cat) or []
                return v[base] if base < len(v) else None
        return None

    def _unit_size(self, cat: str) -> int:
        s = SPECS.get(cat)
        return s.unit_size if s else 0


def _norm(f: Feat, v: float) -> List[float]:
    """按官方 normType 归一化；one_hot 展开成定长。"""
    if f.norm == NORM_ONE_HOT:
        n = max(1, f.one_hot)
        out = [0.0] * n
        k = int(round(v))
        if 0 <= k < n:
            out[k] = 1.0
        return out
    if f.norm == NORM_RATIO:
        span = (f.hi - f.lo) or 1.0
        x = (float(v) - f.lo) / span
        return [max(0.0, min(1.0, x))]
    return [float(v)]


def _unit_values(u: Unit, snap: Snapshot, cat: str, hero_idx: int = -1) -> Dict[str, float]:
    """把一个单位映射到官方键名 -> 原始值。缺失的键留空（后续补 0）。"""
    d: Dict[str, float] = {}
    d["location_x"] = u.x or 0.0
    d["location_z"] = u.z or 0.0
    d["hp_rate"] = u.hp_rate
    d["max_hp"] = float(u.hp_max or 0)
    d["atk"] = float(u.atk_range)
    d["is_alive"] = 1.0 if u.alive else 0.0
    d["belong_to_main_camp"] = 1.0 if (u.camp is not None and u.camp == snap.self_camp) else 0.0
    d["kill_income"] = float(u.kill_income)
    d["in_main_hero_sight"] = 1.0 if snap.dist(u) <= snap.view_distance else 0.0
    d["in_main_hero_view"] = d["in_main_hero_sight"]
    d["distance_from_each_hero"] = snap.dist(u)
    d["distance_from_main_hero"] = snap.dist(u)
    d["relative_location_x"] = (u.x or 0.0) - (snap.self_x or 0.0)
    d["relative_location_z"] = (u.z or 0.0) - (snap.self_z or 0.0)
    d["dist_2_ckae_map"] = snap.dist(u)
    d["cake_need_scds"] = 0.0
    if cat == "soldier":
        d["is_soldier_alive"] = d["is_alive"]
        d["soldier_type"] = float(max(0, min(7, (u.cid or 1690) - 1690)))
        ang = math.atan2((u.z or 0.0) - (snap.self_z or 0.0),
                         (u.x or 0.0) - (snap.self_x or 0.0))
        d["soldier_sin"] = math.sin(ang)
        d["soldier_cos"] = math.cos(ang)
    elif cat == "organ":
        # `type` 用**槽位语义**而不是 cid：内存里建筑只有 92/95 两种取值，
        # 按 cid 建 one_hot 只会得到两维常量（信息量为 0）。
        # 这里的 1/2/3 是"塔/水晶/泉水"的工程编号（推断，非官方常量）。
        slot = getattr(u, "slot", "") or ""
        d["type"] = 2.0 if slot.endswith("_crystal") else (
            3.0 if slot.endswith("_spring_tower") else (1.0 if u.alive else 0.0))
        d["attack_range"] = float(u.atk_range)
        d["conti_attack"] = 0.0
        d["attack_target"] = 0.0
        d["whether_under_each_hero_atk"] = 0.0
        d["whether_organ_atk_each_hero"] = 0.0
        d["organ_invincible_state"] = 0.0
        d["organ_invincible_state_time"] = 0.0
        d["organ_incompetent_state"] = 0.0
        d["organ_incompetent_state_time"] = 0.0
        # 塔下兵线：用本单位附近的我方/敌方兵数算出（原来恒 0，等于没给模型信息）
        near = [m for m in snap.minions
                if m.alive and u.x is not None and m.x is not None
                and math.hypot(m.x - u.x, m.z - u.z) <= ORGAN_SOLDIER_RADIUS]
        d["nearby_emy_soldier_num"] = float(sum(
            1 for m in near if m.camp is not None and m.camp != snap.self_camp))
        d["organ_emy_soldier_cnt"] = d["nearby_emy_soldier_num"]
        d["whether_under_frd_hero_atk"] = 0.0
        d["whether_under_emy_hero_atk"] = 0.0
        mine = [m for m in near if m.camp == snap.self_camp]
        d["organ_emy_soldier_min_dist"] = (
            min(math.hypot(m.x - u.x, m.z - u.z) for m in mine) if mine else MAP_DIAG)
        d["is_emy_soldier_around_organ"] = 1.0 if d["nearby_emy_soldier_num"] > 0 else 0.0
        d["is_organ_attack_emy_soldier"] = 0.0
        d["is_organ_attack_main_hero"] = 0.0
        d["is_organ_now_outside"] = 0.0
    elif cat == "monster":
        d["monster_type"] = float(max(0, min(7, (u.cid or 16100) - 16100)))
        d["monster_buff_marks"] = 0.0
    elif cat == "cake":
        d["cake_type"] = 0.0
        d["is_visiable"] = 1.0
    elif cat == "hero":
        # 相对主英雄的几何量（官方对 10 个英雄逐个算）
        d["dist_from_each_hero"] = snap.dist(u)
        d["relative_location_x"] = (u.x or 0.0) - (snap.self_x or 0.0)
        d["relative_location_z"] = (u.z or 0.0) - (snap.self_z or 0.0)
        d["x_diff"] = d["relative_location_x"]
        d["z_diff"] = d["relative_location_z"]
        d["atk_range"] = float(u.atk_range)
        d["is_in_hero_atk_range"] = 1.0 if snap.dist(u) <= u.atk_range else 0.0
        d["under_atk_by_each_hero"] = 1.0 if u.under_atk else 0.0
        d["whether_atk_each_main_camp_hero"] = 1.0 if u.atk_me else 0.0
        d["is_atk_main_hero"] = 1.0 if u.atk_me else 0.0
        d["buff_marks"] = 0.0
        d["has_spec_buffs"] = 0.0
        d["phy_def"] = 0.0
        # 斩杀线：对方剩余血量 < 我方一次普攻量级（工程近似）
        d["is_kill_by_hero_norm_atk"] = 1.0 if (u.hp or 0) <= 400 else 0.0
        d["past_1s_attack_soldier"] = 0.0
        d["past_1s_attack_soldier_hprate"] = 0.0
        d["past_1s_attack_soldier_lowhp"] = 0.0
    return d


def _pack_category(spec: VecSpec, units: Sequence[Unit], snap: Snapshot) -> Tuple[List[float], Dict[str, int]]:
    """按距离排序取前 N，拼定长向量。"""
    key = spec.key.split("_")[1]                 # vec_soldier_config -> soldier
    total = len(units)
    if spec.sorted_by_dist:
        units = sorted(units, key=lambda u: snap.dist(u))[: spec.max_units]
    else:
        units = list(units)[: spec.max_units]
    vec: List[float] = []
    for u in units:
        vals = _unit_values(u, snap, key)
        for f in spec.feats:
            raw = vals.get(f.name)
            vec.extend(_norm(f, 0.0 if raw is None else raw))
    pad = spec.total_size - len(vec)
    if pad > 0:
        vec.extend([0.0] * pad)
    return vec, {"n": len(units), "culled": max(0, total - spec.max_units)}


def _cd_of(u, role: str) -> float:
    """按角色取某类技能的剩余 CD（秒）：summoner / heal。

    内存里技能区是 7 槽（`docs/wzry-ai-data-inputs.md` §11）：
    slot5=治疗(90003)、slot6=召唤师(80115)、slot7=回城(90005)。
    """
    for s in (u.skills or []):
        r = str(s.get("role") or "")
        sid = int(s.get("id") or 0)
        if r == role or (role == "summoner" and 80000 <= sid < 90000) \
                or (role == "heal" and sid == 90003):
            return float(s.get("cd_left_ms") or 0) / 1000.0
    return 0.0


def _self_vec(snap: Snapshot) -> List[float]:
    me = snap.self_unit
    d: Dict[str, float] = {}
    if me is not None:
        sk = {int(s.get("slot") or 0): s for s in (me.skills or [])}
        # 法力：内存侧无该字段（见 docs 待办 P0.2），改用 HUD 蓝条比例。
        # 官方也是"当前值 / 该英雄上限"（`hero_ep_type_list` + `ep_max_value_dict`），
        # 归一化后与 `ep_rate` 同值，故 ep 直接取比例、ep_rate 同口径。
        mp = snap.self_ep_rate
        if mp is None and me.ep_rate is not None:
            mp = me.ep_rate
        mp = 0.0 if mp is None else float(mp)
        d.update({
            "hp": float(me.hp or 0),
            "hp_rate": me.hp_rate,
            "ep": mp, "ep_rate": mp,
            "atk_range": float(me.atk_range),
            "level": float(me.level or 0),
            "gold": 0.0,
            "summoner_cd": _cd_of(me, "summoner"),
            "heal_cd": _cd_of(me, "heal"),
            "skill1_cd": float((sk.get(1) or {}).get("cd_left_ms") or 0) / 1000.0,
            "skill2_cd": float((sk.get(2) or {}).get("cd_left_ms") or 0) / 1000.0,
            "skill3_cd": float((sk.get(3) or {}).get("cd_left_ms") or 0) / 1000.0,
            "skill1_ready": 1.0 if (sk.get(1) or {}).get("ready") or
            ((sk.get(1) or {}).get("cd_left_ms") == 0) else 0.0,
            "skill2_ready": 1.0 if (sk.get(2) or {}).get("ready") or
            ((sk.get(2) or {}).get("cd_left_ms") == 0) else 0.0,
            "skill3_ready": 1.0 if (sk.get(3) or {}).get("ready") or
            ((sk.get(3) or {}).get("cd_left_ms") == 0) else 0.0,
            "move_degree": 0.0, "mov_spd": 0.0, "forward_degree": 0.0,
        })
    vec: List[float] = []
    for f in VEC_SELF:
        vec.extend(_norm(f, d.get(f.name, 0.0)))
    return vec


def _global_vec(snap: Snapshot, notes: List[str]) -> List[float]:
    from .. import official_rules as _orl
    dmin = snap.enemy_hero_min_dist()
    danger = 1.0 if dmin < _orl.DANGER_DIST else 0.0
    if danger:
        notes.append("m_in_dangerous_place")
    d = {
        "grid_tactics_state": float(snap.grid_state),
        "arrived_strategy_target": 1.0 if snap.arrived_target else 0.0,
        "enemy_hero_min_dis": dmin,
        "m_in_dangerous_place": danger,
        "m_ally_crystal_is_atked": 1.0 if snap.ally_crystal_atked else 0.0,
        "clock_s": snap.clock_s,
        "ally_gold": snap.ally_gold,
        "enemy_gold": snap.enemy_gold,
        "kill_diff": snap.kill_diff,
    }
    vec: List[float] = []
    for f in VEC_GLOBAL:
        vec.extend(_norm(f, d.get(f.name, 0.0)))
    return vec


def build(snap: Snapshot, interval_ok: bool = True) -> FeaturePack:
    """构建一帧特征包。

    `interval_ok=False` 时只做最小集（对齐官方 `feature_extract_interval` 的抽样节拍：
    非节拍点不重算，由调用方复用上一帧）。
    """
    pack = FeaturePack(frame=snap.frame, clock_s=snap.clock_s)
    if not interval_ok:
        pack.notes.append("skip:not_interval")
        return pack

    pack.vec["self"] = _self_vec(snap)
    pack.vec["global"] = _global_vec(snap, pack.notes)
    pack.index["self"] = layout(VecSpec("self", "Self", VEC_SELF, 1))
    pack.index["global"] = layout(VecSpec("global", "Global", VEC_GLOBAL, 1))

    # ⚠️ **没有坐标的单位一律不进特征**。`_unit_values` 里 `u.x or 0.0` 会把缺坐标的兵
    #   写成世界原点（地图正中），16 个小兵就全叠在 (0,0) 上——这不是"数据缺失"，
    #   而是**伪造了一个强特征**（"地图中心有 16 个满血兵"）。真机核对过：兵/野/塔
    #   的 pos 组件指针为 0，读不到坐标但血量是真的（7500/8500/9000 为不同兵种，
    #   且存在 7236/7500 的战损值）。所以这里按类别丢弃并记数，供审计。
    located = {cat: [u for u in pool if u.x is not None and u.z is not None]
               for cat, pool in (("soldier", snap.minions),
                                 ("monster", snap.monsters),
                                 ("cake", snap.cakes))}
    for cat, pool in (("soldier", snap.minions), ("monster", snap.monsters),
                      ("cake", snap.cakes)):
        lost = len(pool) - len(located[cat])
        if lost:
            snap.unlocated[cat] = lost
            pack.notes.append(f"unlocated:{cat}={lost}")
    groups = {"soldier": [m for m in located["soldier"] if snap.dist(m) <= snap.view_distance],
              # 建筑走**槽位序**：第 k 段永远对应同一个语义建筑（官方按名索引，不遍历）
              "organ": _organ_units(snap),
              "monster": [m for m in located["monster"] if snap.dist(m) <= snap.view_distance],
              "cake": [c for c in located["cake"] if snap.dist(c) <= snap.view_distance],
              # 官方 all_10_hero_feature 含自己（另有 main_hero_feature 单独一段）；
              #   英雄名单不按视野裁剪 —— 官方也是全局 10 人（带 god_vision 差异）
              "hero": list(snap.heroes)}
    for cat, units in groups.items():
        spec = SPECS[cat]
        if cat == "organ":
            vec, meta = _pack_slots(spec, units, snap)
        else:
            vec, meta = _pack_category(spec, units, snap)
        pack.vec[cat] = vec
        pack.index[cat] = layout(spec)
        pack.n_units[cat] = meta["n"]
        pack.culled[cat] = meta["culled"]
        if meta["culled"]:
            pack.notes.append(f"more than units limitation: {cat}")
    if snap.organ_slots:
        pack.notes.append(f"organ_slots:{len(snap.organ_slots)}/22")
    return pack


def _organ_units(snap: Snapshot) -> List[Unit]:
    """按 22 槽位顺序取建筑。

    ⚠️ 没有活体槽位时**返回空**，不退回 `snap.organs`：那些单位没有坐标
    （建筑 pos 组件为 0，见 docs §10.4），`_unit_values` 会把它们写成世界原点，
    于是 6 座建筑全叠在 (0,0) 上、还带着看起来是真的血量（6000/9000/10000）。
    宁可留空：位置信息由 `snap.organ_geo`（22 项静态拓扑）单独供规则与价值场使用，
    两者职责分开，不靠伪造特征来"补全"。
    """
    if not snap.organ_slots:
        return []
    from . import organ_slots as _osl
    return list(_osl.ordered_units(snap))


def _pack_slots(spec: VecSpec, units: Sequence[Unit], snap: Snapshot):
    """槽位序打包：不排序、不截断（顺序即语义，长度固定为 max_units）。"""
    key = spec.key.split("_")[1]
    vec: List[float] = []
    n = 0
    for u in units[: spec.max_units]:
        vals = _unit_values(u, snap, key)
        for f in spec.feats:
            raw = vals.get(f.name)
            vec.extend(_norm(f, 0.0 if raw is None else raw))
        n += 1
    pad = spec.total_size - len(vec)
    if pad > 0:
        vec.extend([0.0] * pad)
    return vec, {"n": n, "culled": max(0, len(units) - spec.max_units)}


def skill_range(u: Unit) -> float:
    return float(u.atk_range or 0.0) * SKILL_RANGE_FACTOR


def summary(pack: FeaturePack) -> str:
    """一行摘要（对齐官方 `vec_feature_len:%d` 的日志习惯）。"""
    parts = [f"{k}={len(v)}" for k, v in pack.vec.items() if v]
    return (f"frame={pack.frame} clock={pack.clock_s:.0f}s len={pack.size} "
            f"[{' '.join(parts)}] units={pack.n_units}")
