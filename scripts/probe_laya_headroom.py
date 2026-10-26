# -*- coding: utf-8 -*-
"""Laya 重排的"可翻转余量"探针（S2-4 配套，只读）。

回答一个问题：**`eval_laya_shadow_offline` 里翻转率很低，是模型没信号，还是接线没给机会？**

做法：不加载模型，只在规则候选集上算三组数——
  1. `top1-top2` 原始规则分差（重排要跨过它才能换第一）；
  2. `top-k` 内是否存在与 top1 **不同粗类**的候选（不存在则无论 β 多大都不可能翻转）；
  3. 每帧"翻到异粗类所需的最小分差"分布，对照 β 能补的极差（= β）。

用法::

  venv/Scripts/python.exe -X utf8 scripts/probe_laya_headroom.py \
      --keys runs/eval/active_keys_sample.json --json runs/eval/laya_headroom.json
  venv/Scripts/python.exe -X utf8 scripts/probe_laya_headroom.py --limit 300

口径提醒：`COARSE` 必须与 `eval_laya_shadow_offline.COARSE` 一致，否则翻转率不可比。
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MATCHES = ROOT / "data" / "matches"
COARSE = {
    "skill": "engage", "attack": "engage", "combo": "engage",
    "map_move": "move", "recall": "move", "restore": "move", "none": "none",
}


def _load_eval_module():
    path = ROOT / "scripts" / "eval_decision_offline.py"
    spec = importlib.util.spec_from_file_location("eval_decision_offline", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _coarse(action):
    t = str((action or {}).get("type") or "none")
    return COARSE.get(t, t)


def _q(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(p * (len(xs) - 1)))], 4)


def iter_frames(limit, keys_file, seed):
    ev = _load_eval_module()
    want = None
    if keys_file:
        raw = json.loads(Path(keys_file).read_text(encoding="utf-8"))
        want = {k: set(int(i) for i in v) for k, v in raw.items()}
    rows = []
    for path in sorted(MATCHES.glob("*/states.jsonl")):
        name = path.parent.name
        if want is not None and name not in want:
            continue
        for line in path.open("r", encoding="utf-8", errors="replace"):
            if not line.strip():
                continue
            try:
                st = json.loads(line)
            except json.JSONDecodeError:
                continue
            if want is not None and st.get("frame_id") not in want[name]:
                continue
            hp, _ = ev._hp_of(st)
            try:
                rv = ev._rule_view(st, hp)
            except Exception:
                continue
            if ev._is_fallback(rv):
                continue
            rows.append((name, st.get("frame_id"), st, hp, rv))
    if want is None:
        import random
        random.Random(seed).shuffle(rows)
        rows = rows[:limit] if limit else rows
    return rows


def scored_of(ev, st, hp):
    """复刻 `_rule_view` 的候选与打分（含 v2.55 保命硬过滤）。"""
    from wzry.decision import optimizer as opt

    f = opt._feat(st, hp)
    cands = opt.gen_candidates(f)
    if len(f["reds"]) >= 6 or hp < 0.5:
        keep = ("stand", "follow", "hold", "retreat", "stand_")
        cands = [c for c in cands
                 if any(str(c[0].get("reason", "")).startswith(p) for p in keep)
                 or c[0].get("type") == "none"]
    return sorted(((opt.evaluate(c, f, "blue") + 0.35 * opt._forecast(c, f), c)
                   for c in cands), key=lambda x: -x[0])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="无 --keys 时的随机抽样帧数；0=全部")
    ap.add_argument("--keys", default=None, help="活跃帧清单 {session:[frame_id]}（推荐）")
    ap.add_argument("--seed", type=int, default=20261024)
    ap.add_argument("--top-k", type=int, default=8, help="与 LayaScorer.top_k 一致")
    ap.add_argument("--json", default=str(ROOT / "runs" / "eval" / "laya_headroom.json"))
    args = ap.parse_args()

    ev = _load_eval_module()
    frames = iter_frames(args.limit, args.keys, args.seed)
    if not frames:
        raise SystemExit("没有可评估帧（先用 eval_laya_shadow_offline.py --scan-only 导出活跃帧）")

    k = args.top_k
    gaps_top2 = []
    gaps_cross = []          # 翻到"异粗类最高分"所需的最小分差
    top1_coarse = collections.Counter()
    same_coarse_top2 = 0
    k_has_cross = 0
    label_dups = 0
    n_scored = collections.Counter()
    reasons = collections.Counter()

    from wzry.decision.laya_scorer import candidate_label

    for _name, _fid, st, hp, rv in frames:
        reasons[str((rv.get("solved") or {}).get("reason") or "?")] += 1
        try:
            s = scored_of(ev, st, hp)
        except Exception:
            continue
        if len(s) < 2:
            continue
        n_scored[len(s)] += 1
        c0 = _coarse(s[0][1][0])
        top1_coarse[c0] += 1
        gaps_top2.append(s[0][0] - s[1][0])
        if _coarse(s[1][1][0]) == c0:
            same_coarse_top2 += 1
        kk = min(k, len(s))
        cross = [s[i][0] for i in range(kk) if _coarse(s[i][1][0]) != c0]
        if cross:
            k_has_cross += 1
            gaps_cross.append(s[0][0] - max(cross))
        labels = [candidate_label(s[i][1]) for i in range(kk)]
        if len(set(labels)) != len(labels):
            label_dups += 1

    n = len(gaps_top2)
    headroom = {
        "frames": n,
        "top_k": k,
        "top1_coarse": dict(top1_coarse),
        "same_coarse_top2": same_coarse_top2,
        "k_has_cross_coarse": k_has_cross,
        "k_cross_rate": round(k_has_cross / n, 4) if n else None,
        "label_dups": label_dups,
        "gap_top2": {p: _q(gaps_top2, q) for p, q in
                     (("p10", .1), ("p50", .5), ("p90", .9), ("max", 1.0))},
        "gap_cross": {p: _q(gaps_cross, q) for p, q in
                      (("p10", .1), ("p50", .5), ("p90", .9), ("max", 1.0))},
        "flippable_by_beta": {b: sum(1 for g in gaps_cross if g <= b)
                              for b in (0.5, 1, 2, 4, 8, 16, 32)},
        "cand_count": dict(sorted(n_scored.items())),
        "grid_best": reasons.get("grid_best", 0),
        "grid_best_rate": round(reasons.get("grid_best", 0) / n, 4) if n else None,
        "top_reasons": dict(reasons.most_common(8)),
    }

    print(f"[frames] {n}（规则活跃）  候选数分布 {dict(sorted(n_scored.items()))}")
    print(f"[top1] 粗类 {dict(top1_coarse)}")
    print(f"[gap]  top1-top2 原始分差 p50={headroom['gap_top2']['p50']} "
          f"p90={headroom['gap_top2']['p90']} max={headroom['gap_top2']['max']}")
    print(f"[head] top2 与 top1 同粗类: {same_coarse_top2}/{n}")
    print(f"[head] top-{k} 内存在异粗类候选: {k_has_cross}/{n}"
          f"（{headroom['k_cross_rate']:.1%}，= k 内理论翻转上限）")
    print(f"[head] 翻到异粗类所需分差 p50={headroom['gap_cross']['p50']} "
          f"p90={headroom['gap_cross']['p90']} max={headroom['gap_cross']['max']}")
    print(f"[beta] β 可补极差 = β；可按 β 翻转的帧数: {headroom['flippable_by_beta']}")
    print(f"[short] `gv >= r_max` 返回 grid_best（Laya 选择被丢弃）: "
          f"{headroom['grid_best']}/{n}（{headroom['grid_best_rate']:.1%}）")
    if label_dups:
        print(f"[warn] top-{k} 标签重复（Laya 会 fail-open 放弃）: {label_dups} 帧")

    p = Path(args.json)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(headroom, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[json] {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
