# -*- coding: utf-8 -*-
"""决策输出协议：把所有决策路径统一成同一个返回对象。

source 取值：rule / official / optimizer / hunt / policy / safety / stdin。
task 只在宏动作（推进/撤退/回城）上有值，用于在轨迹里表达"这一步想干什么"，
当前不参与执行仲裁。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from wzry.runtime.action import Action, ACTION_TYPES
from wzry.task.protocol import Task

VALID_SOURCES = {"rule", "official", "optimizer", "hunt", "policy", "safety", "stdin"}


@dataclass
class DecisionOutput:
    action: dict[str, Any]
    source: str = "rule"
    reason: str = ""
    latency_ms: float = 0.0
    candidates: int = 0
    meta: dict[str, Any] = field(default_factory=dict)
    task: Task | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, dict) or not self.action.get("type"):
            raise ValueError("DecisionOutput.action 必须包含 type")
        if self.source not in VALID_SOURCES:
            raise ValueError(f"非法决策来源: {self.source}")
        if not self.reason:
            self.reason = str(self.action.get("reason") or "")

    @property
    def action_type(self) -> str:
        return str(self.action.get("type"))

    def to_action(self) -> Action | None:
        """转换成冻结的 Action 协议；none/stick_* 例外返回 None。"""
        return Action.from_decision(self.action)

    def validated_action(self) -> dict[str, Any]:
        """走一遍 Action 协议校验，再还原成执行器使用的 dict。

        none 等非协议动作原样返回；reason/duration_ms 由 Action 自身携带，
        保证与输入逐键一致。
        """
        act = self.to_action()
        return act.to_legacy_dict() if act is not None else dict(self.action)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "source": self.source,
            "reason": self.reason,
            "latency_ms": round(self.latency_ms, 3),
            "candidates": self.candidates,
            "meta": self.meta,
            "task": self.task.to_dict() if self.task else None,
        }
