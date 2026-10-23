# -*- coding: utf-8 -*-
"""离线回放推进评估（v16.0）：用 behavior.jsonl 里记录的**真实输入**重跑 push_ok / _push_eval。

为什么需要：
  - 真机一局 7 分钟，一轮阈值只跑一局 → 迭代太慢；
  - "局间 hp 分布差异"已经污染过两次对照（中位 0.557 vs 0.391，无法归因）。
把输入存下来后，"调阈值 / 改条件"变成**离线扫描**：不碰游戏、没有局间方差、可任意对比同一批真实帧。

口径保证：本脚本**直接调用 m2 里的同一套函数**（`push_ok` / `push_target` / `_push_eval`），
不做任何重写；再拿"回放出的 ok/why"与"记录里的 ok/why"**逐拍比对自检** ——
不一致率高（>5%）就说明记录缺输入或代码版本漂移，此时不要采信结论。

用法：
    venv\\Scripts\\python.exe scripts\\replay_push.py                        # 用代码里的当前常量复跑 + 自检
    venv\\Scripts\\python.exe scripts\\replay_push.py --set PUSH_SELF_HP_MIN=0.35
    venv\\Scripts\\python.exe scripts\\replay_push.py --set PUSH_MIN_ALLY=0 --set PUSH_STAY_WITH_ALLY=0.30
    venv\\Scripts\\python.exe scripts\\replay_push.py --file temp/_run_C.jsonl
    # 扫描（PowerShell）：
    foreach ($hp in 0.30,0.35,0.40,0.45) { .\\venv\\Scripts\\python.exe scripts\\replay_push.py --set PUSH_SELF_HP_MIN=$hp }
"""
import argparse
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_FILE = ROOT / "temp" / "live_annot" / "behavior.jsonl"
_EPOCH_MIN = 1_600_000_000.0     # 2020-09 起才可能是真时间戳: 更小的 t 是脏数据(实测有 t=0 的行)


