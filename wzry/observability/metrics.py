# -*- coding: utf-8 -*-
"""轻量指标聚合（线程安全，无第三方依赖）。

只记录进程内累计值；落盘/看板由调用方决定。设计目标是让决策/执行路径
能回答"每帧各来源命中多少、耗时多少、丢了多少动作"，而不是再引一套监控栈。
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = defaultdict(float)
        self._gauges: dict[str, float] = {}
        self._timing: dict[str, list[float]] = defaultdict(lambda: [0, 0.0, 0.0])
        self.started_at = time.time()

    def inc(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self._counters[name] += float(value)

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = float(value)

    def observe(self, name: str, ms: float) -> None:
        ms = float(ms)
        with self._lock:
            row = self._timing[name]
            row[0] += 1
            row[1] += ms
            row[2] = max(row[2], ms)

    def counter(self, name: str) -> float:
        with self._lock:
            return self._counters.get(name, 0.0)

    def gauge(self, name: str) -> float | None:
        with self._lock:
            return self._gauges.get(name)

    def timing(self, name: str) -> dict[str, float]:
        with self._lock:
            n, total, mx = self._timing.get(name, [0, 0.0, 0.0])
        return {"n": int(n), "avg_ms": round(total / n, 3) if n else 0.0,
                "max_ms": round(mx, 3)}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = {k: round(v, 3) for k, v in self._counters.items()}
            gauges = {k: round(v, 3) for k, v in self._gauges.items()}
            timing = {
                k: {"n": int(v[0]),
                    "avg_ms": round(v[1] / v[0], 3) if v[0] else 0.0,
                    "max_ms": round(v[2], 3)}
                for k, v in self._timing.items()
            }
        return {
            "uptime_s": round(time.time() - self.started_at, 1),
            "counters": counters,
            "gauges": gauges,
            "timing": timing,
        }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._timing.clear()
            self.started_at = time.time()
