# -*- coding: utf-8 -*-
"""统一决策引擎。

所有决策来源（规则 / 官方复刻 / 优化器 / 主动找敌 / BC 策略 / 安全兜底）
都通过同一对象产出 DecisionOutput，并累计来源与耗时指标。引擎不决定
来源优先级——顺序仍由 m2 主循环控制，避免把既有决策语义搬进新层。

用法：
    eng = DecisionEngine(decide)
    out = eng.decide_rule(state, cooldowns)
    action = out.action
    out = eng.adopt(action, source="optimizer", latency_ms=...)
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Callable

from wzry.decision.output import DecisionOutput
from wzry.observability.metrics import Metrics
from wzry.task.protocol import Task


class DecisionEngine:
    def __init__(self, rule_decide: Callable[[dict, dict], dict],
                 metrics: Metrics | None = None):
        self._rule_decide = rule_decide
        self.metrics = metrics or Metrics()
        self.frames = 0
        self.adoptions = 0
        self.last: DecisionOutput | None = None
        self.source_counts: dict[str, int] = defaultdict(int)

    def _record(self, out: DecisionOutput, metric_name: str) -> DecisionOutput:
        self.last = out
        self.source_counts[out.source] += 1
        self.metrics.inc(f"decision.source.{out.source}")
        self.metrics.observe(metric_name, out.latency_ms)
        return out

    def decide_rule(self, state: dict, cooldowns: dict) -> DecisionOutput:
        t0 = time.perf_counter()
        action = self._rule_decide(state, cooldowns)
        latency = (time.perf_counter() - t0) * 1000.0
        if not isinstance(action, dict) or not action.get("type"):
            self.metrics.inc("decision.error.rule")
            raise TypeError(f"规则决策返回非法 action: {action!r}")
        out = DecisionOutput(
            action=action,
            source="rule",
            reason=str(action.get("reason") or ""),
            latency_ms=latency,
        )
        self.frames += 1
        self.metrics.inc("decision.frames")
        return self._record(out, "decision.rule_ms")

    def adopt(self, action: dict, source: str, *,
              latency_ms: float = 0.0,
              reason: str | None = None,
              meta: dict[str, Any] | None = None,
              task: Task | None = None) -> DecisionOutput:
        """接管一次非规则决策；不增加 frames（同一帧可多次改写）。"""
        if not isinstance(action, dict) or not action.get("type"):
            self.metrics.inc(f"decision.error.{source}")
            raise TypeError(f"{source} 返回非法 action: {action!r}")
        out = DecisionOutput(
            action=action,
            source=source,
            reason=str(reason if reason is not None else action.get("reason") or ""),
            latency_ms=max(0.0, float(latency_ms)),
            meta=dict(meta or {}),
            task=task,
        )
        self.adoptions += 1
        self.metrics.inc("decision.adoptions")
        return self._record(out, f"decision.{source}_ms")

    def decide_with(self, fn: Callable[..., dict], source: str, *args,
                    **kwargs) -> DecisionOutput:
        """执行一个候选来源并接管其结果；异常原样抛出由调用方兜底。"""
        t0 = time.perf_counter()
        action = fn(*args, **kwargs)
        latency = (time.perf_counter() - t0) * 1000.0
        return self.adopt(action, source=source, latency_ms=latency)

    def summary(self) -> dict[str, Any]:
        return {
            "frames": self.frames,
            "adoptions": self.adoptions,
            "sources": dict(self.source_counts),
            "last_source": self.last.source if self.last else None,
            "last_reason": self.last.reason if self.last else None,
            "metrics": self.metrics.snapshot(),
        }
