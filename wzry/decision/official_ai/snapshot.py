# -*- coding: utf-8 -*-
"""对局快照：把 state_dict（内存优先）整理成官方 AI 吃的那种"按类别分组"的单位集合。

官方在引擎里直接持有 `SGCore::BattleLogicUnit` 指针；我们只有 `state_dict`，
所以这一层做的是**形状转换**：世界坐标 + 分类 + 相对主英雄的距离，全部算好，
让上层特征抽取只做"取值 + 归一化"，与官方 `feature::Vec*::Process()` 的职责一致。

坐标口径：一律用**世界坐标**（与官方 301 危险半径同一单位）。
视觉的小地图归一化坐标只作为兜底（`x/z` 缺失时按 `geo.norm_to_world` 反算），并打 `coord_src` 标记。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import geo

# 世界坐标范围：统一从 geo 取，避免三处各写一份（z 轴方向错一次就是整局镜像）
WORLD_X = (geo.MAP_X_MIN, geo.MAP_X_MAX)
WORLD_Z = (geo.MAP_Z_MIN, geo.MAP_Z_MAX)
MAP_DIAG = math.hypot(WORLD_X[1] - WORLD_X[0], WORLD_Z[1] - WORLD_Z[0])

# 建筑/单位的 cid 归类（与 mem_battle.CID_* 保持一致，避免两处漂移）
CID_MINION = range(1690, 1700)
CID_TURRET = (92, 95)
CID_BASE = (21, 35, 44, 46)
CID_MONSTER = (16100, 16101)

DEFAULT_ATK_RANGE = 300.0        # 近战近似；远程英雄由调用方覆盖
SKILL_RANGE_FACTOR = 2.2         # 技能射程 ≈ 普攻射程 × 2.2（工程近似，可覆盖）


@dataclass
class Unit:
    """统一单位表示（英雄/兵/建筑/野怪/血包都用它，多余字段留 None）。"""
    kind: str                     # hero / minion / organ / monster / cake
    cid: int
    camp: Optional[int]
    x: Optional[float]
    z: Optional[float]
    hp: Optional[float] = None
    hp_max: Optional[float] = None
    alive: bool = True
    level: int = 0
    atk_range: float = DEFAULT_ATK_RANGE
    # 英雄专属
    skills: List[dict] = field(default_factory=list)
    under_atk: bool = False
    atk_me: bool = False
    in_atk_range: bool = False
    kill_income: float = 0.0
    coord_src: str = "mem"        # mem / screen / none
    respawn_s: Optional[float] = None   # 复活倒计时（秒，内存 0x15D4）
    data_src: str = "mem"         # mem / mixed / vision / none（**取数来源审计**）        # mem / screen
    # 法力：官方 `HeroBaseInfo.ep` 的同位字段。
    #   **本地英雄**已打通（`self_state.mp` ← EP_CUR/EP_MAX，0x1C4/0x1C8）；
    #   敌方英雄没有逐英雄法力（actor 记录不含 EP），只有 HUD 蓝条比例时按
    #   `ep=ratio, ep_max=1.0` 落位，绝不伪造成绝对值。
    ep: Optional[float] = None
    ep_max: Optional[float] = None
    # 已购买装备（config_id 列表；含合成小件）。ID→名称需自建映射。
    equip: List[int] = field(default_factory=list)
    # 建筑专属：分配到的具名槽位（organ_slots.assign 写入）
    slot: str = ""

    @property
    def hp_rate(self) -> float:
        if not self.hp_max:
            return 0.0
        return max(0.0, min(1.0, float(self.hp or 0) / float(self.hp_max)))

    @property
    def ep_rate(self) -> Optional[float]:
        """法力比例；无法力数据时返回 None（上层按 0 处理，不伪造）。"""
        if self.ep is None or not self.ep_max:
            return None
        return max(0.0, min(1.0, float(self.ep) / float(self.ep_max)))


@dataclass
class Snapshot:
    frame: int = 0
    clock_s: float = 0.0
    self_cid: int = 0
    self_camp: Optional[int] = None
    self_x: Optional[float] = None
    self_z: Optional[float] = None
    self_unit: Optional[Unit] = None
    # 自己的定位来源：cid / cid+pos / ambiguous / missing / fallback_first
    self_match: str = ""
    # 自身阵营来源：memory / inferred_base / unknown
    self_camp_src: str = ""
    heroes: List[Unit] = field(default_factory=list)     # 含自己，共 10
    minions: List[Unit] = field(default_factory=list)
    organs: List[Unit] = field(default_factory=list)
    monsters: List[Unit] = field(default_factory=list)
    cakes: List[Unit] = field(default_factory=list)
    organ_slots: Dict[str, Unit] = field(default_factory=dict)   # 22 具名槽位（有活体数据时才有）
    # 22 槽位的**静态几何**（世界坐标）。地图固定 → 恒有 22 项。
    #   与 organ_slots 分开：几何永远可用；血量/存活要靠活体数据或视觉。
    organ_geo: Dict[str, tuple] = field(default_factory=dict)
    # 视觉给出的存活标记（mm_rules_v7 的 towers 输出）；无检测时为空 = 未知
    organ_alive: Dict[str, bool] = field(default_factory=dict)
    # 存活标记的来源（"vision" / ""）。内存侧暂不可读，见 docs §10.4
    organ_alive_src: str = ""
    # 读得到单位、读不到坐标的条数（按类别）。内存侧 pos 组件缺失，见 docs §10.4
    unlocated: Dict[str, int] = field(default_factory=dict)
    # 自己的装备（与 Unit.equip 同源，方便直接取）
    self_equip: List[int] = field(default_factory=list)
    # 已购装备里是否有复活甲（贤者的庇护 1337）；ID 表见 configs/equip_ids.json。
    # 只表示"买过"，不含 150s CD / 每局 2 次的可用性。
    revive_armor_usable: bool = False
    ally_gold: float = 0.0
    enemy_gold: float = 0.0
    kill_diff: float = 0.0
    grid_state: int = 0
    arrived_target: bool = False
    ally_crystal_atked: bool = False
    # 自身法力比例：内存 `+0x1C4/+0x1C8`（2026-10-21 打通）优先，HUD 蓝条兜底
    self_ep_rate: Optional[float] = None
    # 可见半径（官方 view_distance；由 policy 按难度档写入）
    view_distance: float = 1200.0
    # 连续"下了指令但没位移"的帧数（卡墙/卡门判据），由 policy 写入
    stuck_frames: int = 0
    # 最近 N 步动作标签（官方 add_last_n_yaction_feature_size 的同构物）
    last_actions: List[str] = field(default_factory=list)
    # 槽位分配诊断（冲突/未分配），供真机校验与回放
    organ_notes: List[str] = field(default_factory=list)
    coord_src: str = "mem"

    # ---- 便捷派生量（官方也都在 ctx 里缓存这些）----
    def dist(self, u: Unit) -> float:
        if None in (self.self_x, self.self_z, u.x, u.z):
            return MAP_DIAG
        return math.hypot(u.x - self.self_x, u.z - self.self_z)

    def enemies(self) -> List[Unit]:
        return [h for h in self.heroes if h.camp is not None and h.camp != self.self_camp]

    def allies(self) -> List[Unit]:
        return [h for h in self.heroes
                if h.camp is not None and h.camp == self.self_camp and h.cid != self.self_cid]

    def enemy_hero_min_dist(self) -> float:
        d = [self.dist(h) for h in self.enemies() if h.alive]
        return min(d) if d else MAP_DIAG

    def ally_hero_min_dist(self) -> float:
        d = [self.dist(h) for h in self.allies() if h.alive]
        return min(d) if d else MAP_DIAG

    def own_spring_pt(self):
        """己方泉水坐标：活体槽位 > 静态几何 > None。"""
        u = (self.organ_slots or {}).get("friend_spring_tower")
        if u is not None and u.x is not None:
            return (u.x, u.z)
        g = (self.organ_geo or {}).get("friend_spring_tower")
        return (g[0], g[1]) if g else None

    def retreat_point(self, step: float = 3500.0):
        """撤退点：朝己方泉水方向走 `step`（并夹在地图内）。

        之前"撤退"动作的点就是自身坐标，等于原地不动 —— 这个 helper 统一给
        `enumerate_moves(retreat=True)` / `rules._retreat` / `[UnpredictProcess]` 用。
        """
        if self.self_x is None or self.self_z is None:
            return None
        spr = self.own_spring_pt()
        if spr is None:
            sign = -1.0 if (self.self_camp or 1) == 1 else 1.0
            spr = (self.self_x + sign * 10000.0, self.self_z + sign * 10000.0)
        dx, dz = spr[0] - self.self_x, spr[1] - self.self_z
        n = math.hypot(dx, dz) or 1.0
        k = min(1.0, step / n)
        x = self.self_x + dx * k
        z = self.self_z + dz * k
        return (max(WORLD_X[0], min(WORLD_X[1], x)),
                max(WORLD_Z[0], min(WORLD_Z[1], z)))


def _screen_to_world(scr) -> tuple:
    """小地图归一化 (0..1) -> 世界坐标。仅在没有内存坐标时使用。

    ⚠️ 必须走 `geo.norm_to_world`：z 轴是反序的（小地图 ny 向下、世界 z 向上）。
    旧实现用 `Z_MIN + ny*H`，会把"小地图下方"算成"世界坐标上方"，
    视觉兜底单位因此整体镜像到对角半场。
    """
    try:
        nx, ny = float(scr[0]), float(scr[1])
    except Exception:
        return None, None
    return geo.norm_to_world(nx, ny)


def infer_camp(x: Optional[float], z: Optional[float] = None) -> Optional[int]:
    """阵营兜底推断：只在"站在某方泉水附近"时由位置判阵营。

    不再用 x 符号推断：英雄可以越界到对面半场，中期会把入侵者判反。
    仅在内存没给 camp 时使用（实测结算/重连后 actor 表可读但 camp 可能为空）。
    """
    if x is None or z is None:
        return None
    from wzry.vision.camp_identity import camp_id, infer_camp_from_base
    return camp_id(infer_camp_from_base((x, z)))


def _kind_of(cid: int, raw_kind: str = "", actor_type: int = 1) -> str:
    if raw_kind in ("hero", "minion", "organ", "monster", "cake"):
        return raw_kind
    if raw_kind == "turret" or raw_kind == "building":
        return "organ"
    if actor_type == 0:
        return "hero"
    if cid in CID_MINION:
        return "minion"
    if cid in CID_MONSTER:
        return "monster"
    if cid in CID_TURRET or cid in CID_BASE:
        return "organ"
    return "other"


def _camp_id(v) -> Optional[int]:
    """阵营统一成 1/2（1=蓝, 2=红）。

    ⚠️ 内存侧 `MemUnit.camp` 是字符串 **"blue"/"red"**，视觉兜底侧是 1/2 —— 两套混用会让
    `organ_slots.assign`（要求 int 1/2）静默跳过、`geo_table(self_camp)` 生成 11 项而不是 22 项。
    统一在入口处转一次，内部一律用 int。
    """
    if isinstance(v, str):
        s = v.strip().lower()
        return {"blue": 1, "red": 2, "1": 1, "2": 2}.get(s)
    if isinstance(v, (int, float)) and int(v) in (1, 2):
        return int(v)
    return None


def from_state_dict(state: dict, self_id: Optional[int] = None,
                    atk_range: float = DEFAULT_ATK_RANGE,
                    organ_slots: Optional[Dict[str, int]] = None,
                    require_mem: bool = False) -> Snapshot:
    """把 m2 的 state_dict 转成 Snapshot。

    **取数优先级：内存 > 视觉**（`require_mem=True` 时彻底禁用视觉兜底）。
      - 内存来源：`extra.mem_heroes` / `extra.mem_units`（世界坐标、精确血量、技能 CD、法力）
      - 视觉来源：`state["units"]`（只有小地图归一化坐标，精度差一个量级）
    结果记在 `Snapshot.data_src`：mem / mixed / vision / none，便于审计"到底靠没靠视觉"。
    """
    ui = state.get("ui") or {}
    extra = state.get("extra") or {}
    snap = Snapshot()

    snap.frame = int(ui.get("frame") or extra.get("frame") or 0)
    clk = ui.get("match_clock", extra.get("mem_clock_s"))
    snap.clock_s = float(clk or 0)
    snap.grid_state = int(ui.get("grid_state") or 0)
    snap.arrived_target = bool(ui.get("arrived_strategy_target"))
    snap.ally_crystal_atked = bool(ui.get("ally_crystal_atked"))

    # ---- 英雄（内存全量 10 人，含世界坐标与技能）----
    mem_heroes = extra.get("mem_heroes") or []
    mem_units = extra.get("mem_units") or []
    if require_mem and not mem_heroes:
        # 严格内存模式：没有内存数据就直接交空快照，不拿视觉凑
        snap.data_src = "none"
        snap.coord_src = "none"
        return snap
    if self_id is None:
        self_id = ui.get("hero_id") or (mem_heroes[0]["id"] if mem_heroes else 0)

    for h in mem_heroes:
        u = Unit(kind="hero", cid=int(h.get("id") or 0), camp=_camp_id(h.get("camp")),
                 x=h.get("x"), z=h.get("z"), hp=h.get("hp"), hp_max=h.get("hp_max"),
                 alive=(h.get("hp") or 0) > 0, level=int(h.get("level") or 0),
                 atk_range=float(h.get("atk_range") or atk_range),
                 skills=list(h.get("skills") or []),
                 ep=h.get("ep", h.get("mp")), ep_max=h.get("ep_max", h.get("mp_max")),
                 equip=list(h.get("equip_ids") or []),
                 respawn_s=h.get("respawn_s"))
        if u.x is None:
            u.coord_src = "none"
        snap.heroes.append(u)

    used_vision = False
    # 没有内存英雄时退回视觉单位表
    if not snap.heroes:
        used_vision = True
        for u0 in state.get("units") or []:
            cls = u0.get("cls") or ""
            if cls not in ("self", "enemy_hero", "ally_hero"):
                continue
            x, z = _screen_to_world(u0.get("screen"))
            cid = int(u0.get("hero_id") or u0.get("cid") or 0)
            camp = 1 if cls in ("self", "ally_hero") else 2
            snap.heroes.append(Unit(kind="hero", cid=cid, camp=camp, x=x, z=z,
                                    hp=u0.get("hp"), hp_max=u0.get("hp_max"),
                                    atk_range=atk_range, coord_src="screen"))
        snap.coord_src = "screen"

    # camp 缺失时按"是否在泉水附近"推断（不用 x 符号：越界英雄会被判反）。
    # 判不出的保持 None，宁可算 unknown，也不冒充盟友/敌人。
    had_camp = {id(h): h.camp is not None for h in snap.heroes}
    if any(h.camp is None for h in snap.heroes):
        for h in snap.heroes:
            if h.camp is None:
                h.camp = infer_camp(h.x, h.z)

    # 自己：cid 唯一时直接命中；同 cid 多实例用 self 状态对象的坐标消歧。
    # 不假设 heroes[0] 就是自己（数组顺序与本地玩家无关）。
    from wzry.vision.camp_identity import pick_self
    ss = state.get("self_state") or extra.get("self_state") or {}
    self_pos = None
    if isinstance(ss, dict) and ss.get("x") is not None and ss.get("z") is not None:
        self_pos = (float(ss["x"]), float(ss["z"]))
    me, self_match = None, "missing"
    if self_id:
        me, self_match, _n = pick_self(snap.heroes, self_id, self_pos)
    if me is None and self_match == "missing" and snap.heroes:
        me, self_match = snap.heroes[0], "fallback_first"
    snap.self_unit = me
    snap.self_match = self_match
    if me is not None:
        snap.self_cid, snap.self_camp = me.cid, me.camp
        snap.self_x, snap.self_z = me.x, me.z
        snap.coord_src = me.coord_src
        snap.self_equip = list(me.equip or [])
        if snap.self_equip:
            from . import equips as _eq
            snap.revive_armor_usable = _eq.is_revive_armor(snap.self_equip)
        snap.self_camp_src = ("memory" if had_camp.get(id(me))
                             else ("inferred_base" if me.camp else "unknown"))
    else:
        snap.self_camp_src = "unknown"

    # ---- 法力：优先内存（`self_state.mp` ← EP_CUR/EP_MAX），再退 HUD 蓝条比例 ----
    #   逐英雄法力未打通（actor 记录里没有 EP），只有**本地英雄**能从 self 对象读；
    #   因此这里先给 self 补绝对法力，`ep` 语义才与官方 `HeroBaseInfo.ep` 对齐。
    if me is not None and me.ep is None and isinstance(ss, dict):
        _mp, _mp_max = ss.get("mp"), ss.get("mp_max")
        if (isinstance(_mp, (int, float)) and isinstance(_mp_max, (int, float))
                and 0 < float(_mp_max) <= 50_000 and 0 <= float(_mp) <= float(_mp_max)):
            me.ep, me.ep_max = float(_mp), float(_mp_max)
    ep_rate = me.ep_rate if me is not None else None
    if ep_rate is None:
        try:
            if ui.get("mp") is not None:
                ep_rate = max(0.0, min(1.0, float(ui["mp"])))
        except Exception:
            ep_rate = None
    snap.self_ep_rate = ep_rate
    if me is not None and ep_rate is not None and me.ep is None:
        # 只有比例时按 `ep=ratio, ep_max=1` 落位，上层统一走 `ep_rate`
        me.ep, me.ep_max = ep_rate, 1.0

    # ---- 非英雄单位（内存 actor 表；没有就只用视觉点）----
    for u0 in mem_units:
        kind = _kind_of(int(u0.get("cid") or 0), str(u0.get("kind") or ""),
                        int(u0.get("actor_type") or 1))
        if kind == "hero":
            continue
        u = Unit(kind=kind, cid=int(u0.get("cid") or 0), camp=_camp_id(u0.get("camp")),
                 x=u0.get("x"), z=u0.get("z"), hp=u0.get("hp"),
                 hp_max=u0.get("hp_max"), alive=not bool(u0.get("dead")))
        if kind == "minion":
            snap.minions.append(u)
        elif kind == "organ":
            snap.organs.append(u)
        elif kind == "monster":
            snap.monsters.append(u)

    # 视觉兜底：小兵/塔只有屏幕点
    if (not require_mem) and (not snap.minions or not snap.organs):
        used_vision = True
        for u0 in state.get("units") or []:
            cls = u0.get("cls") or ""
            x, z = _screen_to_world(u0.get("screen"))
            if cls == "enemy_minion" and not snap.minions:
                snap.minions.append(Unit("minion", 0, 2, x, z,
                                         u0.get("hp"), u0.get("hp_max"),
                                         coord_src="screen"))
            elif cls == "enemy_turret" and not snap.organs:
                snap.organs.append(Unit("organ", 0, 2, x, z,
                                        u0.get("hp"), u0.get("hp_max"),
                                        coord_src="screen"))

    # ---- 22 具名槽位 ----
    #   两条路径：
    #   a) 显式 `organ_slots`（旧接口：槽位名 -> cid）—— 仅当外部已标定 cid 时使用；
    #   b) 默认走**位置拓扑**（organ_slots.assign）：不依赖 cid（内存里建筑只有 92/95 两种），
    #      按"分路夹角 + 层级最近点"落到固定槽位。
    if organ_slots:
        by_cid: Dict[int, List[Unit]] = {}
        for o in snap.organs:
            by_cid.setdefault(o.cid, []).append(o)
        for name, cid in organ_slots.items():
            cand = by_cid.get(int(cid)) or []
            if cand:
                snap.organ_slots[name] = cand.pop(0)
    elif snap.organs:
        from . import organ_slots as _osl
        # 真机优先：cid 定层级 + z 序定分路（精确，不依赖静态表坐标精度）；
        # 失败才退回静态表几何匹配。
        mapping, notes = _osl.assign_measured(snap.organs, snap.self_camp or 1)
        if len(mapping) < 20:
            mapping, notes = _osl.assign(snap.organs, snap.self_camp or 1)
        snap.organ_slots = mapping          # type: ignore[assignment]
        snap.organ_notes = notes
        # 槽位坐标一律用**实测**位置覆盖静态几何（静态表偏差可达 18k）
        for name, u in mapping.items():
            if getattr(u, "x", None) is not None:
                snap.organ_geo[name] = (float(u.x), float(u.z))

    # ---- 静态几何 + 视觉存活（永远可用的那一层）----
    from . import organ_slots as _osl
    camp0 = snap.self_camp or 1
    snap.organ_geo = _osl.geo_table(camp0)
    minimap = state.get("minimap") or {}
    # ⚠️ 取数纪律：内存优先。塔的存活**内存侧读不到**（建筑 pos 组件为空，见 §10.4），
    #   所以这里只能用视觉；但 `require_mem=True`（严格不碰视觉）时必须放弃，留"未知"。
    if (not require_mem) and minimap.get("found") and (minimap.get("towers") or {}):
        alive = _osl.alive_from_vision(camp0, minimap.get("towers"))
        if alive:
            snap.organ_alive = alive
            snap.organ_alive_src = "vision"
            used_vision = True          # 用到了视觉数据 -> data_src 必须如实标 mixed
    if not snap.organ_slots:
        snap.organ_notes.append("organ_slots:empty(no_live_pos)")
    else:
        # 活体槽位没用到的几何项标注出来，便于复核
        for name in snap.organ_geo:
            if name not in snap.organ_slots and name in snap.organ_alive:
                snap.organ_notes.append(f"geo_only:{name}")

    # ---- 经济/击杀差 ----
    if mem_heroes and snap.self_camp is not None:
        ally = [h for h in mem_heroes if h.get("camp") == snap.self_camp]
        foe = [h for h in mem_heroes if h.get("camp") not in (None, snap.self_camp)]
        snap.ally_gold = float(sum(h.get("gold_total") or h.get("gold") or 0 for h in ally))
        snap.enemy_gold = float(sum(h.get("gold_total") or h.get("gold") or 0 for h in foe))
        snap.kill_diff = float(sum(h.get("kills") or 0 for h in ally)
                               - sum(h.get("kills") or 0 for h in foe))

    # ---- 取数来源审计（"到底靠没靠视觉"）----
    if not mem_heroes and not mem_units:
        snap.data_src = "none" if require_mem else "vision"
    elif used_vision:
        snap.data_src = "mixed"
    else:
        snap.data_src = "mem"
    return snap
