# -*- coding: utf-8 -*-
"""离线回放评估（EVAL-01）。

把 `data/matches/<session>/{states,actions}.jsonl` 按时间对齐，逐帧调用当前
`DecisionEngine.decide_rule`，统计：
  - 与当时实际下发动作的 **type/reason 一致率**（稳定性回归，不是人类胜率）；
  - 动作分布、决策耗时 p50/p95、异常数。

口径限制（必须一起读）：
  - 历史动作由当时的规则产生，当前规则改动后一致率下降不一定代表变差；
  - cooldowns 只能在新进程里重建（camp 默认 blue），无法完全复现历史内部状态；
  - 因此本报告用于**版本间回归对比**，不能当"正确率"对外引用。
"""
from __future__ import annotations

import bisect
import json
import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

STATE_KEYS = ("t", "screen_size", "units", "minimap", "ui", "extra", "frame_id")


@dataclass
class SessionResult:
    session: str
    frames: int = 0
    matched: int = 0
    type_agree: int = 0
    reason_agree: int = 0
    errors: int = 0
    types: Counter = field(default_factory=Counter)
    reasons: Counter = field(default_factory=Counter)
    latencies: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "frames": self.frames,
            "matched": self.matched,
            "type_agree": self.type_agree,
            "reason_agree": self.reason_agree,
            "errors": self.errors,
            "type_agreement": (round(self.type_agree / self.matched, 4)
                               if self.matched else None),
            "reason_agreement": (round(self.reason_agree / self.matched, 4)
                                 if self.matched else None),
            "types": dict(self.types),
            "reasons": dict(self.reasons.most_common(10)),
            "p95_ms": round(_pct(self.latencies, 95), 3) if self.latencies else None,
        }


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(math.ceil(p / 100.0 * len(s))) - 1))
    return s[k]


def load_actions(path: str | Path) -> list[tuple[float, dict]]:
    out: list[tuple[float, dict]] = []
    p = Path(path)
    if not p.exists():
        return out
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = row.get("t")
            if isinstance(t, (int, float)):
                out.append((float(t), row))
    out.sort(key=lambda x: x[0])
    return out


def nearest_action(actions: list[tuple[float, dict]], t: float,
                   tol: float) -> dict | None:
    if not actions:
        return None
    times = [a[0] for a in actions]
    i = bisect.bisect_left(times, t)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(actions):
            delta = abs(actions[j][0] - t)
            if delta <= tol and (best is None or delta < best[0]):
                best = (delta, actions[j][1])
    return best[1] if best else None


def evaluate_session(*, session: str, states_path: str | Path,
                     actions_path: str | Path,
                     decide: Callable[[dict, dict], Any], tolerance: float = 1.0,
                     limit: int = 0, camp: str = "blue") -> SessionResult:
    actions = load_actions(actions_path)
    cooldowns: dict[str, Any] = {"camp": camp}
    res = SessionResult(session=session)
    with open(states_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if limit and res.frames >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                res.errors += 1
                continue
            st = {k: raw.get(k) for k in STATE_KEYS if k in raw}
            t = st.get("t")
            if not isinstance(t, (int, float)):
                res.errors += 1
                continue
            if "match_start_t" not in cooldowns:
                cooldowns["match_start_t"] = float(t)
            try:
                out = decide(st, cooldowns)
                act = out.action if hasattr(out, "action") else out
                latency = float(getattr(out, "latency_ms", 0.0))
            except Exception:
                res.errors += 1
                continue
            res.frames += 1
            res.latencies.append(latency)
            res.types[str(act.get("type"))] += 1
            if act.get("reason"):
                res.reasons[str(act["reason"])] += 1
            rec = nearest_action(actions, float(t), tolerance)
            if rec is None:
                continue
            res.matched += 1
            if str(rec.get("type")) == str(act.get("type")):
                res.type_agree += 1
            if rec.get("reason") and rec.get("reason") == act.get("reason"):
                res.reason_agree += 1
    return res


def summarize(results: Iterable[SessionResult]) -> dict[str, Any]:
    rows = list(results)
    frames = sum(r.frames for r in rows)
    matched = sum(r.matched for r in rows)
    types: Counter = Counter()
    reasons: Counter = Counter()
    lat: list[float] = []
    for r in rows:
        types.update(r.types)
        reasons.update(r.reasons)
        lat.extend(r.latencies)
    return {
        "sessions": len(rows),
        "frames": frames,
        "matched": matched,
        "type_agreement": round(sum(r.type_agree for r in rows) / matched, 4)
        if matched else None,
        "reason_agreement": round(sum(r.reason_agree for r in rows) / matched, 4)
        if matched else None,
        "errors": sum(r.errors for r in rows),
        "latency_p50_ms": round(_pct(lat, 50), 3) if lat else None,
        "latency_p95_ms": round(_pct(lat, 95), 3) if lat else None,
        "actions": dict(types.most_common()),
        "reasons": dict(reasons.most_common(15)),
        "sessions_detail": [r.to_dict() for r in rows],
    }


def render_markdown(summary: dict[str, Any], title: str = "离线回放评估") -> str:
    lines = [
        f"# {title}", "",
        f"- 会话：{summary['sessions']} ｜ 帧：{summary['frames']} ｜ 可对齐：{summary['matched']}",
        f"- 动作 type 一致率：{summary['type_agreement']} ｜ reason 一致率：{summary['reason_agreement']}",
        f"- 决策 p50/p95：{summary['latency_p50_ms']} / {summary['latency_p95_ms']} ms ｜ 异常：{summary['errors']}",
        "",
        "> 一致率是**与历史规则动作**的对比，不是对错；用于版本间回归。", "",
        "## 动作分布", "",
        "| type | count |", "|---|---:|",
    ]
    for k, v in summary["actions"].items():
        lines.append(f"| {k} | {v} |")
    lines += ["", "## reason Top", "", "| reason | count |", "|---|---:|"]
    for k, v in summary["reasons"].items():
        lines.append(f"| {k} | {v} |")
    return "\n".join(lines) + "\n"
