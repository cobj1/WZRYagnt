# -*- coding: utf-8 -*-
"""视觉层改动 · 真机复测（只读 states.jsonl）。

背景：本轮的视觉层改动（小地图框自适应 / 野怪-BUFF 门控 / 技能状态接线 / HSV 阈值收敛 /
      性能优化）的收益此前只在 236 张**离线帧**上做过等价性验证。真机跑一场后，用本脚本读
      `states.jsonl` 即可确认这些改动在**生产数据**里是否真的生效。

设计原则
  - **只读**：不改任何文件，可在对局进行中反复跑（半写行会被跳过并计数）。
  - 每条检查给 PASS / WARN / FAIL + **证据**（分布、占比、样例），不只看一个布尔。
  - 阈值按"改动前后应在数据上留下的**方向性**差异"设定，而非绝对值：
      * 小地图框  : 改动前 20:9 下 center=(121,121) r=121；改后应为 (x0+s/2, s/2)
      * towers_v7 : 改动前 20:9 下 97% 帧为空；改后应出现 10 我 + 10 敌
      * 野怪/BUFF : 改动前恒等于点位常量（14 / 6）；改后应随证据变化
      * skill_*   : 改动前 100% 缺失；改后应非空
      * self_bars : 性能项，本脚本用 "帧间隔(dt)" 间接看感知是否跟得上

用法
    python scripts/verify_vision_live.py                      # 自动取最新会话
    python scripts/verify_vision_live.py --match <目录|states.jsonl>
    python scripts/verify_vision_live.py --baseline <改动前会话>   # 并排对比
    python scripts/verify_vision_live.py --json temp/vision_live_report.json
    python scripts/verify_vision_live.py --ls                 # 只列可用会话
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision.mm_rules_v7 import resolve_mm_box  # noqa: E402

MATCH_ROOT = ROOT / "data" / "matches"

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"
TAG = {PASS: "[PASS]", WARN: "[WARN]", FAIL: "[FAIL]", INFO: "[ -- ]"}


def _w(s):
    """显示宽度（CJK 记 2 列），用于表格对齐。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in str(s))


def _pad(s, width):
    s = str(s)
    return s + " " * max(0, width - _w(s))

# 改动前的"常量"基线（用于判定门控是否真的生效）
LEGACY_MONSTER_CONST = 14
LEGACY_BUFF_CONST = 6
# 改动前 20:9 的小地图框（center 半径 121 = 242/2）
LEGACY_CENTER_2029 = (121.0, 121.0)


# --------------------------------------------------------------------------- IO
def find_sessions():
    return sorted((p / "states.jsonl" for p in MATCH_ROOT.glob("*")),
                  key=lambda p: p.stat().st_mtime if p.exists() else 0)


def latest_session():
    s = [p for p in find_sessions() if p.exists()]
    return s[-1] if s else None


