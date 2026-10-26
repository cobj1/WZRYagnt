# -*- coding: utf-8 -*-
"""抽查生产决策：`none` 与 `skill` 两类帧判得对不对（只读）。

输入：`report_action_distribution.py --prod --dump` 产出的逐帧 jsonl。

两个必须避开的口径陷阱
----------------------
1. **`none` 不等于"漏决策"**。实测 1 304 帧里 1 272 帧（97.5%）reason 是
   `recall_in_progress` —— 回城读条期间本来就该不动。剩下 32 帧是 `hook_flight` /
   `skill_debounce`，同样是设计内的静默。**扣除这些之后，真正的"无故不动"需要单独统计。**
2. **小地图红点不能当敌人距离**。全语料 **61.7% 的帧红点为空**，拿它判断"敌人贴不贴脸"
   会把大多数帧误判成"没有敌人"。可靠证据是 `units` 里屏幕检测到的 `enemy_hero`。

用法
----
  cd WZRYagnt
  venv/Scripts/python.exe -X utf8 scripts/audit_none_skill.py \
      --dump runs/eval/decisions_prod.jsonl --md ../../docs/数据专题/决策抽查-none与skill-2026-10-26.md
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

SILENT_REASONS = ("recall_in_progress", "hook_flight", "skill_debounce")


def load(path: Path):
    return [json.loads(l) for l in path.open(encoding="utf-8") if l.strip()]


def pct(n, d):
    return f"{n / d * 100:.1f}%" if d else "—"


def main() -> int:
    ap = argparse.ArgumentParser(description="抽查 none/skill 决策（只读）")
    ap.add_argument("--dump", required=True)
    ap.add_argument("--md", default=None)
    ap.add_argument("--samples", type=int, default=12)
    args = ap.parse_args()

    rows = load(Path(args.dump))
    if not rows:
        sys.exit("dump 为空")
    n = len(rows)
    by_type = collections.Counter(r["type"] for r in rows)

    out = []
    def emit(s=""):
        print(s)
        out.append(s)

    emit(f"总帧 **{n}**，类型分布："
         + "，".join(f"`{k}` {v}（{pct(v, n)}）" for k, v in by_type.most_common()))
    emit("")

    # ---------- none ----------
    none = [r for r in rows if r["type"] == "none"]
    if none:
        emit(f"## 一、`none`（{len(none)} 帧 / {pct(len(none), n)}）")
        emit("")
        reasons = collections.Counter(str(r.get("reason")) for r in none)
        emit("| reason | 帧数 | 占比 | 性质 |")
        emit("|---|---:|---:|---|")
        nature = {"recall_in_progress": "回城读条，设计内静默",
                  "hook_flight": "钟馗钩子在飞行，设计内",
                  "skill_debounce": "技能后 50ms 防抖，设计内"}
        for k, v in reasons.most_common():
            emit(f"| `{k}` | {v} | {pct(v, len(none))} | {nature.get(k, '**待查**')} |")
        emit("")
        silent = [r for r in none if str(r.get("reason")) in SILENT_REASONS]
        odd = [r for r in none if str(r.get("reason")) not in SILENT_REASONS]
        emit(f"**设计内静默 {len(silent)} 帧（{pct(len(silent), len(none))}），"
             f"无故不动 {len(odd)} 帧（{pct(len(odd), len(none))}）**")
        emit("")
        if odd:
            e = sum(1 for r in odd if (r.get("n_enemy_screen") or 0) > 0)
            low = sum(1 for r in odd if (r.get("hp") or 1) < 0.35)
            emit(f"- 其中屏幕内有敌英：**{e}/{len(odd)}**（{pct(e, len(odd))}）→ 可能是漏决策")
            emit(f"- 其中 hp<35%：**{low}/{len(odd)}**（{pct(low, len(odd))}）")
            emit("")
            emit("| session | frame | hp | 屏幕敌英 | 红点 | reason |")
            emit("|---|---|---:|---:|---:|---|")
            for r in odd[: args.samples]:
                emit(f"| {r['s']} | {r['fid']} | {r['hp']} | {r.get('n_enemy_screen')} | "
                     f"{r['n_red']} | `{r.get('reason')}` |")
            emit("")

    # ---------- skill ----------
    sk = [r for r in rows if r["type"] == "skill"]
    if sk:
        emit(f"## 二、`skill`（{len(sk)} 帧 / {pct(len(sk), n)}）")
        emit("")
        reasons = collections.Counter(str(r.get("reason")) for r in sk)
        emit("reason：" + "，".join(f"`{k}` {v}" for k, v in reasons.most_common(6)))
        emit("")
        has = sum(1 for r in sk if (r.get("n_enemy_screen") or 0) > 0)
        emit(f"- 屏幕内有敌英：**{has}/{len(sk)}**（{pct(has, len(sk))}）→ 有明确释放对象")
        none_scr = [r for r in sk if (r.get("n_enemy_screen") or 0) == 0]
        emit(f"- 屏幕内无敌英：**{len(none_scr)}/{len(sk)}**（{pct(len(none_scr), len(sk))}）"
             f" → 可能放空（或敌人在屏幕外/被遮挡，units 漏检）")
        emit("")
        if none_scr:
            emit("| session | frame | hp | 红点 | 最近红点 | reason |")
            emit("|---|---|---:|---:|---:|---|")
            for r in none_scr[: args.samples]:
                emit(f"| {r['s']} | {r['fid']} | {r['hp']} | {r['n_red']} | "
                     f"{r.get('near_red')} | `{r.get('reason')}` |")
            emit("")

    # ---------- 对照 ----------
    emit("## 三、参照：屏幕内敌英覆盖率")
    emit("")
    for t in ("map_move", "none", "skill", "attack"):
        sub = [r for r in rows if r["type"] == t]
        if not sub:
            continue
        has = sum(1 for r in sub if (r.get("n_enemy_screen") or 0) > 0)
        emit(f"- `{t}`：{has}/{len(sub)}（{pct(has, len(sub))}）帧屏幕内有敌英")
    emit("")

    if args.md:
        p = Path(args.md)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(out) + "\n", encoding="utf-8")
        print(f"\n写入 {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
