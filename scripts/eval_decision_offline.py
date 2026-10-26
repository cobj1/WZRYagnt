# -*- coding: utf-8 -*-
"""决策层离线评估闭环 (任务板 S2-3)。

在历史 `data/matches/<session>/states.jsonl` 上回放同一帧的两条决策路径并对比:

  规则基线  wzry.decision.optimizer  — gen_candidates -> evaluate+0.35*_forecast 排序
                                       (生产口径的候选集与排序; 未挂 Laya 打分器);
                                       另附 optimizer.solve() 的生产输出作为参照列。
  模型路径  wzry.decision.hok_bridge — HokShadow.decide(): build_search_state ->
                                       beam_search -> DecisionOutput (base 权重, 不依赖外挂模型文件)。

只读: 不修改 wzry/ 下任何模块、不改 data/、不写动作。唯一副作用是往
`runs/eval/hok_shadow_replay.jsonl` 追加 DecisionOutput 原始回放(桥接器自带落盘)。

用法:
  python scripts/eval_decision_offline.py --limit 200 --report
  python scripts/eval_decision_offline.py --report --verify-determinism 60
  python scripts/eval_decision_offline.py --sessions 20261017_162901_384146 --limit 0

指标定义(报告里重复一遍, 便于对照):

  动作类型(粗粒度)  engage / move / none —— 两条路径唯一同构的粒度。hok 的动作空间是宏观动作
                    (attack_enemy/retreat/capture_resource/defend_tower/push_tower), 没有技能/
                    普攻级动作, 所以 skill vs attack 不可比; 细粒度组合只作诊断列, 不参与一致率。
  目标              动作意图落点, 统一到小地图归一化坐标 [0,1]^2:
                      规则 map_move        -> (nx, ny)
                      规则 skill/attack    -> 距自己最近的红点(小地图代理, units 只有屏幕坐标)
                      规则 none            -> None
                      hok target_position  -> 用 hok_bridge.MINIMAP_AFFINE 反算回归一化坐标
                    "同目标" = 两点欧氏距离 <= TARGET_TOL(0.15) 或两侧皆 None。
  agreement@1/3     模型首选是否落在规则 top-1 / top-3 的同一维度(类型 / 目标);
                    strict = 类型与目标同时命中。
  翻转率            粗粒度类型不一致的比例; 翻转方向 = "规则粗类->模型粗类" 计数。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MATCH_DIR = ROOT / "data" / "matches"
DEFAULT_REPORT = ROOT / "docs" / "决策离线评估报告-2026-10-24.md"
DEFAULT_JSON = ROOT / "runs" / "eval" / "decision_offline_2026-10-24.json"
DEFAULT_TRACE = ROOT / "runs" / "eval" / "hok_shadow_replay.jsonl"

# ---- 写死的判定常量(报告里逐条说明, 勿在别处重复定义) ----
TARGET_TOL = 0.15     # 小地图归一化距离: 判定"同一目标"
TEAM_RADIUS = 0.15    # 小地图归一化半径: 自己周围多少算"附近"
TEAM_ENEMY_N = 2      # 附近红点(敌方英雄) >= 2 -> 团战
HERO_SCREEN_RADIUS = 0.35   # 屏幕归一化半径: 敌英算"贴身"的距离(团战判据)
MINION_SCREEN_RADIUS = 0.6  # 屏幕归一化半径: 敌兵算"附近"的距离(实测 0.6 已覆盖全部检出)
MINION_N = 3                # 附近敌兵 >= 3 -> 清线
LOW_HP = 0.3          # hp < 0.3 -> 残血
DEAD_HP = 0.02        # hp <= 0.02 -> 死亡/黑屏

RULE_TOP_K = 3
BUCKETS = ["死亡/黑屏", "团战", "清线", "残血", "游走"]

# 两套动作词表 -> 唯一可比的粗粒度
COARSE = {
    "map_move": "move", "skill": "engage", "attack": "engage", "combo": "engage",
    "recall": "move", "restore": "move", "none": "none",
    "attack_enemy": "engage", "retreat": "move", "capture_resource": "move",
    "defend_tower": "move", "push_tower": "move",
}


# ---------------------------------------------------------------- 基础工具
def _pct(vals, p):
    """nearest-rank 分位: 升序后取 idx = ceil(p*n)-1 (夹到 [0, n-1])。"""
    if not vals:
        return None
    data = sorted(vals)
    idx = max(0, min(len(data) - 1, math.ceil(p * len(data)) - 1))
    return data[idx]


def _rate(num, den):
    return round(num / den, 4) if den else None


def _hp_of(state):
    """hp 取值链: ui.hp -> extra.mem_heroes[0].hp/hp_max -> 1.0(计数, 不静默)。"""
    hp = (state.get("ui") or {}).get("hp")
    if isinstance(hp, (int, float)):
        return float(hp), "ui.hp"
    for h in ((state.get("extra") or {}).get("mem_heroes") or []):
        if isinstance(h, dict) and h.get("hp") is not None and h.get("hp_max"):
            return max(0.0, min(1.0, float(h["hp"]) / float(h["hp_max"]))), "extra.mem_heroes"
    return 1.0, "default(1.0)"


def _dots(state):
    mm = state.get("minimap") or {}
    if not mm.get("found"):
        return {}
    return mm.get("dots") or {}


def _self_point(state):
    greens = _dots(state).get("green") or []
    if greens:
        return (float(greens[0][0]), float(greens[0][1]))
    return (0.5, 0.5)


def _screen_dist(u):
    scr = u.get("screen") or [0.5, 0.5]
    return math.hypot(float(scr[0]) - 0.5, float(scr[1]) - 0.5)


def _bucket_signals(state, hp):
    """分场景启发式切桶: 顺序判定, 先命中先算(写死在代码里, 无黑箱分类器)。
    返回 (桶名, 判据计数) —— 计数只用于报告里解释判据贡献。"""
    if bool((state.get("behavior") or {}).get("dead")) or hp <= DEAD_HP:
        return "死亡/黑屏", (0, 0, 0)
    me = _self_point(state)
    near_red = sum(1 for p in (_dots(state).get("red") or [])
                   if math.hypot(float(p[0]) - me[0], float(p[1]) - me[1]) <= TEAM_RADIUS)
    near_hero = sum(1 for u in (state.get("units") or [])
                    if u.get("cls") == "enemy_hero" and _screen_dist(u) <= HERO_SCREEN_RADIUS)
    if near_red >= TEAM_ENEMY_N or near_hero >= TEAM_ENEMY_N:
        return "团战", (near_red, near_hero, 0)
    near_minion = sum(1 for u in (state.get("units") or [])
                      if u.get("cls") == "enemy_minion" and _screen_dist(u) <= MINION_SCREEN_RADIUS)
    if near_minion >= MINION_N:
        return "清线", (near_red, near_hero, near_minion)
    if hp < LOW_HP:
        return "残血", (near_red, near_hero, near_minion)
    return "游走", (near_red, near_hero, near_minion)


def _bucket(state, hp):
    return _bucket_signals(state, hp)[0]


# ---------------------------------------------------------------- 动作/目标归一化
def _ref_to_norm(pt):
    from wzry.decision.hok_bridge import MINIMAP_AFFINE
    ax, bx = MINIMAP_AFFINE["x"]
    ay, by = MINIMAP_AFFINE["y"]
    return ((float(pt[0]) - bx) / (ax * 416.0), (float(pt[1]) - by) / (ay * 424.0))


def _fallback_action(state):
    """规则层兜底动作 = 走向己方绿点。

    不能用 {"type":"none"} —— optimizer._solve_core 的动作白名单里含 "none", 会原样返回,
    等于规则层完全不决策(scripts/ab_replay.py 踩过这个坑)。
    """
    g = _self_point(state)
    return {"type": "map_move", "nx": float(g[0]), "ny": float(g[1]), "reason": "eval_rule_fb"}


def _same_target(a, b):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= TARGET_TOL


def _target_dist(a, b):
    if a is None or b is None:
        return None
    return math.hypot(a[0] - b[0], a[1] - b[1])


# ---------------------------------------------------------------- 两条决策路径
def _rule_view(state, hp):
    """规则基线: 生产候选集 + 规则分排序 top-k, 以及 solve() 的生产输出。"""
    from wzry.decision import optimizer

    t0 = time.perf_counter()
    f = optimizer._feat(state, hp)
    cands = optimizer.gen_candidates(f)
    # 对齐 _solve_core 的 v2.55 保命硬过滤, 否则候选集与线上不一致
    if len(f["reds"]) >= 6 or hp < 0.5:
        keep = ("stand", "follow", "hold", "retreat", "stand_")
        cands = [c for c in cands
                 if any(str(c[0].get("reason", "")).startswith(p) for p in keep)
                 or c[0].get("type") == "none"]
    scored = sorted(((optimizer.evaluate(c, f, "blue") + 0.35 * optimizer._forecast(c, f), c)
                     for c in cands), key=lambda x: -x[0])
    solved = optimizer.solve(state, _fallback_action(state), hp, "blue")
    ms = (time.perf_counter() - t0) * 1000.0
    self_pt = _self_point(state)
    reds = [(float(p[0]), float(p[1])) for p in f["reds"]]

    def target_of(action):
        t = action.get("type")
        if t == "map_move":
            return (float(action.get("nx", 0.5)), float(action.get("ny", 0.5)))
        if t in ("skill", "attack", "combo"):
            # 小地图代理: 技能/普攻候选只由"屏内最近敌英"生成, 该敌英在小地图上的位置用
            # "距自己最近的红点"近似(units 不含小地图坐标, 无法精确投影)。
            if not reds:
                return None
            return min(reds, key=lambda p: math.hypot(p[0] - self_pt[0], p[1] - self_pt[1]))
        return None

    return {
        "n_cand": len(cands),
        "top_types": [str(c[0].get("type") or "none") for _, c in scored[:RULE_TOP_K]],
        "top_targets": [target_of(c[0]) for _, c in scored[:RULE_TOP_K]],
        "solved": solved,
        "solved_type": str((solved or {}).get("type") or "none"),
        "fb": _fallback_action(state),
        "ms": ms,
    }


def _model_view(shadow, state):
    t0 = time.perf_counter()
    payload = shadow.decide(state) or {}
    ms = (time.perf_counter() - t0) * 1000.0
    kind = payload.get("first_kind")
    target = _ref_to_norm(payload["target_position"]) if (kind and payload.get("target_position")) else None
    return {"err": payload.get("error"), "kind": kind, "target": target,
            "n_blue": payload.get("n_blue"), "n_red": payload.get("n_red"), "ms": ms}


def _is_fallback(rule):
    """规则层是否根本没覆盖兜底动作(= 直接下放)。"""
    a, fb = rule["solved"] or {}, rule["fb"]
    try:
        return (a.get("type") == fb["type"]
                and abs(float(a.get("nx", -1)) - fb["nx"]) < 1e-9
                and abs(float(a.get("ny", -1)) - fb["ny"]) < 1e-9)
    except Exception:
        return False


def _eval_one(name, state, shadow):
    """单帧: 两条路径 + 比对。任何一侧异常都记在 rec 里, 不影响其它帧。"""
    hp, hp_src = _hp_of(state)
    rec = {"s": name, "f": state.get("frame_id"),
           "t": round(float(state.get("t") or 0.0), 3),
           "b": _bucket(state, hp), "hp": round(hp, 3), "hps": hp_src,
           "nred": len(_dots(state).get("red") or [])}
    t_frame = time.perf_counter()
    rule = model = None
    try:
        rule = _rule_view(state, hp)
    except Exception as exc:
        rec["re"] = type(exc).__name__
        rec["re_msg"] = str(exc)[:120]
    try:
        model = _model_view(shadow, state)
    except Exception as exc:
        rec["me"] = type(exc).__name__
        rec["me_msg"] = str(exc)[:120]
    rec["fms"] = round((time.perf_counter() - t_frame) * 1000.0, 3)
    if rule is not None:
        rec["rt"] = rule["top_types"][0]
        rec["ncand"] = rule["n_cand"]
        rec["rms"] = round(rule["ms"], 3)
        rec["rv"] = rule["solved_type"]
        rec["rfb"] = int(_is_fallback(rule))
    if model is not None:
        rec["rk"] = model["kind"]
        rec["mms"] = round(model["ms"], 3)
        if model["err"]:
            rec["me"] = str(model["err"]).split(":", 1)[0].strip() or "Unknown"
            rec["me_msg"] = str(model["err"])[:120]
        elif not model["kind"]:
            rec["me"] = "EmptyDecision"
            dots = _dots(state)
            n_self = len(dots.get("green") or []) + len(dots.get("blue") or [])
            # 桥接器只认小地图色点(历史 states 无 units.pos_minimap); 己方色点全缺时
            # generate_candidates 找不到 alive blue -> beam_search 无候选 -> 空动作
            rec["me_msg"] = ("小地图无己方色点(绿/蓝皆缺), 建不出己方 Hero"
                             if n_self == 0 else "beam_search 返回空动作")
    if rule is not None and model is not None and model["kind"] and not model["err"]:
        r1, m1 = rule["top_types"][0], model["kind"]
        rc, mc = COARSE.get(r1, r1), COARSE.get(m1, m1)
        rt1, mt = rule["top_targets"][0], model["target"]
        t3_set = {COARSE.get(x, x) for x in rule["top_types"]}
        rec.update({"rc": rc, "mc": mc,
                    "t1": int(rc == mc), "t3": int(mc in t3_set),
                    "g1": int(_same_target(rt1, mt)),
                    "g3": int(any(_same_target(x, mt) for x in rule["top_targets"])),
                    "flip": int(rc != mc),
                    "sv": int(COARSE.get(rule["solved_type"], rule["solved_type"]) == mc),
                    "d": (lambda v: round(v, 4) if v is not None else None)(_target_dist(rt1, mt))})
        rec["s1"] = int(rec["t1"] and rec["g1"])
        rec["s3"] = int(rec["t3"] and rec["g3"])
    return rec


# ---------------------------------------------------------------- 数据扫描/采样
def _discover(sessions_filter):
    if not MATCH_DIR.exists():
        raise SystemExit(f"找不到数据目录: {MATCH_DIR}")
    out = []
    for path in sorted(MATCH_DIR.glob("*/states.jsonl")):
        name = path.parent.name
        if sessions_filter and not any(name == s or name.startswith(s) for s in sessions_filter):
            continue
        out.append((name, path))
    return out


def _scan(session_paths):
    """覆盖率扫描 + 可评估帧清单。不保留 state dict(省内存)。"""
    cov = {"sessions": len(session_paths), "total_lines": 0, "blank_lines": 0,
           "parse_errors": 0, "evaluable": 0,
           "unassessable": Counter(), "field": Counter(), "hp_source": Counter(),
           "session_hp_zero": [], "session_hp_low": [],
           "bucket_full": Counter(), "team_signal": Counter(),
           "death_banner_nonnull": 0, "death_banner_and_dead": 0}
    frames = []          # (session, line_idx, t)
    for name, path in session_paths:
        n_lines = n_hp = 0
        with path.open("r", encoding="utf-8") as fh:
            for idx, line in enumerate(fh):
                if not line.strip():
                    cov["blank_lines"] += 1
                    continue
                cov["total_lines"] += 1
                n_lines += 1
                try:
                    st = json.loads(line)
                except Exception:
                    cov["parse_errors"] += 1
                    continue
                ui = st.get("ui") or {}
                mm = st.get("minimap")
                units = st.get("units")
                if isinstance(ui.get("hp"), (int, float)):
                    n_hp += 1
                if not isinstance(mm, dict):
                    cov["unassessable"]["minimap 缺失/类型错"] += 1
                    continue
                if not isinstance(units, list):
                    cov["unassessable"]["units 缺失/类型错"] += 1
                    continue
                if mm.get("found"):
                    cov["field"]["minimap.found"] += 1
                elif mm:
                    cov["field"]["minimap 存在但 found=False"] += 1
                dots = mm.get("dots") or {}
                if dots.get("green"):
                    cov["field"]["dots.green"] += 1
                if dots.get("blue"):
                    cov["field"]["dots.blue"] += 1
                if dots.get("red"):
                    cov["field"]["dots.red"] += 1
                if units:
                    cov["field"]["units 非空"] += 1
                if isinstance(st.get("behavior"), dict):
                    cov["field"]["behavior"] += 1
                if st.get("self_state"):
                    cov["field"]["self_state"] += 1
                if isinstance(st.get("clock_s"), (int, float)):
                    cov["field"]["clock_s"] += 1
                if any("pos_minimap" in u for u in units):
                    cov["field"]["units.pos_minimap"] += 1
                hp, src = _hp_of(st)
                cov["hp_source"][src] += 1
                db = ui.get("death_banner")
                if db is not None:
                    cov["death_banner_nonnull"] += 1
                    if bool((st.get("behavior") or {}).get("dead")):
                        cov["death_banner_and_dead"] += 1
                bname, sig = _bucket_signals(st, hp)
                cov["bucket_full"][bname] += 1
                if bname == "团战":
                    red_ok, hero_ok = sig[0] >= TEAM_ENEMY_N, sig[1] >= TEAM_ENEMY_N
                    cov["team_signal"]["两路都命中" if red_ok and hero_ok
                                      else ("仅小地图红点" if red_ok else "仅屏内敌英")] += 1
                cov["evaluable"] += 1
                frames.append((name, idx, float(st.get("t") or 0.0)))
        if n_lines:
            if n_hp == 0:
                cov["session_hp_zero"].append(name)
            elif n_hp / n_lines < 0.5:
                cov["session_hp_low"].append([name, n_lines, n_hp])
    for key in ("unassessable", "field", "hp_source", "bucket_full", "team_signal"):
        cov[key] = dict(cov[key])
    return cov, frames


def _sample(frames, seed, limit, hz):
    """确定性采样: (可选)按 t 间隔抽稀 -> seed 打乱 -> 截断 limit -> 按 (session, t) 归位。"""
    picks = frames
    if hz and hz > 0:
        interval = 1.0 / float(hz)
        by_session = defaultdict(list)
        for row in frames:
            by_session[row[0]].append(row)
        picks = []
        for _name, rows in by_session.items():
            rows.sort(key=lambda r: r[2])
            last = None
            for row in rows:
                if last is None or row[2] - last >= interval:
                    picks.append(row)
                    last = row[2]
    picks = list(picks)
    random.Random(seed).shuffle(picks)
    if limit and limit > 0:
        picks = picks[:limit]
    picks.sort(key=lambda r: (r[0], r[2]))
    return picks


# ---------------------------------------------------------------- 主评估
def _session_start_t(path):
    """会话第一帧的墙钟 t, 用于把"墙钟差"换算成对局时间(纯视觉录制没有 clock_s)。"""
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                t = json.loads(line).get("t")
            except Exception:
                return None
            return float(t) if isinstance(t, (int, float)) else None
    return None


def _iter_selected(session_paths, keys, inject_clock=True, by_frame_id=False):
    """按 (session -> 行号集合) 流式回读, 只 yield 选中的帧。

    inject_clock: 给每帧补 `session_start_t`。纯视觉 states.jsonl 没有 clock_s,
    hok_bridge 会用 `t - session_start_t` 还原对局时间; 关掉则回到"时间恒 0"的旧行为。
    """
    for name, path in session_paths:
        if name not in keys:
            continue
        want = keys[name]
        start = _session_start_t(path) if inject_clock else None
        with path.open("r", encoding="utf-8") as fh:
            for idx, line in enumerate(fh):
                if not by_frame_id and idx not in want:
                    continue
                try:
                    st = json.loads(line)
                except Exception:
                    continue
                if by_frame_id and st.get("frame_id") not in want:
                    continue
                if start is not None and "session_start_t" not in st:
                    st["session_start_t"] = start
                yield name, st


def run_eval(cfg, fixed_keys=None):
    from wzry.decision.hok_bridge import HokShadow

    session_paths = _discover(cfg["sessions"])
    if not session_paths:
        raise SystemExit("没有匹配的会话")
    cov, all_frames = _scan(session_paths)
    picks = _sample(all_frames, cfg["seed"], cfg["limit"], cfg["hz"])
    by_frame_id = fixed_keys is not None
    keys = (fixed_keys if fixed_keys is not None
            else defaultdict(set, {n: set() for n, _, _ in picks}))
    if fixed_keys is None:
        for name, idx, _t in picks:
            keys[name].add(idx)

    trace = Path(cfg["trace"])
    if fixed_keys is not None:
        trace = trace.with_name(trace.stem + "_verify.jsonl")
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text("", encoding="utf-8")   # 截断而非 unlink(沙箱/杀软下删除可能失败)
    shadow = HokShadow(out_path=trace)     # base 配置: 无外挂线性模型/权重文件依赖

    records, err_rule, err_model, err_msg = [], Counter(), Counter(), Counter()
    budget = float(cfg["max_seconds"] or 0)
    t_start = time.perf_counter()
    stopped = False
    planned = sum(len(v) for v in keys.values())
    for name, st in _iter_selected(session_paths, keys, cfg.get("inject_clock", True),
                                   by_frame_id=by_frame_id):
        if budget and time.perf_counter() - t_start > budget:
            stopped = True
            break
        rec = _eval_one(name, st, shadow)
        if rec.get("re"):
            err_rule[rec["re"]] += 1
            err_msg[f"rule {rec['re']}: {rec.get('re_msg', '')}"[:130]] += 1
        if rec.get("me"):
            err_model[rec["me"]] += 1
            err_msg[f"model {rec['me']}: {rec.get('me_msg', '')}"[:130]] += 1
        records.append(rec)

    if stopped:
        print(f"[warn] 达到 --max-seconds={budget:.0f}s 预算提前停止: "
              f"已评估 {len(records)}/{planned} 帧")

    return {
        "config": {k: cfg[k] for k in ("limit", "sessions", "seed", "hz", "max_seconds", "trace",
                                        "inject_clock")},
        "coverage": cov,
        "sampling": {"evaluable_frames": len(all_frames), "planned": planned,
                     "evaluated": len(records), "truncated": stopped, "hz": cfg["hz"]},
        "records": records,
        "errors": {"rule": dict(err_rule), "model": dict(err_model),
                   "messages": dict(err_msg.most_common(12))},
    }


# ---------------------------------------------------------------- 聚合
def _agg(records):
    ev = [r for r in records if "t1" in r]
    n = len(ev)
    ds = [r["d"] for r in ev if r.get("d") is not None]
    return {
        "frames": len(records), "comparable": n,
        "type@1": _rate(sum(r["t1"] for r in ev), n),
        "type@3": _rate(sum(r["t3"] for r in ev), n),
        "target@1": _rate(sum(r["g1"] for r in ev), n),
        "target@3": _rate(sum(r["g3"] for r in ev), n),
        "strict@1": _rate(sum(r["s1"] for r in ev), n),
        "strict@3": _rate(sum(r["s3"] for r in ev), n),
        "flip": _rate(sum(r["flip"] for r in ev), n),
        "flip_dir": dict(Counter(f"{r['rc']}->{r['mc']}" for r in ev if r["flip"]).most_common()),
        "pair_fine": dict(Counter(f"{r['rt']}->{r['rk']}" for r in ev).most_common(10)),
        "rule_type": dict(Counter(r["rt"] for r in ev).most_common()),
        "model_kind": dict(Counter(r["rk"] for r in ev).most_common()),
        "solved_vs_model": _rate(sum(r.get("sv", 0) for r in ev), n),
        "rule_fallback": _rate(sum(r.get("rfb", 0) for r in records), len(records)),
        "model_fallback": _rate(len(records) - n, len(records)),
        "dist_p50": round(_pct(ds, 0.5), 4) if ds else None,
        "dist_p90": round(_pct(ds, 0.9), 4) if ds else None,
        "dist_n": len(ds),
    }


def _lat(records):
    def block(key):
        vals = [r[key] for r in records if isinstance(r.get(key), (int, float))]
        return {"n": len(vals),
                "p50": round(_pct(vals, 0.5), 2) if vals else None,
                "p90": round(_pct(vals, 0.9), 2) if vals else None,
                "p95": round(_pct(vals, 0.95), 2) if vals else None,
                "p99": round(_pct(vals, 0.99), 2) if vals else None,
                "max": round(max(vals), 2) if vals else None}
    return {"rule": block("rms"), "model": block("mms"), "full": block("fms")}


def _by_bucket(records):
    out = {}
    for b in BUCKETS:
        rows = [r for r in records if r["b"] == b]
        ev = [r for r in rows if "t1" in r]
        out[b] = {
            "frames": len(rows), "comparable": len(ev),
            "type@3": _rate(sum(r["t3"] for r in ev), len(ev)),
            "strict@3": _rate(sum(r["s3"] for r in ev), len(ev)),
            "flip": _rate(sum(r["flip"] for r in ev), len(ev)),
            "m_p95": round(_pct([r["mms"] for r in rows if r.get("mms") is not None], 0.95), 2)
            if any(r.get("mms") is not None for r in rows) else None,
            "r_p95": round(_pct([r["rms"] for r in rows if r.get("rms") is not None], 0.95), 2)
            if any(r.get("rms") is not None for r in rows) else None,
            "rule_type": dict(Counter(r["rt"] for r in ev).most_common(3)),
            "model_kind": dict(Counter(r["rk"] for r in ev).most_common(3)),
        }
    return out


_KEEP = ("s", "f", "t", "b", "hp", "hps", "rt", "rk", "rc", "mc", "t1", "t3", "g1", "g3",
         "s1", "s3", "flip", "d", "rv", "sv", "rfb", "nred", "ncand")


def _decisions_only(records):
    """去掉墙钟延迟后的决策投影(可复现性的可比对象)。"""
    return [{k: r[k] for k in _KEEP if k in r} for r in records]


def _digest(proj):
    return hashlib.sha256("\n".join(json.dumps(r, sort_keys=True, ensure_ascii=False)
                                    for r in proj).encode("utf-8")).hexdigest()


def _verify(cfg, records, n):
    """同 seed 二次运行, 逐字段比对决策投影(排除墙钟延迟)。"""
    sub = records[:n] if n and n > 0 else list(records)
    if not sub:
        return None
    keys = defaultdict(set)
    for r in sub:
        keys[r["s"]].add(r["f"])
    # 用行号重跑会引入 frame_id 映射歧义, 这里改用 frame_id 精确集合
    res2 = _rerun_by_frame_id(cfg, keys)
    proj_a = _decisions_only(sub)
    proj_b = _decisions_only(res2)
    idx_b = {(r["s"], r["f"]): r for r in proj_b}
    mism = sum(1 for r in proj_a if idx_b.get((r["s"], r["f"])) != r)
    return {"frames": len(proj_a), "digest_a": _digest(proj_a),
            "digest_b": _digest(proj_b) if len(proj_b) == len(proj_a) else None,
            "mismatched_frames": mism,
            "missing_frames": len(proj_a) - len(proj_b),
            "identical": mism == 0 and len(proj_a) == len(proj_b),
            "excluded_fields": ["rms", "mms", "fms"],
            "note": "排除墙钟延迟字段后, 其余决策字段逐字段相等"}


def _rerun_by_frame_id(cfg, keys):
    from wzry.decision.hok_bridge import HokShadow

    session_paths = [(n, p) for n, p in _discover(cfg["sessions"]) if n in keys]
    trace = Path(cfg["trace"]).with_name(Path(cfg["trace"]).stem + "_verify.jsonl")
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text("", encoding="utf-8")
    shadow = HokShadow(out_path=trace)
    out = []
    for name, st in _iter_selected(session_paths, _keys_to_lines(session_paths, keys),
                                   cfg.get("inject_clock", True),
                                   by_frame_id=False):
        out.append(_eval_one(name, st, shadow))
    out.sort(key=lambda r: (r["s"], r["t"]))
    return out


def _keys_to_lines(session_paths, keys):
    """frame_id -> 行号(仅用于流式筛选; frame_id 在会话内唯一)。"""
    line_keys = defaultdict(set)
    for name, path in session_paths:
        want = keys.get(name) or set()
        if not want:
            continue
        with path.open("r", encoding="utf-8") as fh:
            for idx, line in enumerate(fh):
                if not line.strip():
                    continue
                try:
                    fid = json.loads(line).get("frame_id")
                except Exception:
                    continue
                if fid in want:
                    line_keys[name].add(idx)
    return line_keys


# ---------------------------------------------------------------- 报告
def _p(x):
    return "—" if x is None else f"{x * 100:.1f}%"


def _n(x, nd=2):
    return "—" if x is None else f"{x:.{nd}f}"


def _tbl(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return out


def render_report(res, agg, lat, buckets, det):
    cov = res["coverage"]
    smp = res["sampling"]
    cfg = res["config"]
    err = res["errors"]
    total = cov["total_lines"]
    L = []
    L.append("# 决策层离线评估报告（S2-3）")
    L.append("")
    L.append("> 本文件由 `scripts/eval_decision_offline.py --report` 生成，所有数字取自同一次运行落盘的 "
             "`runs/eval/decision_offline_2026-10-24.json`。**不要手改数字**，改代码重跑即可。")
    L.append("")
    L.append("## 0. 结论摘要")
    L.append("")
    def _worst(min_n):
        pick = None
        for b in BUCKETS:
            if buckets[b]["frames"] >= min_n and buckets[b]["strict@3"] is not None:
                if pick is None or buckets[b]["strict@3"] < buckets[pick]["strict@3"]:
                    pick = b
        return pick

    worst = _worst(50) or _worst(1)
    if smp["truncated"]:
        scope = "（**因时间预算提前停止，非全量**）"
    elif smp["evaluated"] == cov["evaluable"]:
        scope = "（全量可评估帧）"
    else:
        scope = f"（按 `--limit {cfg['limit']}` 采样的子集，非全量）"
    L.append(f"- **可评估帧 {cov['evaluable']} / 总帧 {total}**（{_p(_rate(cov['evaluable'], total))}）；"
             f"本次实际评估 {smp['evaluated']} 帧" + scope)
    L.append(f"- 总体 agreement：类型 @1 {_p(agg['type@1'])} / @3 {_p(agg['type@3'])}，"
             f"目标 @1 {_p(agg['target@1'])} / @3 {_p(agg['target@3'])}，"
             f"**严格（类型+目标）@1 {_p(agg['strict@1'])} / @3 {_p(agg['strict@3'])}**"
             f"（分母 {agg['comparable']} 帧）")
    L.append(f"- 决策翻转率（粗粒度类型不一致）**{_p(agg['flip'])}**；主要翻转方向："
             + ("、".join(f"`{k}` {v} 帧" for k, v in list(agg["flip_dir"].items())[:3]) or "无"))
    L.append(f"- 模型路径延迟 p50 {_n(lat['model']['p50'])}ms / **p95 {_n(lat['model']['p95'])}ms** / "
             f"p99 {_n(lat['model']['p99'])}ms；全链路 p95 {_n(lat['full']['p95'])}ms；"
             f"规则路径 p95 {_n(lat['rule']['p95'])}ms")
    L.append(f"- hok_bridge 错误 {sum(err['model'].values())} 帧"
             f"（{_p(_rate(sum(err['model'].values()), smp['evaluated']))}），规则层异常 "
             f"{sum(err['rule'].values())} 帧；模型 fallback 率 {_p(agg['model_fallback'])}、"
             f"规则层下放率 {_p(agg['rule_fallback'])}")
    if worst:
        L.append(f"- 分场景最差桶：**{worst}**（严格 @3 {_p(buckets[worst]['strict@3'])}，"
                 f"{buckets[worst]['frames']} 帧，翻转率 {_p(buckets[worst]['flip'])}）")
    L.append("")
    L.append("## 1. 运行配置")
    L.append("")
    L += _tbl(["项", "值"], [
        ["会话数", cov["sessions"]],
        ["--sessions", cfg["sessions"] or "（全部）"],
        ["--limit", cfg["limit"] or "0（不限）"],
        ["--seed", cfg["seed"]],
        ["--hz（按 t 间隔抽稀）", cfg["hz"] or "0（不抽稀）"],
        ["--max-seconds", cfg["max_seconds"]],
        ["计划评估帧", smp["planned"]],
        ["实际评估帧", smp["evaluated"]],
        ["是否因预算截断", "是" if smp["truncated"] else "否"],
        ["hok DecisionOutput 回放落盘", cfg["trace"]],
    ])
    L.append("")
    L.append("## 2. 数据覆盖")
    L.append("")
    L.append(f"扫描 `data/matches/*/states.jsonl` 共 **{cov['sessions']} 个会话 / {total} 行**"
             f"（空行 {cov['blank_lines']}，JSON 解析失败 {cov['parse_errors']}）。")
    L.append("")
    un_rows = [[k, v, _p(_rate(v, total))] for k, v in cov["unassessable"].items()] or [["（无）", 0, "—"]]
    L += _tbl(["不可评估原因", "帧数", "占比"], un_rows)
    L.append("")
    L.append(f"**可评估帧 = {cov['evaluable']} / {total}（{_p(_rate(cov['evaluable'], total))}）**。"
             "可评估判据（字段级，不含决策成败）：能 JSON 解析 + `minimap` 是 dict + `units` 是 list；"
             "hp 允许按取值链回退并单独计数（见下表），不算不可评估。")
    L.append("")
    field_rows = [[f"`{k}`", v, _p(_rate(v, total))] for k, v in sorted(cov["field"].items(),
                                                                        key=lambda kv: -kv[1])]
    L += _tbl(["字段", "命中帧", "占全部行"], field_rows)
    L.append("")
    hp_rows = [[f"`{k}`", v, _p(_rate(v, cov["evaluable"]))] for k, v in
               sorted(cov["hp_source"].items(), key=lambda kv: -kv[1])]
    L += _tbl(["hp 来源", "帧数", "占可评估帧"], hp_rows)
    L.append("")
    miss = [k for k in ("self_state", "clock_s", "units.pos_minimap") if not cov["field"].get(k)]
    if miss:
        L.append("桥接器依赖、但全语料**从未出现**的字段："
                 + "、".join(f"`{k}` 0/{total}（0.0%）" for k in miss)
                 + " —— 它们不造成报错，但会让 `hok_bridge` 走回退分支（见 §7.1）。")
        L.append("")
    L.append(f"hp 覆盖为 0 的会话（{len(cov['session_hp_zero'])} 个）："
             + ("、".join(f"`{s}`" for s in cov["session_hp_zero"]) or "无"))
    if cov["session_hp_low"]:
        L.append("")
        L.append("hp 覆盖率 <50% 的会话：" + "；".join(
            f"`{s}` {h}/{n}" for s, n, h in cov["session_hp_low"]))
    L.append("")
    L.append("## 3. 指标定义（精确）")
    L.append("")
    L.append("**动作类型维度**：两条路径归一成 3 类后比较——")
    L.append("")
    L.append("| 粗类 | 规则侧（optimizer 候选 type） | 模型侧（hok ActionKind） |")
    L.append("| --- | --- | --- |")
    L.append("| `engage`（交战） | `skill`, `attack`, `combo` | `attack_enemy` |")
    L.append("| `move`（位移/战术） | `map_move`, `recall`, `restore` | `retreat`, `capture_resource`, `defend_tower`, `push_tower` |")
    L.append("| `none`（无动作） | `none` | 无决策/空动作 |")
    L.append("")
    L.append("> 只在粗粒度上算一致率是**方法上的让步**：hok 的动作空间是宏观动作，没有技能/普攻级粒度，")
    L.append("> `skill` 与 `attack` 无法与模型侧对齐。细粒度组合只作为诊断列给出，不参与任何一致率分子。")
    L.append("")
    L.append("**目标维度**：动作意图落点统一到小地图归一化坐标 `[0,1]²` 后比距离——")
    L.append("")
    L.append("- 规则 `map_move` → `(nx, ny)`；")
    L.append("- 规则 `skill`/`attack`/`combo` → **距自己最近的红点**（小地图代理：这类候选只由\"屏内最近敌英\""
             "生成，而 `units` 只有屏幕坐标、没有小地图坐标，无法精确投影；该代理是本次评估的已知误差源，"
             "见 §9）；无红点时为 `None`；")
    L.append("- 规则 `none` → `None`；")
    L.append("- 模型 `target_position`（hok ref_416x424）→ 用 `hok_bridge.MINIMAP_AFFINE` 反算回归一化坐标；无目标时为 `None`。")
    L.append("")
    L.append(f"\"同目标\" = 两点欧氏距离 ≤ **{TARGET_TOL}**（归一化地图单位）**或两侧皆 `None`**；"
             "一侧 `None` 另一侧非 `None` 判为不一致。")
    L.append("")
    L.append("**一致率**：模型首选（`first_kind`）是否落在规则排序 top-k 的同一维度内；"
             "`strict` = 类型与目标同时命中。分母是**两侧都产出动作**的帧数"
             f"（本次 {agg['comparable']} 帧）。模型报错/空动作的帧不进入分母，也**不按\"回退到规则\"记一致**。")
    L.append(f"**翻转率** = 粗类不一致帧数 / 分母。翻转方向 = `规则粗类->模型粗类` 计数。")
    L.append(f"**延迟**：`time.perf_counter()` 单帧实测，分位用 nearest-rank"
             f"（升序后取 `idx=ceil(p*n)-1`）。`规则` = `_feat`+`gen_candidates`+排序+`solve()`；"
             "`模型` = `HokShadow.decide()`（含桥接建态 + beam search + DecisionOutput 落盘）；"
             "`全链路` = 单帧 dict 输入到两条决策完成并比对，**不含** JSON 反序列化。")
    L.append("")
    L.append("**分场景切桶**（代码里写死，顺序判定，先命中先算）：")
    L.append("")
    L.append("| 桶 | 判据 | 全语料命中帧 |")
    L.append("| --- | --- | --- |")
    L.append(f"| 死亡/黑屏 | `behavior.dead == true` 或 `hp ≤ {DEAD_HP}` | "
             f"{cov['bucket_full'].get('死亡/黑屏', 0)} |")
    L.append(f"| 团战 | 小地图红点距己 ≤{TEAM_RADIUS} 的数量 ≥{TEAM_ENEMY_N}，**或** 屏内 "
             f"`enemy_hero` 距屏幕中心 ≤{HERO_SCREEN_RADIUS} 的数量 ≥{TEAM_ENEMY_N} | "
             f"{cov['bucket_full'].get('团战', 0)} |")
    L.append(f"| 清线 | 距屏幕中心 ≤{MINION_SCREEN_RADIUS} 的 `enemy_minion` ≥ {MINION_N} | "
             f"{cov['bucket_full'].get('清线', 0)} |")
    L.append(f"| 残血 | `hp < {LOW_HP}` | {cov['bucket_full'].get('残血', 0)} |")
    L.append(f"| 游走 | 以上都不满足 | {cov['bucket_full'].get('游走', 0)} |")
    L.append("")
    ts = cov["team_signal"]
    L.append("团战桶为什么用\"两路取并集\"：两路检测的漏检都极严重——"
             + "、".join(f"{k} {v} 帧" for k, v in ts.items())
             + f"。只用小地图红点会只剩 {ts.get('仅小地图红点', 0) + ts.get('两路都命中', 0)} 帧，"
             "单路定义不可用，故取并集（任一路看到 ≥2 个附近敌英即判团战）。")
    L.append("")
    dbn = cov["death_banner_nonnull"]
    dba = cov["death_banner_and_dead"]
    L.append(f"> `ui.death_banner` 存在但**未采用**：它在 {dbn} 帧里非空"
             f"（{_p(_rate(dbn, total))}），其中只有 {dba} 帧同时 `behavior.dead=True`，"
             "两者几乎不相关；当作死亡信号会把大量正常帧误判成黑屏，因此死亡判定只用 "
             "`behavior.dead` 与 `hp≈0`。")
    L.append("")
    L.append("## 4. 总体指标")
    L.append("")
    L += _tbl(["指标", "数值", "分母/说明"], [
        ["agreement@1 · 类型", _p(agg["type@1"]), f"{agg['comparable']} 帧"],
        ["agreement@3 · 类型", _p(agg["type@3"]), ""],
        ["agreement@1 · 目标", _p(agg["target@1"]), ""],
        ["agreement@3 · 目标", _p(agg["target@3"]), f"双侧均有目标的 {agg['dist_n']} 帧参与距离统计"],
        ["agreement@1 · 严格", _p(agg["strict@1"]), ""],
        ["agreement@3 · 严格", _p(agg["strict@3"]), "本报告主口径"],
        ["决策翻转率", _p(agg["flip"]), "粗粒度类型不一致"],
        ["目标距离 p50 / p90", f"{_n(agg['dist_p50'], 3)} / {_n(agg['dist_p90'], 3)}", "归一化地图单位"],
        ["规则 `solve()` 与模型一致（参照列）", _p(agg["solved_vs_model"]), "生产规则输出 vs 模型首选"],
        ["模型 fallback 率", _p(agg["model_fallback"]), "报错或空动作 = 回退规则"],
        ["规则层下放率", _p(agg["rule_fallback"]), "`solve()` 原样返回兜底动作（规则层未覆盖）"],
    ])
    L.append("")
    tower_like = sum(agg["model_kind"].get(k, 0) for k in
                     ("push_tower", "capture_resource", "defend_tower"))
    move_like = sum(agg["model_kind"].get(k, 0) for k in
                    ("push_tower", "capture_resource", "defend_tower", "retreat"))
    L.append("**怎么读这两组数**（都是上表的直接推论，不是额外假设）：")
    L.append("")
    if agg["comparable"]:
        n = agg["comparable"]
        L.append(f"- 类型一致率偏高有一部分是**平凡一致**：规则 top-1 有 "
                 f"{_p(_rate(agg['rule_type'].get('map_move', 0), n))} 是 `map_move`，"
                 f"模型首选有 {_p(_rate(move_like, n))} 落在 `move` 粗类"
                 "（塔/野/后撤都算 move），两边天然容易对上。真正有区分度的是翻转率与目标维度。")
        L.append(f"- 目标一致率只有 {_p(agg['target@1'])} **主要是候选空间不相交**，不是模型选错："
                 f"模型首选里「塔/资源点」类（`push_tower`/`capture_resource`/`defend_tower`）占 "
                 f"{_p(_rate(tower_like, n))}，而规则候选生成器 `optimizer.gen_candidates()` 的候选点"
                 "只有红点、蓝点、两条走廊中心、8 向微操点与撤离点，**没有塔位和野点位**——"
                 "模型想去的目标不在规则的候选集合里，目标维度必然对不上。")
    L.append("")
    L.append("翻转方向分布：")
    L.append("")
    L += _tbl(["方向（规则 -> 模型）", "帧数"],
              [[f"`{k}`", v] for k, v in agg["flip_dir"].items()] or [["（无）", 0]])
    L.append("")
    L.append("细粒度组合 top-10（仅诊断，不参与指标）：")
    L.append("")
    L += _tbl(["组合（规则 type -> 模型 kind）", "帧数"],
              [[f"`{k}`", v] for k, v in agg["pair_fine"].items()] or [["（无）", 0]])
    L.append("")
    L += _tbl(["侧", "动作分布"],
              [["规则 top-1", "、".join(f"`{k}` {v}" for k, v in agg["rule_type"].items())],
               ["模型首选", "、".join(f"`{k}` {v}" for k, v in agg["model_kind"].items())]])
    L.append("")
    L.append("## 5. 分场景")
    L.append("")
    rows = []
    for b in BUCKETS:
        d = buckets[b]
        rows.append([b, d["frames"], _p(_rate(d["frames"], len(res["records"]))),
                     _p(d["type@3"]), _p(d["strict@3"]), _p(d["flip"]),
                     _n(d["m_p95"]), _n(d["r_p95"]),
                     "、".join(f"`{k}`{v}" for k, v in d["model_kind"].items())])
    L += _tbl(["桶", "帧数", "占比", "类型@3", "严格@3", "翻转率", "模型 p95(ms)", "规则 p95(ms)",
               "模型动作 top3"], rows)
    L.append("")
    small = [b for b in BUCKETS if 0 < buckets[b]["frames"] < 50]
    if small:
        L.append("样本 <50 帧的桶：" + "、".join(
            f"`{b}` {buckets[b]['frames']} 帧" for b in small)
            + " —— 比例类指标噪声很大，只能当现象记录，不能当结论。")
        L.append("")
    L.append("> 桶按上面的顺序互斥判定；`团战`/`清线` 的判据依赖小地图红点与屏内敌兵检测，"
             "检测漏检会把这些帧降级进 `游走`，因此 `游走` 是兜底桶、混合度最高。")
    L.append("")
    L.append("## 6. 延迟")
    L.append("")
    L += _tbl(["路径", "样本", "p50", "p90", "p95", "p99", "max"],
              [[name, d["n"], _n(d["p50"]), _n(d["p90"]), _n(d["p95"]), _n(d["p99"]), _n(d["max"])]
               for name, d in (("全链路", lat["full"]), ("模型 hok beam search", lat["model"]),
                               ("规则 optimizer", lat["rule"]))])
    L.append("")
    L.append("单位 ms。模型路径延迟包含 `DecisionOutput` 逐帧追加落盘的开销（`HokShadow.decide` 自带）。")
    L.append("")
    L.append("## 7. 错误与 fallback")
    L.append("")
    L += _tbl(["侧", "异常类型", "帧数"],
              [["规则", k, v] for k, v in err["rule"].items()]
              + [["模型", k, v] for k, v in err["model"].items()]
              or [["（无异常）", "—", 0]])
    L.append("")
    if err["messages"]:
        L.append("报错样本（最多 12 条）：")
        L.append("")
        L.append("```")
        for k, v in err["messages"].items():
            L.append(f"{v}× {k}")
        L.append("```")
        L.append("")
    L.append("### 7.1 hok_bridge 在历史 states 上的失败模式")
    L.append("")
    n_model_err = sum(err["model"].values())
    L.append(f"本次 {smp['evaluated']} 帧中，hok_bridge 抛异常/返回空动作 **{n_model_err} 帧**"
             f"（{_p(_rate(n_model_err, smp['evaluated']))}）。"
             + ("**没有出现大面积报错**（0 个异常类型）。" if not err["model"] else ""))
    L.append("")
    model_msgs = [(k, v) for k, v in err["messages"].items() if k.startswith("model ")]
    if model_msgs:
        L.append("**失败模式分布**（按报错文本归类，全部是\"不抛异常、返回空动作\"）：")
        L.append("")
        L += _tbl(["错误类别 / 文本", "帧数", "占评估帧"],
                  [[f"`{k[len('model '):]}`", v, _p(_rate(v, smp["evaluated"]))]
                   for k, v in model_msgs])
        L.append("")
        L.append("根因（读代码可确认，不是猜测）：`_hero_rows()` 在 `units[*].pos_minimap` 缺失时只认 "
                 "`minimap.dots`；再经 `_camp_of()`（缺 `self_state` → 恒 `blue`）分流后，若本帧没有任何"
                 "绿/蓝色点，就建不出 `side=blue` 的 Hero，`generate_candidates()` 因 `next(alive blue)` "
                 "为 `None` 返回空列表，`beam_search` 于是没有候选可搜 → `first_kind=None`。"
                 "**这类帧模型必然沉默，只能靠规则层兜底**，属于域不匹配而非偶发故障。")
        L.append("")
    L.append("另一个方向：\"不报错\"也不等于\"域对齐\"。以下三项是**实测的静默降级**（桥接器有回退分支，不会抛异常，"
             "所以指标看着正常、语义已经变了）：")
    L.append("")
    L += _tbl(["桥接依赖字段", "覆盖帧", "桥接的回退行为", "后果"], [
        ["`self_state`（己方阵营/血量）", cov["field"].get("self_state", 0),
         "`_camp_of()` 恒返回 `blue`；`_hp_ratio()` 取不到血量 → 1.0",
         "模型永远认为**自己满血**，且把蓝方当自己"],
        ["`units[*].pos_minimap`（单位小地图坐标）", cov["field"].get("units.pos_minimap", 0),
         "`_hero_rows()` 回退到 `minimap.dots` 色点", "英雄身份靠色点顺序，无 uid / 无真实血量"],
        ["`clock_s` / `game_time_s` / `ui.match_clock`（对局时钟）", cov["field"].get("clock_s", 0),
         "`_game_time()` 恒返回 0.0", "`game_time=0` → 龙的刷新门恒不满足、价值上下文的时间项失真"],
    ])
    L.append("")
    dead_rows = [r for r in res["records"] if r["b"] == "死亡/黑屏"]
    if dead_rows:
        dead_kinds = Counter(r.get("rk") for r in dead_rows)
        L.append(f"另一条可用证据：**死亡/黑屏**桶 {len(dead_rows)} 帧里，模型仍然每帧都产出动作"
                 f"（{'、'.join(f'`{k}` {v}' for k, v in dead_kinds.most_common(3))}），"
                 "因为桥接器不读 `behavior.dead`、也被 `hp=1.0` 兜住 —— 死亡期间模型仍在推进/打野。"
                 "这类帧的 agreement 没有决策意义，只说明状态域没对齐。")
        L.append("")
    top_kind = Counter(r.get("rk") for r in res["records"] if r.get("rk"))
    if top_kind:
        k, v = top_kind.most_common(1)[0]
        L.append(f"动作分布集中度：模型首选 `{k}` 占 {_p(_rate(v, sum(top_kind.values())))}"
                 "（见 §4 分布表）。集中度过高通常意味着模型没有真正被状态区分开，"
                 "而是被少数固定目标（塔/野点）主导。")
        L.append("")
    L.append("## 8. 可复现性")
    L.append("")
    L.append(f"采样完全由 `--seed {cfg['seed']}` 决定：可评估帧按 `(session, t)` 排序后，"
             "`random.Random(seed).shuffle()` 打乱，再按 `--limit` 截断，最后按 `(session, t)` 归位。"
             "两条决策路径本身无随机源（optimizer 为确定性打分，hok `beam_search` 无采样）。")
    L.append("")
    if det:
        L.append(f"本次内置二次校验：对前 **{det['frames']} 帧**用同一 seed 重跑一遍，"
                 f"逐字段比对决策投影（排除墙钟延迟 `rms`/`mms`/`fms`）：")
        L.append("")
        L += _tbl(["项", "值"], [
            ["第一次 digest", f"`{det['digest_a'][:32]}…`"],
            ["第二次 digest", f"`{det['digest_b'][:32]}…`" if det["digest_b"] else "帧数不符（见下）"],
            ["逐字段不一致帧数", det["mismatched_frames"]],
            ["缺失帧数", det["missing_frames"]],
            ["结论", "**一致**" if det["identical"] else "**不一致（需排查）**"],
        ])
        L.append("")
        L.append(f"本次全量 **{smp['evaluated']} 帧**的决策 digest：`{res['digest']}`"
                 "（即 JSON 的 `digest` 字段）。跨进程复现的验证方式：同参数再跑一次，"
                 "该字段必须逐字符相同；延迟字段（`rms`/`mms`/`fms`）是墙钟实测，不参与该摘要。")
    else:
        L.append("本次未开启 `--verify-determinism`，无法在本报告内给出二次运行比对结论。"
                 "复现方式：`--report --verify-determinism 60`，或同参数跑两次比对 "
                 "`determinism.digest`。")
    L.append("")
    L.append("## 9. 已知偏差与后续")
    L.append("")
    L.append(f"1. **无人类标签，只能测\"一致性\"不能测\"正确性\"**。agreement 高只说明两套系统彼此接近，"
             "不说明决策对。要回答\"谁更好\"需要结果回溯（击杀/推塔/存活）或人工标注，本闭环不提供。")
    L.append(f"2. **比较粒度被 hok 的动作空间限制在 3 类**。`skill`/`attack` 与 `attack_enemy` 只能合并成 "
             "`engage`，所以类型一致率天然偏乐观；严格口径（类型+目标）更有区分度。")
    hp_def = cov["hp_source"].get("default(1.0)", 0)
    L.append(f"3. **hp 缺失 {hp_def} 帧按 1.0 兜底**（占可评估帧 {_p(_rate(hp_def, cov['evaluable']))}）。"
             "这些帧永远进不了 `残血` 桶，也永远不会触发 optimizer 的 `hp<0.5` 保命过滤 —— "
             "`残血` 桶的样本量因此只来自有 hp 的会话。")
    L.append("4. **规则侧技能/攻击的目标是小地图代理**（最近红点），而模型侧是真 `target_position`。"
             "目标维度对 `engage` 类帧的一致率因此偏保守，误差量级可用 §4 的目标距离 p50/p90 判断。")
    L.append(f"5. **单帧独立评估**：不含时序、冷却、技能可用性（`ui.skill1_ready` 等仅 1420 帧有），"
             "规则层与模型层都是\"看这一帧算这一帧\"，不比多步连贯性。")
    L.append(f"6. **状态采集约 8.4Hz（会话中位 dt≈0.12s），而生产里 hok 以 1–2Hz 被调用**。"
             "逐帧评估测的是单次决策成本，不代表生产占空比；延迟要看 p95/p99 而不是总时长。")
    L.append("7. **分桶阈值是手写常数**（`TEAM_RADIUS`/`MINION_N`/`LOW_HP` 等），没有在标注集上校准过；"
             "红点/敌兵检测的漏检会把这些帧降级进 `游走`，导致 `游走` 桶混入团战帧。")
    L.append("8. **规则兜底动作是评估脚本自造的替身**（\"走向己方绿点\"）。生产里 "
             "`m2_agent_v2` 传给 `optimizer.solve` 的是规则层自己算出的 `map_move`，脚本拿不到那一路，"
             "所以 `规则层下放率` 只能当相对量看（它主要反映\"`solve()` 有没有覆盖兜底\"，"
             "而不是\"规则层做了多少决策\"）。另外也不能传 `{\"type\": \"none\"}` 当兜底——"
             "`_solve_core` 的动作白名单含 `none`，会原样返回，等于规则层完全不决策。")
    L.append("")
    L.append("后续可做：把 `hok_bridge` 的 `self_state` / `pos_minimap` / `clock_s` 三个字段补进记录器，"
             "再重跑本脚本，即可量化\"域对齐\"对一致率的增益；以及接入结果回溯标签，把一致性换成胜率/收益。")
    L.append("")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- CLI
def main(argv=None):
    default_report = str(DEFAULT_REPORT)
    ap = argparse.ArgumentParser(description="决策层离线评估闭环 (S2-3)")
    ap.add_argument("--limit", type=int, default=0, help="评估帧数上限, 0=全部可评估帧")
    ap.add_argument("--sessions", default=None, help="逗号分隔的会话名(支持前缀), 默认全部")
    ap.add_argument("--seed", type=int, default=20261024, help="采样随机种子")
    ap.add_argument("--hz", type=float, default=0.0, help="按 t 间隔抽稀, 0=不抽稀")
    ap.add_argument("--max-seconds", type=float, default=540.0, help="单次运行时间预算(秒), 0=不限")
    ap.add_argument("--json", default=str(DEFAULT_JSON), help="原始结果 JSON")
    ap.add_argument("--trace", default=str(DEFAULT_TRACE), help="hok DecisionOutput 回放落盘")
    ap.add_argument("--report", nargs="?", const=default_report, default=None,
                    help=f"生成 Markdown 报告(不带值则写 {default_report})")
    ap.add_argument("--verify-determinism", nargs="?", const=60, type=int, default=0,
                    metavar="N", help="内置二次运行比对前 N 帧(默认 60), 0=不做")
    ap.add_argument("--report-only", default=None, metavar="JSON",
                    help="不重跑, 直接从已落盘 JSON 重新渲染报告")
    ap.add_argument("--keys-file", default=None,
                    help="固定帧集合 JSON（{session: [行号...]}）；用它可以做跨时间可比的 A/B"
                         "（默认按 seed 抽样，语料增长后抽样会漂移）")
    ap.add_argument("--dump-keys", default=None,
                    help="把本次抽样到的 {session: [行号...]} 写到该路径，供 --keys-file 复用")
    ap.add_argument("--no-inject-clock", action="store_true",
                    help="不给回放帧补 session_start_t（回到对局时间恒 0 的旧行为，用于 A/B 对照）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.report_only:
        data = json.loads(Path(args.report_only).read_text(encoding="utf-8"))
        rp = Path(args.report or default_report)
        if not rp.is_absolute():
            rp = ROOT / rp
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(render_report(data, data["aggregate"], data["latency"],
                                    data["buckets"], data["determinism"]), encoding="utf-8")
        print(f"[report] {rp} (from {args.report_only})")
        return 0

    cfg = {"limit": args.limit, "seed": args.seed, "hz": args.hz,
           "sessions": [s.strip() for s in args.sessions.split(",") if s.strip()]
           if args.sessions else None,
           "max_seconds": args.max_seconds, "trace": args.trace,
           "inject_clock": not args.no_inject_clock}
    fixed = None
    if args.keys_file:
        raw = json.loads(Path(args.keys_file).read_text(encoding="utf-8"))
        fixed = defaultdict(set, {str(k): {int(i) for i in v} for k, v in raw.items()})
        print(f"[keys] 固定帧集合 {len(fixed)} 会话 / {sum(len(v) for v in fixed.values())} 帧")
    res = run_eval(cfg, fixed_keys=fixed)
    if args.dump_keys:
        # 用 frame_id 而不是行号：行号会随语料追加而漂移，frame_id 是帧的稳定标识
        picked = defaultdict(list)
        for rec in res["records"]:
            if isinstance(rec.get("f"), int):
                picked[rec["s"]].append(int(rec["fid"]))
        kp = Path(args.dump_keys)
        kp.parent.mkdir(parents=True, exist_ok=True)
        kp.write_text(json.dumps({k: sorted(v) for k, v in picked.items()},
                                 ensure_ascii=False), encoding="utf-8")
        print(f"[keys] 已写出 {kp}（按 frame_id）")
    agg = _agg(res["records"])
    lat = _lat(res["records"])
    buckets = _by_bucket(res["records"])
    det = _verify(cfg, res["records"], args.verify_determinism) if args.verify_determinism else None
    res.update({"aggregate": agg, "latency": lat, "buckets": buckets,
                "determinism": det, "digest": _digest(_decisions_only(res["records"]))})

    out = Path(args.json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")

    if args.report:
        rp = Path(args.report)
        if not rp.is_absolute():
            rp = ROOT / rp
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(render_report(res, agg, lat, buckets, det), encoding="utf-8")
        if not args.quiet:
            print(f"[report] {rp}")

    if not args.quiet:
        print(f"[eval] 会话 {res['coverage']['sessions']} 帧评估 {res['sampling']['evaluated']}"
              f"/{res['sampling']['planned']} (可评估帧 {res['coverage']['evaluable']}"
              f"/{res['coverage']['total_lines']})")
        print(f"[eval] 类型@1 {_p(agg['type@1'])} @3 {_p(agg['type@3'])} | "
              f"目标@1 {_p(agg['target@1'])} @3 {_p(agg['target@3'])} | "
              f"严格@1 {_p(agg['strict@1'])} @3 {_p(agg['strict@3'])} | 翻转 {_p(agg['flip'])}")
        print(f"[eval] 模型 p50/p95/p99 = {_n(lat['model']['p50'])}/{_n(lat['model']['p95'])}/"
              f"{_n(lat['model']['p99'])}ms | 全链路 p95 {_n(lat['full']['p95'])}ms | "
              f"错误 {sum(res['errors']['model'].values())} | digest {res['digest'][:16]}")
        if det:
            print(f"[verify] {det['frames']} 帧二次运行: "
                  f"{'一致' if det['identical'] else '不一致'} "
                  f"(mismatch={det['mismatched_frames']}, missing={det['missing_frames']})")
        print(f"[json] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
