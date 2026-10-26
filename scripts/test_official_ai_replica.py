# -*- coding: utf-8 -*-
"""复刻版官方 AI 的回归测试（无需设备、无需模型权重）。

跑法：venv/Scripts/python.exe -X utf8 scripts/test_official_ai_replica.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.decision import official_ai as oa          # noqa: E402
from wzry.decision.official_ai import actions as A   # noqa: E402
from wzry.decision.official_ai import features as F  # noqa: E402
from wzry.decision.official_ai import rules as R     # noqa: E402
from wzry.decision.official_ai import schema as S    # noqa: E402
from wzry.decision.official_ai.policy import OfficialAI, LinearScorer  # noqa: E402

ATK = 700.0          # 小乔级射程（世界单位）

PASS = FAIL = 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS {name}" + (f"  {extra}" if extra else ""))
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"FAIL {name}  {extra}")


def hero(cid, camp, x, z, hp, hpmax, level=5, skills=None):
    return {"id": cid, "camp": camp, "x": x, "z": z, "hp": hp, "hp_max": hpmax,
            "level": level, "kills": 0, "deaths": 0, "assists": 0,
            "gold": 1000, "gold_total": 1200, "skills": skills or []}


def fixture(hp_ratio=0.8, enemy_dist=600.0, with_enemy_near_tower=True):
    """构造一局最小快照：自己 106（蓝），一个近敌 107（红），若干兵/塔/野。"""
    me_hp = int(4000 * hp_ratio)
    sk = [{"slot": 1, "id": 10610, "level": 0, "state": 0, "ready": True,
           "cd_left_ms": 0, "cd_total_ms": 5000},
          {"slot": 2, "id": 10620, "level": 0, "state": 0, "ready": False,
           "cd_left_ms": 5000, "cd_total_ms": 10000},
          {"slot": 3, "id": 10630, "level": 0, "state": 0, "ready": True,
           "cd_left_ms": 0, "cd_total_ms": 35000}]
    heroes = [hero(106, 1, 0, 0, me_hp, 4000, 8, sk),
              hero(107, 2, enemy_dist, 0, 800, 4000, 8),
              hero(121, 2, 30000, 30000, 4000, 4000),
              hero(110, 1, -20000, -20000, 4000, 4000),
              hero(167, 1, -25000, -18000, 3000, 4000),
              hero(171, 2, 40000, 10000, 2000, 5000),
              hero(187, 1, -15000, -22000, 3500, 4000),
              hero(173, 2, 35000, 20000, 1500, 4000),
              hero(174, 1, -22000, -25000, 3800, 4000),
              hero(105, 2, 42000, 5000, 3000, 5000)]
    units = [
        {"cid": 1690, "kind": "minion", "camp": 2, "x": 400, "z": 100, "hp": 900, "hp_max": 1200, "dead": False},
        {"cid": 1691, "kind": "minion", "camp": 2, "x": 420, "z": -120, "hp": 400, "hp_max": 1200, "dead": False},
        {"cid": 1692, "kind": "minion", "camp": 1, "x": -300, "z": 0, "hp": 1200, "hp_max": 1200, "dead": False},
        {"cid": 92, "kind": "turret", "camp": 2, "x": 520 if with_enemy_near_tower else 40000,
         "z": 0, "hp": 9000, "hp_max": 9000, "dead": False},
        {"cid": 95, "kind": "turret", "camp": 1, "x": -900, "z": 0, "hp": 9000, "hp_max": 9000, "dead": False},
        {"cid": 16100, "kind": "monster", "camp": 2, "x": 1500, "z": 800, "hp": 6000, "hp_max": 9000, "dead": False},
    ]
    state = {"ui": {"match_clock": 120, "hero_id": 106},
             "extra": {"mem_heroes": heroes, "mem_units": units}}
    return state


def main():
    # ---- 1. 模式 ----
    d = S.dims()
    check("T01 维度自洽（每类 = 单位内维数 × 上限）",
          d["hero"] == S.SPECS["hero"].unit_size * 10
          and d["soldier"] == S.SPECS["soldier"].unit_size * 8, str(d))
    check("T02 建筑槽位 = 22 且唯一",
          len(S.ORGAN_SLOTS) == 22 and len(set(S.ORGAN_SLOTS)) == 22,
          f"{len(S.ORGAN_SLOTS)}")
    lay = S.layout(S.SPECS["hero"])
    check("T03 layout 连续覆盖 unit_size",
          lay[0][1] == 0 and lay[-1][1] + lay[-1][2] == S.SPECS["hero"].unit_size,
          f"{len(lay)} 项")

    # ---- 2. 快照 ----
    st = fixture()
    snap = oa.from_state_dict(st, atk_range=ATK)
    check("T04 快照：10 英雄 + 自己定位正确",
          len(snap.heroes) == 10 and snap.self_cid == 106, f"n={len(snap.heroes)}")
    check("T05 快照：单位按类别分流",
          len(snap.minions) == 3 and len(snap.organs) == 2 and len(snap.monsters) == 1,
          f"{len(snap.minions)}/{len(snap.organs)}/{len(snap.monsters)}")
    check("T06 快照：敌我经济/击杀差算出",
          snap.ally_gold > 0 and snap.enemy_gold > 0, f"{snap.ally_gold}/{snap.enemy_gold}")

    # ---- 3. 特征 ----
    pack = F.build(snap)
    check("T07 特征向量长度固定", pack.size == sum(S.dims().values()),
          f"{pack.size}")
    check("T08 归一化落在 [0,1]", all(0.0 <= v <= 1.0 for v in pack.flat()),
          f"max={max(pack.flat()):.3f}")
    check("T09 英雄向量含官方键名 dist_from_each_hero",
          pack.get("hero", "dist_from_each_hero", 0) is not None)
    many = fixture()
    many["extra"]["mem_units"] += [
        {"cid": 1693, "kind": "minion", "camp": 2, "x": 300 + i * 10, "z": 50,
         "hp": 800, "hp_max": 1200, "dead": False} for i in range(12)]
    p2 = F.build(oa.from_state_dict(many, atk_range=ATK))
    check("T10 超上限截断（units limitation）",
          p2.culled.get("soldier", 0) > 0 or p2.n_units.get("soldier") == 8,
          f"n={p2.n_units.get('soldier')} culled={p2.culled.get('soldier')}")

    # ---- 4. 动作空间 ----
    acts = A.enumerate_actions(snap)
    labels = [a.label for a in acts]
    check("T11 双手动作标签：AtkHero / AtkHeroSkill1 都在",
          "AtkHero" in labels and "AtkHeroSkill1" in labels, f"{len(acts)} 个")
    check("T12 技能 CD 未好则不出该槽",
          not any(a.label.endswith("Skill2") for a in acts), "Skill2 cd=5000")
    check("T13 标签无重复", len(labels) == len(set(labels)))
    n_move = len(A.enumerate_moves(snap))
    n_move_retreat = len(A.enumerate_moves(snap, retreat=True))
    check("T14 走位动作是 grid action（8 向 + 原地，撤退另加 1）",
          n_move == 9 and n_move_retreat == 10, f"{n_move}/{n_move_retreat}")

    # 撤退/回城必须真的位移（旧实现 point=self，= 原地不动）
    mv_ret = next(a for a in A.enumerate_moves(snap, retreat=True) if a.meta.get("retreat"))
    spr_pt = snap.own_spring_pt()
    d_before = ((spr_pt[0] - snap.self_x) ** 2 + (spr_pt[1] - snap.self_z) ** 2) ** 0.5
    d_after = ((spr_pt[0] - mv_ret.point[0]) ** 2 + (spr_pt[1] - mv_ret.point[1]) ** 2) ** 0.5
    check("T14b Retreat 朝己方泉水位移（不是原地）",
          d_after < d_before - 1000.0, f"{d_before:.0f} -> {d_after:.0f}")

    # ---- 5. 规则 ----
    low = oa.from_state_dict(fixture(hp_ratio=0.15), atk_range=ATK)
    act = A.enumerate_actions(low)[0]
    fin, hits = R.apply_rules(low, act)
    check("T15 低血触发 CheckReturnCity 改写",
          fin.label == "ReturnCity" and any(h.name == "CheckReturnCity" for h in hits),
          fin.label)
    # 回城必须真的往己方泉水走（旧实现 point=self，等于原地发呆）
    sp_geo = low.organ_geo.get("friend_spring_tower")
    check("T15b ReturnCity 目标点 = 己方泉水（不再是原地）",
          fin.point is not None and sp_geo is not None
          and abs(fin.point[0] - sp_geo[0]) < 1.0
          and abs(fin.point[1] - sp_geo[1]) < 1.0,
          f"{fin.point} vs {sp_geo}")

    near_tower = oa.from_state_dict(fixture(enemy_dist=200.0), atk_range=ATK)
    tw = next(a for a in A.enumerate_actions(near_tower) if a.target_kind == A.T_TOWER)
    fin2, hits2 = R.apply_rules(near_tower, tw)
    check("T16 敌英雄 301 内越塔被否决",
          fin2.label == "NoneAction"
          and any(h.name == "KeepAwayFromEnemyTower" for h in hits2), fin2.label)

    # ---- 6. 管线 ----
    t = [0.0]
    ai = OfficialAI(level="hard", clock=lambda: t[0])
    dec1 = ai.decide(snap)
    check("T17 首帧走 model 分支", dec1.source in ("model", "rule"), dec1.source)
    t[0] = 0.001
    dec2 = ai.decide(snap)
    check("T18 节拍内复用（cached）", dec2.source == "cached", dec2.source)
    t[0] = 1.0
    dec3 = ai.decide(snap)
    check("T19 节拍外重算", dec3.source in ("model", "rule"), dec3.source)
    check("T20 决策带概率与候选数", 0.0 <= dec3.prob <= 1.0 and dec3.candidates > 0,
          f"prob={dec3.prob:.3f} n={dec3.candidates}")

    empty = {"ui": {}, "extra": {}}
    ai2 = OfficialAI(level="normal", clock=lambda: 0.0)
    dec4 = ai2.decide(oa.from_state_dict(empty))
    check("T21 无数据走 [UnpredictProcess] 回退",
          dec4.source == "fallback", dec4.label)

    t2 = [0.0]
    a1 = OfficialAI(level="hard", clock=lambda: t2[0]).decide(snap)
    t2[0] = 0.0
    a2 = OfficialAI(level="hard", clock=lambda: t2[0]).decide(snap)
    check("T22 同输入同输出（确定性）", a1.label == a2.label, f"{a1.label}=={a2.label}")

    # ---- 7. 性能 ----
    ai3 = OfficialAI(level="juewu", clock=lambda: time.monotonic())
    t0 = time.perf_counter()
    N = 50
    for _ in range(N):
        ai3.decide(snap)
    ms = (time.perf_counter() - t0) * 1000 / N
    check("T23 单帧决策 < 5ms", ms < 5.0, f"{ms:.2f}ms")

    # ---- 8. 桥接层 ----
    from wzry.decision.official_ai import bridge as B
    mv = next(a for a in A.enumerate_moves(snap) if a.point)
    leg = B.to_legacy_action(mv, snap)
    check("T24 走位动作转 map_move（归一化 0..1）",
          leg["type"] == "map_move" and 0.0 <= leg["nx"] <= 1.0
          and 0.0 <= leg["ny"] <= 1.0, str(leg))
    sk = next(a for a in A.enumerate_actions(snap) if a.kind == "skill")
    leg2 = B.to_legacy_action(sk, snap)
    check("T25 技能动作转 skill(id=槽位)",
          leg2["type"] == "skill" and leg2["id"] == sk.skill_slot, str(leg2))
    act3, dec5 = B.decide_legacy(fixture(), self_id=106, atk_range=ATK)
    check("T26 decide_legacy 端到端产出可执行 dict",
          isinstance(act3, dict) and act3.get("type") in
          ("map_move", "skill", "attack", "none"), str(act3.get("type")))

    # ---- 9. 走位价值场 ----
    from wzry.decision.official_ai import field as VF
    full = oa.from_state_dict(fixture(hp_ratio=1.0, enemy_dist=800), atk_range=ATK)
    low = oa.from_state_dict(fixture(hp_ratio=0.12, enemy_dist=800), atk_range=ATK)
    bf, bl = VF.best_point(full), VF.best_point(low)
    check("T27 满血：走位朝敌人（+x）", bf is not None and bf[0] > full.self_x,
          f"best={bf}")
    check("T28 残血：走位远离敌人（-x）", bl is not None and bl[0] < low.self_x,
          f"best={bl}")
    far = oa.from_state_dict(fixture(hp_ratio=1.0, enemy_dist=40000), atk_range=ATK)
    bp2 = VF.best_point(far)
    check("T29 无敌人在场：朝敌方基地推进（+x）",
          bp2 is not None and bp2[0] > far.self_x, f"best={bp2}")

    # ---- 10. 走位不再恒定 ----
    st_a = fixture(hp_ratio=1.0, enemy_dist=500)          # 敌人在射程内
    st_b = fixture(hp_ratio=1.0, enemy_dist=40000)        # 敌人很远
    st_b["extra"]["mem_units"] = []
    # 各自独立的实例：同一实例会被 predict_freq 节拍缓存成同一决策
    d_a = OfficialAI(level="juewu", clock=lambda: 0.0).decide(
        oa.from_state_dict(st_a, atk_range=ATK))
    d_b = OfficialAI(level="juewu", clock=lambda: 0.0).decide(
        oa.from_state_dict(st_b, atk_range=ATK))
    kinds = {d_a.action.kind, d_b.action.kind}
    check("T30 局面不同则动作类别不同（攻击 vs 走位）",
          "attack" in kinds or "skill" in kinds, f"{d_a.label}/{d_b.label}")

    # ---- 11. 坐标口径（z 轴反序）----
    from wzry.decision.official_ai import geo as G
    from wzry.decision.official_ai import organ_slots as OS
    from wzry.decision.official_ai import bridge as BR
    from wzry.decision.official_ai.snapshot import Unit, Snapshot

    bx, by = G.world_to_norm(-56379, -53367)      # 蓝方出生点
    rx, ry = G.world_to_norm(55757, 57652)        # 红方出生点
    check("T31 蓝方出生点 -> 小地图左下(0.02,0.98)",
          abs(bx - 0.02) < 0.01 and abs(by - 0.98) < 0.01, f"({bx:.3f},{by:.3f})")
    check("T32 红方出生点 -> 小地图右上(0.98,0.02)",
          abs(rx - 0.98) < 0.01 and abs(ry - 0.02) < 0.01, f"({rx:.3f},{ry:.3f})")
    wx, wz = G.norm_to_world(*G.world_to_norm(1234.5, -9876.5))
    check("T33 世界<->小地图互为逆", abs(wx - 1234.5) < 1e-6 and abs(wz + 9876.5) < 1e-6,
          f"({wx:.1f},{wz:.1f})")
    bnx, bny = BR.world_to_norm(-56379, -53367)
    check("T34 桥接层与视觉同口径（ny 反序）",
          abs(bny - 0.98) < 0.01 and abs(bnx - 0.02) < 0.01, f"({bnx:.3f},{bny:.3f})")
    sx, sz = from_screen = __import__(
        "wzry.decision.official_ai.snapshot", fromlist=["_screen_to_world"]
    )._screen_to_world([0.02, 0.98])
    check("T35 视觉兜底反算回蓝方基地附近",
          sx is not None and sx < -50000 and sz < -50000, f"({sx:.0f},{sz:.0f})")

    # ---- 12. 22 具名槽位 ----
    sc = OS.self_check()
    check("T36 槽位表自检（22/无重复/反解一致/层级单调）", sc["ok"], str(sc))
    check("T37 槽位顺序与 schema.ORGAN_SLOTS 逐字一致",
          tuple(OS.SLOT_ORDER) == tuple(S.ORGAN_SLOTS))
    bld = []
    for _n, (_nx, _ny) in OS.table(1).items():
        _x, _z = G.norm_to_world(_nx, _ny)
        bld.append(Unit("organ", 92 if "tower" in _n else 95,
                        1 if _n.startswith("friend") else 2, _x, _z, 9000, 9000))
    mp, errs = OS.assign(bld, 1)
    check("T38 22 栋真实坐标 -> 22 槽位全命中且无冲突",
          len(mp) == 22 and not errs, f"{len(mp)} {errs[:2]}")
    mid_river = [Unit("organ", 92, 2, *G.norm_to_world(0.5, 0.5), 9000, 9000)]
    mp2, errs2 = OS.assign(mid_river, 1)
    check("T39 不在 22 槽位上的建筑被门限拒绝（不硬塞）",
          len(mp2) == 0 and any(e.startswith("far:") for e in errs2), str(errs2[:1]))

    # ---- 13. organ 特征走槽位序 ----
    st_t = fixture()
    st_t["extra"]["mem_units"] = [
        {"cid": 95, "kind": "turret", "camp": 1, "x": G.norm_to_world(*OS.CRYSTAL_PTS[1])[0],
         "z": G.norm_to_world(*OS.CRYSTAL_PTS[1])[1], "hp": 8000, "hp_max": 9000,
         "dead": False}]
    snap_t = oa.from_state_dict(st_t, atk_range=ATK)
    pack_t = F.build(snap_t)
    organ_spec = S.SPECS["organ"]
    lay_o = {n: st for n, st, _sz in pack_t.index["organ"]}
    first_alive = pack_t.vec["organ"][lay_o["is_alive"]]
    first_hp = pack_t.vec["organ"][lay_o["hp_rate"]]
    check("T40 organ 向量第 1 段固定是 friend_crystal（槽位序，非距离序）",
          abs(first_alive - 1.0) < 1e-9 and 0.85 < first_hp <= 0.9,
          f"alive={first_alive} hp={first_hp:.2f}")
    check("T41 organ 向量长度固定 = 单位内维数 × 24",
          len(pack_t.vec["organ"]) == organ_spec.total_size, f"{len(pack_t.vec['organ'])}")

    # ---- 14. 法力（HUD 视觉兜底）----
    st_mp = fixture()
    st_mp["ui"]["mp"] = 0.31
    snap_mp = oa.from_state_dict(st_mp, atk_range=ATK)
    pack_mp = F.build(snap_mp)
    ep_v = pack_mp.get("self", "ep_rate")
    check("T42 ui.mp -> self.ep_rate", abs((ep_v or 0) - 0.31) < 1e-6, f"{ep_v}")
    st_mp2 = fixture()
    st_mp2["ui"].pop("mp", None)
    snap_np = oa.from_state_dict(st_mp2, atk_range=ATK)
    check("T43 无法力数据时 ep 归零且不报错",
          snap_np.self_ep_rate is None and F.build(snap_np).get("self", "ep_rate") == 0.0)

    # ---- 15. 规则覆盖层（单规则 + 集成）----
    tower_snap = oa.from_state_dict(fixture(hp_ratio=0.62, enemy_dist=600), atk_range=ATK)
    tw_act = next(a for a in A.enumerate_actions(tower_snap)
                  if a.target_kind == A.T_TOWER)
    fin_t, hits_t = R.apply_rules(tower_snap, tw_act)
    check("T44 AvoidKilledByOrgan：0.62 血越塔被否决",
          fin_t.label == "NoneAction"
          and any(h.name == "AvoidKilledByOrgan" and h.verdict == R.VETO
                  for h in hits_t), str([h.name for h in hits_t]))

    cake = Unit("cake", 1, 2, 500.0, 0.0, 100.0, 100.0)
    snap_c = oa.from_state_dict(fixture(hp_ratio=0.8, enemy_dist=600), atk_range=ATK)
    snap_c.cakes.append(cake)
    act_cake = A.Action("AtkCake", "attack", "Cake", cake, 0, meta={"dist": 500.0})
    check("T45 SelectCake：0.8 血不占血包",
          (R.r_select_cake(snap_c, act_cake) or ("", None, ""))[0] == R.VETO)
    snap_cf = oa.from_state_dict(fixture(hp_ratio=0.95, enemy_dist=600), atk_range=ATK)
    snap_cf.cakes.append(cake)
    check("T46 LeaveCake：满血离开血包",
          (R.r_leave_cake(snap_cf, act_cake) or ("", None, ""))[0] == R.VETO)

    snap_j = oa.from_state_dict(fixture(hp_ratio=0.40, enemy_dist=30000), atk_range=ATK)
    mon = Unit("monster", 16100, 2, 900.0, 0.0, 6000, 9000)
    snap_j.monsters.append(mon)
    act_mon = A.Action("AtkMonster", "attack", A.T_MONSTER, mon, 0)
    check("T47 StayWithMonster：残血无援不单开野",
          (R.r_stay_with_monster(snap_j, act_mon) or ("", None, ""))[0] == R.VETO)

    # 阈值来自汇编：`fmov s1,#0.5` 与 cur_hp/max_hp 比较（0x027e557c–0x027e558c）
    st_52 = fixture(hp_ratio=0.52, enemy_dist=30000)
    s52 = oa.from_state_dict(st_52, atk_range=ATK)
    m52 = Unit("monster", 16100, 2, 900.0, 0.0, 6000, 9000)
    s52.monsters.append(m52)
    a52 = A.Action("AtkMonster", "attack", A.T_MONSTER, m52, 0)
    st_48 = fixture(hp_ratio=0.48, enemy_dist=30000)
    s48 = oa.from_state_dict(st_48, atk_range=ATK)
    m48 = Unit("monster", 16100, 2, 900.0, 0.0, 6000, 9000)
    s48.monsters.append(m48)
    a48 = A.Action("AtkMonster", "attack", A.T_MONSTER, m48, 0)
    check("T47b StayWithMonster 阈值 = 0.50（0.52 放行 / 0.48 否决）",
          R.r_stay_with_monster(s52, a52) is None
          and (R.r_stay_with_monster(s48, a48) or ("", None, ""))[0] == R.VETO,
          f"{R.r_stay_with_monster(s52, a52)} / {R.r_stay_with_monster(s48, a48)}")

    # 己方高地被 3 个敌兵压上，自己在远处 -> 回防
    st_h = fixture(hp_ratio=0.9, enemy_dist=30000)
    hx, hz = G.norm_to_world(*OS.LANE_PTS[1]["mid"][2])
    st_h["extra"]["mem_units"] = [
        {"cid": 92, "kind": "turret", "camp": 1, "x": hx, "z": hz,
         "hp": 5000, "hp_max": 9000, "dead": False}] + [
        {"cid": 1690, "kind": "minion", "camp": 2, "x": hx + 100 * i, "z": hz + 50 * i,
         "hp": 400, "hp_max": 1200, "dead": False} for i in range(3)]
    snap_h = oa.from_state_dict(st_h, atk_range=ATK)
    move_act = A.Action("Move0", "move", point=(snap_h.self_x + 500, snap_h.self_z))
    res_h = R.r_atk_soldier_in_frd_high(snap_h, move_act)
    check("T48 AtkSoldierInFrdHigh：高地被压 -> 回防改写",
          res_h is not None and res_h[0] == R.REWRITE and res_h[1].label == "DefensePosition",
          str(res_h[0]) if res_h else "None")

    snap_s = oa.from_state_dict(fixture(hp_ratio=0.8, enemy_dist=600), atk_range=ATK)
    snap_s.stuck_frames = 8
    res_s = R.r_is_stuck_wall(snap_s, move_act)
    check("T49 IsStuckWall：连续 8 帧没走动 -> 换向",
          res_s is not None and res_s[1].label == "StuckBreak",
          str(res_s[0]) if res_s else "None")
    snap_s.stuck_frames = 17
    res_s2 = R.r_long_time_move_stop(snap_s, move_act)
    check("T50 LongTimeMoveStopRule：卡更久 -> 直接换目标点",
          res_s2 is not None and res_s2[1].label == "GridBest",
          str(res_s2[0]) if res_s2 else "None")

    snap_n = oa.from_state_dict(fixture(hp_ratio=0.8, enemy_dist=300), atk_range=ATK)
    res_n = R.r_notarget_optimize(snap_n, A.Action("NoneAction", "none"))
    check("T51 NotargetOptimizeProcess：没动作但敌在射程 -> 补一个普攻",
          res_n is not None and res_n[1].label == "AtkHero",
          res_n[1].label if res_n else "None")

    st_sp = fixture(hp_ratio=0.8, enemy_dist=30000)
    spx, spz = G.norm_to_world(*OS.SPRING_PTS[1])
    st_sp["extra"]["mem_units"] = [
        {"cid": 95, "kind": "turret", "camp": 1, "x": spx, "z": spz,
         "hp": 9000, "hp_max": 9000, "dead": False}]
    st_sp["extra"]["mem_heroes"][0]["x"], st_sp["extra"]["mem_heroes"][0]["z"] = spx, spz
    snap_sp = oa.from_state_dict(st_sp, atk_range=ATK)
    res_sp = R.r_linger_in_spring(snap_sp, A.Action("Move0", "move", point=(0, 0)))
    check("T52 ProcessLingerInSpring：泉水里没回满 -> 先别出去",
          res_sp is not None and res_sp[0] == R.VETO, str(res_sp[0]) if res_sp else "None")

    # 泉水是**方框**（±5000/±4992，证据 stay_in_spring @0x027f2a5c），不是圆
    def _at(dx, dz):
        st = fixture(hp_ratio=0.8, enemy_dist=30000)
        st["extra"]["mem_units"] = []
        st["extra"]["mem_heroes"][0]["x"] = spx + dx
        st["extra"]["mem_heroes"][0]["z"] = spz + dz
        sn = oa.from_state_dict(st, atk_range=ATK)
        return R.r_linger_in_spring(sn, A.Action("Move0", "move", point=(0, 0)))

    inside, outside = _at(4000, 4000), _at(6000, 0)
    check("T52b 泉水方框边界：+4000,+4000 内 / +6000 外",
          inside is not None and inside[0] == R.VETO and outside is None,
          f"{inside[0] if inside else None} / {outside}")

    # ---- 16. 证据分级 / 规则规模 / 场景节拍 ----
    cs = R.chain_stats()
    check("T53 规则链规模：已实现 >=17，其中汇编证据 >=5 条",
          cs["implemented"] >= 17 and cs["disasm"] >= 5,
          f"{cs['implemented']}/{cs['total']} disasm={cs['disasm']}")
    dis_hits = R.apply_rules(tower_snap, tw_act, mode="disasm")[1]
    check("T54 mode=disasm 时不跑名字级规则",
          not any(h.evidence == R.EVID_NAME for h in dis_hits),
          f"{len(dis_hits)} hit")

    tt = [0.0]
    ai_sc = OfficialAI(level="easy", clock=lambda: tt[0])
    d1 = ai_sc.decide(oa.from_state_dict(fixture(hp_ratio=0.2), atk_range=ATK))
    tt[0] = 0.20
    d2 = ai_sc.decide(oa.from_state_dict(fixture(hp_ratio=0.2), atk_range=ATK))
    tt[0] = 0.30
    d3 = ai_sc.decide(oa.from_state_dict(fixture(hp_ratio=0.2), atk_range=ATK))
    check("T55 场景化节拍：残血走 return_city 间隔(0.25s)",
          d1.scene == "return_city" and d2.source == "cached" and d3.source != "cached",
          f"{d1.scene} {d2.source}->{d3.source}")

    # ---- 17. 规则链不破坏既有性能 ----
    ai_p = OfficialAI(level="juewu", clock=lambda: time.monotonic())
    t0 = time.perf_counter()
    for _ in range(30):
        ai_p.decide(oa.from_state_dict(fixture(), atk_range=ATK))
    ms2 = (time.perf_counter() - t0) * 1000 / 30
    check("T56 17 条规则全开单帧仍 < 5ms", ms2 < 5.0, f"{ms2:.2f}ms")

    # ---- 18. 视野裁剪 / 延迟档位一致性 / 红方视角槽位 ----
    st_far = fixture()
    st_far["extra"]["mem_units"] = [
        {"cid": 1690, "kind": "minion", "camp": 2, "x": 400, "z": 100,
         "hp": 900, "hp_max": 1200, "dead": False},
        {"cid": 1691, "kind": "minion", "camp": 2, "x": 3000, "z": 100,
         "hp": 900, "hp_max": 1200, "dead": False}]
    snap_far = oa.from_state_dict(st_far, atk_range=ATK)      # 默认 view_distance=1200
    n_near = F.build(snap_far).n_units.get("soldier")
    snap_far.view_distance = 5000.0
    n_wide = F.build(snap_far).n_units.get("soldier")
    check("T57 view_distance 裁剪：远处兵不进特征，放宽后进",
          n_near == 1 and n_wide == 2 and len(snap_far.minions) == 2,
          f"1200->{n_near} 5000->{n_wide} raw={len(snap_far.minions)}")

    # 端到端：有观测延迟时，规则层拿到的必须是"当前"的卡墙计数/动作历史
    seen = {"stuck": [], "acts": []}
    _orig_apply = R.apply_rules

    def _spy(snap, act, chain=None, mode="all"):
        seen["stuck"].append(snap.stuck_frames)
        seen["acts"].append(list(snap.last_actions))
        return _orig_apply(snap, act, chain=chain, mode=mode)

    tdl = [0.0]
    aid = OfficialAI(level="hard", delay_frames=1, clock=lambda: tdl[0])
    st_mv = fixture(hp_ratio=0.9, enemy_dist=40000)
    st_mv["extra"]["mem_units"] = []        # 无兵无塔 -> 决策必为走位（才能累计"没走动"）
    R.apply_rules = _spy
    try:
        for i in range(4):
            tdl[0] = float(i)          # 每次跨过一个决策间隔，避免命中 cached
            aid.decide(oa.from_state_dict(st_mv, atk_range=ATK))
    finally:
        R.apply_rules = _orig_apply
    ok_delay = (len(seen["stuck"]) >= 2 and aid.stuck_frames >= 1
                and seen["stuck"][-1] == aid.stuck_frames
                and seen["acts"][-1] == list(aid._acts)[:-1])
    check("T58 有观测延迟时，规则层仍拿到当前卡墙计数/动作历史（非 N 帧前旧值）",
          ok_delay, f"seen={seen['stuck']} ai={aid.stuck_frames} "
                    f"acts={seen['acts'][-1]}")

    bld2 = []
    for _n, (_nx, _ny) in OS.table(2).items():
        _x, _z = G.norm_to_world(_nx, _ny)
        bld2.append(Unit("organ", 92 if "tower" in _n else 95,
                         2 if _n.startswith("friend") else 1, _x, _z, 9000, 9000))
    mp3, errs3 = OS.assign(bld2, 2)
    cr = mp3.get("friend_crystal")
    check("T59 红方视角：friend/enemy 前缀随 self_camp 翻转且 22/22",
          len(mp3) == 22 and not errs3 and cr is not None
          and abs(G.world_to_norm(cr.x, cr.z)[0] - OS.CRYSTAL_PTS[2][0]) < 1e-6,
          f"{len(mp3)} {errs3[:1]}")

    # ---- 11. 取数来源审计：内存优先，不先走视觉 ----
    st_mem = fixture()
    s_mem = oa.from_state_dict(st_mem, atk_range=ATK)
    check("T60 纯内存输入：data_src=mem 且不触视觉",
          s_mem.data_src == "mem" and len(s_mem.heroes) == 10
          and len(s_mem.minions) >= 2 and len(s_mem.organs) >= 1,
          f"{s_mem.data_src} h={len(s_mem.heroes)} m={len(s_mem.minions)} o={len(s_mem.organs)}")

    s_rm = oa.from_state_dict(st_mem, atk_range=ATK, require_mem=True)
    check("T61 require_mem=True 有内存时正常产出",
          s_rm.data_src == "mem" and len(s_rm.heroes) == 10, s_rm.data_src)

    st_vis = {"units": [{"cls": "enemy_hero", "screen": [0.6, 0.5], "cid": 107},
                        {"cls": "enemy_turret", "screen": [0.8, 0.3], "cid": 92}]}
    s_vis = oa.from_state_dict(st_vis, atk_range=ATK)
    s_vis2 = oa.from_state_dict(st_vis, atk_range=ATK, require_mem=True)
    check("T62 无内存时默认可退化到视觉，但 require_mem 会拒绝",
          s_vis.data_src == "vision" and s_vis2.data_src == "none"
          and len(s_vis2.heroes) == 0,
          f"{s_vis.data_src}/{s_vis2.data_src}")

    st_mix = dict(fixture())
    st_mix["extra"]["mem_units"] = []
    st_mix["units"] = [{"cls": "enemy_turret", "screen": [0.8, 0.3], "cid": 92}]
    s_mix = oa.from_state_dict(st_mix, atk_range=ATK)
    check("T63 内存+视觉混用会被标记 mixed（便于审计）",
          s_mix.data_src == "mixed", s_mix.data_src)

    # ---- 12. 阵营归一 / 静态几何 / 装备栏 ----
    st_str = fixture()
    for _h in st_str["extra"]["mem_heroes"]:
        _h["camp"] = "blue" if _h["camp"] == 1 else "red"
    for _u in st_str["extra"]["mem_units"]:
        _u["camp"] = "blue" if _u["camp"] == 1 else "red"
    snap_str = oa.from_state_dict(st_str, atk_range=ATK)
    check("T64 内存侧 camp 字符串 -> 内部 int（1/2），几何仍 22 项",
          snap_str.self_camp == 1 and len(snap_str.organ_geo) == 22
          and all(h.camp in (1, 2) for h in snap_str.heroes),
          f"camp={snap_str.self_camp} geo={len(snap_str.organ_geo)}")

    st_nb = fixture()
    st_nb["extra"]["mem_units"] = []
    snap_nb = oa.from_state_dict(st_nb, atk_range=ATK)
    from wzry.decision.official_ai import field as VF2
    eb = VF2.enemy_base(snap_nb)
    check("T65 无活体建筑时仍用静态几何定位敌方基地",
          len(snap_nb.organ_slots) == 0 and len(snap_nb.organ_geo) == 22
          and abs(eb[0] - OS.geo_table(1)["enemy_crystal"][0]) < 1e-6,
          f"geo={len(snap_nb.organ_geo)} base={eb[0]:.0f}")

    st_eq = fixture()
    st_eq["extra"]["mem_heroes"][0]["equip_ids"] = [1424, 1233, 1337]
    check("T66 装备栏：mem_heroes.equip_ids -> snap.self_equip",
          oa.from_state_dict(st_eq, atk_range=ATK).self_equip == [1424, 1233, 1337])
    check("T67 无装备数据时为空列表且不报错",
          oa.from_state_dict(fixture(), atk_range=ATK).self_equip == [])

    # ---- 13. 取数纪律：内存优先，require_mem 下不碰视觉 ----
    st_tw = fixture()
    st_tw["minimap"] = {"found": True, "towers": [[0.032, 0.300], [0.916, 0.415]]}
    s_tw = oa.from_state_dict(st_tw, atk_range=ATK)
    s_tw_strict = oa.from_state_dict(st_tw, atk_range=ATK, require_mem=True)
    check("T70 塔存活只能来自视觉：普通模式标 mixed + src=vision，require_mem 下留未知",
          s_tw.organ_alive_src == "vision" and len(s_tw.organ_alive) == 2
          and s_tw.data_src == "mixed"
          and s_tw_strict.organ_alive == {} and s_tw_strict.organ_alive_src == ""
          and s_tw_strict.data_src == "mem",
          f"{s_tw.data_src}/{s_tw_strict.data_src} n={len(s_tw.organ_alive)}")

    # ---- 12. 复活倒计时：阵亡期不下决策 ----
    st_dead = fixture()
    st_dead["extra"]["mem_heroes"][0]["respawn_s"] = 12
    s_dead = oa.from_state_dict(st_dead, atk_range=ATK)
    d_dead = OfficialAI(level="hard", clock=lambda: 0.0).decide(s_dead)
    check("T68 复活倒计时内：走 [UnpredictProcess] 且标注 respawn",
          d_dead.source == "fallback" and "respawn" in str(
              [h.reason for h in d_dead.hits]),
          f"{d_dead.source} {[h.reason for h in d_dead.hits]}")

    # ---- 20. 血量未知不得当成残血；无坐标单位不得进特征 ----
    from wzry.decision.official_ai.snapshot import Unit as _U
    sc_u = LinearScorer()
    snap_u = oa.from_state_dict(fixture(enemy_dist=400.0), atk_range=ATK)
    known_hi = _U("hero", 200, 2, 400.0, 0.0, 7000, 7000)     # 满血、血量已知
    unknown = _U("hero", 201, 2, 400.0, 0.0, None, None)      # 血量未知
    s_hi = sc_u.score(snap_u, A.Action("AtkHero", "attack", A.T_HERO, known_hi, 0), None)
    s_un = sc_u.score(snap_u, A.Action("AtkHero", "attack", A.T_HERO, unknown, 0), None)
    check("T71 血量未知不享受残血/可击杀加成（不得高于满血已知目标）",
          s_un <= s_hi + 1e-6, f"unknown={s_un:.2f} fullhp={s_hi:.2f}")

    st_unl = fixture()
    for m in st_unl["extra"]["mem_units"]:
        if m.get("kind") == "minion":
            m["x"] = None
            m["z"] = None
    snap_unl = oa.from_state_dict(st_unl, atk_range=ATK)
    pack_unl = F.build(snap_unl)
    n_loc = sum(1 for m in snap_unl.minions if m.x is not None)
    check("T72 无坐标小兵不进特征，且计入 unlocated",
          pack_unl.n_units.get("soldier", 0) == n_loc
          and snap_unl.unlocated.get("soldier", 0) > 0,
          f"feat={pack_unl.n_units.get('soldier')} located={n_loc} "
          f"unlocated={snap_unl.unlocated}")

    st_org = fixture()
    for u in st_org["extra"]["mem_units"]:
        if u.get("kind") in ("turret", "building"):
            u["x"] = None
            u["z"] = None
    snap_org = oa.from_state_dict(st_org, atk_range=ATK)
    pack_org = F.build(snap_org)
    check("T73 建筑无活体坐标时 organ 特征留空（不伪造 (0,0)），几何仍 22 项",
          pack_org.n_units.get("organ", 0) == 0 and len(snap_org.organ_geo) == 22,
          f"organ_n={pack_org.n_units.get('organ')} geo={len(snap_org.organ_geo)}")

    # ---- 21. 真机实测标定（2026-10-21 对局内 24 栋建筑的真实世界坐标） ----
    #   cid 语义：44/46 泉水、21/35 水晶、92/95 基地塔（不在 22 槽位表内）、
    #   1690/1693 一塔、1691/1694 二塔、1696-98（蓝）/1699,16100,16101（红）高地塔。
    REAL = [
        (1, 44, -58900, -58900), (1, 21, -55200, -55100), (1, 92, -44700, -44700),
        (1, 1690, -11849, -9015), (1, 1690, -51800, 21180), (1, 1690, 26000, -50320),
        (1, 1691, -2800, -49800), (1, 1691, -19957, -22637), (1, 1691, -51098, -7132),
        (1, 1696, -49940, -27666), (1, 1697, -34588, -34586), (1, 1698, -27666, -49940),
        (2, 46, 58900, 58900), (2, 35, 55150, 55200), (2, 95, 44700, 44700),
        (2, 1693, 11849, 9015), (2, 1693, -21180, 51800), (2, 1693, 50320, -26000),
        (2, 1694, 2800, 49800), (2, 1694, 51098, 7132), (2, 1694, 19957, 22637),
        (2, 1699, 27600, 49900), (2, 16100, 34588, 34586), (2, 16101, 49950, 27666),
    ]
    real_units = [Unit("organ", cid, camp, float(x), float(z), 9000, 9000)
                  for camp, cid, x, z in REAL]
    mp_real, notes_real = OS.assign_measured(real_units, 1)
    check("T74 实测标定：24 栋 -> 22 槽位（2 栋基地塔不硬塞）",
          len(mp_real) == 22 and sum(1 for n in notes_real if "base_tower" in n) == 2,
          f"{len(mp_real)} notes={notes_real[:2]}")
    check("T75 实测标定：cid 语义正确（泉水/水晶/一塔）",
          mp_real.get("friend_spring_tower") is not None
          and mp_real["friend_spring_tower"].cid == 44
          and mp_real["friend_crystal"].cid == 21
          and mp_real["enemy_crystal"].cid == 35
          and mp_real["friend_top_norm_tower_1st"].cid == 1690
          and mp_real["friend_top_norm_tower_1st"].z == 21180
          and mp_real["friend_bottom_norm_tower_1st"].z == -50320,
          "spring/crystal = {}".format(
              [mp_real.get(k) and mp_real[k].cid
               for k in ("friend_spring_tower", "friend_crystal", "enemy_crystal")]))
    check("T76 实测标定：高地塔分路正确",
          mp_real["friend_top_high_tower"].cid == 1696
          and mp_real["friend_mid_high_tower"].cid == 1697
          and mp_real["friend_bottom_high_tower"].cid == 1698
          and mp_real["enemy_top_high_tower"].cid == 1699,
          " ".join(f"{k}={mp_real[k].cid}" for k in
                   ("friend_top_high_tower", "friend_mid_high_tower",
                    "friend_bottom_high_tower", "enemy_top_high_tower")))

    # ---- 13. 2026-10-21 新增三条规则 ----
    # IsInEnemyHoverArea：2 敌 0 友，在 1499 内 -> 撤回
    st_h = fixture(hp_ratio=0.9, enemy_dist=600)
    hs = st_h["extra"]["mem_heroes"]
    hs[1].update({"x": 700, "z": 0})          # 107
    hs[2].update({"x": -600, "z": 400})       # 121 也拉近（但它是红方）
    hs[2]["camp"] = 2
    s_h = oa.from_state_dict(st_h, atk_range=ATK)
    atk_h = next(a for a in A.enumerate_actions(s_h) if a.kind in ("attack", "skill"))
    fin_h, hits_h = R.apply_rules(s_h, atk_h)
    check("T80 IsInEnemyHoverArea：2v0 被包夹 -> 撤回",
          fin_h.label in ("Retreat", "ReturnCity")
          and any(x.name == "IsInEnemyHoverArea" for x in hits_h),
          f"{fin_h.label} {[x.name for x in hits_h]}")

    # QuitMovePosWhenArrive：目标点 100 内 -> 停
    s_a = oa.from_state_dict(fixture(), atk_range=ATK)
    near_pt = A.Action("MoveX", "move", point=(s_a.self_x + 100, s_a.self_z))
    fin_a, hits_a = R.apply_rules(s_a, near_pt)
    check("T81 QuitMovePosWhenArrive：到达 100 内 -> Stand",
          fin_a.label == "Stand"
          and any(x.name == "QuitMovePosWhenArrive" for x in hits_a), fin_a.label)

    # StayWithMonsterUsingSkill：打野 + 技能就绪 -> 用技能
    st_m = fixture(hp_ratio=0.95)
    st_m["extra"]["mem_units"] = [{"cid": 16100, "kind": "monster", "camp": 2,
                                   "x": 300, "z": 0, "hp": 6000, "hp_max": 9000,
                                   "dead": False}]
    s_m = oa.from_state_dict(st_m, atk_range=ATK)
    mon_act = next(a for a in A.enumerate_actions(s_m) if a.target_kind == A.T_MONSTER)
    fin_m, hits_m = R.apply_rules(s_m, mon_act)
    check("T82 StayWithMonsterUsingSkill：打野时技能就绪 -> 用技能",
          fin_m.kind == "skill" and fin_m.skill_slot >= 1
          and any(x.name == "StayWithMonsterUsingSkill" for x in hits_m),
          f"{fin_m.label}")

    # ---- 14. 锁敌过滤器 ----
    from wzry.decision.official_ai import targeting as TG
    from wzry.decision.official_ai import motion as MO
    from wzry.decision.official_ai import lanes as LN
    from wzry.decision.official_ai import tactics as TC

    s_t = oa.from_state_dict(fixture(hp_ratio=0.9, enemy_dist=700), atk_range=ATK)
    f_def = TG.TargetFilter()
    best = TG.select_target(s_t, f_def)
    check("T83 锁敌：优先敌方英雄且按距离取最近",
          best is not None and best.cid == 107, str(getattr(best, "cid", None)))
    f_only = TG.TargetFilter(kinds=("monster",), max_dist=99999)
    check("T84 锁敌：类型过滤生效",
          TG.select_target(s_t, f_only) is None or
          TG.select_target(s_t, f_only).kind == "monster",
          str(TG.select_target(s_t, f_only)))
    f_black = TG.TargetFilter(blacklist=(107,))
    check("T85 锁敌：黑名单生效",
          (TG.select_target(s_t, f_black) or best).cid != 107,
          str(getattr(TG.select_target(s_t, f_black), "cid", None)))

    # ---- 15. 输出后处理 ----
    s_p = oa.from_state_dict(fixture(), atk_range=ATK)
    mv = A.Action("M", "move", point=(s_p.self_x + 3000, s_p.self_z + 1000))
    out_mv = MO.apply_post(s_p, mv)
    d = ((out_mv.point[0] - s_p.self_x) ** 2 + (out_mv.point[1] - s_p.self_z) ** 2) ** 0.5
    deg = float(out_mv.meta.get("deg", 0.0))
    # 量化后再叠场景偏差（±MAX_DEVIATION），所以允许偏差内的非整数倍
    resid = min(abs(deg % MO.STEP_DEG), MO.STEP_DEG - abs(deg % MO.STEP_DEG))
    check("T86 后处理：单步限幅 + 方向量化(±场景偏差)",
          d <= MO.MAX_SINGLE_MOVE + 1e-6 and resid <= MO.MAX_DEVIATION + 0.01,
          f"d={d:.0f} deg={deg} resid={resid:.1f}")

    # ---- 16. 分路优先级 ----
    plan = LN.plan_for("marksman")
    check("T87 分路：射手优先发育路(bottom)",
          plan.order[0] == "bottom" and plan.weights["bottom"] > plan.weights["top"],
          str(plan.order))

    # ---- 17. 战术配置 schema ----
    ok1, bad1 = TC.validate({"grid_tactics_state": 3, "arrived_strategy_target": 1,
                             "enemy_hero_min_dis": 800})
    ok2, bad2 = TC.validate({"grid_tactics_state": 99, "nope": 1})
    check("T88 战术 schema：合法维度通过 / 越界与未知维度被拒",
          ok1 and not ok2 and len(bad2) >= 2, f"{bad1} {bad2}")

    # ---- 18. 新增两条规则 ----
    st_d = fixture(hp_ratio=0.9)
    st_d["extra"]["mem_units"] = [{"cid": 16100, "kind": "monster", "camp": 2,
                                   "x": 400, "z": 0, "hp": 6000, "hp_max": 9000,
                                   "dead": False}]
    st_d["extra"]["mem_heroes"][1].update({"x": 500, "z": 0})   # 敌英雄在 301 内
    s_d = oa.from_state_dict(st_d, atk_range=ATK)
    st_d["extra"]["mem_heroes"][1].update({"x": 250, "z": 0})   # 敌英雄 250 < 301
    s_d = oa.from_state_dict(st_d, atk_range=ATK)
    mon = next(a for a in A.enumerate_actions(s_d) if a.target_kind == A.T_MONSTER)
    # 直接测该规则（链上会被 IsInDangerousPlace 先短路，那是另一条规则的行为）
    res_d = R.r_is_linger_around_dragon(s_d, mon)
    check("T89 IsLingerAroundDragon：敌人眼皮下不打龙",
          res_d is not None and res_d[0] == "VETO",
          str(res_d[0] if res_d else None))

    st_h2 = fixture(hp_ratio=0.25)
    st_h2["extra"]["mem_heroes"][0].update({"x": 30000, "z": 30000})
    s_h2 = oa.from_state_dict(st_h2, atk_range=ATK)
    s_h2.self_x, s_h2.self_z = 30000, 30000
    s_h2.organ_geo = {"enemy_mid_high_tower": (30100.0, 30000.0)}
    atk2 = next(a for a in A.enumerate_actions(s_h2) if a.kind in ("attack", "skill"))
    res_h2 = R.r_return_city_in_emy_high(s_h2, atk2)
    check("T90 ReturnCityBetween2TowerInEmyHigh：高地深处残血回城",
          res_h2 is not None and res_h2[0] == "REWRITE"
          and res_h2[1].label == "ReturnCity",
          str(res_h2[0] if res_h2 else None))

    # ---- 19. 泉水三条规则 ----
    st_s = fixture(hp_ratio=0.5)
    s_s = oa.from_state_dict(st_s, atk_range=ATK)
    s_s.organ_geo = {"friend_spring_tower": (s_s.self_x, s_s.self_z)}
    mv_s = A.Action("M", "move", point=(s_s.self_x + 800, s_s.self_z))
    res_s = R.r_goto_target_pos_in_spring(s_s, mv_s)
    check("T91 GotoTargetPosInSpring：泉内未满血 -> 原地休整",
          res_s is not None and res_s[0] == "REWRITE"
          and res_s[1].label == "Stand", str(res_s[0] if res_s else None))

    st_r = fixture(hp_ratio=0.9)
    st_r["extra"]["mem_heroes"][0]["respawn_s"] = 8
    s_r = oa.from_state_dict(st_r, atk_range=ATK)
    R.r_move_stop_in_spring_when_revival(s_r, A.Action("M", "move",
                                                       point=(0, 0)))   # 记下复活帧
    s_r2 = oa.from_state_dict(st_r, atk_range=ATK)
    s_r2.self_unit.respawn_s = 0
    s_r2.frame = s_r.frame + 2
    res_r = R.r_move_stop_in_spring_when_revival(s_r2, A.Action("M", "move",
                                                               point=(0, 0)))
    check("T92 MoveStopInSpringWhenRevival：复活 6 帧内不动",
          res_r is not None and res_r[0] == "REWRITE", str(res_r[0] if res_r else None))

    st_l = fixture(hp_ratio=0.9)
    s_l = oa.from_state_dict(st_l, atk_range=ATK)
    s_l.last_actions = ["AtkHero", "AtkHeroSkill1", "AtkHero", "AtkHero",
                        "AtkHeroSkill2", "AtkHero"]
    atk_l = next(a for a in A.enumerate_actions(s_l) if a.kind in ("attack", "skill"))
    res_l = R.r_move_pos_when_long_time_only_atk(s_l, atk_l)
    check("T93 MovePosWhenLongTimeOnlyAtk：连攻 6 步且健康 -> 换位",
          res_l is not None and res_l[0] == "REWRITE"
          and res_l[1].kind == "move", str(res_l[0] if res_l else None))

    # ---- 20. 危险区撤退 / 半场判定 / 召唤师 CD 特征 ----
    st_dr = fixture(hp_ratio=0.8, enemy_dist=250)
    st_dr["extra"]["mem_units"] = []
    s_dr = oa.from_state_dict(st_dr, atk_range=ATK)
    mv_dr = A.Action("M", "move", point=(s_dr.self_x + 2000, s_dr.self_z))
    res_dr = R.r_dangerous_place_retreat(s_dr, mv_dr)
    check("T94 DangerousPlaceRetreat：301 内的普通走位 -> 改为撤退",
          res_dr is not None and res_dr[0] == "REWRITE"
          and res_dr[1].label == "Retreat", str(res_dr[0] if res_dr else None))

    s_camp = oa.from_state_dict(fixture(), atk_range=ATK)
    s_camp.self_camp = 1
    check("T95 IsInMainHeroCamp：蓝方负 x 为自家半场",
          TG.in_main_camp(s_camp, -1000.0) and not TG.in_main_camp(s_camp, 1000.0),
          f"{TG.in_main_camp(s_camp, -1000.0)}/{TG.in_main_camp(s_camp, 1000.0)}")

    st_cd = fixture()
    st_cd["extra"]["mem_heroes"][0]["skills"] = [
        {"slot": 1, "id": 10610, "level": 1, "ready": True, "cd_left_ms": 0, "cd_total_ms": 5000},
        {"slot": 5, "id": 90003, "level": 1, "role": "heal", "ready": False,
         "cd_left_ms": 30000, "cd_total_ms": 60000},
        {"slot": 6, "id": 80115, "level": 1, "role": "summoner", "ready": False,
         "cd_left_ms": 90000, "cd_total_ms": 120000},
    ]
    s_cd = oa.from_state_dict(st_cd, atk_range=ATK)
    pk = F.build(s_cd)
    check("T96 特征含召唤师/治疗 CD（7 槽数据进 self 向量）",
          pk.get("self", "summoner_cd") is not None
          and pk.get("self", "heal_cd") is not None, "")

    # ---- 21. 召唤师技能链路（治疗）----
    st_hl = fixture(hp_ratio=0.30)
    st_hl["extra"]["mem_heroes"][0]["skills"] = [
        {"slot": 1, "id": 10610, "level": 1, "ready": True, "cd_left_ms": 0, "cd_total_ms": 5000},
        {"slot": 5, "id": 90003, "level": 1, "role": "heal", "ready": True,
         "cd_left_ms": 0, "cd_total_ms": 60000},
    ]
    s_hl = oa.from_state_dict(st_hl, atk_range=ATK)
    s_hl.frame = 100
    R._HEAL_MARK["frame"] = -10 ** 9
    act_hl = next(a for a in A.enumerate_actions(s_hl) if a.kind in ("attack", "skill"))
    res_hl = R.r_check_use_zhiliao(s_hl, act_hl)
    check("T97 CheckUseZhiliao：残血 + 治疗就绪 -> 放治疗",
          res_hl is not None and res_hl[0] == "REWRITE"
          and res_hl[1].kind == "summoner", str(res_hl[0] if res_hl else None))

    from wzry.decision.official_ai import bridge as BR
    leg = BR.to_legacy_action(res_hl[1], s_hl) if res_hl else {}
    check("T98 桥接：summoner 动作 -> {type:SummonerSkill}",
          leg.get("type") == "SummonerSkill", str(leg))

    from wzry.action import operate_commands as OC
    check("T99 执行层：SummonerSkill 已注册且别名可解析",
          "SummonerSkill" in [e.value for e in OC.OperateType]
          and OC.OperateType.SUMMONER_SKILL in OC.COMMAND_SUPPORT
          and OC.normalize_command("summoner") is OC.OperateType.SUMMONER_SKILL, "")

    # ---- 22. 闪现时机过滤器 ----
    s_far = oa.from_state_dict(fixture(hp_ratio=0.8, enemy_dist=40000), atk_range=ATK)
    sum_act = A.Action("UseFlash", "summoner", meta={"slot": 6})
    res_far = R.r_stop_shanxian_uncorrect_timing(s_far, sum_act)
    check("T100 闪现：附近无敌人 -> 否决",
          res_far is not None and res_far[0] == "VETO"
          and res_far[2] == "not_enemy_around", str(res_far[2] if res_far else None))

    s_near = oa.from_state_dict(fixture(hp_ratio=0.8, enemy_dist=500), atk_range=ATK)
    res_near = R.r_stop_shanxian_uncorrect_timing(s_near, sum_act)
    check("T101 闪现：敌人在 1500 内 -> 放行",
          res_near is None, str(res_near))

    # ---- 23. 闪现方向差 89° ----
    s_dir = oa.from_state_dict(fixture(hp_ratio=0.8, enemy_dist=500), atk_range=ATK)
    # 敌人 +x 方向（主移动方向 0°），闪现目标指向 -x（180°）-> 差 180 > 89
    flash_bad = A.Action("UseFlash", "summoner",
                         meta={"mode": "drag",
                               "target": (s_dir.self_x - 800, s_dir.self_z)})
    res_bad = R.r_stop_shanxian_uncorrect_timing(s_dir, flash_bad)
    check("T102 闪现：方向差 180° -> 否决（阈值 89）",
          res_bad is not None and res_bad[0] == "VETO"
          and "deg_diff" in res_bad[2], str(res_bad[2] if res_bad else None))

    flash_ok = A.Action("UseFlash", "summoner",
                        meta={"mode": "drag",
                              "target": (s_dir.self_x + 800, s_dir.self_z + 60)})
    res_ok = R.r_stop_shanxian_uncorrect_timing(s_dir, flash_ok)
    check("T103 闪现：同向 -> 放行", res_ok is None, str(res_ok))

    leg_flash = BR.to_legacy_action(flash_bad, s_dir)
    check("T104 桥接：只有世界坐标时退化为 tap（不乱拖）",
          leg_flash.get("mode") == "tap" and "no_frame_target" in leg_flash.get("reason", ""),
          str(leg_flash))
    flash_fr = A.Action("UseFlash", "summoner",
                        meta={"mode": "drag", "target_frame": (900, 500)})
    leg_fr = BR.to_legacy_action(flash_fr, s_dir)
    check("T105 桥接：给了帧坐标才透传 drag/target",
          leg_fr.get("mode") == "drag" and leg_fr.get("target") == (900, 500),
          str(leg_fr))

    # ---- 24. 召唤师技能识别（summoner.py）----
    from wzry.decision.official_ai import summoner as SM
    from wzry.decision.official_ai import grid as GD
    from wzry.decision.official_ai import field as VF2
    check("T106 召唤师识别：实测 id 优先，30s CD 兜底判惩击",
          SM.role_of(80115) == "flash" and SM.role_of(90003) == "heal"
          and SM.role_of(99999, 30000) == "smite"
          and SM.role_of(99999, 120000) is None,
          f"{SM.role_of(80115)}/{SM.role_of(90003)}/{SM.role_of(99999, 30000)}")

    # ---- 25. 惩击 / 寒冰惩击 ----
    st_cj = fixture(hp_ratio=0.9)
    st_cj["extra"]["mem_heroes"][0]["skills"] = [
        {"slot": 1, "id": 10610, "level": 1, "ready": True, "cd_left_ms": 0, "cd_total_ms": 5000},
        {"slot": 6, "id": 80120, "level": 1, "role": "summoner", "ready": True,
         "cd_left_ms": 0, "cd_total_ms": 30000},          # 召唤师号段 + 30s CD -> 判为惩击
    ]
    st_cj["extra"]["mem_units"] = [
        {"cid": 16100, "kind": "monster", "camp": 2, "x": 700, "z": 300,
         "hp": 2000, "hp_max": 9000, "dead": False},
    ]
    s_cj = oa.from_state_dict(st_cj, atk_range=ATK)
    s_cj.frame = 500
    R._SMITE_MARK["frame"] = -10 ** 9
    res_cj = R.r_use_chengji(s_cj, A.Action("None", "none"))
    check("T107 UseChengji：野怪进斩杀线 -> 惩击（monster 分支）",
          res_cj is not None and res_cj[0] == "REWRITE"
          and res_cj[1].kind == "summoner" and "monster" in res_cj[1].label.lower(),
          str(res_cj[2] if res_cj else None))

    s_cj2 = oa.from_state_dict(st_cj, atk_range=ATK)
    s_cj2.frame = 505                       # 只过 5 帧 -> 15 帧防连发
    res_cj2 = R.r_use_chengji(s_cj2, A.Action("None", "none"))
    check("T108 UseChengji：15 帧内不重复惩击",
          res_cj2 is None, str(res_cj2))

    s_cj3 = oa.from_state_dict(st_cj, atk_range=ATK)
    s_cj3.frame = 600
    R._SMITE_MARK["frame"] = -10 ** 9
    res_cj3 = R.r_use_hanbing_chengji(s_cj3, A.Action("None", "none"))
    check("T109 UseHanbingChengji：寒冰惩击射程更远，同一目标可打",
          res_cj3 is not None and res_cj3[0] == "REWRITE"
          and res_cj3[1].label == "UseHanbingChengji",
          str(res_cj3[2] if res_cj3 else None))

    # ---- 26. 传送 ----
    st_tp = fixture(hp_ratio=0.9, enemy_dist=40000)
    st_tp["extra"]["mem_heroes"][0]["skills"] = [
        {"slot": 6, "id": 88888, "level": 1, "role": "summoner", "ready": True,
         "cd_left_ms": 0, "cd_total_ms": 75000},
    ]
    st_tp["extra"]["mem_units"] = []
    s_tp = oa.from_state_dict(st_tp, atk_range=ATK)
    R._SMITE_MARK["frame"] = -10 ** 9
    res_tp0 = R.r_use_chuansong_skill(s_tp, A.Action("None", "none"))
    SM.KNOWN_IDS[88888] = "teleport"        # 模拟"真机确认了传送 id"
    try:
        s_tp2 = oa.from_state_dict(st_tp, atk_range=ATK)
        s_tp2.organ_geo = {
            "friend_top_tower": (-20000.0, 20000.0),
            "friend_bottom_tower": (-20000.0, -20000.0),
            "friend_crystal": (-50000.0, -50000.0),
            "enemy_crystal": (50000.0, 50000.0),
        }
        s_tp2.self_x, s_tp2.self_z = 0.0, 0.0
        s_tp2.minions = [A.Unit(kind="minion", cid=1690, camp=2,
                                x=-20000.0, z=20000.0, hp=500, hp_max=1200)
                         for _ in range(3)]
        res_tp1 = R.r_use_chuansong_skill(s_tp2, A.Action("None", "none"))
    finally:
        SM.KNOWN_IDS.pop(88888, None)
    check("T110 传送：未配置 id 时惰性（不乱交召唤师技能）", res_tp0 is None, str(res_tp0))
    check("T111 传送：配置后选兵线压力最大的己方塔",
          res_tp1 is not None and res_tp1[0] == "REWRITE"
          and res_tp1[1].meta.get("slot_name") == "friend_top_tower",
          str(res_tp1[2] if res_tp1 else None))

    # ---- 27. 无目标技能优化 ----
    st_nt = fixture(hp_ratio=0.9, enemy_dist=30000)
    s_nt = oa.from_state_dict(st_nt, atk_range=ATK)
    gone = A.Unit(kind="hero", cid=107, camp=2, x=50000.0, z=50000.0,
                  hp=800, hp_max=4000)          # 目标已远出射程
    act_nt = A.Action("AtkHeroSkill1", "skill", A.T_HERO, gone, 1)
    res_nt = R.r_optimize_pos_skill_when_notarget(s_nt, act_nt)
    check("T112 OptimizePosSkillWhenNotarget：目标出射程 -> 换备选目标",
          res_nt is not None and res_nt[0] == "REWRITE"
          and res_nt[1].meta.get("backup") is True,
          str(res_nt[2] if res_nt else None))

    s_nt2 = oa.from_state_dict(fixture(hp_ratio=0.9, enemy_dist=40000), atk_range=ATK)
    s_nt2.minions, s_nt2.monsters = [], []
    res_nt2 = R.r_optimize_pos_skill_when_notarget(s_nt2, act_nt)
    check("T113 OptimizePosSkillWhenNotarget：无备选 -> VETO 这次技能",
          res_nt2 is not None and res_nt2[0] == "VETO",
          str(res_nt2[2] if res_nt2 else None))

    res_dir = R.r_optimize_dir_skill_when_notarget(
        s_nt, A.Action("AtkHeroSkill1", "skill", A.T_HERO, None, 1))
    check("T114 OptimizeDirSkillWhenNotarget：无目标技能 -> 否决（本层无定向施法）",
          res_dir is not None and res_dir[0] == "VETO"
          and res_dir[2].startswith("reset_dir_no_target"),
          str(res_dir[2] if res_dir else None))

    # ---- 28. GenMovePos 的 144 格解码 ----
    s_g = oa.from_state_dict(fixture(hp_ratio=1.0, enemy_dist=800), atk_range=ATK)
    cells = GD.cells(s_g)
    gm = GD.gen_move_pos(s_g)
    check("T115 GenMovePos：12×12=144 格解码 + 距离/障碍校验",
          GD.GRID_N * GD.GRID_N == 144 and len(cells) == 144
          and gm is not None and gm["dist"] <= GD.MAX_MOVE_DIST,
          f"cells={len(cells)} best={gm and gm['point']}")

    # ---- 29. GetRetreatPos：塔 vs 水晶 ----
    s_rp = oa.from_state_dict(fixture(hp_ratio=0.5, enemy_dist=3000), atk_range=ATK)
    s_rp.organ_geo = {
        "friend_crystal": (-50000.0, -50000.0),
        "friend_mid_2nd_tower": (-8000.0, -8000.0),
        "enemy_crystal": (50000.0, 50000.0),
    }
    s_rp.organ_alive = {}
    s_rp.organs = []                                  # 清掉活体塔，隔离到静态几何路径
    # 在塔的**外侧**（离水晶比塔离水晶更远）-> 退到塔
    s_rp.self_x, s_rp.self_z = 0.0, 0.0
    p_tower = VF2.retreat_pos(s_rp)
    # 已经退到塔**后方**（离水晶比塔更近）-> 直接回水晶
    s_rp.self_x, s_rp.self_z = -12000.0, -12000.0
    p_crystal = VF2.retreat_pos(s_rp)
    check("T116 GetRetreatPos：远则退塔、近则回水晶",
          p_tower == (-8000.0, -8000.0) and p_crystal == (-50000.0, -50000.0),
          f"{p_tower} / {p_crystal}")

    # ---- 30. 规则链规模 ----
    cs = R.chain_stats()
    check("T117 规则链 37/38（第五、六批共 9 条已注册）",
          cs["implemented"] == 37 and cs["total"] == 38, str(cs))

    # ---- 31. 召唤师技能识别不许误判英雄技能 ----
    st_fp = fixture(hp_ratio=0.9)
    st_fp["extra"]["mem_heroes"][0]["skills"] = [
        {"slot": 1, "id": 10610, "level": 1, "ready": True, "cd_left_ms": 0, "cd_total_ms": 5000},
        {"slot": 3, "id": 10630, "level": 1, "ready": True, "cd_left_ms": 0, "cd_total_ms": 30000},
    ]
    s_fp = oa.from_state_dict(st_fp, atk_range=ATK)
    check("T118 英雄技能（含 30s CD 的大招）不被误判成惩击",
          SM.slots_of(s_fp.self_unit) == [] and not SM.is_smite(s_fp.self_unit),
          str(SM.slots_of(s_fp.self_unit)))

    # ---- 32. IsInSavePlace：已在安全点就别再往家跑 ----
    s_sp = oa.from_state_dict(fixture(hp_ratio=0.25, enemy_dist=30000), atk_range=ATK)
    s_sp.organ_geo = {"friend_mid_1st_tower": (0.0, 0.0),
                      "friend_crystal": (-50000.0, -50000.0),
                      "enemy_crystal": (50000.0, 50000.0)}
    s_sp.self_x, s_sp.self_z = 100.0, 100.0          # 塔下
    rc = A.Action("ReturnCity", "move", point=(-50000.0, -50000.0))
    res_sp = R.r_is_in_save_place(s_sp, rc)
    s_sp.self_x, s_sp.self_z = 20000.0, 20000.0      # 远离
    res_sp2 = R.r_is_in_save_place(s_sp, rc)
    check("T119 IsInSavePlace：塔下否决回城、野外放行",
          res_sp is not None and res_sp[0] == "REWRITE"
          and res_sp[1].label == "Stand" and res_sp2 is None,
          f"{res_sp and res_sp[2]} / {res_sp2}")

    # ---- 33. IsNowEPTooLow：蓝低不打野，但附近有敌人例外 ----
    st_ep = fixture(hp_ratio=0.9, enemy_dist=40000)
    st_ep["extra"]["mem_heroes"][0].update({"ep": 400, "ep_max": 4000})   # 10%
    st_ep["extra"]["mem_units"] = [
        {"cid": 16100, "kind": "monster", "camp": 2, "x": 600, "z": 200,
         "hp": 8000, "hp_max": 9000, "dead": False},
    ]
    s_ep = oa.from_state_dict(st_ep, atk_range=ATK)
    mon_act2 = next(a for a in A.enumerate_actions(s_ep) if a.target_kind == A.T_MONSTER)
    res_ep = R.r_is_now_ep_too_low(s_ep, mon_act2)
    st_ep2 = fixture(hp_ratio=0.9, enemy_dist=200)
    st_ep2["extra"]["mem_heroes"][0].update({"ep": 400, "ep_max": 4000})
    st_ep2["extra"]["mem_units"] = st_ep["extra"]["mem_units"]
    s_ep2 = oa.from_state_dict(st_ep2, atk_range=ATK)
    mon_act3 = next(a for a in A.enumerate_actions(s_ep2) if a.target_kind == A.T_MONSTER)
    res_ep2 = R.r_is_now_ep_too_low(s_ep2, mon_act3)
    check("T120 IsNowEPTooLow：蓝低不打野；附近有敌人则例外",
          res_ep is not None and res_ep[0] == "VETO" and res_ep2 is None,
          f"{res_ep and res_ep[2]} / {res_ep2}")

    # ---- 34. GetHeroSpeedUpSkill：撤退时交闪现拉开 ----
    st_su = fixture(hp_ratio=0.3, enemy_dist=400)
    st_su["extra"]["mem_heroes"][0]["skills"] = [
        {"slot": 6, "id": 80115, "level": 1, "role": "summoner", "ready": True,
         "cd_left_ms": 0, "cd_total_ms": 120000},
    ]
    s_su = oa.from_state_dict(st_su, atk_range=ATK)
    res_su = R.r_get_hero_speed_up_skill(s_su, A.Action("Retreat", "move", point=(-3000, 0)))
    s_su2 = oa.from_state_dict(fixture(hp_ratio=0.3, enemy_dist=5000), atk_range=ATK)
    res_su2 = R.r_get_hero_speed_up_skill(s_su2, A.Action("Retreat", "move", point=(-3000, 0)))
    check("T121 GetHeroSpeedUpSkill：近敌撤退交闪现、远敌不交",
          res_su is not None and res_su[0] == "REWRITE"
          and res_su[1].meta.get("role") == "flash" and res_su2 is None,
          f"{res_su and res_su[2]} / {res_su2}")

    # ---- 35. CheckUseZhongjie：未配置 id 时惰性；配置后收割残血 ----
    st_zj = fixture(hp_ratio=0.9, enemy_dist=600)
    st_zj["extra"]["mem_heroes"][1].update({"hp": 600, "hp_max": 4000})   # 敌 15%
    st_zj["extra"]["mem_heroes"][0]["skills"] = [
        {"slot": 6, "id": 88887, "level": 1, "role": "summoner", "ready": True,
         "cd_left_ms": 0, "cd_total_ms": 60000},
    ]
    s_zj = oa.from_state_dict(st_zj, atk_range=ATK)
    s_zj.frame = 900
    R._ZHONGJIE_MARK.update({"frame": -10 ** 9, "cnt": 0})
    res_zj0 = R.r_check_use_zhongjie(s_zj, A.Action("None", "none"))
    SM.KNOWN_IDS[88887] = "zhongjie"
    try:
        s_zj2 = oa.from_state_dict(st_zj, atk_range=ATK)
        s_zj2.frame = 900
        res_zj1 = R.r_check_use_zhongjie(s_zj2, A.Action("None", "none"))
    finally:
        SM.KNOWN_IDS.pop(88887, None)
    check("T122 CheckUseZhongjie：未配置惰性 / 配置后收割残血",
          res_zj0 is None and res_zj1 is not None and res_zj1[0] == "REWRITE"
          and res_zj1[1].label == "UseZhongjie",
          f"{res_zj0} / {res_zj1 and res_zj1[2]}")

    # ---- 36. CalTowerDirValue：方向朝塔才加分 ----
    s_td = oa.from_state_dict(fixture(hp_ratio=1.0, enemy_dist=40000), atk_range=ATK)
    s_td.organ_geo = {"enemy_mid_1st_tower": (5000.0, 0.0),
                      "enemy_crystal": (50000.0, 50000.0)}
    s_td.organs = []
    s_td.self_x, s_td.self_z = 0.0, 0.0
    v_toward = VF2.tower_dir_value(s_td, 3000.0, 0.0)
    v_away = VF2.tower_dir_value(s_td, -3000.0, 0.0)
    check("T123 CalTowerDirValue：朝塔 1.0、背塔 0.0",
          abs(v_toward - 1.0) < 1e-6 and v_away == 0.0, f"{v_toward:.2f}/{v_away:.2f}")

    # ---- 37. 法力：内存绝对值优先，HUD 蓝条只作回退 ----
    st_mp = fixture()
    st_mp["self_state"] = {"cid": 106, "mp": 450, "mp_max": 1000}
    st_mp["ui"]["mp"] = 0.9          # 故意给错的 HUD 蓝条，验证内存优先
    snap_mp = oa.from_state_dict(st_mp, atk_range=ATK)
    me_mp = snap_mp.self_unit
    check("T124 法力走内存绝对值（ep/ep_max 不被比例覆盖）",
          me_mp.ep == 450.0 and me_mp.ep_max == 1000.0
          and abs((me_mp.ep_rate or 0) - 0.45) < 1e-9,
          f"ep={me_mp.ep}/{me_mp.ep_max} rate={me_mp.ep_rate}")
    check("T125 内存与 HUD 冲突时以内存为准",
          abs((snap_mp.self_ep_rate or 0) - 0.45) < 1e-9,
          f"rate={snap_mp.self_ep_rate}（HUD 给的是 0.9）")

    st_hud = fixture()
    st_hud["ui"]["mp"] = 0.3
    snap_hud = oa.from_state_dict(st_hud, atk_range=ATK)
    me_hud = snap_hud.self_unit
    check("T126 无法力内存时退回 HUD 比例（ep_max=1 不伪造绝对值）",
          abs((me_hud.ep_rate or 0) - 0.3) < 1e-9 and me_hud.ep_max == 1.0,
          f"ep={me_hud.ep}/{me_hud.ep_max} rate={me_hud.ep_rate}")

    from wzry.decision.official_ai import features as FE
    pack_mp = FE.build(snap_mp)
    v_ep = pack_mp.get("self", "ep")
    v_rate = pack_mp.get("self", "ep_rate")
    check("T127 特征列 ep/ep_rate 均为比例且取自内存",
          v_ep is not None and abs(v_ep - 0.45) < 1e-6
          and v_rate is not None and abs(v_rate - 0.45) < 1e-6,
          f"ep={v_ep} rate={v_rate}")

    # ---- 38. 装备 ID 表与复活甲分支 ----
    from wzry.decision.official_ai import equips as EQ
    tbl = EQ.load()
    check("T128 装备表可加载且复活甲 ID 唯一",
          len(tbl.get("items") or {}) >= 100
          and EQ.revive_ids() == {1337}
          and EQ.name_of(1337) == "贤者的庇护",
          f"n={len(tbl.get('items') or {})} revive={EQ.revive_ids()}")
    check("T129 is_revive_armor 只认复活甲",
          EQ.is_revive_armor([1422, 1337]) and not EQ.is_revive_armor([1422, 1137])
          and not EQ.is_revive_armor([]) and not EQ.is_revive_armor(None),
          f"{EQ.names([1422, 1337])}")

    st_eq = fixture()
    for h in st_eq["extra"]["mem_heroes"]:
        if h.get("id") == 106:
            h["equip_ids"] = [1422, 1337, 1137]
    snap_eq = oa.from_state_dict(st_eq, atk_range=ATK)
    check("T130 快照从装备表推出复活甲可用",
          snap_eq.revive_armor_usable is True and 1337 in snap_eq.self_equip,
          f"equip={snap_eq.self_equip} revive={snap_eq.revive_armor_usable}")

    from wzry.decision import official_rules as ORL
    no_ret, why = ORL.return_city_decision(hp=0.9, revive_armor_usable=True,
                                           strategy_return=True)
    check("T131 有复活甲时否决策略回城（官方 not return 分支）",
          no_ret is False and why == ORL.RETURN_CITY_FUHUOJIA, f"{no_ret}/{why}")
    ret_low, why_low = ORL.return_city_decision(hp=0.1, revive_armor_usable=True)
    check("T132 复活甲不拦低血回城（官方顺序：hp 门槛在前）",
          ret_low is True, f"{ret_low}/{why_low}")

    print(f"\n结果: {PASS}/{PASS + FAIL} 通过")
    if FAILS:
        print("失败项:", ", ".join(FAILS))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
