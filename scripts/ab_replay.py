# -*- coding: utf-8 -*-
"""决策层 A/B 回放：用**已录制的对局**跑 legacy 与 official 两套决策，出对照表。

能回答的问题：两套决策在同一批真实局面上"选得一样吗、谁更保守、耗时多少、规则命中多少"。

**不能**回答的问题：胜率/经济差。那需要反事实对局（同一局面下分别真的打 10 局），
录制回放改变不了游戏进程。见 `docs/AI_UPGRADE_ALIGN_OFFICIAL.md` §10.5。

用法：
    venv/Scripts/python.exe -X utf8 scripts/ab_replay.py --games 10
    venv/Scripts/python.exe -X utf8 scripts/ab_replay.py --games 10 --rules disasm --out docs/ab_replay_10.md

指标口径：
  - `frames`     参与比较的帧数（只取带 `extra.mem_heroes` 的帧 —— 有内存单位才能建完整快照）
  - `agree`      两套决策落到同一动作类型（move/attack/skill/none）的比例
  - `danger%`    official 快照里"最近敌英雄 < 301"的帧占比（越保守越低）
  - `skill%`     official 输出技能动作的占比
  - `rule%`      规则覆盖层改写过动作的帧占比（附命中前 3 名）
  - `ms`         official 单帧决策均值
  - `fallback%`  official 走 `[UnpredictProcess]` 回退的占比
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.decision import optimizer as L                      # noqa: E402
from wzry.decision.official_ai import OfficialAI, from_state_dict  # noqa: E402
from wzry.decision.official_ai import field as vfield         # noqa: E402
from wzry.decision.official_ai import geo, rules as rulesmod  # noqa: E402


def _sessions(games: int):
    dirs = [p for p in sorted((ROOT / "data" / "matches").glob("*/states.jsonl"))]
    out = []
    for p in reversed(dirs):                     # 新的优先
        n = 0
        with p.open(encoding="utf-8") as f:
            for line in f:
                if '"mem_heroes"' in line:
                    n += 1
        if n >= 5:                               # 至少有 5 帧可用
            out.append((p.parent.name, p, n))
        if len(out) >= games:
            break
    return out


def _frames(path: Path):
    with path.open(encoding="utf-8") as f:
        for line in f:
            if '"mem_heroes"' not in line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def _fair_state(st: dict, self_id: int = 106) -> dict:
    """把内存单位**反投影成视觉点**，让 legacy 与 official 吃同一份局面。

    录制帧里的 `minimap.dots.red` 经常是空的（视觉没检出红点），而 `extra.mem_heroes`
    有全量 10 人 —— 直接把原帧喂给 legacy 会让它 100% 回退 `none`，对比就没意义了。
    这里统一用内存数据重建 dots（与 `bench_official_ai.to_visual` 同一口径）。
    """
    hs = (st.get("extra") or {}).get("mem_heroes") or []
    us = (st.get("extra") or {}).get("mem_units") or []
    if not hs:
        return st
    me = next((h for h in hs if h.get("id") == self_id), hs[0])
    my = me.get("camp")
    reds, blues = [], []
    for h in hs:
        if h.get("id") == me.get("id") or h.get("x") is None:
            continue
        pt = list(geo.world_to_norm(h["x"], h["z"]))
        (blues if h.get("camp") == my else reds).append(pt)
    units = [{"cls": ("enemy_minion" if u.get("cid", 0) in range(1690, 1700)
                      else "enemy_turret" if u.get("kind") == "turret" else "monster"),
              "screen": list(geo.world_to_norm(u["x"], u["z"])),
              "hp": (u.get("hp") or 0) / (u.get("hp_max") or 1)}
             for u in us if u.get("x") is not None and u.get("camp") != my]
    out = dict(st)
    out["units"] = list(st.get("units") or []) + units
    out["minimap"] = {"found": True,
                      "dots": {"red": reds, "blue": blues, "yellow": [],
                               "green": [list(geo.world_to_norm(me["x"], me["z"]))]}}
    out["ui"] = dict(st.get("ui") or {})
    if not isinstance(out["ui"].get("hp"), (int, float)) and me.get("hp_max"):
        out["ui"]["hp"] = me["hp"] / me["hp_max"]
    return out


_TYPE_MAP = {"map_move": "move", "move": "move", "skill": "skill",
             "attack": "attack", "combo": "attack", "none": "none"}


def _norm_type(t: str) -> str:
    """把两套动作词表归一成 move/attack/skill/none（否则 map_move vs move 永远不一致）。"""
    return _TYPE_MAP.get(t, t)


def _legacy(state: dict) -> str:
    ui = state.get("ui") or {}
    hp = ui.get("hp")
    if not isinstance(hp, (int, float)):
        hs = (state.get("extra") or {}).get("mem_heroes") or []
        hp = next((h["hp"] / h["hp_max"] for h in hs
                   if h.get("hp_max") and h.get("hp") is not None), 1.0)
    camp = "blue" if (state.get("extra") or {}).get("mem_heroes", [{}])[0].get("camp") in ("blue", 1) else "blue"
    # ⚠️ 兜底动作不能传 `{"type":"none"}`：`_solve_core` 的动作白名单里有 "none"，
    #   会**直接原样返回**，legacy 等于不决策（这一坑在第一次跑时把一致率打成 0%）。
    #   生产里 m2 传的是规则层的 map_move，这里同样传"向己方绿点"的 map_move。
    dots = (state.get("minimap") or {}).get("dots") or {}
    g = (dots.get("green") or [[0.5, 0.5]])[0]
    fb = {"type": "map_move", "nx": float(g[0]), "ny": float(g[1]), "reason": "ab"}
    try:
        act = L.solve(state, fb, float(hp), camp)
    except Exception:
        return "error"
    return _norm_type(str((act or {}).get("type") or "none"))


def run(games: int, level: str, rule_mode: str, limit_ms: float):
    rows = []
    agg = Counter()
    hits = Counter()
    ms_all = []
    for name, path, n_avail in _sessions(games):
        ai = OfficialAI(level=level, rule_mode=rule_mode, clock=lambda: 0.0)
        cnt = Counter()
        ms = []
        for st in _frames(path):
            snap = from_state_dict(st, atk_range=float((st.get("ui") or {}).get("atk_range") or 650.0))
            if snap.self_unit is None:
                continue
            t0 = time.perf_counter()
            dec = ai.decide(snap)
            ms.append((time.perf_counter() - t0) * 1000)
            otype = _norm_type(dec.action.kind)
            ltype = _legacy(_fair_state(st))
            cnt["frames"] += 1
            cnt[f"o_{otype}"] += 1
            cnt[f"l_{ltype}"] += 1
            cnt["agree"] += int(otype == ltype)
            cnt["danger"] += int(snap.enemy_hero_min_dist() < 301)
            cnt["rule"] += int(bool(dec.hits))
            cnt["fallback"] += int(dec.source == "fallback")
            for h in dec.hits[:2]:
                hits[h.name] += 1
            if len(ms) >= 400:
                break
        if not cnt["frames"]:
            continue
        f = cnt["frames"]
        rows.append({
            "game": name, "frames": f,
            "agree": cnt["agree"] / f,
            "danger": cnt["danger"] / f,
            "skill": cnt["o_skill"] / f,
            "rule": cnt["rule"] / f,
            "fallback": cnt["fallback"] / f,
            "l_none": (cnt["l_none"] + cnt["l_error"]) / f,
            "ms": statistics.mean(ms) if ms else 0.0,
            "o": {k[2:]: v for k, v in cnt.items() if k.startswith("o_")},
            "l": {k[2:]: v for k, v in cnt.items() if k.startswith("l_")},
        })
        agg.update(cnt)
        ms_all.extend(ms)

    print("> 两套决策吃同一份局面（内存单位反投影为视觉点，见 `_fair_state`）\n")
    hdr = ("| 对局 | 帧数 | 动作一致率 | legacy 无动作率 | 危险区占比 | official 技能率 "
           "| 规则改写率 | 回退率 | 单帧 ms |")
    sep = "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    lines = [hdr, sep]
    for r in rows:
        lines.append(f"| {r['game']} | {r['frames']} | {r['agree']*100:.0f}% "
                     f"| {r['l_none']*100:.0f}% "
                     f"| {r['danger']*100:.0f}% | {r['skill']*100:.0f}% "
                     f"| {r['rule']*100:.0f}% | {r['fallback']*100:.0f}% "
                     f"| {r['ms']:.2f} |")
    if rows:
        fsum = agg["frames"]
        lines.append(
            f"| **合计/均值** | {fsum} | {agg['agree']/fsum*100:.0f}% "
            f"| {(agg['l_none'] + agg['l_error'])/fsum*100:.0f}% "
            f"| {agg['danger']/fsum*100:.0f}% | {agg['o_skill']/fsum*100:.0f}% "
            f"| {agg['rule']/fsum*100:.0f}% | {agg['fallback']/fsum*100:.0f}% "
            f"| {statistics.mean(ms_all):.2f} |")
    print("\n".join(lines))
    if hits:
        print("\n规则命中 TOP:")
        for k, v in hits.most_common(6):
            ev = next((r.evidence for r in rulesmod.CHAIN if r.name == k), "?")
            print(f"  {k:<28} {v:>5}  [{ev}]")
    missing = games - len(rows)
    if missing > 0:
        print(f"\n注意：只有 {len(rows)} 局带内存单位（需要 --mem 录制），目标 {games} 局。")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=10)
    ap.add_argument("--level", default="hard")
    ap.add_argument("--rules", choices=["all", "disasm"], default="all")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    table = run(a.games, a.level, a.rules, 400.0)
    if a.out:
        p = ROOT / a.out
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(table + "\n", encoding="utf-8")
        print(f"\n已写入 {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
