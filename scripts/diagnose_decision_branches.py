# -*- coding: utf-8 -*-
"""决策分支体检：`optimizer.solve` 到底走了哪条分支、输出了什么类型。

为什么需要
----------
离线评估里"规则动作分布"只有一个 `map_move: 100%` 的结果，看不出是
"没有敌人信息"、"移动格点永远更优"，还是"战术动作被白名单挡掉"。三者的处理方式完全不同：
  - 没有敌人信息   -> 感知/字段问题，改代码没用，得补采（`--mem` / 位置）
  - 移动永远更优   -> 打分口径问题，需要重标定
  - 战术被白名单挡 -> 策略范围问题，放开即可（但要先有 ground truth）

本脚本按 `_solve_core` 的真实分支复算，把每帧归到唯一一类，给出占比与分场景分布。

用法：
  python -X utf8 scripts/diagnose_decision_branches.py --limit 1500
  python -X utf8 scripts/diagnose_decision_branches.py --limit 0 --json runs/eval/branch_report.json
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MATCHES = ROOT / "data" / "matches"
TACTICAL = ("skill", "attack", "combo", "recall", "restore")


def iter_frames(limit: int, seed: int):
    rows = []
    for path in sorted(MATCHES.glob("*/states.jsonl")):
        name = path.parent.name
        for line in path.open("r", encoding="utf-8", errors="replace"):
            if not line.strip():
                continue
            try:
                st = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(st.get("minimap"), dict) or not isinstance(st.get("units"), list):
                continue
            rows.append((name, st))
    if limit:
        random.Random(seed).shuffle(rows)
        rows = rows[:limit]
    return rows


def bucket(st: dict) -> str:
    ui = st.get("ui") or {}
    hp = ui.get("hp")
    if (st.get("behavior") or {}).get("dead") or (isinstance(hp, (int, float)) and hp <= 0.02):
        return "死亡/黑屏"
    dots = (st.get("minimap") or {}).get("dots") or {}
    if isinstance(hp, (int, float)) and hp < 0.3:
        return "残血"
    if len(dots.get("red") or []) >= 2:
        return "团战"
    if len([u for u in (st.get("units") or []) if (u.get("cls") or "").endswith("minion")]) >= 3:
        return "清线"
    return "游走"


def classify(st: dict) -> tuple[str, str | None]:
    """返回 (分支名, 实际输出类型)。分支名与 `_solve_core` 一一对应。"""
    from wzry.decision import optimizer as opt

    hp = (st.get("ui") or {}).get("hp", 1.0)
    fb = opt._fallback_action(st) if hasattr(opt, "_fallback_action") else None
    fallback_type = "map_move"
    try:
        f = opt._feat(st, hp)
        if not f["reds"]:
            return "① 无敌人证据 -> fallback", fallback_type
        cands = opt.gen_candidates(f)
        if len(f["reds"]) >= 6 or hp < 0.5:
            keep = ("stand", "follow", "hold", "retreat", "stand_")
            cands = [c for c in cands
                     if any(str(c[0].get("reason", "")).startswith(p) for p in keep)
                     or c[0].get("type") == "none"]
        scored = [(opt.evaluate(c, f, "blue") + 0.35 * opt._forecast(c, f), c) for c in cands]
        if not scored:
            return "⑤ 无候选 -> fallback", fallback_type
        _gx, _gy, gv = opt._grid_best(f, "blue")
        r_max = max(s[0] for s in scored)
        if gv >= r_max:
            return "② 网格移动更优 -> map_move(grid_best)", "map_move"
        best = max(scored, key=lambda x: x[0])[1][0]
        btype = best.get("type")
        if btype in TACTICAL:
            return "③ 战术候选最优但被白名单挡 -> fallback", fallback_type
        return f"④ 返回候选({btype})", btype
    except Exception:                                  # noqa: BLE001
        return "⑥ 异常 -> fallback", fallback_type


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1500, help="0=全部")
    ap.add_argument("--seed", type=int, default=20261024)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    frames = iter_frames(args.limit, args.seed)
    if not frames:
        raise SystemExit("没有可分析帧")

    branch = collections.Counter()
    out_type = collections.Counter()
    by_bucket = collections.defaultdict(collections.Counter)
    for name, st in frames:
        br, ot = classify(st)
        branch[br] += 1
        out_type[str(ot)] += 1
        by_bucket[bucket(st)][br] += 1

    n = len(frames)
    print(f"帧数 {n}（{len({x for x, _ in frames})} 会话）")
    print("\n分支分布：")
    for k, v in sorted(branch.items()):
        print(f"  {k:<42}{v:>7}  {v / n:>6.1%}")
    print("\n实际输出类型：")
    for k, v in out_type.most_common():
        print(f"  {k:<20}{v:>7}  {v / n:>6.1%}")
    print("\n分场景（只列占比 >2% 的分支）：")
    for b, ctr in sorted(by_bucket.items()):
        tot = sum(ctr.values())
        print(f"  [{b}] {tot} 帧")
        for k, v in ctr.most_common():
            if v / tot > 0.02:
                print(f"      {k:<40}{v:>6} {v / tot:>6.1%}")

    if args.json:
        out = Path(args.json)
        if not out.is_absolute():
            out = ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "frames": n,
            "branches": dict(branch),
            "out_types": dict(out_type),
            "by_bucket": {k: dict(v) for k, v in by_bucket.items()},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
