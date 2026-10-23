# -*- coding: utf-8 -*-
"""legacy（价值场 argmax）vs official（官方架构复刻）的离线对照台。

目的：给 P2.4 / P5.2 提供**可复现的量化口径**，不依赖设备：

  - 候选数：`optimizer.gen_candidates()` vs `official_ai.actions.enumerate_*`
  - 决策耗时：`optimizer.solve()` vs `OfficialAI.decide()`（含特征构建）
  - 动作分布 / 规则命中：official 每条规则的 VETO/REWRITE 计数与证据等级

跑法：
    venv/Scripts/python.exe -X utf8 scripts/bench_official_ai.py
    venv/Scripts/python.exe -X utf8 scripts/bench_official_ai.py --repeat 20 --out docs/bench_ai_arch.md

口径说明：
  - 夹具是**构造**的（6 个典型局面），不是真机录包；真机指标（金币/分钟、胜率）必须上设备，
    见 `docs/AI_UPGRADE_ALIGN_OFFICIAL.md` §9 的阻塞项。
  - legacy 侧把内存单位转换成视觉口径（小地图归一化 + cls），保证两条链路吃同一份局面。
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.decision import optimizer as L                       # noqa: E402
from wzry.decision.official_ai import OfficialAI, features     # noqa: E402
from wzry.decision.official_ai import geo, organ_slots as O    # noqa: E402
from wzry.decision.official_ai.snapshot import from_state_dict  # noqa: E402

ATK = 650.0
SELF_ID = 106


# ---------------------------------------------------------------- 夹具
def _heroes(hp_ratio=0.8, enemy_dist=420.0, n_enemy=1, n_ally=4):
    sk = [{"slot": 1, "id": 10610, "level": 0, "ready": True, "cd_left_ms": 0, "cd_total_ms": 5000},
          {"slot": 2, "id": 10620, "level": 0, "ready": False, "cd_left_ms": 4200, "cd_total_ms": 10000},
          {"slot": 3, "id": 10630, "level": 0, "ready": True, "cd_left_ms": 0, "cd_total_ms": 35000}]

    def h(cid, camp, x, z, hp, hpmax, skills=None):
        return {"id": cid, "camp": camp, "x": x, "z": z, "hp": hp, "hp_max": hpmax,
                "level": 8, "kills": 0, "deaths": 0, "assists": 0,
                "gold": 1000, "gold_total": 1200, "skills": skills or []}

    out = [h(SELF_ID, 1, 0, 0, int(4000 * hp_ratio), 4000, sk)]
    foe_ids = [107, 121, 171, 173, 105]
    for i in range(n_enemy):
        out.append(h(foe_ids[i % 5], 2, enemy_dist * (1 + 0.3 * i), 60 * i, 900, 4000))
    ally_ids = [110, 167, 187, 174]
    for i in range(n_ally):
        out.append(h(ally_ids[i % 4], 1, -1200 - 300 * i, -900 * i, 4000, 4000))
    for i in range(len(out), 10):
        out.append(h(foe_ids[i % 5], 2, 30000 + 1000 * i, 30000, 4000, 4000))
    return out[:10]


def _buildings(self_camp=1, hp=9000):
    out = []
    for name, (nx, ny) in O.table(self_camp).items():
        camp = self_camp if name.startswith("friend") else (3 - self_camp)
        x, z = geo.norm_to_world(nx, ny)
        out.append({"cid": 92 if "tower" in name else 95, "kind": "turret",
                    "camp": camp, "x": x, "z": z, "hp": hp, "hp_max": 9000, "dead": False})
    return out


def _minions(near_closer=True, n=6):
    out = []
    for i in range(n):
        out.append({"cid": 1690 + (i % 3), "kind": "minion", "camp": 2,
                    "x": (300 if near_closer else 1200) + 90 * i, "z": 80 * i - 200,
                    "hp": 500 + 100 * i, "hp_max": 1200, "dead": False})
    return out


SCENES = {
    # 走廊对拼：单敌在射程内，兵线在脚边
    "lane_1v1": dict(hp_ratio=0.8, enemy_dist=420, n_enemy=1, n_minion=6),
    # 小规模团战：2 敌 + 满血
    "teamfight_2v2": dict(hp_ratio=0.75, enemy_dist=600, n_enemy=2, n_minion=4),
    # 以少打多 + 残血（考规则覆盖层）
    "outnumbered_low": dict(hp_ratio=0.35, enemy_dist=500, n_enemy=4, n_minion=3),
    # 濒死回城
    "critical_hp": dict(hp_ratio=0.15, enemy_dist=900, n_enemy=1, n_minion=0),
    # 无敌人的推进局面（考 grid action / 推进项）
    "push_no_enemy": dict(hp_ratio=1.0, enemy_dist=40000, n_enemy=0, n_minion=4),
    # 满血打野（野怪近、敌远）
    "jungle_full": dict(hp_ratio=0.95, enemy_dist=30000, n_enemy=0, n_minion=0, monster=True),
}


def make_state(name: str):
    cfg = SCENES[name]
    units = _buildings(1) + _minions(n=cfg.get("n_minion", 4))
    if cfg.get("monster"):
        units.append({"cid": 16100, "kind": "monster", "camp": 2, "x": 1500, "z": 800,
                      "hp": 6000, "hp_max": 9000, "dead": False})
    return {"ui": {"match_clock": 300, "hero_id": SELF_ID, "mp": 0.7,
                   "hp": cfg["hp_ratio"], "camp": "blue"},
            "extra": {"mem_heroes": _heroes(cfg["hp_ratio"], cfg["enemy_dist"],
                                            cfg.get("n_enemy", 1)),
                      "mem_units": units}}


def to_visual(state: dict) -> dict:
    """把内存口径转成 legacy 认识的可视口径（小地图归一化 + cls）。"""
    heroes = state["extra"]["mem_heroes"]
    units_v = []
    mine = next(h for h in heroes if h["id"] == SELF_ID)
    for h in heroes:
        if h["id"] == SELF_ID:
            continue
        nx, ny = geo.world_to_norm(h["x"], h["z"])
        units_v.append({"cls": "enemy_hero" if h["camp"] != mine["camp"] else "ally_hero",
                        "screen": [nx, ny], "hp": h["hp"] / h["hp_max"], "conf": 0.9,
                        "camp_src": "px"})
    for u in state["extra"]["mem_units"]:
        nx, ny = geo.world_to_norm(u["x"], u["z"])
        cls = ("enemy_minion" if u["cid"] in range(1690, 1700)
               else "enemy_turret" if u["kind"] == "turret" else "monster")
        if u["camp"] == mine["camp"]:
            cls = cls.replace("enemy", "ally")
        units_v.append({"cls": cls, "screen": [nx, ny], "hp": u["hp"] / u["hp_max"]})
    sx, sy = geo.world_to_norm(mine["x"], mine["z"])
    reds = [u["screen"] for u in units_v if u["cls"].startswith("enemy")]
    blues = [u["screen"] for u in units_v if u["cls"].startswith("ally")]
    return {"ui": {"hp": state["ui"]["hp"], "mp": state["ui"]["mp"]},
            "units": units_v,
            "minimap": {"found": True, "dots": {"red": reds, "blue": blues,
                                               "green": [[sx, sy]], "yellow": []}}}


# ---------------------------------------------------------------- 评测
def bench_legacy(state: dict, visual: dict, repeat: int):
    hp = float(state["ui"]["hp"])
    # 候选数：直接调 gen_candidates（与 solve 内部一致）
    f = L._feat(visual, hp)
    n_candidates = len(L.gen_candidates(f))
    ts = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        L.solve(visual, {"type": "none", "reason": "bench"}, hp, "blue")
        ts.append((time.perf_counter() - t0) * 1000)
    fixed = {"n_candidates": n_candidates, "solve_ms": statistics.mean(ts)}
    # 不保存上限值供 table 用
    return fixed


def bench_official(state: dict, repeat: int, mode: str = "all"):
    snap = from_state_dict(state, self_id=SELF_ID, atk_range=ATK)
    t0 = time.perf_counter()
    pack = features.build(snap)
    feat_ms = (time.perf_counter() - t0) * 1000

    ai = OfficialAI(level="hard", clock=lambda: 0.0, rule_mode=mode)
    dec = ai.decide(snap)
    cands = dec.candidates
    ts = []
    for _ in range(repeat):
        a = OfficialAI(level="hard", clock=lambda: 0.0, rule_mode=mode)
        s2 = from_state_dict(state, self_id=SELF_ID, atk_range=ATK)
        t0 = time.perf_counter()
        a.decide(s2)
        ts.append((time.perf_counter() - t0) * 1000)
    return {"n_candidates": cands, "decide_ms": statistics.mean(ts), "feat_ms": feat_ms,
            "label": dec.label, "source": dec.source, "scene": dec.scene,
            "slots": len(snap.organ_slots),
            "hits": [(h.name, h.verdict, h.evidence) for h in dec.hits],
            "feat_len": pack.size}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=10)
    ap.add_argument("--rules", choices=["all", "disasm"], default="all")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    rows = []
    hit_counter = {}
    for name in SCENES:
        state = make_state(name)
        visual = to_visual(state)
        lg = bench_legacy(state, visual, a.repeat)
        of = bench_official(state, a.repeat, a.rules)
        for hn, hv, he in of["hits"]:
            hit_counter[(hn, hv, he)] = hit_counter.get((hn, hv, he), 0) + 1
        rows.append((name, lg, of))

    hdr = ("| 场景 | legacy 候选 | official 候选 | 降幅 | legacy solve ms | official decide ms "
           "| 特征 ms | official 动作 | 来源 | 场景 | 槽位 |")
    sep = "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    lines = [hdr, sep]
    for name, lg, of in rows:
        drop = (1 - of["n_candidates"] / lg["n_candidates"]) * 100 if lg["n_candidates"] else 0.0
        lines.append(
            f"| {name} | {lg['n_candidates']} | {of['n_candidates']} | {drop:.0f}% "
            f"| {lg['solve_ms']:.2f} | {of['decide_ms']:.2f} | {of['feat_ms']:.2f} "
            f"| {of['label']} | {of['source']} | {of['scene']} | {of['slots']}/22 |")

    lg_c = statistics.mean(r[1]["n_candidates"] for r in rows)
    of_c = statistics.mean(r[2]["n_candidates"] for r in rows)
    lg_ms = statistics.mean(r[1]["solve_ms"] for r in rows)
    of_ms = statistics.mean(r[2]["decide_ms"] for r in rows)
    fm = statistics.mean(r[2]["feat_ms"] for r in rows)

    print("\n".join(lines))
    print(f"\n均值：候选 {lg_c:.1f} -> {of_c:.1f}（{100 * (1 - of_c / lg_c):.0f}%）　"
          f"耗时 {lg_ms:.2f}ms -> {of_ms:.2f}ms　特征 {fm:.2f}ms")
    if hit_counter:
        print("\n规则命中（场景累计）：")
        for (hn, hv, he), n in sorted(hit_counter.items(), key=lambda kv: -kv[1]):
            print(f"  {hn:<28} {hv:<8} [{he}] x{n}")

    if a.out:
        p = ROOT / a.out if not Path(a.out).is_absolute() else Path(a.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n已写入 {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