def load_states(src):
    p = Path(src)
    if p.is_dir():
        p = p / "states.jsonl"
    if not p.exists():
        raise SystemExit(f"找不到 states.jsonl: {p}")
    frames, bad = [], 0
    for line in p.open(encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            frames.append(json.loads(line))
        except Exception:
            bad += 1          # 半写行（对局进行中会读到）
    return p, frames, bad


def _dig(d, *path):
    """按路径取值；任一层缺失/为 None 则返回 None。"""
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
        if cur is None:
            return None
    return cur


def _in_match(frames):
    return [f for f in frames if f.get("phase") == "in_match"]


def _mm_found(frames):
    return [f for f in frames if (f.get("minimap") or {}).get("found")]


def _dist(vals):
    return dict(sorted(Counter(vals).items()))


# ----------------------------------------------------------------------- checks
def check_layout(frames, im):
    ss = Counter()
    for f in frames:
        v = f.get("screen_size") or []
        if len(v) >= 2:
            ss[f"{int(v[0])}x{int(v[1])}"] += 1
    layouts = []
    for k in ss:
        w, h = (int(x) for x in k.split("x"))
        layouts.append(f"{k}({w / h:.2f}:1)")
    detail = " / ".join(f"{k}x{n}帧" for k, n in ss.most_common())
    return dict(name="1. 采集布局", status=INFO,
                detail=detail or "(无 screen_size)",
                evidence={"同比": layouts, "分布": dict(ss)})


def check_mm_box(frames, im):
    rows = []
    for f in _mm_found(im):
        v = f.get("screen_size") or []
        mm = f.get("minimap") or {}
        if len(v) < 2 or not mm.get("center") or mm.get("radius") is None:
            continue
        w, h = int(v[0]), int(v[1])
        x0, y0, s = resolve_mm_box(np.zeros((h, w, 3), np.uint8))
        rows.append((f"{w}x{h}", (x0 + s / 2.0, y0 + s / 2.0, s / 2.0),
                     (float(mm["center"][0]), float(mm["center"][1]), float(mm["radius"]))))
    if not rows:
        return dict(name="2. 小地图框 (①)", status=WARN, detail="无 minimap.found 帧可核",
                    evidence={})
    dx = max(abs(a[2][0] - a[1][0]) for a in rows)
    dy = max(abs(a[2][1] - a[1][1]) for a in rows)
    dr = max(abs(a[2][2] - a[1][2]) for a in rows)
    status = PASS if (dx <= 3 and dy <= 3 and dr <= 3) else FAIL
    layout, exp, got = rows[0]
    detail = (f"{layout} 期望 center=({exp[0]:.0f},{exp[1]:.0f}) r={exp[2]:.0f} | "
              f"实测 center=({got[0]:.0f},{got[1]:.0f}) r={got[2]:.0f} | "
              f"最大偏差 dx={dx:.0f} dy={dy:.0f} dr={dr:.0f}px")
    if status == FAIL and abs(got[0] - LEGACY_CENTER_2029[0]) <= 2 \
            and abs(got[1] - LEGACY_CENTER_2029[1]) <= 2:
        detail += "  ← 仍是改动前的 242 方框(原点 0,0)，改动未生效"
    return dict(name="2. 小地图框 (①)", status=status, detail=detail,
                evidence={"比对帧数": len(rows), "dx": round(dx, 1),
                          "dy": round(dy, 1), "dr": round(dr, 1)})


def check_towers(frames, im):
    ally, enemy = [], []
    for f in _mm_found(im):
        tv = _dig(f, "minimap", "towers_v7") or {}
        ally.append(len(tv.get("ally") or []))
        enemy.append(len(tv.get("enemy") or []))
    if not ally:
        return dict(name="3. 分阵营塔 towers_v7 (①)", status=WARN, detail="无 found 帧",
                    evidence={})
    ma, me = max(ally), max(enemy)
    if ma >= 10 and me >= 10:
        status = PASS
    elif ma >= 6 and me >= 6:
        status = WARN
    else:
        status = FAIL
    detail = (f"帧数 {len(ally)} | ally: 中位 {st.median(ally):.0f} 最大 {ma} | "
              f"enemy: 中位 {st.median(enemy):.0f} 最大 {me} | "
              f"满分(10+10)帧 {sum(1 for a, e in zip(ally, enemy) if a >= 10 and e >= 10)}")
    if status == FAIL:
        detail += "  ← 改动前 20:9 下 97% 帧为空(中位 0)"
    return dict(name="3. 分阵营塔 towers_v7 (①)", status=status, detail=detail,
                evidence={"ally分布": _dist(ally), "enemy分布": _dist(enemy)})


def _pts(v):
    """把 dots/buff 列表统一成"点位集合"（四舍五入到 1e-3），用于判断是否只是个数恒定。"""
    out = []
    for it in v:
        p = it.get("n") if isinstance(it, dict) else it
        try:
            out.append((round(float(p[0]), 3), round(float(p[1]), 3)))
        except Exception:
            pass
    return tuple(sorted(out))


def check_marker_gate(frames, im, key, label, legacy_const, num):
    nm = _mm_found(im)
    if not nm:
        # 小地图没定位到时 dots/v7 本来就没有内容，不能笼统报"字段缺失"
        return dict(name=f"{num}. {label}门控 (③)", status=WARN,
                    detail=f"无 minimap.found 帧（未定位到小地图，无法判定门控）",
                    evidence={})
    # v15.5/15.6: 生产者可以显式声明"该字段不可用"（野怪/BUFF 的颜色判据对常驻营地图标无效）。
    # 此时空输出是**有意**的，不能当门控失效 —— 报 INFO 并说明原因。
    _sup = {
        ("v7", "buff"): ("buff_supported", "BUFF",
                         "颜色证据落在常驻营地图标上，无法区分在野状态",
                         "mm_rules_v7.MM_BUFF_MODE"),
        ("dots", "yellow"): ("monster_supported", "野怪",
                             "14 个点位里 9 个恒亮(常驻图标)、4 个恒灭(空坐标)、"
                             "1 个是英雄头像，逐点复核证明不产生真实信息",
                             "mm_rules_v7.MM_MONSTER_MODE"),
    }
    if key in _sup:
        flag, who, why, where = _sup[key]
        flags = [_dig(f, "minimap", "v7", flag) for f in nm]
        if any(v is False for v in flags):
            return dict(name=f"{num}. {label}门控 (③)", status=INFO,
                        detail=f"生产者已标注 {flag}=False（{who}不可用）：{why} → 有意输出空。"
                               f"详见 {where}",
                        evidence={flag: False})
    vals, sets, missing = [], set(), 0
    for f in nm:
        v = _dig(f, "minimap", *key)
        if v is None:
            missing += 1
            continue
        vals.append(len(v) if isinstance(v, list) else int(v))
        if isinstance(v, list):
            sets.add(_pts(v))
    if not vals:
        return dict(name=f"{num}. {label}门控 (③)", status=WARN,
                    detail=f"字段缺失（{len(nm)} 个 found 帧里都取不到 {'/'.join(key)}）",
                    evidence={})
    uniq = sorted(set(vals))
    const = len(uniq) == 1
    if not const:
        status = PASS
    elif uniq[0] == legacy_const:
        status = FAIL
    else:
        status = WARN
    detail = (f"取值 {uniq if len(uniq) <= 8 else str(uniq[:8]) + '...'} | "
              f"均值 {st.mean(vals):.1f} 中位 {st.median(vals):.0f} "
              f"min {min(vals)} max {max(vals)} | 恒定={'是' if const else '否'} | "
              f"点位集合种类={len(sets)}")
    if const and uniq[0] == legacy_const:
        detail += f"  ← 仍恒等于点位常量 {legacy_const}，门控未生效"
    elif const and len(sets) == 1 and uniq[0] > 0:
        # 个数恒定 **且** 每次都是同一批点 -> 这些点上的"证据"是常驻的（装饰/描边），
        # 门控等于没做：它不反映目标的生死。这是比"个数恒定"更强的判定。
        detail += (f"  ← {uniq[0]} 个点**始终同时成立**：该点位上的证据是常驻的"
                   f"（装饰/描边），门控对该项无效")
        status = FAIL
    elif const:
        detail += ("  ← 恒定但非旧常量：可能是 detect 缓存命中(帧间隔<MM_CACHE_TTL)、"
                   "门控过严(全 0)或本局确实无该目标")
    return dict(name=f"{num}. {label}门控 (③)", status=status, detail=detail,
                evidence={"取值分布": _dist(vals)})


def check_skill(frames, im):
    n = max(1, len(im))
    has = sum(1 for f in im if _dig(f, "ui", "skill_states") not in (None, {}, []))
    per = {k: sum(1 for f in im if _dig(f, "ui", k) is not None)
           for k in ("skill1_ready", "skill2_ready", "skill3_ready")}
    rate = has / n
    if not im:
        return dict(name="6. 技能状态 (P0 ①)", status=INFO,
                    detail="无 in_match 帧（本会话未进对局），无样本可判",
                    evidence={"skill_states": 0, "in_match": 0})
    # 非真实采集（source != 'live'，如离线合成/回放）时无法判定 P0 是否生效：
    # 技能状态由 m2 运行时写入，合成数据造不出来。降级为 INFO，避免误导。
    srcs = Counter(f.get("source") for f in im)
    live = all(f.get("source") == "live" for f in im)
    if not live:
        return dict(name="6. 技能状态 (P0 ①)", status=INFO,
                    detail=f"数据非 live 采集(source={dict(srcs)})，技能状态由 m2 运行时写入，"
                           f"离线/合成数据无法判定 —— 请用真机会话核查",
                    evidence={"skill_states": has, "in_match": len(im), "平铺位": per})
    # 技能状态与 death_banner / ally_hp_min 写在**同一个 0.5s 节流块**里
    # （m2 的 ui 读数块），所以它的"应达覆盖率"不是 100%，而是与节流节奏一致。
    # 实测真机 1180 帧：skill_states / death_banner / ally_hp_min 都是 281 帧，中位间隔 0.57s。
    # 因此用 death_banner 的非空帧数当节流基线，判"是否跟着节流块一起活着"。
    throttle_n = sum(1 for f in im if _dig(f, "ui", "death_banner") is not None)
    base = max(throttle_n, 1)
    ratio = has / base
    if has > 0 and (ratio >= 0.8 or rate >= 0.5):
        status = PASS
    elif has > 0:
        status = WARN
    else:
        status = FAIL
    detail = (f"skill_states 非空 {has}/{n} ({rate:.0%}) | "
              f"节流块基线 death_banner={throttle_n} → 相对覆盖 {ratio:.0%} | "
              + " ".join(f"{k}={v}" for k, v in per.items()))
    if status == FAIL:
        detail += "  ← 改动前 100% 缺失(P0 缺陷症状)"
    return dict(name="6. 技能状态 (P0 ①)", status=status, detail=detail,
                evidence={"skill_states": has, "in_match": len(im), "平铺位": per})


def check_hp_mp(frames, im):
    n = max(1, len(im))
    cov = {k: sum(1 for f in im if _dig(f, "ui", k) is not None) / n
           for k in ("hp", "mp")}
    lo = min(cov.values())
    status = PASS if lo >= 0.8 else (WARN if lo >= 0.5 else FAIL)
    detail = " | ".join(f"{k} {v:.0%}" for k, v in cov.items())
    others = {k: sum(1 for f in im if _dig(f, "ui", k) is not None)
              for k in ("death_banner", "kill_banner", "lock_target",
                        "ally_hp_min", "enemy_hp_min")}
    if not im:
        return dict(name="7. 血量/MP 读数", status=INFO,
                    detail="无 in_match 帧（本会话未进对局），无样本可判", evidence=others)
    if lo == 0:
        # 整局无血量读数。三个可区分的成因（判据数值见 self_bars.py）：
        #   真死亡（"查看死亡回放"深红横幅）death_banner_px >= 2500（实测 13259）
        #   死亡后观战（红色血条 HUD）      death_banner_px ~1000，不达阈值
        #   都不是 → 读数缺陷（如分段式血条把绿条切碎，v16.0 已修）
        db = [v for v in (_dig(f, "ui", "death_banner") for f in im) if isinstance(v, (int, float))]
        dead = sum(1 for v in db if v >= 2500)
        selfdot = sum(1 for f in im if _dig(f, "minimap", "dots", "green"))
        if db and dead >= 0.5 * len(db):
            detail += "  ← death_banner>=2500 占多数：整局死亡（挂机），非读数缺陷"
        elif selfdot == 0:
            detail += ("  ← 且全无自身绿点：非本人视角（观战/跨局），"
                       "该会话不能用于血量判读")
        else:
            detail += "  ← 但在场有自身绿点：**疑似读数缺陷**（v16.0 已针对性修复分段式血条）"
    return dict(name="7. 血量/MP 读数", status=status, detail=detail, evidence=others)


def check_decision_input(frames, im):
    if not im:
        return dict(name="8. 决策层输入可用性", status=INFO,
                    detail="无 in_match 帧（本会话未进对局），无样本可判", evidence={})
    n = len(im)
    hero = sum(1 for f in im if any(u.get("cls") == "enemy_hero"
                                    for u in (f.get("units") or [])))
    red = sum(1 for f in im if _dig(f, "minimap", "dots", "red"))
    blue = sum(1 for f in im if _dig(f, "minimap", "dots", "blue"))
    green = sum(1 for f in im if _dig(f, "minimap", "dots", "green"))
    tvals = [len((_dig(f, "minimap", "towers_v7", "enemy") or [])) for f in _mm_found(im)]
    tmed = st.median(tvals) if tvals else 0
    # v15.8: 生产端可声明"小地图敌英不可用"，此时红点为 0 是**有意**的，
    # 不能读成"附近没有敌人"（判据在该画风下无精度无召回）。
    red_off = any(_dig(f, "minimap", "v7", "red_supported") is False for f in _mm_found(im))
    red_txt = "不可用(red_supported=False)" if red_off else f"{red}/{n}"
    detail = (f"屏内敌英框 {hero}/{n} ({hero / n:.0%}) | 小地图红点 {red_txt} | "
              f"蓝点 {blue}/{n} | 绿点(自身) {green}/{n} | 敌塔数中位 {tmed:.0f}")
    return dict(name="8. 决策层输入可用性", status=INFO, detail=detail,
                evidence={"敌塔数分布": _dist(tvals), "red_supported": not red_off})


def check_throttle(frames, im):
    ts = [f.get("t") for f in im if isinstance(f.get("t"), (int, float))]
    dts = [b - a for a, b in zip(ts, ts[1:]) if 0 < b - a < 5]
    if not dts:
        return dict(name="9. 感知节拍", status=INFO, detail="无时间戳", evidence={})
    med = st.median(dts)
    detail = (f"帧间 dt 中位 {med * 1000:.0f}ms (≈{1 / med:.1f} Hz) | "
              f"p90 {st.quantiles(dts, n=10)[8] * 1000:.0f}ms | 帧数 {len(dts) + 1}")
    return dict(name="9. 感知节拍", status=INFO, detail=detail,
                evidence={"dt中位ms": round(med * 1000, 1)})


# ------------------------------------------------------------------------- main
def analyze(src):
    p, frames, bad = load_states(src)
    im = _in_match(frames)
    checks = [
        check_layout(frames, im),
        check_mm_box(frames, im),
        check_towers(frames, im),
        check_marker_gate(frames, im, ("dots", "yellow"), "野怪", LEGACY_MONSTER_CONST, 4),
        check_marker_gate(frames, im, ("v7", "buff"), "BUFF", LEGACY_BUFF_CONST, 5),
        check_skill(frames, im),
        check_hp_mp(frames, im),
        check_decision_input(frames, im),
        check_throttle(frames, im),
    ]
    cnt = Counter(c["status"] for c in checks)
    return dict(
        source=str(p),
        mtime=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime)),
        frames=len(frames), in_match=len(im), bad_lines=bad,
        checks=checks,
        summary={k: cnt.get(k, 0) for k in (PASS, WARN, FAIL, INFO)},
    )


