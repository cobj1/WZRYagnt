#!/usr/bin/env python
"""逐帧不变量审计：用已录制（带内存）的对局跑 official 决策，检查"动作是否可执行"。

设计原则：这里**不判断打得好不好**（那要真机胜负率），只判断"这一帧的动作是不是
一个能执行的动作"。上一轮抓到的 `Retreat`/`ReturnCity` 原地不动，以及坐标 z 反向，
都属于这一类：离线逻辑看起来都在跑，但动作本身一执行就错了。

检查项
  I1  自身坐标存在且在有效范围
  I2  所有动作点落在世界边界内，且归一化后在小地图 0..1
  I3  攻击/技能目标存在、存活、且确实在快照单位集合里（不是悬空引用）
  I4  技能槽位在 0..3
  I5  走位点与自身重合（"假位移"）—— 除显式 `stay` 语义外都算违规
  I6  数值无 NaN/Inf
  I7  连续同一标签的"僵死"窗口（同一标签超过 N 帧且目标点几乎不变）
  I8  data_src / 取数来源标记齐全

用法
  python scripts/audit_frames.py                # 全部 session
  python scripts/audit_frames.py --limit 3      # 只看前 3 个 session
  python scripts/audit_frames.py --self-id 106
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
from collections import Counter, defaultdict

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wzry.decision.official_ai import (OfficialAI, from_state_dict,  # noqa: E402
                                       actions as A, geo, rules as R)

WORLD_X = (-58700.0, 58100.0)
WORLD_Z = (-55700.0, 60000.0)
STALE_WINDOW = 30          # 连续 N 帧同标签且点不动 -> 记录


class Audit:
    def __init__(self, name: str):
        self.name = name
        self.frames = 0
        self.bad: Counter = Counter()
        self.samples: dict = defaultdict(list)
        self.labels: Counter = Counter()
        self.srcs: Counter = Counter()
        self.rule_hits: Counter = Counter()
        self.unlocated: Counter = Counter()
        self.self_move_frames = 0

    def fail(self, key: str, detail: str):
        self.bad[key] += 1
        if len(self.samples[key]) < 3:
            self.samples[key].append(detail)


def _finite(*vals) -> bool:
    for v in vals:
        if v is None:
            continue
        try:
            f = float(v)
        except Exception:
            return False
        if math.isnan(f) or math.isinf(f):
            return False
    return True


def _in_world(x, z) -> bool:
    return (WORLD_X[0] - 1 <= x <= WORLD_X[1] + 1
            and WORLD_Z[0] - 1 <= z <= WORLD_Z[1] + 1)


def _find_unit(snap, tgt):
    """目标对象是否真的在快照的单位集合里（按 id() 比对，防悬空引用）。"""
    if tgt is None:
        return False
    pools = (snap.heroes, snap.minions, snap.organs, snap.monsters, snap.cakes)
    for pool in pools:
        for u in pool:
            if u is tgt:
                return True
    return False


def audit_action(a: Audit, snap, act: A.Action, tag: str):
    # I2 动作点
    if act.point is not None:
        if not _finite(act.point[0], act.point[1]):
            a.fail("I6 NaN/Inf 动作点", f"{tag} {act.label} point={act.point}")
        elif not _in_world(act.point[0], act.point[1]):
            a.fail("I2 动作点越界", f"{tag} {act.label} point={act.point}")
        else:
            nx, ny = geo.world_to_norm(*act.point)
            if not (0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0):
                a.fail("I2 归一化越界", f"{tag} {act.label} norm=({nx:.3f},{ny:.3f})")

    # I3 攻击目标
    if act.kind in ("attack", "skill"):
        if act.target is None:
            a.fail("I3 攻击无目标", f"{tag} {act.label}")
        elif not _find_unit(snap, act.target):
            a.fail("I3 目标不在快照里", f"{tag} {act.label} tgt={act.target.cid}")
        elif not act.target.alive:
            a.fail("I3 目标已死亡", f"{tag} {act.label} tgt={act.target.cid}")

    # I4 技能槽
    if act.kind == "skill":
        if not (0 <= int(act.skill) <= 3):
            a.fail("I4 技能槽越界", f"{tag} {act.label} skill={act.skill}")

    # I5 假位移
    if act.kind == "move" and act.point is not None and snap.self_x is not None:
        d = math.hypot(act.point[0] - snap.self_x, act.point[1] - snap.self_z)
        if d < 1.0 and not act.meta.get("stay"):
            a.fail("I5 假位移（点到自身）", f"{tag} {act.label} d={d:.2f}")


def audit_session(path: pathlib.Path, self_id: int, ai_level: str) -> Audit:
    a = Audit(path.parent.name)
    ai = OfficialAI(level=ai_level, clock=lambda: 0.0)   # 关掉节拍缓存，逐帧都真算
    prev = None            # (label, point)
    prev_self = None
    stale = 0

    lines = path.read_text(encoding="utf-8").splitlines()
    for ln, line in enumerate(lines):
        if '"mem_heroes"' not in line:
            continue
        try:
            st = json.loads(line)
        except Exception:
            continue
        snap = from_state_dict(st, self_id=self_id or None, atk_range=650.0)
        a.frames += 1
        a.srcs[snap.data_src] += 1

        # I1
        if snap.self_x is None or snap.self_z is None:
            a.fail("I1 自身坐标缺失", f"frame#{ln} src={snap.data_src}")
            continue
        if not _in_world(snap.self_x, snap.self_z):
            a.fail("I1 自身坐标越界", f"frame#{ln} ({snap.self_x:.0f},{snap.self_z:.0f})")
        if not _finite(snap.self_x, snap.self_z):
            a.fail("I6 NaN/Inf 自身坐标", f"frame#{ln}")

        dec = ai.decide(snap)
        a.labels[dec.label] += 1
        for h in dec.hits:
            a.rule_hits[f"{h.name}:{h.verdict}"] += 1
        audit_action(a, snap, dec.action, f"frame#{ln}/{dec.source}")

        # I7 僵死窗口（只在"自身确实在移动"的前提下判：录制里站桩时不算 AI 的账）
        moved = (prev_self is not None
                 and math.hypot(snap.self_x - prev_self[0], snap.self_z - prev_self[1]) > 30)
        prev_self = (snap.self_x, snap.self_z)
        if moved:
            self_move_frames += 1
        p = dec.action.point
        key = (dec.label, None if p is None else (round(p[0] / 200), round(p[1] / 200)))
        if moved and prev is not None and key == prev:
            stale += 1
        elif moved:
            stale = 0
        prev = key
        if stale >= STALE_WINDOW:
            a.fail("I7 僵死窗口", f"frame#{ln} {dec.label} 连续 {stale + 1} 帧同点")

        # I8
        if not snap.data_src:
            a.fail("I8 data_src 未标", f"frame#{ln}")

        # I9 特征污染：无坐标单位若被写进特征，会在世界原点堆成一坨假目标
        from wzry.decision.official_ai import features as FE
        pack = FE.build(snap)
        for cat, pool in (("soldier", snap.minions), ("monster", snap.monsters),
                          ("cake", snap.cakes)):
            n_loc = sum(1 for u in pool if u.x is not None and u.z is not None)
            if pack.n_units.get(cat, 0) > n_loc:
                a.fail("I9 无坐标单位进了特征",
                       f"frame#{ln} {cat} feat={pack.n_units.get(cat)} located={n_loc}")
        # 注意看 `n_units`（真实写入的条数），不是 `vec` 长度 —— 向量永远定长，空位是补 0 的
        if pack.n_units.get("organ", 0) and not snap.organ_slots:
            a.fail("I9 建筑特征来自伪造坐标",
                   f"frame#{ln} organ_n={pack.n_units.get('organ')}")
        for note in pack.notes:
            if note.startswith("unlocated:"):
                a.unlocated[note.split(":")[1]] += 1
    return a


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--self-id", type=int, default=0)
    ap.add_argument("--ai-level", default="hard")
    args = ap.parse_args()

    paths = []
    for p in sorted(pathlib.Path(ROOT / "data/matches").glob("*/states.jsonl")):
        if '"mem_heroes"' in p.read_text(encoding="utf-8"):
            paths.append(p)
    if args.limit:
        paths = paths[: args.limit]
    if not paths:
        print("没有带内存的录制；先跑 `m2_agent_v2.py --mem` 录一局")
        return 1

    print(f"审计 {len(paths)} 个 session / 规则链 "
          f"{R.chain_stats()['implemented']}/{len(R.CHAIN)} 条\n")
    total = Counter()
    rows = []
    for p in paths:
        a = audit_session(p, args.self_id, args.ai_level)
        total.update(a.bad)
        rows.append(a)
        status = "PASS" if not a.bad else "FAIL"
        print(f"{a.name}  {a.frames:>4} 帧  {status}")
        for k, v in a.bad.items():
            print(f"    ✗ {k:<24} ×{v}   e.g. {a.samples[k][0] if a.samples[k] else ''}")

    frames = sum(a.frames for a in rows)
    print(f"\n合计 {frames} 帧")
    lab = Counter()
    src = Counter()
    hits = Counter()
    for a in rows:
        lab.update(a.labels)
        src.update(a.srcs)
        hits.update(a.rule_hits)
    print("动作分布:", dict(lab.most_common(8)))
    print("取数来源:", dict(src))
    unl = Counter()
    for a in rows:
        unl.update(a.unlocated)
    print("坐标缺失（被剔除的单位/帧）:", dict(unl) if unl else "（无）")
    print("自身实际位移帧数:", sum(a.self_move_frames for a in rows), "/", frames)
    print("规则命中:", dict(hits.most_common(8)) if hits else "（无）")
    if total:
        print("\n违规汇总:", dict(total))
    else:
        print("\n全部不变量通过")
    return 0 if not total else 2


if __name__ == "__main__":
    raise SystemExit(main())
