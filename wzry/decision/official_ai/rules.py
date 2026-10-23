# -*- coding: utf-8 -*-
"""规则覆盖层：对齐官方 `RuleTactics` 的「模型之后否决/改写」。

官方顺序（`docs/wzry-ai-reverse.md` §8.5 从调度器 0x27d0e14–0x27d1310 还原的槽位序列）
是**固定顺序、命中即短路**，每条规则返回 1 位结果写回 ctx（`+0x1040/+0x1044/+0x1700/+0x1780`）。
本模块保持同一语义：按顺序跑，第一条给出 VETO/REWRITE 的规则生效，其余跳过。

**证据分级**（`Rule.evidence`）——不许含糊：
  - `disasm`：函数地址 + 常量/分支都来自反汇编（`wzry/decision/official_rules.py`），共 4 条；
  - `name`  ：只有规则名（`.rela.dyn` 的 141 条具名表）或只有日志串地址，**常量是工程值**，共 13 条。
             （`FrdFzDonotTakeBloodPack` 属于这一类：日志串 `[ProcessRule] FrdFzDonotTakeBloodPack`
              能定位到 0x027d9030，但"队友更近"的判定阈值没从汇编里拿到。）
默认 `mode="all"`（全跑）；做 A/B 或怀疑误伤时用 `mode="disasm"` 只跑有汇编证据的那几条。
每条命中都带 evidence 与原因，便于回放对账。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from .. import official_rules as orl
from .actions import Action, T_CRYSTAL, T_MONSTER, T_SOLDIER, T_TOWER
from .snapshot import Snapshot

PASS = "PASS"
VETO = "VETO"
REWRITE = "REWRITE"

EVID_DISASM = "disasm"
EVID_NAME = "name"

# 工程常量（名字级规则用；与 official_rules 的汇编常量区分开）
#   ⚠️ 阈值都刻意放在 0.60 以上：`CheckReturnCity` 的血量门限是 0.60（占位值，官方真值未知），
#   低于它的动作会被回城规则先短路掉。这里把"风险判断"类规则放在 0.60~0.70 这个"还能打但不健康"
#   的窗口里，才不会整族规则被回城吃掉。
TOWER_KILL_HP_RATE = 0.65        # 越塔时低于此血量视为"可能被塔秒"
CAKE_NEED_HP_RATE = 0.55         # 血包值得吃的最低血量
CAKE_LEAVE_HP_RATE = 0.90        # 高于此血量不该占血包
MONSTER_MIN_HP_RATE = 0.50       # 残血不打野（0x027e557c `fmov s1,#0.5` × [0x1324]=max_hp，与 [0x9a4]=cur_hp 比较，低于一半即 Ignore）
# 泉水区域：**轴对齐方框**，不是圆。证据 `stay_in_spring @0x027f2a5c`：
#   0x027f2aec  subs w8, w8, w10        ; dx = x - spring_x
#   0x027f2af0  cneg w8, w8, mi         ; |dx|
#   0x027f2af4  lsr  w8, w8, #3         ; >>3（8 单位一档）
#   0x027f2af8  cmp  w8, #0x271         ; 625  -> |dx| <= 8*625 = 5000
#   0x027f2b14  cmp  w8, #0x270         ; 624  -> |dz| <= 8*624 = 4992
#   0x027f2afc  b.lo / 0x027f2b18 b.hi  ; 两个方向都在框内才算"在泉水"
SPRING_BOX_HALF_X = 8.0 * 625      # 5000
SPRING_BOX_HALF_Z = 8.0 * 624      # 4992
HIGH_DEFEND_RADIUS = 1500.0      # 高地塔"被围"半径
HIGH_DEFEND_ALLY_DIST = 5000.0   # 离高地塔多远才值得回防
STUCK_WALL_FRAMES = 8            # 连续 N 帧无位移 -> 判卡墙
STUCK_LONG_FRAMES = 16           # 更久 -> 直接换目标点
ONLY_ATK_FRAMES = 6              # 连续 N 步只攻击不移动
RISK_HP_RATE = 0.70              # 态势判断的"血不健康"上限

# ---- 2026-10-21 新增三条（名字级 + 汇编常量）----
# IsInEnemyHoverArea @0x027dfa28：日志 "In hover area, but target pos is in tower
#   attach range, tower pos is %d, %d, %d"；函数内立即数 1499 与"半径"量级一致，
#   取作包夹判定半径（其余逻辑为推断：敌数 > 友数 且 敌数 >= 2）。
HOVER_RADIUS = 1499.0
HOVER_MIN_ENEMIES = 2
# QuitMovePosWhenArrive @0x027f2d2c：日志 "movepos pos:(%d,%d) distance:%d quit:%d"
#   到达判定用 150（工程值，官方阈值未从立即数取到）。
ARRIVE_EPS = 150.0
# StopShanxianUseInUncorrectTimeing @0x02558f0c（SetTacticsResult 同函数内的过滤段）
#   日志：`becuz shanxian is not usable` / `becuz not enemy around` /
#        `main_move_degree:%d, shanxian_degree:%d` / `361 pass` /
#        `shanxian degree is :%d diff is too large, stop shanxian`
#   立即数：0x168=360（角度上限）、0x169=361（越界即放行）、0x59=89（角度差阈值）。
SHANXIAN_DEGREE_MAX = 360
SHANXIAN_DEGREE_PASS = 361
SHANXIAN_DIFF_MAX = 89
SHANXIAN_ENEMY_RADIUS = 1500.0
# CheckUseZhiliao @0x027e14ec：日志 "not zhiliao, skill_config_id:%d"（先确认召唤师技能是治疗）
#   + "summon_skill_frame_no:%d + 15 < m_cur_frame_no:%d"（15 帧防连发门限）。
HEAL_HP_RATE = 0.40
HEAL_MIN_FRAMES = 15
# GotoTargetPosInSpring @0x027dd190：日志 "enemy hero around, stop / no target pos, stop /
#   not move action, stop / in spring, rest"；立即数 150 / 301 / 451（与出生规则同源）。
SPRING_REST_HP = 0.98
# MoveStopInSpringWhenRevival @0x027f2b98：日志 "In revival delay, elapsed:%d, total:%d"；
#   立即数 548（毫秒量级）→ 复活后约 0.55s 的"起身延迟"，按 8Hz 折算 ~5 帧，取 6 帧。
REVIVE_STOP_FRAMES = 6
# MovePosWhenLongTimeOnlyAtk @0x027eb200：日志 "longtime_only_atk / atk_cnt:%d"
#   连续攻击 N 步后主动换位（而不是像 LongTimeOnlyAtk 那样只处理残血）。
ONLY_ATK_MOVE_FRAMES = 6
# IsLingerAroundDragon @0x027eafb4：日志 "frame_no:%d, expect:%d, cur:%d / clear / set
#   linger around" + 配置键 rule_move_pos_when_linger_around_dragon —— 在龙坑附近"等时机"。
DRAGON_LINGER_RADIUS = 1800.0
# ReturnCityBetween2TowerInEmyHigh @0x027f24b0：日志 "Enter CD: exceeded_max_duration"
#   + "ReturnCityBetween2TowerInEmyHigh_keep" —— 在敌方高地两塔之间时回城（CD 保护）。
EMY_HIGH_DEEP_RADIUS = 1600.0
EMY_HIGH_RETURN_HP = 0.40
# StayWithMonsterUsingSkill @0x027e620c：日志 "Ignore, main hero has low hp" +
#   "Ignore, no target monster" —— 残血不打、无目标不打；血量门限复用 MONSTER_MIN_HP_RATE。



@dataclass
class RuleHit:
    name: str
    verdict: str
    reason: str
    addr: str = ""
    implemented: bool = True
    evidence: str = EVID_NAME


@dataclass
class Rule:
    name: str
    fn: Callable[[Snapshot, Action], Optional[Tuple[str, Optional[Action], str]]]
    addr: str = ""
    implemented: bool = True
    evidence: str = EVID_DISASM


def _retreat(snap: Snapshot) -> Action:
    """撤退点：朝己方泉水方向退 `step`（统一口径见 `Snapshot.retreat_point`）。"""
    return Action("Retreat", "move", point=snap.retreat_point(),
                  meta={"retreat": True})


def _return_city(snap: Snapshot) -> Action:
    """回城/回泉：**真的往己方泉水走**，而不是原地待命。

    之前这里返回的是 `point=(self_x, self_z)`，即"站着不动但标签叫 ReturnCity" ——
    等于回城规则只起到"否决别的动作"的作用，从来没把人送回去（真机上表现为残血原地发呆）。
    现在目标点取己方泉水中心：活体槽位优先，缺失时用 22 槽位的静态几何（`organ_geo`）。
    语义上覆盖官方 `CheckReturnCity` 的动作部分与 `GotoTargetPosInSpring` 的意图。
    """
    pt = None
    spr = (snap.organ_slots or {}).get("friend_spring_tower")
    if spr is not None and spr.x is not None:
        pt = (spr.x, spr.z)
    else:
        g = (snap.organ_geo or {}).get("friend_spring_tower")
        if g is not None:
            pt = (g[0], g[1])
    if pt is None:
        pt = (snap.self_x, snap.self_z)
    return Action("ReturnCity", "move", point=pt, meta={"return_city": True})


def _nearest_enemy(snap: Snapshot):
    foes = sorted([h for h in snap.enemies() if h.alive], key=snap.dist)
    return foes[0] if foes else None


def _stuck_break_point(snap: Snapshot):
    """卡墙脱困点：价值场全局最优点（换方向走），退化时朝己方基地。"""
    try:
        from . import field as _vf
        bp = _vf.best_point(snap, radius=1500.0, step=500.0)
        if bp is not None:
            return bp
    except Exception:
        pass
    if snap.self_x is None:
        return None
    sign = -1.0 if (snap.self_camp or 1) == 1 else 1.0
    return (snap.self_x + sign * 1500.0, snap.self_z + sign * 1500.0)


# ---------------------------------------------------------------- 有汇编证据的规则
def r_check_return_city(snap: Snapshot, act: Action):
    """RuleTactics::CheckReturnCity @0x027df394（常量 70 / 301 / 527）。"""
    me = snap.self_unit
    if me is None:
        return None
    hp = me.hp_rate
    # ep 分支：数据链已打通（HUD 蓝条比例），阈值未从汇编确定 -> 保持 None = 不触发
    need, reason = orl.return_city_decision(hp=hp, ep=snap.self_ep_rate)
    if not need:
        return None
    # 已经在撤退/回城动作上，不重复改写
    if act.label in ("ReturnCity", "Retreat"):
        return None
    return REWRITE, _return_city(snap), reason


def r_keep_away_from_tower(snap: Snapshot, act: Action):
    """RuleTactics::KeepAwayFromEnemyTower @0x027dbd98。

    官方判据：目标为塔 + 最近敌英雄距离 < 301 → 放弃越塔。
    """
    if act.target_kind not in (T_TOWER, T_CRYSTAL) or act.target is None:
        return None
    if snap.enemy_hero_min_dist() < orl.DANGER_DIST:
        return VETO, _retreat(snap), "enemy_hero_around_tower"
    return None


def r_dangerous_place(snap: Snapshot, act: Action):
    """RuleTactics::IsInDangerousPlace @0x027db2ac（危险半径 301）。"""
    if not act.is_attack:
        return None
    ed = snap.enemy_hero_min_dist()
    ad = snap.ally_hero_min_dist()
    if ed < orl.DANGER_DIST and ad > orl.DANGER_DIST:
        return REWRITE, _retreat(snap), "danger_radius_no_ally"
    return None


def r_first_born(snap: Snapshot, act: Action):
    """RuleTactics::GotoTargetPosWhenFirstBorn @0x027dd40c（451 帧 ≈ 30s）。"""
    ok, why = orl.first_born_gate(
        elapsed_s=snap.clock_s,
        enemy_dist_norm=None if snap.enemy_hero_min_dist() >= orl.DANGER_DIST
        else snap.enemy_hero_min_dist() / orl.MAP_UNITS,
        action_is_move=act.kind == "move",
        has_target=act.kind != "none")
    if ok:
        return None
    if why == "enemy_hero_around" and act.kind == "move":
        return VETO, None, why
    return None


def r_dont_take_blood_pack(snap: Snapshot, act: Action):
    """RuleTactics::FrdFzDonotTakeBloodPack（队友更近就别抢）。"""
    if act.target_kind != "Cake" or act.target is None:
        return None
    d_me = snap.dist(act.target)
    for a in snap.allies():
        if a.alive and snap.dist(a) < d_me - 50:
            return VETO, None, "frd_nearer_cake"
    return None


def r_wait_revive_armor(snap: Snapshot, act: Action):
    """RuleTactics::WaitResurrectionArmor —— 装备栏已通，缺 ID→名称映射。

    现状（2026-10-21）：`snap.self_equip` 能拿到已购装备的 config_id 序列，
    但 `equip_config_id_file` 不随包，无法判断"哪个 ID 是复活甲（贤者的庇护）"。
    在自建常用装备表之前**不猜**：宁可返回 None，也不按"装备数量"瞎猜。
    待办：把常用 30~50 件装备的 ID 与名称钉进 `configs/equip_ids.json`，再启用本条。
    """
    return None


# ---------------------------------------------------------------- 名字级规则（语义推断）
def r_avoid_killed_by_organ(snap: Snapshot, act: Action):
    """AvoidKilledByOrgan：塔能一套带走我时别贴塔（**按名字推断的近似，非官方判据**）。

    ⚠️ 语义差异（2026-10-21 用 `rule_consts.py 0x27f39b4` 核对过）：官方同名规则的分支是
    `cmp w9,#0x96`(150) / `cmp w8,#6` / `cmp w8,#0xc`(12) —— 走的是"目标 id / 类型"判断，
    **没有**我们这里的 hp 阈值比较。也就是说官方那条做的事和"残血别越塔"并不完全一样。
    本实现保留 `evidence="name"`，阈值 `TOWER_KILL_HP_RATE` 是工程值；
    地址留档只为下一轮把它逐指令还原，不代表已复刻。
    """
    if act.target_kind not in (T_TOWER, T_CRYSTAL) or act.target is None:
        return None
    me = snap.self_unit
    if me is None:
        return None
    if me.hp_rate <= TOWER_KILL_HP_RATE and act.target.alive:
        return VETO, _retreat(snap), f"tower_can_kill hp={me.hp_rate:.2f}"
    return None


def r_select_cake(snap: Snapshot, act: Action):
    """SelectCake：血量够高就不占血包（血包留给更需要的队友/自己后手）。"""
    if act.target_kind != "Cake":
        return None
    me = snap.self_unit
    if me is not None and me.hp_rate > CAKE_NEED_HP_RATE:
        return VETO, None, f"hp_high={me.hp_rate:.2f}"
    return None


def r_leave_cake(snap: Snapshot, act: Action):
    """LeaveCake：满血且在血包处停留 → 离开。"""
    if act.target_kind != "Cake":
        return None
    me = snap.self_unit
    if me is not None and me.hp_rate >= CAKE_LEAVE_HP_RATE and \
            snap.enemy_hero_min_dist() > orl.DANGER_DIST:
        return VETO, None, "hp_full_leave_cake"
    return None


def r_use_multi_person_cake(snap: Snapshot, act: Action):
    """UseMultiPersonCake：多人挤同一个血包 → 让位（只留最需要的那个）。"""
    if act.target_kind != "Cake" or act.target is None:
        return None
    near = [a for a in snap.allies() if a.alive and snap.dist(a) < 600.0]
    if len(near) >= 2:
        return VETO, None, f"multi_person_cake n={len(near)}"
    return None


def r_stay_with_monster(snap: Snapshot, act: Action):
    """RuleTactics::StayWithMonster @0x027e5438 —— 残血不打野。

    汇编证据（`reverse/rule_consts.py 0x27e5438`）：

        0x027e5578  ldr  s0, [x19, #0x1324]     ; max_hp
        0x027e557c  fmov s1, #0.5
        0x027e5580  fmul s0, s0, s1             ; 0.5 * max_hp
        0x027e5584  ldr  s1, [x19, #0x9a4]      ; cur_hp
        0x027e5588  fcmp s1, s0
        0x027e558c  b.pl 0x27e55fc              ; hp >= 50% -> 不管是
        0x027e5590  ... 打 "Ignore, main hero has low hp: %f"

    即阈值是 **hp_rate < 0.50**（不是我们原先拍的 0.45）；日志串
    `Ignore, main hero has low hp: %f` 印证了这个分支的语义。
    """
    if act.target_kind != T_MONSTER:
        return None
    me = snap.self_unit
    if me is None:
        return None
    if me.hp_rate < MONSTER_MIN_HP_RATE and snap.ally_hero_min_dist() > 1200.0:
        return VETO, None, f"low_hp_jungle hp={me.hp_rate:.2f}"
    return None


def r_atk_soldier_in_frd_high(snap: Snapshot, act: Action):
    """AtkSoldierInFrdHigh：己方高地被兵线压上时回家清线。

    位置优先取活体建筑，缺活体数据时退回 22 槽位**静态几何**（塔位是固定的）；
    若视觉明确报告该塔已倒（`organ_alive=False`）就不回防。
    """
    me = snap.self_unit
    if me is None or snap.self_x is None:
        return None
    names = [n for n in (snap.organ_geo or {}) if n.startswith("friend_") and "high_tower" in n]
    names += [n for n in (snap.organ_slots or {})
              if n.startswith("friend_") and "high_tower" in n and n not in names]
    for name in names:
        if snap.organ_alive.get(name) is False:
            continue
        u = (snap.organ_slots or {}).get(name)
        if u is not None and u.x is not None:
            tx, tz = u.x, u.z
        else:
            g = (snap.organ_geo or {}).get(name)
            if g is None:
                continue
            tx, tz = g
        foes_near = [m for m in snap.minions
                     if m.alive and m.camp is not None and m.camp != snap.self_camp
                     and m.x is not None
                     and math.hypot(m.x - tx, m.z - tz) <= HIGH_DEFEND_RADIUS]
        if len(foes_near) < 3:
            continue
        if math.hypot(snap.self_x - tx, snap.self_z - tz) < HIGH_DEFEND_ALLY_DIST:
            return None            # 已经在回防/在场，不必改写
        return (REWRITE, Action("DefensePosition", "move", point=(tx, tz),
                                meta={"backdoor_defense": name}),
                f"frd_high_pushed {name} n={len(foes_near)}")
    return None


def r_is_stuck_wall(snap: Snapshot, act: Action):
    """IsStuckWall / RuleStuckWallV3：连续 N 帧下了移动指令却没位移 → 换方向。"""
    if act.kind != "move":
        return None
    if snap.stuck_frames < STUCK_WALL_FRAMES:
        return None
    if snap.stuck_frames >= STUCK_LONG_FRAMES:
        return None                    # 交给 LongTimeMoveStopRule 处理
    pt = _stuck_break_point(snap)
    if pt is None:
        return None
    return REWRITE, Action("StuckBreak", "move", point=pt,
                           meta={"stuck_break": True}), f"stuck={snap.stuck_frames}"


def r_long_time_move_stop(snap: Snapshot, act: Action):
    """LongTimeMoveStopRule：卡得更久 → 直接换目标点（放弃当前方向）。"""
    if snap.stuck_frames < STUCK_LONG_FRAMES:
        return None
    pt = _stuck_break_point(snap)
    if pt is None:
        return None
    return REWRITE, Action("GridBest", "move", point=pt,
                           meta={"grid_best": True}), f"stuck_long={snap.stuck_frames}"


def r_long_time_only_atk(snap: Snapshot, act: Action):
    """LongTimeOnlyAtk：连续多步只攻击不机动且血线告急 → 拉开。"""
    if not act.is_attack:
        return None
    recent = list(snap.last_actions or [])[-ONLY_ATK_FRAMES:]
    if len(recent) < ONLY_ATK_FRAMES or any(not r.lower().startswith(("atk", "grid"))
                                           for r in recent):
        return None
    me = snap.self_unit
    if me is not None and me.hp_rate < RISK_HP_RATE:
        return REWRITE, _retreat(snap), "only_atk_low_hp"
    return None


def r_situation_dangerous(snap: Snapshot, act: Action):
    """SituationAssessmentDangerous：人数差 + 血线双差时不做进攻动作。"""
    if not act.is_attack:
        return None
    me = snap.self_unit
    if me is None:
        return None
    foes = [h for h in snap.enemies() if h.alive]
    allies = [a for a in snap.allies() if a.alive]
    if len(foes) >= len(allies) + 2 and me.hp_rate < RISK_HP_RATE:
        return REWRITE, _retreat(snap), f"outnumbered {len(foes)}v{len(allies) + 1}"
    return None


def r_is_in_enemy_hover_area(snap: Snapshot, act: Action):
    """RuleTactics::IsInEnemyHoverArea @0x027dfa28 —— 被包夹时不接团。

    官方日志：`In hover area, but target pos is in tower attach range, tower pos is ...`
    → 判定"在敌方包夹区"时，若目标点落在塔攻击圈内则**不算**（塔下反而是安全点）。
    本实现取其中可复现的一半：敌数 > 友数且敌数 >= 2 时判为被包夹，撤回。
    """
    if not act.is_attack:
        return None
    me = snap.self_unit
    if me is None:
        return None
    r = HOVER_RADIUS
    foes = [h for h in snap.enemies() if h.alive and snap.dist(h) <= r]
    if len(foes) < HOVER_MIN_ENEMIES:
        return None
    mates = [a for a in snap.allies() if a.alive and snap.dist(a) <= r]
    if len(foes) <= len(mates):
        return None
    return REWRITE, _retreat(snap), f"hover_area:{len(foes)}v{len(mates)}"


def r_quit_move_pos_when_arrive(snap: Snapshot, act: Action):
    """RuleTactics::QuitMovePosWhenArrive @0x027f2d2c —— 到达目标点即停，防抖动。

    官方日志：`movepos pos:(%d,%d) distance:%d quit:%d`
    """
    if act.kind != "move" or act.point is None or snap.self_x is None:
        return None
    d = math.hypot(act.point[0] - snap.self_x, act.point[1] - snap.self_z)
    if d > ARRIVE_EPS:
        return None
    if act.meta.get("stand") or act.meta.get("retreat"):
        return None
    return REWRITE, Action("Stand", "move", point=(snap.self_x, snap.self_z),
                           meta={"stand": True}), f"arrived:{d:.0f}"


def r_stay_with_monster_using_skill(snap: Snapshot, act: Action):
    """RuleTactics::StayWithMonsterUsingSkill @0x027e620c —— 打野时用技能加速。

    官方日志：`Ignore, main hero has low hp: %f` / `Ignore, no target monster`
    → 残血不打、没目标不打；本实现再加一条"技能得就绪"。
    """
    if act.target_kind != T_MONSTER or act.target is None or act.kind != "attack":
        return None
    me = snap.self_unit
    if me is None or me.hp_rate < MONSTER_MIN_HP_RATE:
        return None
    for s in (me.skills or []):
        slot = int(s.get("slot") or 0)
        if slot < 1 or slot > 3:
            continue
        if int(s.get("cd_left_ms") or 0) > 0:
            continue
        return REWRITE, Action(f"AtkMonsterSkill{slot}", "skill", T_MONSTER,
                               act.target, slot,
                               meta={"dist": act.meta.get("dist", 0.0)}), \
            f"monster_skill{slot}"
    return None


def r_is_linger_around_dragon(snap: Snapshot, act: Action):
    """RuleTactics::IsLingerAroundDragon @0x027eafb4 —— 别在敌人眼皮底下开龙。

    官方语义是"在龙坑附近徘徊等时机"；可复现的一半是：**目标为龙且敌英雄在危险半径内**
    时放弃（官方 `DANGER_DIST=301` 与此同源）。
    """
    if act.target_kind != T_MONSTER or act.target is None:
        return None
    if snap.dist(act.target) > DRAGON_LINGER_RADIUS:
        return None
    ed = snap.enemy_hero_min_dist()
    if ed > orl.DANGER_DIST:
        return None
    return VETO, None, f"linger_dragon:enemy{ed:.0f}"


def r_return_city_in_emy_high(snap: Snapshot, act: Action):
    """RuleTactics::ReturnCityBetween2TowerInEmyHigh @0x027f24b0 —— 敌方高地深处残血就回。

    官方日志：`Enter CD: exceeded_max_duration` / `..._keep`
    """
    me = snap.self_unit
    if me is None or snap.self_x is None:
        return None
    if me.hp_rate > EMY_HIGH_RETURN_HP:
        return None
    deep = False
    for lane in ("top", "mid", "bottom"):
        u = (snap.organ_slots or {}).get(f"enemy_{lane}_high_tower")
        g = (snap.organ_geo or {}).get(f"enemy_{lane}_high_tower")
        pt = (u.x, u.z) if (u is not None and u.x is not None) else (g if g else None)
        if pt is None:
            continue
        if math.hypot(pt[0] - snap.self_x, pt[1] - snap.self_z) <= EMY_HIGH_DEEP_RADIUS:
            deep = True
            break
    if not deep:
        return None
    if act.label in ("ReturnCity", "Retreat"):
        return None
    return REWRITE, _return_city(snap), "emy_high_low_hp"


def _in_spring_box(snap: Snapshot) -> bool:
    me = snap.self_unit
    if me is None:
        return False
    try:
        from . import targeting as _tg
        return _tg._in_spring(snap, me)
    except Exception:
        return False


def r_goto_target_pos_in_spring(snap: Snapshot, act: Action):
    """RuleTactics::GotoTargetPosInSpring @0x027dd190 —— 泉水里的"起身"规则。

    官方四个中止/分支（日志原文）：
      `enemy hero around, stop` / `no target pos, stop` / `not move action, stop` / `in spring, rest`
    实现：在泉水内时，(a) 敌英雄进 301 → 否决移动；(b) 血没满 → 原地休整（rest）。
    """
    if not _in_spring_box(snap):
        return None
    me = snap.self_unit
    if act.kind == "move" and snap.enemy_hero_min_dist() < orl.DANGER_DIST:
        return VETO, None, "enemy_hero_around"
    if me is not None and me.hp_rate < SPRING_REST_HP and act.kind == "move":
        return REWRITE, Action("Stand", "move",
                               point=(snap.self_x, snap.self_z),
                               meta={"stand": True}), "in_spring_rest"
    return None


_REVIVE_MARK = {"cid": None, "frame": -10 ** 9}


def r_move_stop_in_spring_when_revival(snap: Snapshot, act: Action):
    """RuleTactics::MoveStopInSpringWhenRevival @0x027f2b98 —— 复活后先"起身"再动。

    官方日志：`In revival delay, elapsed:%d, total:%d` / `Revival delay finished, elapsed:%d`
    → 复活瞬间有一段不可移动的延迟（立即数 548，毫秒量级 ≈ 0.55s）。
    """
    me = snap.self_unit
    if me is None:
        return None
    if (me.respawn_s or 0) > 0:                 # 还在倒计时：记下"刚死/刚复活"的时刻
        _REVIVE_MARK["cid"] = me.cid
        _REVIVE_MARK["frame"] = int(snap.frame)
        return None
    if _REVIVE_MARK["cid"] != me.cid:
        return None
    if int(snap.frame) - int(_REVIVE_MARK["frame"]) > REVIVE_STOP_FRAMES:
        return None
    if act.kind != "move":
        return None
    return REWRITE, Action("Stand", "move", point=(snap.self_x, snap.self_z),
                           meta={"stand": True}), "revival_delay"


def r_move_pos_when_long_time_only_atk(snap: Snapshot, act: Action):
    """RuleTactics::MovePosWhenLongTimeOnlyAtk @0x027eb200 —— 久攻不动就换位。

    官方日志：`longtime_only_atk / frame=%d, cid=%d, yaction=%d, atk_cnt=%d`
    与 `LongTimeOnlyAtk` 的分工：那条管"残血拉开"，这条管"健康时也要换角度"。
    """
    if not act.is_attack:
        return None
    recent = list(snap.last_actions or [])[-ONLY_ATK_MOVE_FRAMES:]
    if len(recent) < ONLY_ATK_MOVE_FRAMES:
        return None
    if any(not str(r).lower().startswith(("atk", "grid")) for r in recent):
        return None
    me = snap.self_unit
    if me is not None and me.hp_rate < RISK_HP_RATE:
        return None                              # 残血交给 LongTimeOnlyAtk 处理
    try:
        from . import field as _vf
        bp = _vf.best_point(snap, radius=1500.0, step=500.0)
    except Exception:
        bp = None
    if bp is None:
        return None
    return REWRITE, Action("Reposition", "move", point=bp,
                           meta={"reposition": True}), "longtime_only_atk_repos"


_HEAL_MARK = {"frame": -10 ** 9}


def _heal_slot(me):
    """找治疗槽（内存 slot5，id 90003）。返回 (slot, cd_left_ms) 或 (None, 0)。"""
    for s in (me.skills or []):
        sid = int(s.get("id") or 0)
        if sid == 90003 or str(s.get("role") or "") == "heal":
            return int(s.get("slot") or 5), int(s.get("cd_left_ms") or 0)
    return None, 0


def r_check_use_zhiliao(snap: Snapshot, act: Action):
    """RuleTactics::CheckUseZhiliao @0x027e14ec —— 召唤师技能"治疗"的使用。

    官方判定顺序（日志原文）：
      1) `not use, not zhiliao, skill_config_id:%d` —— 召唤师技能不是治疗就不放；
      2) `not use, summon_skill_frame_no:%d + 15 < m_cur_frame_no:%d` —— 15 帧内不重复放。
    本实现再加"血量低于 0.40 才放"（官方血量门限未取到立即数，取工程值）。
    """
    me = snap.self_unit
    if me is None:
        return None
    slot, cd = _heal_slot(me)
    if slot is None or cd > 0:
        return None
    if me.hp_rate >= HEAL_HP_RATE:
        return None
    if int(snap.frame) - int(_HEAL_MARK["frame"]) < HEAL_MIN_FRAMES:
        return None
    _HEAL_MARK["frame"] = int(snap.frame)
    return REWRITE, Action("UseHeal", "summoner", meta={"slot": slot}), "use_zhiliao"


def r_stop_shanxian_uncorrect_timing(snap: Snapshot, act: Action):
    """`StopShanxianUseInUncorrectTimeing` @0x02558f0c —— 闪现时机过滤器。

    官方三条否决（日志原文）：
      1) `becuz shanxian is not usable` —— 闪现不在可用状态；
      2) `becuz not enemy around` —— 附近没敌人，闪现没意义；
      3) `shanxian degree is :%d diff is too large, stop shanxian` ——
         **闪现方向与移动方向夹角 > 89°**（立即数 0x59）视为误操作。
    第 3 条需要"带方向的闪现"（拖拽施法）；我们的执行层目前是 tap，拿不到方向，
    所以只落地前两条，第 3 条留到执行层支持 `mode="drag"` 的召唤师技能再接。
    """
    if act.kind != "summoner":
        return None
    me = snap.self_unit
    if me is None:
        return None
    slot, cd = _heal_slot(me)
    # 召唤师槽可能是闪现而不是治疗：只要该槽在 CD 中就不放
    for s in (me.skills or []):
        sid = int(s.get("id") or 0)
        if 80000 <= sid < 90000 or sid == 90003:
            if int(s.get("cd_left_ms") or 0) > 0:
                return VETO, None, "shanxian_not_usable"
    if snap.enemy_hero_min_dist() > SHANXIAN_ENEMY_RADIUS:
        return VETO, None, "not_enemy_around"

    # 第 3 条：方向差 > 89° 视为误操作（立即数 0x59）。
    #   需要动作带方向（meta.target，帧坐标或世界坐标都可，只要同坐标系）。
    tgt = (act.meta or {}).get("target")
    if tgt is not None and snap.self_x is not None:
        move_deg = _main_move_degree(snap)
        flash_deg = _deg_to(snap, tgt)
        if move_deg is not None and flash_deg is not None:
            diff = abs(move_deg - flash_deg) % 360.0
            if diff > 180.0:
                diff = 360.0 - diff
            if diff > SHANXIAN_DIFF_MAX:
                return VETO, None, f"shanxian_deg_diff:{diff:.0f}"
    return None


def _deg_to(snap: Snapshot, pt) -> Optional[float]:
    """自身 -> 点的方位角（0..360）。"""
    if snap.self_x is None or pt is None:
        return None
    dx, dz = float(pt[0]) - snap.self_x, float(pt[1]) - snap.self_z
    if abs(dx) < 1e-6 and abs(dz) < 1e-6:
        return None
    return math.degrees(math.atan2(dz, dx)) % 360.0


def _main_move_degree(snap: Snapshot) -> Optional[float]:
    """当前"主移动方向"：有走位目标用它，否则用最近敌人的方向。"""
    pt = (snap.move_goal or None) if hasattr(snap, "move_goal") else None
    if pt is None:
        foes = [h for h in snap.enemies() if h.alive]
        if foes:
            u = min(foes, key=snap.dist)
            pt = (u.x, u.z)
    if pt is None:
        return None
    return _deg_to(snap, pt)


def r_dangerous_place_retreat(snap: Snapshot, act: Action):
    """RuleTactics::DangerousPlaceRetreat @0x027de1dc —— 危险区的**动作**部分。

    `IsInDangerousPlace` 只回答"是否危险"，这条负责"往哪退"。
    官方日志只有一个标签 `[DangerousPlaceRetreat] DANGEROUS_PLACE`，撤退点由
    `GetRetreatPos @0x027e1b98` 计算；这里用"离所有敌人最远的价值场点"近似。
    """
    if act.kind != "move" or act.meta.get("retreat"):
        return None
    if snap.enemy_hero_min_dist() >= orl.DANGER_DIST:
        return None
    # 已经在往回走就不重复改写
    if snap.self_x is not None and act.point is not None:
        from . import targeting as _tg
        if _tg.in_main_camp(snap, act.point[0]) and not _tg.in_main_camp(snap):
            return None
    return REWRITE, _retreat(snap), "dangerous_place_retreat"


def r_notarget_optimize(snap: Snapshot, act: Action):
    """NotargetOptimizeProcess：没有动作但身边就有能打的目标 → 打最近的。"""
    if act.kind != "none":
        return None
    me = snap.self_unit
    if me is None:
        return None
    rng = float(me.atk_range or 300.0) * 1.15
    for u, tk in ([(h, "Hero") for h in snap.enemies() if h.alive]
                  + [(m, T_SOLDIER) for m in snap.minions
                     if m.alive and m.camp is not None and m.camp != snap.self_camp]):
        if snap.dist(u) <= rng:
            return (REWRITE, Action(f"Atk{tk}", "attack", tk, u, 0,
                                    meta={"dist": snap.dist(u), "from": "notarget"}),
                    f"nearby_{tk.lower()}")
    return None


def r_linger_in_spring(snap: Snapshot, act: Action):
    """ProcessLingerInSpring / stay_in_spring：在泉水里且没回满，先别出去。

    泉水判定用**方框**（±5000 / ±4992，证据见 `SPRING_BOX_HALF_X/Z` 注释，
    来自 `stay_in_spring @0x027f2a5c`），位置优先取活体建筑、缺活体时用 22 槽位静态几何。
    """
    if snap.self_x is None:          # 位置没有就没法判"在不在泉水"；几何有 organ_geo 兜底
        return None
    me = snap.self_unit
    if me is None or me.hp_rate >= 0.98:
        return None
    spr = snap.organ_slots.get("friend_spring_tower")
    sx = sy = None
    if spr is not None and spr.x is not None:
        sx, sy = spr.x, spr.z
    else:
        g = (snap.organ_geo or {}).get("friend_spring_tower")
        if g is not None:
            sx, sy = g
    if sx is None:
        return None
    if (abs(snap.self_x - sx) > SPRING_BOX_HALF_X
            or abs(snap.self_z - sy) > SPRING_BOX_HALF_Z):
        return None
    if act.kind == "none":
        return None
    return VETO, None, f"linger_in_spring hp={me.hp_rate:.2f}"


CHAIN: List[Rule] = [
    # ---- 有汇编证据（顺序按调度器槽位近似）----
    Rule("CheckReturnCity", r_check_return_city, "0x027df394", evidence=EVID_DISASM),
    Rule("KeepAwayFromEnemyTower", r_keep_away_from_tower, "0x027dbd98", evidence=EVID_DISASM),
    Rule("IsInDangerousPlace", r_dangerous_place, "0x027db2ac", evidence=EVID_DISASM),
    Rule("GotoTargetPosWhenFirstBorn", r_first_born, "0x027dd40c", evidence=EVID_DISASM),
    Rule("FrdFzDonotTakeBloodPack", r_dont_take_blood_pack,
         addr="0x027d9030", evidence=EVID_NAME),
    Rule("WaitResurrectionArmor", r_wait_revive_armor, implemented=False),
    # ---- 名字级（语义推断，常量可调）----
    Rule("AvoidKilledByOrgan", r_avoid_killed_by_organ, addr="0x027f39b4",
         evidence=EVID_NAME),
    Rule("SituationAssessmentDangerous", r_situation_dangerous, evidence=EVID_NAME),
    Rule("AtkSoldierInFrdHigh", r_atk_soldier_in_frd_high, addr="0x027e9a3c",
         evidence=EVID_NAME),
    Rule("ProcessLingerInSpring", r_linger_in_spring, addr="0x027d61c0",
         evidence=EVID_NAME),
    Rule("SelectCake", r_select_cake, addr="0x027df040", evidence=EVID_NAME),
    Rule("LeaveCake", r_leave_cake, addr="0x027dcc5c", evidence=EVID_NAME),
    Rule("UseMultiPersonCake", r_use_multi_person_cake, evidence=EVID_NAME),
    Rule("StayWithMonster", r_stay_with_monster, addr="0x027e5438",
         evidence=EVID_DISASM),
    Rule("IsStuckWall", r_is_stuck_wall, addr="0x027e766c", evidence=EVID_NAME),
    Rule("LongTimeMoveStopRule", r_long_time_move_stop, addr="0x027f32ec",
         evidence=EVID_NAME),
    Rule("LongTimeOnlyAtk", r_long_time_only_atk, evidence=EVID_NAME),
    Rule("NotargetOptimizeProcess", r_notarget_optimize, addr="0x027f02a8",
         evidence=EVID_NAME),
    # ---- 2026-10-21 新增（日志串 + 立即数已定位，判定逻辑为推断）----
    Rule("IsInEnemyHoverArea", r_is_in_enemy_hover_area, addr="0x027dfa28",
         evidence=EVID_NAME),
    Rule("StayWithMonsterUsingSkill", r_stay_with_monster_using_skill,
         addr="0x027e620c", evidence=EVID_NAME),
    Rule("QuitMovePosWhenArrive", r_quit_move_pos_when_arrive, addr="0x027f2d2c",
         evidence=EVID_NAME),
    Rule("IsLingerAroundDragon", r_is_linger_around_dragon, addr="0x027eafb4",
         evidence=EVID_NAME),
    Rule("ReturnCityBetween2TowerInEmyHigh", r_return_city_in_emy_high,
         addr="0x027f24b0", evidence=EVID_NAME),
    Rule("GotoTargetPosInSpring", r_goto_target_pos_in_spring, addr="0x027dd190",
         evidence=EVID_NAME),
    Rule("MoveStopInSpringWhenRevival", r_move_stop_in_spring_when_revival,
         addr="0x027f2b98", evidence=EVID_NAME),
    Rule("MovePosWhenLongTimeOnlyAtk", r_move_pos_when_long_time_only_atk,
         addr="0x027eb200", evidence=EVID_NAME),
    Rule("DangerousPlaceRetreat", r_dangerous_place_retreat, addr="0x027de1dc",
         evidence=EVID_NAME),
    Rule("CheckUseZhiliao", r_check_use_zhiliao, addr="0x027e14ec",
         evidence=EVID_NAME),
    Rule("StopShanxianUseInUncorrectTimeing", r_stop_shanxian_uncorrect_timing,
         addr="0x02558f0c", evidence=EVID_NAME),
]

MODES = ("all", "disasm")


def chain_stats() -> dict:
    """规则链规模：总数 / 已实现 / 有汇编证据 / 名字级。"""
    impl = [r for r in CHAIN if r.implemented]
    dis = [r for r in impl if r.evidence == EVID_DISASM]
    return {"total": len(CHAIN), "implemented": len(impl),
            "disasm": len(dis), "name": len(impl) - len(dis),
            "official_total": 141}


def apply_rules(snap: Snapshot, act: Action,
                chain: Optional[List[Rule]] = None,
                mode: str = "all") -> Tuple[Action, List[RuleHit]]:
    """按官方顺序跑规则链，命中即短路。返回 (最终动作, 命中列表)。"""
    final = act
    hits: List[RuleHit] = []
    for r in (chain if chain is not None else CHAIN):
        if not r.implemented:
            continue
        if mode == "disasm" and r.evidence != EVID_DISASM:
            continue
        try:
            res = r.fn(snap, final)
        except Exception as e:                     # 单条规则异常不影响整链
            hits.append(RuleHit(r.name, PASS, f"error:{type(e).__name__}", r.addr,
                                evidence=r.evidence))
            continue
        if not res:
            continue
        verdict, new_act, reason = res
        hits.append(RuleHit(r.name, verdict, reason, r.addr, evidence=r.evidence))
        if verdict == VETO:
            return Action("NoneAction", "none", meta={"vetoed_by": r.name}), hits
        if verdict == REWRITE and new_act is not None:
            final = new_act
            break
    return final, hits
