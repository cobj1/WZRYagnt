# -*- coding: utf-8 -*-
"""动作分布对照：规则层实际输出 vs 人类语义先验（只读）。

回答一个问题：**规则层的行为分布，离"人类会怎么动"有多远？**
对照双方：
  规则层  —— 生产状态构造（`eval_decision_offline._rule_view`）+ `optimizer.solve`
  人类先验 —— `hokdata/action_prior.json`（Dota2 语义动作映射到 HoK 10 类后的比例）

为什么必须复用 `_rule_view`
---------------------------
自拼 `optimizer.solve(state, 固定 fallback, hp)` 会让规则退化成 100% `map_move`；
同语料用 `_rule_view` 能出 412 个 `skill`（见 `eval_laya_shadow_offline.py` 顶部说明）。
两者混用会得出完全相反的结论。

输出口径
--------
- **活跃帧**（非兜底）：规则真正做了决策的帧，是唯一有判别力的子集。
- **兜底帧**：`_is_fallback` 为真，规则没给候选，只有兜底动作。
- 四分类对照：`move` / `skill`（含 attack）/ `recall` / `none`，与先验的
  move / skill / recall / none 一一对齐；`restore` 先验里没有，单列。

用法
----
  cd WZRYagnt
  venv/Scripts/python.exe -X utf8 scripts/report_action_distribution.py \
      --json runs/eval/action_dist.json --limit 0
  # 只扫 2000 帧快速看
  venv/Scripts/python.exe -X utf8 scripts/report_action_distribution.py --limit 2000
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
WORKSPACE = ROOT.parent
PRIOR = WORKSPACE / "hokdata" / "action_prior.json"

# 规则动作类型 -> 对照用的四分类
BUCKET = {
    "map_move": "move", "move": "move", "move_dir": "move",
    "skill": "skill", "skill1": "skill", "skill2": "skill", "skill3": "skill",
    "attack": "skill", "combo": "skill",
    "recall": "recall",
    "restore": "restore",
    "none": "none", "stop": "none", "wait": "none", "hold": "none",
}


def _load_eval():
    path = ROOT / "scripts" / "eval_decision_offline.py"
    spec = importlib.util.spec_from_file_location("eval_decision_offline", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def scan(ev, limit: int, sessions_prefix: str | None, prod: bool = False):
    """遍历 states.jsonl，产出每帧的动作类型与是否兜底。

    prod=True 时调用生产决策 `m2_agent_v2.decide(state, cooldowns)`（**唯一真实口径**），
    并按会话维护 cooldowns（技能节流依赖它）。
    prod=False 时走 `eval_decision_offline._rule_view`，其中 `solved` 依赖该脚本内的
    **占位 fallback**（走己方绿点），会把技能/攻击/回城全部吞成 map_move ——
    只能用来对比"评估口径 vs 生产口径"的差距，不代表生产。
    """
    types = collections.Counter()
    buckets = collections.Counter()
    act_types = collections.Counter()
    act_buckets = collections.Counter()
    solved_types = collections.Counter()
    n_frames = n_fallback = n_err = 0
    per_session = collections.Counter()

    decide = None
    if prod:
        sys.path.insert(0, str(ROOT / "scripts"))
        import m2_agent_v2  # noqa: WPS433
        decide = m2_agent_v2.decide

    cooldowns = {}
    cur_session = None

    for path in sorted(MATCHES.glob("*/states.jsonl")):
        name = path.parent.name
        if sessions_prefix and not name.startswith(sessions_prefix):
            continue
        if name != cur_session:
            cur_session = name
            cooldowns = {}          # 每局独立的节流状态

        for line in path.open("r", encoding="utf-8", errors="replace"):
            if limit and n_frames >= limit:
                break
            if not line.strip():
                continue
            try:
                st = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not (isinstance(st.get("minimap"), dict) and isinstance(st.get("units"), list)):
                continue

            if prod:
                try:
                    act = decide(st, cooldowns)
                except Exception:
                    n_err += 1
                    continue
                tt = act.get("type")
                is_fb = str(act.get("reason", "")).startswith(("eval_rule_fb", "fb"))
            else:
                hp, _src = ev._hp_of(st)
                try:
                    rv = ev._rule_view(st, hp)
                except Exception:
                    n_err += 1
                    continue
                tt = rv.get("solved_type") or (rv["top_types"][0] if rv.get("top_types") else None)
                solved_types[rv.get("solved_type")] += 1
                is_fb = ev._is_fallback(rv)
            if not tt:
                n_err += 1
                continue

            n_frames += 1
            per_session[name] += 1
            types[tt] += 1
            buckets[BUCKET.get(tt, "other")] += 1
            if not is_fb:
                act_types[tt] += 1
                act_buckets[BUCKET.get(tt, "other")] += 1
            else:
                n_fallback += 1
        if limit and n_frames >= limit:
            break
    return {
        "frames": n_frames, "fallback": n_fallback, "errors": n_err,
        "active": n_frames - n_fallback,
        "types": dict(types.most_common()),
        "buckets": dict(buckets.most_common()),
        "active_types": dict(act_types.most_common()),
        "active_buckets": dict(act_buckets.most_common()),
        "sessions": len(per_session),
        "solved_types": dict(solved_types.most_common()),
        "mode": "prod(decide)" if prod else "eval(_rule_view)",
    }


def load_prior():
    """返回 (四分类比例, 先验指标)。文件缺失时返回 ({}, {})。"""
    if not PRIOR.exists():
        return {}, {}
    d = json.loads(PRIOR.read_text(encoding="utf-8"))
    hp = d.get("hok_prior") or {}
    # 先验 10 类 -> 四分类
    rule_bucket = {"move": "move", "skill1": "skill", "skill2": "skill", "skill3": "skill",
                   "attack": "skill", "summoner": "skill",
                   "recall": "recall", "none": "none", "buy": "other", "restore": "other"}
    out = collections.Counter()
    for k, v in hp.items():
        out[rule_bucket.get(k, "other")] += v
    total = sum(out.values()) or 1.0
    return {k: v / total for k, v in out.items()}, d.get("metrics") or {}


def pct(d, keys):
    tot = sum(d.get(k, 0) for k in keys) or 1
    return {k: d.get(k, 0) / tot for k in keys}


def main() -> int:
    ap = argparse.ArgumentParser(description="规则层动作分布 vs 人类语义先验（只读）")
    ap.add_argument("--limit", type=int, default=0, help="最多扫描帧数，0=全部")
    ap.add_argument("--sessions", default=None, help="会话名前缀过滤")
    ap.add_argument("--json", default=None)
    ap.add_argument("--prod", action="store_true",
                    help="调用生产决策 m2_agent_v2.decide（真实口径）；默认走 eval 口径")
    args = ap.parse_args()

    t0 = time.time()
    ev = _load_eval()
    r = scan(ev, args.limit, args.sessions, prod=args.prod)
    dt = time.time() - t0
    if not r["frames"]:
        print("没有可评估帧（先确认 data/matches 下有 states.jsonl）")
        return 1

    prior = load_prior()
    keys = ["move", "skill", "recall", "none", "restore", "other"]

    print(f"口径: {r['mode']}")
    print(f"扫描 {r['frames']} 帧 / {r['sessions']} 会话，兜底 {r['fallback']} "
          f"({r['fallback']/r['frames']*100:.1f}%)，活跃 {r['active']}，跳过 {r['errors']}，耗时 {dt:.1f}s")
    print("\n规则动作类型 Top:", dict(list(r["types"].items())[:8]))
    print("活跃帧动作类型:", dict(list(r["active_types"].items())[:8]) or "（无活跃帧）")
    if r.get("solved_types"):
        print("eval 的 solved_type（依赖占位 fallback）:", r["solved_types"])

    print(f"\n{'四分类':8}{'规则(全帧)':>12}{'规则(活跃)':>12}{'人类先验':>10}{'差值(活跃-先验)':>16}")
    allp = pct(r["buckets"], keys)
    actp = pct(r["active_buckets"], keys) if r["active"] else {}
    prp = prior[0] if prior else {}
    for k in keys:
        a = f"{allp.get(k,0)*100:11.1f}%"
        b = f"{actp.get(k,0)*100:11.1f}%" if actp else f"{'-':>11}"
        c = f"{prp.get(k,0)*100:9.1f}%" if prp else f"{'-':>9}"
        d = (f"{(actp.get(k,0)-prp.get(k,0))*100:+15.1f}pp" if actp and prp else f"{'-':>15}")
        print(f"{k:8}{a}{b}{c}{d}")

    if args.json:
        out = {"scan": r, "rule_all": allp, "rule_active": actp,
               "prior": prp, "prior_metrics": prior[1] if prior else {},
               "seconds": round(dt, 1), "limit": args.limit}
        p = Path(args.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n写入 {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
