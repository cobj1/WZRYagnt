# -*- coding: utf-8 -*-
"""决策轨迹汇总与 HTML 面板（OBS-01）。

输入是 m2 `--decision-trace` 产出的 decision_trace.jsonl；输出：
  - summary dict（来源/动作/reason 分布，decide/loop/capture/infer 的 p50/p95/max）；
  - 单文件 HTML（无外部依赖，可直接在浏览器打开复盘）。

只读、离线；不依赖设备，也不改任何决策。
"""
from __future__ import annotations

import html
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    k = max(0, min(len(vals) - 1, int(math.ceil(p / 100.0 * len(vals))) - 1))
    return round(vals[k], 1)


def _num(row: dict, key: str) -> float | None:
    v = row.get(key)
    return float(v) if isinstance(v, (int, float)) else None


def summarize(rows: Iterable[dict]) -> dict[str, Any]:
    n = 0
    sources = Counter()
    actions = Counter()
    reasons = Counter()
    tasks = Counter()
    lat = {"latency_ms": [], "loop_ms": [], "capture_lag_ms": [], "infer_ms": []}
    hp_bad = 0
    for row in rows:
        n += 1
        sources[str(row.get("source") or "?")] += 1
        actions[str(row.get("action_type") or "?")] += 1
        if row.get("reason"):
            reasons[str(row["reason"])] += 1
        task = row.get("task")
        if isinstance(task, dict) and task.get("goal"):
            tasks[str(task["goal"])] += 1
        hp = row.get("hp")
        if isinstance(hp, (int, float)) and hp < 0.3:
            hp_bad += 1
        for key, bucket in lat.items():
            v = _num(row, key)
            if v is not None:
                bucket.append(v)
    timing = {k: {"n": len(v), "p50_ms": _pct(v, 50), "p95_ms": _pct(v, 95),
                  "max_ms": round(max(v), 1) if v else None}
              for k, v in lat.items()}
    return {
        "frames": n,
        "sources": dict(sources.most_common()),
        "actions": dict(actions.most_common()),
        "reasons": dict(reasons.most_common(15)),
        "tasks": dict(tasks.most_common(15)),
        "timing": timing,
        "low_hp_frames": hp_bad,
    }


def summarize_file(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return summarize(json.loads(line) for line in f if line.strip())


def _cards(summary: dict[str, Any]) -> str:
    items = [
        ("帧数", summary["frames"]),
        ("来源数", len(summary["sources"])),
        ("低血量帧", summary["low_hp_frames"]),
        ("decide p95", (summary["timing"]["latency_ms"]["p95_ms"] or 0)),
        ("loop p95", (summary["timing"]["loop_ms"]["p95_ms"] or 0)),
        ("capture p95", (summary["timing"]["capture_lag_ms"]["p95_ms"] or 0)),
        ("infer p95", (summary["timing"]["infer_ms"]["p95_ms"] or 0)),
    ]
    return "".join(
        f'<div class="card"><div class="k">{html.escape(str(k))}</div>'
        f'<div class="v">{html.escape(str(v))}</div></div>' for k, v in items)


def _table(title: str, data: dict[str, int], limit: int = 20) -> str:
    body = "".join(
        f"<tr><td>{html.escape(str(k))}</td><td>{v}</td></tr>"
        for k, v in list(data.items())[:limit])
    return (f"<h2>{html.escape(title)}</h2>"
            f'<table><thead><tr><th>key</th><th>count</th></tr></thead>'
            f"<tbody>{body}</tbody></table>")


def render_html(summary: dict[str, Any], title: str = "决策轨迹复盘") -> str:
    body = [
        "<!doctype html><meta charset='utf-8'>",
        f"<title>{html.escape(title)}</title>",
        "<style>body{font:14px/1.5 system-ui,Segoe UI,sans-serif;margin:24px;"
        "background:#0f172a;color:#e2e8f0}h1{font-size:20px}h2{font-size:15px;"
        "margin-top:24px;color:#93c5fd}.cards{display:flex;gap:10px;flex-wrap:wrap}"
        ".card{background:#1e293b;border:1px solid #334155;border-radius:8px;"
        "padding:10px 14px;min-width:120px}.card .k{color:#94a3b8;font-size:12px}"
        ".card .v{font-size:20px;font-weight:600}table{border-collapse:collapse;"
        "margin-top:8px;min-width:360px}th,td{border-bottom:1px solid #334155;"
        "padding:4px 10px;text-align:left}th{color:#94a3b8}</style>",
        f"<h1>{html.escape(title)}</h1>",
        "<div class='cards'>", _cards(summary), "</div>",
        _table("决策来源", summary["sources"]),
        _table("动作类型", summary["actions"]),
        _table("reason Top", summary["reasons"]),
        _table("宏任务意图 Top", summary["tasks"]),
    ]
    return "".join(body)