def print_report(r, title="真机复测报告"):
    print("=" * 78)
    print(f" {title}")
    print("=" * 78)
    print(f" 数据源 : {r['source']}")
    print(f" 写入时间: {r['mtime']}   总帧 {r['frames']}   in_match {r['in_match']}"
          + (f"   (跳过半写行 {r['bad_lines']})" if r["bad_lines"] else ""))
    if r["in_match"] == 0:
        print("\n ⚠️  本会话没有 in_match 帧（未进对局）—— 复测无意义，请重跑一场到对局内。\n")
    print("-" * 78)
    for c in r["checks"]:
        print(f" {TAG[c['status']]} {c['name']}")
        print(f"         {c['detail']}")
        if c.get("evidence"):
            ev = ", ".join(f"{k}={v}" for k, v in c["evidence"].items())
            if len(ev) <= 200:
                print(f"         证据: {ev}")
    s = r["summary"]
    print("-" * 78)
    print(f" 汇总: PASS {s[PASS]}  WARN {s[WARN]}  FAIL {s[FAIL]}  (INFO {s[INFO]})")
    print("=" * 78)


def compare(base, cur):
    """并排对比改动前/后会话的关键指标。"""
    def pick(r):
        out = {}
        for c in r["checks"]:
            n = c["name"]
            if n.startswith("2."):
                out["小地图框最大偏差(px)"] = c["evidence"].get("dx")
            elif n.startswith("3."):
                a = c["evidence"].get("ally分布") or {}
                e = c["evidence"].get("enemy分布") or {}
                out["塔 ally 最大"] = max((int(k) for k in a), default=None)
                out["塔 enemy 最大"] = max((int(k) for k in e), default=None)
            elif "野怪门控" in n:
                if (c.get("evidence") or {}).get("monster_supported") is False:
                    out["野怪"] = "不可用(有意为空)"
                else:
                    out["野怪"] = f"{len(c['evidence'].get('取值分布') or {})} 种取值"
            elif "BUFF门控" in n:
                if (c.get("evidence") or {}).get("buff_supported") is False:
                    out["BUFF"] = "不可用(有意为空)"
                else:
                    out["BUFF"] = f"{len(c['evidence'].get('取值分布') or {})} 种取值"
            elif n.startswith("6."):
                out["skill_states 非空帧(需>0)"] = c["evidence"].get("skill_states")
            elif n.startswith("7."):
                out["hp/mp 覆盖率"] = c["detail"]
        return out
    b, c = pick(base), pick(cur)
    keys = list(dict.fromkeys(list(b) + list(c)))
    w = max(_w(k) for k in keys) if keys else 10
    print("=" * 78)
    print(" 基线(--baseline) vs 当前(--match)｜同项对比")
    print("=" * 78)
    print(f" {_pad('指标', w)} | {_pad('基线', 24)} | 当前")
    print("-" * 78)
    for k in keys:
        print(f" {_pad(k, w)} | {_pad(str(b.get(k, '-')), 24)} | {c.get(k, '-')}")
    print("=" * 78)


