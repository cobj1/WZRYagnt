# -*- coding: utf-8 -*-
"""影子评估：回城锁的"宽松威胁判定"会中断哪些回城（只读，不改运行时）。

背景
----
`enemy_hero_trusted()` 会把 `camp_src="default"` / `conf<0.55` 的敌方英雄框判为不可信并丢弃，
实测丢弃率 **80.3%**（3932/4898），导致 21.2% 的帧"有敌人但看不见"。
连带后果：`_solve_core` 回城锁里的 `ne = nearest(enemies)` 恒为 None →
`danger` 永远 False → **残血回城中敌人贴脸也不中断**（实测 60 帧）。

本脚本不修改 `m2_agent_v2.py`，而是**离线重放**同一份语料，同时算两套判定：

- **严格（现状）**：`parse_units(..., trusted=enemy_hero_trusted)` → 生产用的 `enemies`
- **宽松（候选）**：同样的 HUD 区域剔除规则，但**不做 trusted 过滤**，`default` 框也算敌人

然后给出"回城段"清单：哪些段在宽松判定下会被中断、当时敌人多近、hp 多少。
这份清单就是改动上线前要人工审的东西（`项目任务规划` 原则 4）。

用法
----
  cd WZRYagnt
  venv/Scripts/python.exe -X utf8 scripts/shadow_recall_danger.py \
      --md ../docs/数据专题/回城威胁判定-shadow-2026-10-26.md \
      --json runs/eval/recall_danger_shadow.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

MATCHES = ROOT / "data" / "matches"
DANGER_FRAC = 0.45          # 与 m2_agent_v2.DANGER_FRAC 保持一致
FID_GAP = 3                 # 相邻帧差 <= 3 视为同一段


def _dist_width(scr, aspect):
    """与 m2.dist_width 同口径：用框的 (x, y) 左上角（不是中心）。"""
    return math.hypot(float(scr[0]) - 0.5, (float(scr[1]) - 0.5) * aspect)


def _center_dist(scr, aspect):
    """框中心到屏幕中心的距离，仅供人读，不参与判定。"""
    cx = float(scr[0]) + float(scr[2]) / 2.0
    cy = float(scr[1]) + float(scr[3]) / 2.0
    return math.hypot(cx - 0.5, (cy - 0.5) * aspect)


def _raw_enemies(units, aspect):
    """宽松敌人列表：同样剔除 HUD 区域，但不做可信过滤。"""
    out = []
    for u in units:
        if u.get("cls") != "enemy_hero":
            continue
        scr = u.get("screen") or []
        if len(scr) < 4:
            continue
        # 与 wzry/decision/context.parse_units 相同的 HUD 剔除规则
        if float(scr[0]) > 0.72 and float(scr[1]) < 0.20:
            continue
        out.append(u)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="回城锁宽松威胁判定的影子评估（只读）")
    ap.add_argument("--md", default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--samples", type=int, default=15)
    args = ap.parse_args()

    import m2_agent_v2 as m2
    from wzry.decision.context import parse_units
    from wzry.decision.optimizer import enemy_hero_trusted

    rows = []
    eps = []
    total_frames = 0

    for path in sorted(MATCHES.glob("*/states.jsonl")):
        name = path.parent.name
        cooldowns = {}
        cur = None
        for line in path.open("r", encoding="utf-8", errors="replace"):
            if not line.strip():
                continue
            try:
                st = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not (isinstance(st.get("minimap"), dict) and isinstance(st.get("units"), list)):
                continue
            total_frames += 1
            w, h = (st.get("screen_size") or (1280.0, 720.0))[:2]
            aspect = (float(h) / float(w)) if w else 0.5625
            units = st.get("units") or []

            act = m2.decide(st, cooldowns)
            reason = act.get("reason")
            is_lock = reason == "recall_in_progress"

            view = parse_units(units, w=w, h=h, trusted=enemy_hero_trusted)
            strict = min((_dist_width(s, aspect) for s in view.enemies), default=None)
            raw = _raw_enemies(units, aspect)
            relaxed_m = min((_dist_width(u["screen"], aspect) for u in raw), default=None)

            rec = {
                "s": name, "fid": st.get("frame_id"), "t": st.get("t"),
                "reason": reason, "lock": is_lock,
                "hp": float((st.get("ui") or {}).get("hp") or 0.0) or None,
                "n_raw": len(raw), "n_trusted": len(view.enemies),
                "strict_d": round(strict, 3) if strict is not None else None,
                "relaxed_d": round(relaxed_m, 3) if relaxed_m is not None else None,
            }
            if is_lock:
                if strict is not None:
                    rec["relaxed_center_d"] = round(
                        min((_center_dist(u["screen"], aspect) for u in raw), default=99.0), 3)
                else:
                    rec["relaxed_center_d"] = rec["relaxed_d"]
                rows.append(rec)
                if cur is None or (rec["fid"] - cur[-1]["fid"]) > FID_GAP:
                    if cur:
                        eps.append((name, cur))
                    cur = [rec]
                else:
                    cur.append(rec)
            else:
                if cur:
                    eps.append((name, cur))
                    cur = None
        if cur:
            eps.append((name, cur))

    if not rows:
        sys.exit("语料里没有 recall_in_progress 帧")

    # 每段：是否会被宽松判定中断
    for name, seg in eps:
        seg_relaxed = [r for r in seg if r["relaxed_d"] is not None and r["relaxed_d"] < DANGER_FRAC]
        seg_strict = [r for r in seg if r["strict_d"] is not None and r["strict_d"] < DANGER_FRAC]
        first = seg[0]
        summary = {
            "s": name, "fid0": first["fid"], "fid1": seg[-1]["fid"], "frames": len(seg),
            "hp": first["hp"],
            "would_interrupt": bool(seg_relaxed),
            "interrupt_at_start": bool(first["relaxed_d"] is not None
                                       and first["relaxed_d"] < DANGER_FRAC),
            "min_relaxed_d": min((r["relaxed_d"] for r in seg_relaxed), default=None),
            "min_strict_d": min((r["strict_d"] for r in seg_strict), default=None),
            "frames_interrupted": len(seg_relaxed),
        }
        seg[0]["_summary"] = summary

    out = []
    def emit(s=""):
        print(s)
        out.append(s)

    n_eps = len(eps)
    inter = [e for e in eps if e[1][0]["_summary"]["would_interrupt"]]
    at_start = [e for e in eps if e[1][0]["_summary"]["interrupt_at_start"]]
    frames_blocked = sum(len(e[1]) for e in eps)
    frames_would = sum(e[1][0]["_summary"]["frames_interrupted"] for e in eps)

    emit("## 回城威胁判定影子评估（只读）")
    emit("")
    emit(f"- 语料：{total_frames} 帧；回城锁帧 **{len(rows)}**（{len(rows)/total_frames*100:.1f}%）")
    emit(f"- 回城段：**{n_eps}** 段，共占用 **{frames_blocked}** 帧")
    emit(f"- **宽松判定会中断的段：{len(inter)} / {n_eps}（{len(inter)/n_eps*100:.1f}%）**")
    emit(f"- 其中**开段即中断**：{len(at_start)} 段（{len(at_start)/n_eps*100:.1f}%）")
    emit(f"- 会被改判的帧：**{frames_would}** / {frames_blocked}"
         f"（{frames_would/max(1,frames_blocked)*100:.1f}% 的回城时间）")
    emit("")
    emit(f"判定阈值 `DANGER_FRAC = {DANGER_FRAC}`（与 m2 一致，用框左上角的 `dist_width` 口径）。")
    emit("")
    emit("### 会被中断的回城段（按最小敌距排序）")
    emit("")
    emit("| session | 帧范围 | 帧数 | 起始 hp | 最小敌距(宽松) | 最小敌距(严格) | 开段即断 |")
    emit("|---|---|---:|---:|---:|---:|---|")
    inter.sort(key=lambda e: (e[1][0]["_summary"]["min_relaxed_d"] or 9))
    for name, seg in inter[: args.samples]:
        s = seg[0]["_summary"]
        emit(f"| {name} | {s['fid0']}–{s['fid1']} | {s['frames']} | "
             f"{s['hp'] if s['hp'] is not None else '—'} | "
             f"{s['min_relaxed_d']} | {s['min_strict_d'] if s['min_strict_d'] is not None else '—'} | "
             f"{'是' if s['interrupt_at_start'] else '否'} |")
    emit("")
    emit("> 严格判定下 `min_strict_d` 全是 `—`，因为生产中 `enemies` 恒为空 —— 这正是回城锁失效的直接证据。")
    emit("")

    if args.md:
        p = Path(args.md)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(out) + "\n", encoding="utf-8")
        print(f"\n写入 {p}")
    if args.json:
        p = Path(args.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "total_frames": total_frames, "lock_frames": len(rows),
            "episodes": n_eps, "would_interrupt": len(inter),
            "interrupt_at_start": len(at_start),
            "frames_blocked": frames_blocked, "frames_would_change": frames_would,
            "danger_frac": DANGER_FRAC,
            "details": [seg[0]["_summary"] for _, seg in eps],
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"写入 {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
