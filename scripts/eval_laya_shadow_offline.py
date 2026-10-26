# -*- coding: utf-8 -*-
"""Laya scorer 离线影子 A/B（S2-4）：规则 vs 规则+Laya。

设计要点（都是踩过坑之后的结论）
--------------------------------
1. **必须复用 `eval_decision_offline._rule_view()` 的状态构造**。
   自己拼 `optimizer.solve(state, 固定 fallback, hp)` 会让规则退化成 100% `map_move`
   （实测全量 15929 帧 0 个非 map_move，而同语料 `_rule_view` 能出 412 个 `skill`）。
2. **只在"规则活跃帧"上评**。语料里规则有 76% 走兜底、90.8% 输出 `map_move`，
   这类帧 Laya 没有可重排空间，混进总体只会稀释结论。
3. **按真实节奏回放**。`LayaScorer` 是异步 worker + 缓存 + 节流，`reorder()` 只入队，
   结果由下一次调用消费；不限速回放会让绝大多数帧被判 throttled，得到假结论。

用法
----
  # 1) 扫出规则活跃帧（不跑模型，很快）
  python -X utf8 scripts/eval_laya_shadow_offline.py --limit 0 --scan-only \
      --dump-active-keys runs/eval/active_keys.json
  # 2) 在活跃子集上做 A/B
  python -X utf8 scripts/eval_laya_shadow_offline.py --keys runs/eval/active_keys.json \
      --hz 2 --json runs/eval/laya_ab_active.json --report docs/Laya离线影子AB-2026-10-26.md
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MATCHES = ROOT / "data" / "matches"
COARSE = {
    "skill": "engage", "attack": "engage", "combo": "engage",
    "map_move": "move", "recall": "move", "restore": "move",
    "none": "none",
}


def _load_eval_module():
    path = ROOT / "scripts" / "eval_decision_offline.py"
    spec = importlib.util.spec_from_file_location("eval_decision_offline", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def load_frames(limit: int, keys_file: str | None, seed: int, only_active: bool):
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
            if want is not None:
                if st.get("frame_id") not in want[name]:
                    continue
            elif not (isinstance(st.get("minimap"), dict) and isinstance(st.get("units"), list)):
                continue
            hp, _src = ev._hp_of(st)
            try:
                rv = ev._rule_view(st, hp)
            except Exception:
                continue
            if only_active and ev._is_fallback(rv):
                continue
            rows.append({"s": name, "st": st, "hp": hp, "fb": ev._fallback_action(st),
                         "rule_top": rv["top_types"][0] if rv.get("top_types") else None,
                         "rule_solved": rv.get("solved_type"),
                         "rule_active": not ev._is_fallback(rv),
                         "ncand": rv.get("n_cand")})
    if want is None:
        import random
        random.Random(seed).shuffle(rows)
        rows = rows[:limit] if limit else rows
    elif limit:
        rows = rows[:limit]
    return rows


def run_pass(rows, scorer, hz: float):
    from wzry.decision import optimizer as opt

    if scorer is None:
        opt.set_scorer(None)
    else:
        opt.set_scorer(scorer, beta=scorer.beta, mode=scorer.mode)
    out = []
    t0 = None
    for i, r in enumerate(rows):
        if hz > 0:
            if t0 is None:
                t0 = time.perf_counter()
            drift = (time.perf_counter() - t0) - i / hz
            if drift < 0:
                time.sleep(-drift)
        t_start = time.perf_counter()
        act = opt.solve(r["st"], dict(r["fb"]), r["hp"], "blue")
        out.append({"s": r["s"], "f": r["st"].get("frame_id"),
                    "type": act.get("type"), "reason": act.get("reason"),
                    "ms": round((time.perf_counter() - t_start) * 1000, 2)})
    return out


def pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * (len(xs) - 1)))], 2)


def _headroom_section(summary: dict) -> list[str]:
    """引用 `probe_laya_headroom.py` 的产物，解释翻转率上限与 β 的物理含义。

    没有这个文件就明确写"未测"，不编造数字——翻转率低有三个独立原因（规则分差、
    异步当拍无概率、`gv >= r_max` 短路），不区分就会把"接线没给机会"误读成"模型没信号"。
    """
    p = ROOT / "runs" / "eval" / "laya_headroom.json"
    if not p.exists():
        return ["## 4. 可翻转余量", "", "未测：先跑 `scripts/probe_laya_headroom.py`。", ""]
    h = json.loads(p.read_text(encoding="utf-8"))
    gc = h.get("gap_cross") or {}
    fl = h.get("flippable_by_beta") or {}
    return [
        "## 4. 可翻转余量（解释翻转率上限）",
        "",
        f"> 来自 `scripts/probe_laya_headroom.py`（不加载模型，只算候选分差）：",
        f"> `{p.as_posix().split('aiKings/')[-1]}`，`{h['frames']}` 帧、`top_k={h['top_k']}`。",
        "",
        f"- 规则 top-1 粗类：`{h['top1_coarse']}`——所以翻转绝大多数是 `move → engage/none`",
        f"- top-2 与 top-1 **同粗类**：{h['same_coarse_top2']}/{h['frames']}"
        "（换序不算翻转，真正的翻转必须跨类）",
        f"- top-`k` 内存在异粗类候选：**{h['k_has_cross_coarse']}/{h['frames']}"
        f"（{h['k_cross_rate']:.1%}）= k 内理论翻转上限**",
        f"- 翻到异粗类所需的原始分差：p50 **{gc.get('p50')}**、p90 {gc.get('p90')}、"
        f"max {gc.get('max')}",
        f"- β 能补的**极差等于 β 本身**（bonus 为 `β·(p−1/K)`，最大间距 = β），"
        f"故可按 β 翻转的帧数：{fl}",
        "",
        "**这条链路的三层限制（互不重叠，必须分开看）**：",
        "",
        "| 层 | 机制 | 量级 | 后果 |",
        "|---|---|---|---|",
        "| 1 规则侧 | `_solve_core` 先算 `r_max`（原始分）再 `reorder`，之后 `if gv >= r_max: return grid_best` | "
        f"本次 `grid_best` {h.get('grid_best')}/{h['frames']}"
        f"（{(h.get('grid_best_rate') or 0) * 100:.1f}%） | 该分支用原始分比较，"
        "Laya 的选择被整体丢弃 |",
        "| 2 异步侧 | `reorder()` 只入队，结果由**下一次**调用消费；缓存键是 `(状态桶, 候选标签)` | "
        f"本次 submits={summary['scorer'].get('submits')} 但 reorders={summary['scorer'].get('reorders')}"
        f"（拿到概率的帧 {summary['scorer'].get('reorders', 0) / max(1, summary['scorer'].get('submits') or 1):.0%}）"
        " | 开 `--stale-ttl` 才能让滞后概率复用 |",
        "| 3 信号侧 | 混合分要跨过分差，需 `β·(p_异 − p_1) > gap` | "
        f"gap p50={gc.get('p50')}；β=4 时仍需 Δp > {round((gc.get('p50') or 0) / 4, 3)} | "
        "模型对非规则首选没有压倒性置信度 |",
        "",
        "→ **结论：翻转率 0 不能判 laya 无效**。第 1、2 层是可修的接线问题，"
        "第 3 层才是模型能力问题；三层混在一起时任何 β 标定都无意义。",
        "",
        "**证据等级说明**：层 1/2 的量级是逐帧实测；层 3 的「需 Δp > …」来自 `gap_cross` 与 β 的"
        "算术比较，**未逐帧确认那 120 个 reorder 帧是否恰好落在 grid_best 子集里**（两者若高度重叠，"
        "主因应归层 1）。要闭合这个缺口，需在 `run_pass` 里同时记录 `reorder` 后的 `scored[0]` 类型，"
        "与 `solve()` 最终返回类型逐帧对比。",
        "",
    ]


def render_report(summary: dict, out_path: Path, report_path: Path) -> None:
    """从 summary 渲染报告。集中一处，`--report-only` 与正常跑共用。"""
    sc = summary.get("scorer") or {}
    scanned = summary.get("scanned_frames") or summary.get("frames") or 0
    active = summary.get("scanned_active") or summary.get("frames") or 0
    n = summary.get("frames") or 0
    pct_active = (active / scanned) if scanned else 0.0
    L = [
        "# Laya scorer 离线影子 A/B（S2-4）",
        "",
        "> 脚本 `scripts/eval_laya_shadow_offline.py`；"
        f"结果 `{out_path.as_posix().split('aiKings/')[-1]}`",
        f"> 生成 {time.strftime('%Y-%m-%d %H:%M')}　{summary.get('hz')}Hz　"
        f"β={summary.get('beta')}　{summary.get('backend')}",
        "",
        "## 0. 摘要",
        "",
        f"- 扫描 {scanned} 帧，其中**规则活跃 {active} 帧**（{pct_active:.1%}），"
        f"A/B 在这 {n} 帧上进行",
        f"- 规则 vs 规则+Laya：细类一致 **{summary['exact_rate']:.1%}**、"
        f"粗类一致 **{summary['coarse_rate']:.1%}**、翻转 **{summary['flip']} 帧"
        f"（{summary['flip_rate']:.1%}）**",
        f"- scorer：submits={sc.get('submits')}、predicts={sc.get('predicts')}、"
        f"reorders={sc.get('reorders')}、throttled={sc.get('throttled')}、errors={sc.get('errors')}",
        f"- 延迟：模型 e2e p50 **{sc.get('e2e_p50')}ms** / p95 {sc.get('e2e_p95')}ms；"
        f"纯推理 p50 {sc.get('ov_infer_p50')}ms；加载 {summary.get('model_load_s')}s",
        "",
        "## 1. 按规则动作类型拆分",
        "",
        "| 规则动作 | 帧 | 翻转 | 翻转率 |",
        "|---|---:|---:|---:|",
    ]
    for k, v in (summary.get("by_rule_type") or {}).items():
        L.append(f"| {k} | {v['n']} | {v['flip']} | {v['flip_rate']:.1%} |")
    L += [
        "",
        "## 2. 翻转方向",
        "",
        f"- 规则分布：{summary.get('rule_dist')}",
        f"- 规则+Laya 分布：{summary.get('laya_dist')}",
        f"- 翻转对（前 10）："
        f"{dict(sorted((summary.get('flip_by_pair') or {}).items(), key=lambda kv: -kv[1])[:10])}",
        "",
        "## 3. 口径与局限",
        "",
        "1. 基线 = `optimizer.solve()` 在生产状态构造下的输出（fallback 由 `_fallback_action(state)` 给出，"
        "hp 由 `_hp_of(state)` 给出），与 `_rule_view()` 一致；不再自拼 fallback。",
        "2. 只在**规则活跃帧**（非 fallback）上评：语料里 76% 帧走兜底，那些帧 Laya 没有重排空间。",
        f"3. 异步 scorer 必须按真实节奏回放（{summary.get('hz')}Hz）；不限速会让绝大多数帧判 throttled。",
        "4. **没有 ground truth**：翻转率只说明\"改了多少\"，不说明\"改得对不对\"。"
        "判优劣需 S2-3 收益标签或真机胜率。",
        "",
    ]
    L += _headroom_section(summary)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(L) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="帧数上限；默认不截断。配合 --keys 时几乎总应为 0/省略")
    ap.add_argument("--keys", default=None, help="固定帧集合（frame_id）")
    ap.add_argument("--seed", type=int, default=20261024)
    ap.add_argument("--hz", type=float, default=2.0, help="回放节奏；0=不限速（异步 scorer 会失真）")
    ap.add_argument("--beta", type=float, default=1.0)
    ap.add_argument("--backend", default="openvino")
    ap.add_argument("--stale-ttl", type=float, default=0.0,
                    help="滞后一拍概率复用窗口(秒)；0=关闭(旧行为)")
    ap.add_argument("--only-active", action="store_true",
                    help="只保留规则非 fallback 的帧（默认在抽样时不过滤，报表分列）")
    ap.add_argument("--scan-only", action="store_true", help="只扫规则，不跑模型")
    ap.add_argument("--dump-active-keys", default=None)
    ap.add_argument("--json", default=str(ROOT / "runs" / "eval" / "laya_ab.json"))
    ap.add_argument("--report", nargs="?", const=str(ROOT / "docs" / "Laya离线影子AB-2026-10-26.md"))
    ap.add_argument("--report-only", default=None, metavar="JSON",
                    help="不重跑，只从已落盘 JSON 重渲染报告（改了模板时用）")
    args = ap.parse_args()

    if args.report_only:
        data = json.loads(Path(args.report_only).read_text(encoding="utf-8"))
        render_report(data, Path(args.report_only),
                      Path(args.report or str(ROOT / "docs" / "Laya离线影子AB-2026-10-26.md")))
        print(f"[report] {args.report or ROOT / 'docs' / 'Laya离线影子AB-2026-10-26.md'}")
        return 0

    rows = load_frames(args.limit, args.keys, args.seed, args.only_active)
    if not rows:
        raise SystemExit("没有可评估帧")
    active = [r for r in rows if r["rule_active"]]
    print(f"[frames] {len(rows)}（{len({r['s'] for r in rows})} 会话），规则活跃 {len(active)}"
          f"（{len(active) / len(rows):.1%}）")

    if args.dump_active_keys:
        agg = collections.defaultdict(list)
        for r in active:
            agg[r["s"]].append(int(r["st"].get("frame_id")))
        p = Path(args.dump_active_keys)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({k: sorted(v) for k, v in agg.items()}, ensure_ascii=False),
                     encoding="utf-8")
        print(f"[active] {len(active)} 帧 → {p}")
    if args.scan_only:
        return 0

    # 只在活跃子集上跑 A/B：非活跃帧没有重排空间，混入会把翻转率稀释成 0
    work = active if args.only_active or args.keys else active
    if not work:
        print("[warn] 活跃子集为空，无法 A/B")
        return 0
    print(f"[ab] 在活跃子集上评估：{len(work)} 帧")

    base = run_pass(work, None, args.hz)

    from wzry.decision.laya_scorer import LayaScorer
    scorer = LayaScorer(mode="ensemble", beta=args.beta, backend=args.backend,
                        ov_max_len=512, stale_probs_ttl=args.stale_ttl)
    t_load = time.perf_counter()
    scorer.start()
    while not scorer.stats().get("model_ok") and time.perf_counter() - t_load < 180:
        time.sleep(0.5)
    load_s = round(time.perf_counter() - t_load, 1)
    if not scorer.stats().get("model_ok"):
        print(f"[warn] 模型未加载：{scorer.stats().get('last_error') or 'unknown'}")
    laya = run_pass(work, scorer, args.hz)
    time.sleep(2.0)
    st1 = scorer.stats()
    scorer.stop()

    n = len(work)
    exact = sum(1 for a, b in zip(base, laya) if a["type"] == b["type"])
    coarse = sum(1 for a, b in zip(base, laya)
                 if COARSE.get(str(a["type"]), a["type"]) == COARSE.get(str(b["type"]), b["type"]))
    flips = [{"s": a["s"], "f": a["f"], "rule": a["type"], "laya": b["type"]}
             for a, b in zip(base, laya) if a["type"] != b["type"]]
    by_rule = collections.defaultdict(lambda: {"n": 0, "flip": 0})
    for a, b in zip(base, laya):
        rec = by_rule[str(a["type"])]
        rec["n"] += 1
        rec["flip"] += int(a["type"] != b["type"])

    summary = {
        "frames": n, "hz": args.hz, "beta": args.beta, "backend": args.backend,
        "model_load_s": load_s, "stale_probs_ttl": args.stale_ttl,
        "exact_same": exact, "exact_rate": round(exact / n, 4),
        "coarse_same": coarse, "coarse_rate": round(coarse / n, 4),
        "flip": len(flips), "flip_rate": round(len(flips) / n, 4),
        "rule_ms_p50": pct([r["ms"] for r in base], 0.5),
        "rule_ms_p95": pct([r["ms"] for r in base], 0.95),
        "with_laya_ms_p50": pct([r["ms"] for r in laya], 0.5),
        "with_laya_ms_p95": pct([r["ms"] for r in laya], 0.95),
        "scorer": {k: st1.get(k) for k in
                   ("model_ok", "submits", "predicts", "reorders", "throttled",
                    "cache_hits", "errors", "label_dups", "tripped", "stale_hits",
                    "ov_infer_p50", "ov_infer_p95", "e2e_p50", "e2e_p95")},
        "by_rule_type": {k: {"n": v["n"], "flip": v["flip"],
                             "flip_rate": round(v["flip"] / v["n"], 4)}
                         for k, v in sorted(by_rule.items())},
        "flip_by_pair": dict(collections.Counter(f"{f['rule']}->{f['laya']}" for f in flips)),
        "rule_dist": dict(collections.Counter(str(r["type"]) for r in base)),
        "laya_dist": dict(collections.Counter(str(r["type"]) for r in laya)),
        "scanned_frames": len(rows), "scanned_active": len(active),
    }

    out = Path(args.json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({**summary, "flips": flips[:300],
                               "rule_rows": base, "laya_rows": laya},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[ab] 细类一致 {summary['exact_rate']:.1%} / 粗类 {summary['coarse_rate']:.1%}"
          f" / 翻转 {summary['flip']} 帧（{summary['flip_rate']:.1%}）")
    print(f"[ab] scorer submits={st1.get('submits')} predicts={st1.get('predicts')}"
          f" reorders={st1.get('reorders')} throttled={st1.get('throttled')}"
          f" e2e_p50={st1.get('e2e_p50')}ms")

    if args.report:
        render_report(summary, out, Path(args.report))
        print(f"[report] {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
