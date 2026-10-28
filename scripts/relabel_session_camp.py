# -*- coding: utf-8 -*-
"""回填存量会话的自身阵营（把 `unknown_hero` 救成 ally/enemy）。

为什么：2026-10-26 那次修复让"解不出自身阵营"时如实输出 `unknown_*`（正确，
但不可用）。真机实测发现 **89% 的单位是 `unknown_hero`** —— 这批数据对训练
等于废的。根因是 `self_cid` 解不出（`ui.hero_id` 为空），不是阵营真的不可观测。

解法（已接入 `MatchRecorder._camp_from_minimap_vote`）：小地图绿点就是自己，
英雄有世界坐标 → `world_to_norm` → 最近邻匹配 + 多数票锁定。
真机抽样 66/66 可匹配、63 帧指向同一 cid、典型距离 0.034。

本脚本对**存量**会话做同样的事：units 里每条都保留了 `camp`（内存原始阵营），
只需补 `side` / `cls` 两个派生字段，其余字段一律不动。

用法：
    python -X utf8 scripts/relabel_session_camp.py --session 20261026_212503_169231
    python -X utf8 scripts/relabel_session_camp.py --all-unknown --apply
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.data.collector import MatchRecorder  # noqa: E402
from wzry.vision.camp_identity import classify  # noqa: E402

_KIND_TO_BASE = {"hero": "hero", "turret": "turret", "crystal": "crystal",
                 "minion": "minion", "monster": "neutral_monster"}


def relabel(path: Path, apply: bool) -> dict:
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    rec = MatchRecorder(base_dir=path.parent)     # 只借用投票逻辑，不 start()
    before = Counter()
    after = Counter()
    n_units = n_known = 0
    for r in rows:
        heroes = (r.get("extra") or {}).get("mem_heroes") or []
        self_camp = (r.get("sources") or {}).get("self_camp")
        if self_camp not in ("blue", "red"):
            self_camp = rec._camp_from_minimap_vote(r, heroes)
        for u in (r.get("units") or []):
            if u.get("coord_src") != "memory":
                continue
            n_units += 1
            before[str(u.get("cls"))] += 1
            if not self_camp:
                after[str(u.get("cls"))] += 1
                continue
            kind = str(u.get("kind") or "")
            base = _KIND_TO_BASE.get(kind)
            if base is None:
                after[str(u.get("cls"))] += 1
                continue
            side = classify({"camp": u.get("camp")}, self_camp)
            if base == "neutral_monster":
                cls = "neutral_monster"
            else:
                cls = f"{side}_{base}"
            u["side"] = side
            u["cls"] = cls
            if side != "unknown":
                n_known += 1
            after[cls] += 1
        src = r.setdefault("sources", {})
        if self_camp and src.get("self_camp") != "minimap_vote":
            src["self_camp"] = "minimap_vote"
        # 注：self_state 在存量行里多为 null（内存态没落进这一列），
        # 阵营只通过 units[*].camp / sources.self_camp 表达，这里不再动它。

    info = {"path": str(path), "rows": len(rows), "units": n_units,
            "known_after": n_known, "camp": rec._camp_locked,
            "before": dict(before.most_common(6)), "after": dict(after.most_common(6))}
    if apply:
        bak = path.with_suffix(".jsonl.bak-camp")
        if not bak.exists():
            shutil.copy2(path, bak)
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                        encoding="utf-8")
        man = path.parent / "manifest.json"
        if man.is_file():
            m = json.loads(man.read_text(encoding="utf-8"))
            m.setdefault("meta", {})["camp_relabel"] = {
                "self_camp": rec._camp_locked, "units": n_units,
                "known_after": n_known, "tool": "relabel_session_camp.py"}
            man.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=None)
    ap.add_argument("--all-unknown", action="store_true",
                    help="扫描所有 units 里 unknown 占比 >50% 的会话")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    base = ROOT / "data" / "matches"
    targets = []
    if args.session:
        targets = [base / args.session / "states.jsonl"]
    elif args.all_unknown:
        for p in sorted(base.glob("*/states.jsonl")):
            try:
                first = p.read_text(encoding="utf-8").splitlines()[:200]
                rows = [json.loads(l) for l in first if l.strip()]
            except Exception:
                continue
            tot = sum(len(r.get("units") or []) for r in rows)
            unk = sum(1 for r in rows for u in (r.get("units") or [])
                      if str(u.get("cls", "")).startswith("unknown"))
            if tot and unk / tot > 0.5:
                targets.append(p)
    else:
        ap.error("需要 --session 或 --all-unknown")

    for p in targets:
        if not p.is_file():
            print(f"跳过（不存在）: {p}")
            continue
        info = relabel(p, args.apply)
        print(f"\n{Path(info['path']).parent.name}")
        print(f"  行 {info['rows']} / 内存单位 {info['units']} / 回填后已知阵营 {info['known_after']}")
        print(f"  锁定自身阵营: {info['camp']}")
        print(f"  改前 {info['before']}")
        print(f"  改后 {info['after']}")
    print("\n（dry-run：未写盘；确认后加 --apply）" if not args.apply else "\n已写盘（原文件备份 *.bak-camp）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