def load_m2():
    """按路径加载 m2（它的 import 有副作用，故不在模块顶部 import）。"""
    spec = importlib.util.spec_from_file_location("m2mod_rp", ROOT / "scripts" / "m2_agent_v2.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def frames(path):
    """读 behavior.jsonl 里"带 push 输入"的拍（保持时间顺序）。文件不存在 -> 返回 []（由调用方提示）。"""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        p = rec.get("push")
        if isinstance(p, dict) and isinstance(p.get("in"), dict):
            out.append((rec, p))
    return out


def rebuild(rec, pin, mode="shadow"):
    """用记录里的输入重建 (state_dict, cooldowns)：只放判据真正读到的字段。"""
    units = [{"cls": "enemy_hero", "screen": [0.5, 0.5, 0.02, 0.03], "conf": 0.9,
              "camp_src": "bar", "dead": False}
             for _ in range(int(pin.get("nscene") or 0))]     # 只喂个数: F3 的盲区计时只数个数
    st = {"t": float(rec.get("t") or 0.0), "screen_size": (1280, 720),
          "phase": "in_match", "units": units,
          "ui": {"hp": pin.get("hp"), "mp": 1.0}, "extra": {},
          "minimap": {"found": True,
                      "dots": {"blue": pin.get("blue") or [], "red": pin.get("red") or [],
                               "green": [], "yellow": []},
                      "towers": [],
                      "towers_v7": {"ally": [],
                                    "enemy": [{"n": t} for t in (pin.get("towers") or [])]},
                      "v7": {"minions": {"ally": [{"n": p} for p in (pin.get("waves") or [])],
                                         "enemy": []}}}}
    co = {"camp": pin.get("camp") or "blue",
          "self_est": tuple(pin.get("est") or (0.5, 0.5)),
          "push_mode": mode}
    if pin.get("esc"):        # 判据7 只关心"在不在窗口内"; 记的是布尔, 这里还原成"刚发生"
        co["escape_t"] = float(rec.get("t") or 0.0)
    return st, co


def run(mod, data, mode="shadow"):
    """逐拍回放。**只跨拍继承 F3 的盲区计时 `blind_start`**（它必须连续；线上也是每拍最多评估一次），
    其余 cooldowns 字段**每拍重建** —— 否则上一拍的逃生窗口会泄漏到下一拍
    （判定会变成"陈旧逃"，自检会报出一批假不一致）。"""
    co = None
    rows = []
    for rec, p in data:
        st, nco = rebuild(rec, p["in"], mode)
        if co is not None and "blind_start" in co:
            nco["blind_start"] = co["blind_start"]       # 唯一需要跨拍继承的字段
        co = nco
        info = mod._push_eval(st, co, p.get("scope") or "fallback",
                              tuple(p["in"]["sup"]) if p["in"].get("sup") else None)
        rows.append((rec, p, info))
    return rows


def summarize(rows, quiet=False, selfcheck=True):
    """打印统计。selfcheck=True 时才与记录逐拍比对（覆盖过常量时**必须**关掉：
    改了阈值本来就该得出不同结论，那种"不一致"不是错误）。"""
    scope_c = Counter(r[1].get("scope") or "?" for r in rows)
    why_c = Counter(("ok" if i["ok"] else i["why"]) for _, _, i in rows)
    e_ok = sum(1 for _, _, i in rows if i.get("e_ok"))
    noc = why_c.get("ok", 0)
    print(f"回放拍数: {len(rows)}  | scope: {dict(scope_c)}")
    print(f"会推: {noc} / {len(rows)} = {100.0 * noc / max(1, len(rows)):.1f}%   (scope=support 里 "
          f"{e_ok} 拍是 E-P1 会接管的)")
    print(f"闸门分布: {dict(why_c.most_common())}")
    if not selfcheck:
        print("[自检] 跳过（本次覆盖了常量 -> 与记录本就该不同）")
        return {"rows": len(rows), "ok": noc, "e_ok": e_ok, "mism": None}
    mism = [(r, p, i) for r, p, i in rows
            if bool(p.get("ok")) != bool(i["ok"])
            or (not i["ok"] and p.get("why") != i["why"])]
    print(f"[自检] 与记录不一致: {len(mism)} / {len(rows)} "
          f"({100.0 * len(mism) / max(1, len(rows)):.1f}%)  —— 应接近 0；高了就别采信")
    for r, p, i in mism[:(8 if quiet else 40)]:
        print(f"   不一致 t={r.get('t')} 记录 ok={p.get('ok')}/{p.get('why')} "
              f"回放 ok={i['ok']}/{i['why']}")
    return {"rows": len(rows), "ok": noc, "e_ok": e_ok, "mism": len(mism)}


def summarize_live(data, quiet=False):
    """只读记录里的 push 字段（**不回放**）：线上那一局真实发生了什么。

    这里能直接回答 E-P2 的关键问题之一：**有多少拍推进被 `solve()` 覆盖掉了**
    （`used=True` = decide 里已经定成推进目标，但最终动作 reason 变成了别的 → 被上游换掉）。
    """
    scope_c = Counter(p.get("scope") or "?" for _, p in data)
    why_c = Counter(("ok" if p.get("ok") else p.get("why")) for _, p in data)
    ok = why_c.get("ok", 0)
    e_ok = sum(1 for _, p in data if p.get("e_ok"))
    used = [(r, p) for r, p in data if p.get("used")]
    over = [(r, p) for r, p in used if r.get("reason") != p.get("reason")]
    if not quiet:
        print("[live] 评估拍: {}  | scope: {} | 会推: {} ({:.1f}%) | e_ok(E-P1 会接管): {}".format(
            len(data), dict(scope_c), ok, 100.0 * ok / max(1, len(data)), e_ok))
        print(f"[live] 闸门分布: {dict(why_c.most_common())}")
        print(f"[live] 实际接管(used): {len(used)}  | 其中被 solve() 覆盖成别的动作: {len(over)}")
        for r, p in over[:5]:
            print(f"   t={r.get('t')} decide 想推 {p.get('reason')} -> 最终 {r.get('reason')}")
    return {"rows": len(data), "ok": ok, "e_ok": e_ok, "used": len(used), "overridden": len(over)}


def all_records(path):
    """读**整局**记录（对照用：动作占比/回城/死亡代理量要看全部拍，不只是评过 push 的拍）。"""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def metrics(recs, evals):
    """把一局压成对照指标（**全部来自记录字段**，不重放、不猜）。

    `recs` = 整局记录；`evals` = (rec, push) 且带 `push.in` 的拍。
    注意 `label_recalling` / `reason_dead` / `reason_recall` 是**代理量** ——
    记录里没有"绝对死亡数/回城中断数"这种字段，只有每拍的 reason/label。
    """
    n = len(recs)
    reasons = Counter(r.get("reason") or "" for r in recs)
    labels = Counter(r.get("label") or "" for r in recs)
    scores = [float(r["score"]) for r in recs if isinstance(r.get("score"), (int, float))]
    # 时长要过滤脏时间戳: 实测记录里存在 t=0 这类行, 会让 max-min 变成 1.79e9 秒
    ts = [float(r["t"]) for r in recs
          if isinstance(r.get("t"), (int, float)) and float(r["t"]) >= _EPOCH_MIN]
    sup = [p for _, p in evals if p.get("scope") == "support"]
    used = [(r, p) for r, p in evals if p.get("used")]
    over = [(r, p) for r, p in used if r.get("reason") != p.get("reason")]
    m = {"frames": n,
         "dur_s": round(max(ts) - min(ts), 1) if ts else 0.0,
         "mean_score": round(sum(scores) / len(scores), 4) if scores else None,
         "push_exec": reasons.get("push_follow_wave", 0) + reasons.get("push_lane_tower", 0),
         "static_push": reasons.get("dps_lane_push", 0),
         "support_family": sum(v for k, v in reasons.items() if k.startswith("support")),
         "eval_n": len(evals),
         "support_n": len(sup),
         "support_ok": sum(1 for p in sup if p.get("ok")),
         "e_ok": sum(1 for _, p in evals if p.get("e_ok")),
         "used": len(used),
         "overridden": len(over),
         "label_recalling": labels.get("recalling", 0),
         "reason_dead": sum(v for k, v in reasons.items() if "dead" in k),
         "reason_recall": sum(v for k, v in reasons.items() if "recall" in k)}
    m["support_ratio"] = round(m["support_family"] / max(1, n), 4)
    m["support_ok_ratio"] = round(m["support_ok"] / max(1, m["support_n"]), 4)
    return m


def metrics_of(path):
    return metrics(all_records(path), frames(path))


_ROWS = ("frames", "dur_s", "mean_score", "eval_n", "support_n", "support_ok_ratio",
         "e_ok", "used", "overridden", "push_exec", "static_push", "support_ratio",
         "label_recalling", "reason_dead", "reason_recall")


def compare(path_a, path_b, min_support_ok=0.20):
    """两局对照 + 设计 §6 的达标线判定。约定 A = 对照局(shadow)，B = 变更局(on)。"""
    ma, mb = metrics_of(path_a), metrics_of(path_b)
    if ma["frames"] == 0 or mb["frames"] == 0:
        sys.exit(f"对照失败：{'A' if ma['frames'] == 0 else 'B'} 局没有读到记录"
                 f"（{path_a if ma['frames'] == 0 else path_b}）")
    print(f"对照 A(shadow)={path_a}\n     B(on)={path_b}")
    print(f"{'指标':<20}{'A':>12}{'B':>12}{'Δ(B-A)':>12}")
    for k in _ROWS:
        va, vb = ma.get(k), mb.get(k)
        try:
            d = f"{float(vb) - float(va):+.4g}"
        except (TypeError, ValueError):
            d = "—"
        print(f"{k:<20}{str(va):>12}{str(vb):>12}{d:>12}")
    ok_ok = mb["support_ok_ratio"] >= min_support_ok
    sc_a, sc_b = ma.get("mean_score"), mb.get("mean_score")
    print("\n达标线（设计 §6，提案）:")
    print(f"  [{'OK ' if ok_ok else 'NO '}] support 分支 ok 比例 >= {min_support_ok:.0%}"
          f"   (B {mb['support_ok']}/{mb['support_n']} = {mb['support_ok_ratio']:.1%})")
    print(f"  [{'OK ' if mb['support_ratio'] < ma['support_ratio'] else 'NO '}] support 家族占比下降"
          f"   (A {ma['support_ratio']:.1%} -> B {mb['support_ratio']:.1%})")
    print(f"  [{'OK ' if (sc_b is not None and sc_a is not None and sc_b >= sc_a) else 'NO '}]"
          f" 平均 score 不下降   (A {sc_a} -> B {sc_b})")
    print(f"  [{'OK ' if mb['reason_dead'] <= ma['reason_dead'] else 'NO '}] 死亡代理量不上升"
          f"   (A {ma['reason_dead']} -> B {mb['reason_dead']}，**代理量**：记录无绝对死亡数)")
    print(f"  参考: B 实际推出 push_* 动作 {mb['push_exec']} 拍，"
          f"其中被 solve() 覆盖 {mb['overridden']} 拍")
    if ma["eval_n"] == 0 and mb["eval_n"] == 0:
        print("  提示: 两局的 eval_n 都是 0 —— 文件来自**加 push.in 之前**的跑；"
              "push 相关指标全为 0 属正常（需 --push shadow 重跑）。")
    return ma, mb


def main():
    ap = argparse.ArgumentParser(description="离线回放推进评估（不碰游戏）")
    ap.add_argument("--file", default=str(DEFAULT_FILE), help="behavior.jsonl 路径")
    ap.add_argument("--set", action="append", default=[],
                    help="覆盖常量, 例: --set PUSH_SELF_HP_MIN=0.35（可重复）")
    ap.add_argument("--quiet", action="store_true", help="自检不一致明细最多打 8 条")
    ap.add_argument("--no-replay", action="store_true",
                    help="只看 live 统计（不回放），最快")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"), default=None,
                    help="两局对照 + 达标线判定（A=shadow 对照局, B=on 变更局）")
    args = ap.parse_args()

    if args.compare:                      # 只看文件, 不需要加载 m2
        compare(args.compare[0], args.compare[1])
        return

    mod = load_m2()
    changed = []
    for kv in args.set:
        k, _, v = kv.partition("=")
        if not hasattr(mod, k):
            sys.exit(f"未知常量: {k}（常量名见 scripts/m2_agent_v2.py 顶部）")
        old = getattr(mod, k)
        setattr(mod, k, float(v))
        changed.append(f"{k}: {old} -> {float(v)}")

    data = frames(args.file)
    if not data:
        sys.exit(f"没读到含 push.in 的拍（{args.file}）\n"
                 "可能原因: (1) 文件不存在（还没跑过 shadow 局）；(2) 该文件来自没接 --push 的跑；"
                 "(3) 来自加 push.in 之前的旧数据。→ 用 --push shadow 重跑一局即可。")

    print(f"文件: {args.file}")
    print(f"常量覆盖: {changed or '（无，用代码里的当前值）'}")
    summarize_live(data)
    if args.no_replay:
        return
    summarize(run(mod, data), quiet=args.quiet, selfcheck=not changed)


if __name__ == "__main__":
    main()
