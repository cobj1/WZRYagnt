# -*- coding: utf-8 -*-
"""复刻版官方 AI 的演示/调试入口。

用法：
  # 真机/模拟器（读内存，需要 memscan 与一局对局）
  venv/Scripts/python.exe -X utf8 scripts/run_official_ai_replica.py --live --frames 5 --level hard

  # 离线夹具（不需要设备）
  venv/Scripts/python.exe -X utf8 scripts/run_official_ai_replica.py --fixture

输出：每帧的特征摘要、候选数、选中动作（官方风格标签）、概率、规则命中。
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.decision.official_ai import OfficialAI, from_state_dict  # noqa: E402
from wzry.decision.official_ai import geo, organ_slots as O        # noqa: E402
from wzry.decision.official_ai.actions import describe            # noqa: E402


def _real_buildings(self_camp=1):
    """按 22 个具名槽位的标定坐标生成建筑（id 统一用 92/95，模拟内存只给两种 cid）。"""
    out = []
    for name, (nx, ny) in O.table(self_camp).items():
        camp = self_camp if name.startswith("friend") else (3 - self_camp)
        x, z = geo.norm_to_world(nx, ny)
        out.append({"cid": 92 if "tower" in name else 95, "kind": "turret",
                    "camp": camp, "x": x, "z": z,
                    "hp": 9000, "hp_max": 9000, "dead": False})
    return out


def _fixture_state(hp_ratio=0.8):
    """与单测同构的最小夹具。"""
    sk = [{"slot": 1, "id": 10610, "level": 0, "ready": True, "cd_left_ms": 0, "cd_total_ms": 5000},
          {"slot": 2, "id": 10620, "level": 0, "ready": False, "cd_left_ms": 4200, "cd_total_ms": 10000},
          {"slot": 3, "id": 10630, "level": 0, "ready": True, "cd_left_ms": 0, "cd_total_ms": 35000}]

    def h(cid, camp, x, z, hp, hpmax, skills=None):
        return {"id": cid, "camp": camp, "x": x, "z": z, "hp": hp, "hp_max": hpmax,
                "level": 8, "kills": 0, "gold": 1000, "gold_total": 1200,
                "skills": skills or []}

    heroes = [h(106, 1, 0, 0, int(4000 * hp_ratio), 4000, sk),
              h(107, 2, 420, 0, 900, 4000), h(121, 2, 30000, 30000, 4000, 4000),
              h(110, 1, -20000, -20000, 4000, 4000), h(167, 1, -25000, -18000, 3000, 4000),
              h(171, 2, 40000, 10000, 2000, 5000), h(187, 1, -15000, -22000, 3500, 4000),
              h(173, 2, 35000, 20000, 1500, 4000), h(174, 1, -22000, -25000, 3800, 4000),
              h(105, 2, 42000, 5000, 3000, 5000)]
    units = [{"cid": 1690, "kind": "minion", "camp": 2, "x": 380, "z": 120,
              "hp": 900, "hp_max": 1200, "dead": False},
             {"cid": 1691, "kind": "minion", "camp": 2, "x": 400, "z": -120,
              "hp": 400, "hp_max": 1200, "dead": False},
             {"cid": 92, "kind": "turret", "camp": 2, "x": 520, "z": 0,
              "hp": 9000, "hp_max": 9000, "dead": False},
             {"cid": 16100, "kind": "monster", "camp": 2, "x": 1500, "z": 800,
              "hp": 6000, "hp_max": 9000, "dead": False}]
    # 补上 22 栋真实坐标的建筑：让 organ 特征走"槽位序"，并演示槽位门限
    #   （上面那栋 (520,0) 在河道中央，不属于 22 槽位 -> 会被标成 far 并留空）
    units += _real_buildings(1)
    return {"ui": {"match_clock": 120, "hero_id": 106, "mp": 0.62},
            "extra": {"mem_heroes": heroes, "mem_units": units}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--fixture", action="store_true")
    ap.add_argument("--frames", type=int, default=3)
    ap.add_argument("--interval", type=float, default=0.25)
    ap.add_argument("--level", default="hard", choices=["easy", "normal", "hard", "juewu"])
    ap.add_argument("--self-id", type=int, default=106)
    ap.add_argument("--atk-range", type=float, default=650.0,
                    help="主英雄普攻射程（世界单位；内存未提供，需按英雄给）")
    ap.add_argument("--serial", default="127.0.0.1:16384")
    a = ap.parse_args()

    mb = None
    if a.live:
        from wzry.vision.mem_battle import MemBattle
        mb = MemBattle(serial=a.serial)
        print(f"pid={mb.connect()}")

    ai = OfficialAI(level=a.level)
    print(f"params: {ai.params}")
    for i in range(a.frames):
        if a.live:
            state = {"ui": {}, "extra": {}}
            summary = mb.patch_state(state, self_id=a.self_id)
            if not summary.get("n_units"):
                print("未读到单位：可能不在对局中")
                return 1
        else:
            state = _fixture_state(hp_ratio=0.8 - 0.15 * i)

        snap = from_state_dict(state, self_id=a.self_id, atk_range=a.atk_range)
        t0 = time.perf_counter()
        dec = ai.decide(snap)
        dt = (time.perf_counter() - t0) * 1000

        print(f"\n--- frame {dec.frame}  ({dt:.2f}ms, source={dec.source})")
        print(f"  features : {dec.pack_summary}")
        print(f"  units    : heroes={len(snap.heroes)} minions={len(snap.minions)} "
              f"organs={len(snap.organs)} monsters={len(snap.monsters)} "
              f"slots={len(snap.organ_slots)}/22")
        if snap.organ_notes:
            print(f"  organ    : {'; '.join(snap.organ_notes[:3])}"
                  + (" ..." if len(snap.organ_notes) > 3 else ""))
        print(f"  enemy_min_dis={snap.enemy_hero_min_dist():.0f} "
              f"hp_rate={(snap.self_unit.hp_rate if snap.self_unit else 0):.2f} "
              f"ep_rate={(snap.self_ep_rate if snap.self_ep_rate is not None else -1):.2f} "
              f"clock={snap.clock_s:.0f}s  scene={dec.scene}")
        print(f"  candidates={dec.candidates}  prob={dec.prob:.3f}  rank={dec.rank}")
        print(f"  ACTION   : {dec.label}   {describe(dec.action)}")
        for h in dec.hits:
            print(f"    rule {h.name:<28} {h.verdict:<8} [{h.evidence}] {h.reason}  {h.addr}")
        if a.frames > 1:
            time.sleep(a.interval)

    print(f"\nstats: {ai.stats()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
