# -*- coding: utf-8 -*-
"""检测器阵营抖动量化（审计 A2 遗留项）。

背景：`camp_correct` 用血条颜色探针判敌/友，探针取不到条时落 `default` 兜底。
已知个案：同一静止英雄（绿血条）在连续帧被判敌/友两种
（`20261020_224657_708566` frame_000002 / 000004）。
阵营是训练标签里最大的两类之一，抖动会直接污染 `ally_hero` / `enemy_hero` 监督。

做法（无需人工标注，纯自洽性检验）：
  1. 同一会话内取**相邻两帧**；
  2. 按屏幕框 IoU ≥ 阈值把单位配对（同一物理目标）；
  3. 配上的对里，`cls` 由 ally_hero ↔ enemy_hero 互换的计一次**翻转**；
  4. 按 `camp_src` 归因：翻转对里有多少涉及 `default`（无证据兜底）。

输出：总翻转率、按 camp_src 的分布、以及"涉及 default 的翻转占比"。
`default` 占比高 → 说明抖动主要来自兜底，值得做时序投票修复。

用法：
  venv/Scripts/python.exe -X utf8 scripts/eval_camp_jitter.py
  venv/Scripts/python.exe -X utf8 scripts/eval_camp_jitter.py --min-iou 0.6 --json out.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from collections import Counter


def iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ax2, ay2 = ax + aw, ay + ah
    bx2, by2 = bx + bw, by + bh
    ix = max(0.0, min(ax2, bx2) - max(ax, bx))
    iy = max(0.0, min(ay2, by2) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


HERO_CLS = {"ally_hero", "enemy_hero"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-iou", type=float, default=0.5)
    ap.add_argument("--glob", default="data/matches/*/states.jsonl")
    ap.add_argument("--json", default="runs/eval/camp_jitter.json")
    args = ap.parse_args()

    pairs = 0
    flips = 0
    src_dist = Counter()
    flip_by_src = Counter()
    per_session = []
    missing_src = 0

    for path in sorted(glob.glob(args.glob)):
        prev = None
        s_pairs = s_flips = 0
        for line in open(path, encoding="utf-8"):
            if not line.strip():
                continue
            d = json.loads(line)
            cur = [u for u in (d.get("units") or [])
                   if u.get("cls") in HERO_CLS and u.get("screen")]
            for u in cur:
                src_dist[u.get("camp_src")] += 1
                if u.get("camp_src") is None:
                    missing_src += 1
            if prev is not None:
                for a in prev:
                    # 找 IoU 最大的当前帧单位
                    best, best_iou = None, args.min_iou
                    for b in cur:
                        v = iou(a["screen"], b["screen"])
                        if v >= best_iou:
                            best, best_iou = b, v
                    if best is None:
                        continue
                    pairs += 1
                    s_pairs += 1
                    if a["cls"] != best["cls"]:
                        flips += 1
                        s_flips += 1
                        key = tuple(sorted([str(a.get("camp_src")), str(best.get("camp_src"))]))
                        flip_by_src[key] += 1
            prev = cur
        if s_pairs:
            per_session.append({"session": os.path.basename(os.path.dirname(path)),
                                "pairs": s_pairs, "flips": s_flips,
                                "rate": round(s_flips / s_pairs, 4)})

    rate = flips / pairs if pairs else 0.0
    print(f"英雄单位总数（含阵营）      : {sum(src_dist.values())}")
    print(f"camp_src 分布               : {dict(src_dist)}")
    print(f"其中 camp_src 缺失          : {missing_src}")
    print(f"相邻帧配对（IoU≥{args.min_iou}）: {pairs}")
    print(f"阵营翻转                    : {flips}  → 翻转率 {rate:.2%}")
    print("\n翻转对的 camp_src 组合（前 8）：")
    for k, v in flip_by_src.most_common(8):
        print(f"  {k[0]:>10s} ↔ {k[1]:<10s} {v}")

    inv_default = sum(v for k, v in flip_by_src.items()
                      if "default" in k or "None" in k)
    print(f"\n涉及 default/None 的翻转     : {inv_default} "
          f"({inv_default / flips:.1%} of flips)" if flips else "")

    worst = sorted(per_session, key=lambda r: -r["rate"])[:5]
    if worst:
        print("\n翻转率最高的 5 个会话：")
        for r in worst:
            print(f"  {r['session']}  {r['flips']}/{r['pairs']} = {r['rate']:.2%}")

    out = {
        "hero_units": sum(src_dist.values()), "camp_src": dict(src_dist),
        "camp_src_missing": missing_src,
        "pairs": pairs, "flips": flips, "flip_rate": round(rate, 4),
        "flips_by_src": {f"{k[0]}<->{k[1]}": v for k, v in flip_by_src.items()},
        "flips_involving_default": inv_default,
        "per_session": per_session,
    }
    os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
    with open(args.json, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[out] {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