def main():
    ap = argparse.ArgumentParser(description="视觉层改动的真机复测（只读 states.jsonl）")
    ap.add_argument("--match", help="会话目录 或 states.jsonl 路径（默认取最新）")
    ap.add_argument("--baseline", help="改动前的会话，用于并排对比")
    ap.add_argument("--json", help="把报告写到该路径")
    ap.add_argument("--ls", action="store_true", help="只列出可用会话后退出")
    args = ap.parse_args()

    if args.ls:
        for p in find_sessions():
            if p.exists():
                mt = time.strftime("%m-%d %H:%M", time.localtime(p.stat().st_mtime))
                n = sum(1 for _ in p.open(encoding="utf-8", errors="replace"))
                print(f"{mt}  {n:>6}帧  {p.parent.name}")
        return

    src = args.match or latest_session()
    if not src:
        raise SystemExit(f"data/matches 下没有 states.jsonl（在 {MATCH_ROOT}）")
    rep = analyze(src)
    print_report(rep, "视觉层真机复测 · 当前会话")

    out = {"current": rep}
    if args.baseline:
        base = analyze(args.baseline)
        out["baseline"] = base
        print()
        compare(base, rep)

    if args.json:
        o = Path(args.json)
        o.parent.mkdir(parents=True, exist_ok=True)
        o.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入 {o}")

    return 0 if rep["summary"][FAIL] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
