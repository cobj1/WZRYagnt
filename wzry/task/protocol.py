"""任务协议（P0 冻结）。

任务只描述"下一步想干什么"，不描述坐标、技能编号或鼠标动作。
单 AI 时由自主规划器产生；多 AI 时由 Coordinator 产生，Policy 不感知来源。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

VALID_SOURCES = {"self", "coordinator"}


@dataclass(frozen=True)
class Claim:
    """Coordinator 侧目标锁，防止多个英雄扑同一目标。"""
    target_id: str | None = None
    lease_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {"target_id": self.target_id, "lease_seconds": self.lease_seconds}


@dataclass(frozen=True)
class OnAbort:
    """任务中止时的统一清理动作。"""
    release_stick: bool = True
    rollback_cooldowns: bool = False
    cancel_pending_combo: bool = True

    def to_dict(self) -> dict:
        return {
            "release_stick": self.release_stick,
            "rollback_cooldowns": self.rollback_cooldowns,
            "cancel_pending_combo": self.cancel_pending_combo,
        }


@dataclass
class Task:
    task_id: str
    agent_id: str = "hero_A"
    source: str = "self"
    goal: str = ""
    priority: int = 0
    preemptible: bool = True
    preconditions: list[str] = field(default_factory=list)
    expires_in_seconds: float = 5.0
    abort_conditions: list[str] = field(default_factory=list)
    on_abort: OnAbort = field(default_factory=OnAbort)
    claim: Claim | None = None
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("task_id 不能为空")
        if self.source not in VALID_SOURCES:
            raise ValueError(f"非法 source: {self.source}")
        if not self.goal:
            raise ValueError("goal 不能为空")
        if not 0 <= int(self.priority) <= 100:
            raise ValueError(f"priority 必须在 0..100: {self.priority}")
        if float(self.expires_in_seconds) <= 0:
            raise ValueError("expires_in_seconds 必须为正")
        if self.claim is not None and not isinstance(self.claim, Claim):
            raise ValueError("claim 必须是 Claim 或 None")

    def is_expired(self, now: float | None = None) -> bool:
        now = time.time() if now is None else float(now)
        return now - self.created_at >= float(self.expires_in_seconds)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "source": self.source,
            "goal": self.goal,
            "priority": int(self.priority),
            "preemptible": bool(self.preemptible),
            "preconditions": list(self.preconditions),
            "expires_in_seconds": float(self.expires_in_seconds),
            "abort_conditions": list(self.abort_conditions),
            "on_abort": self.on_abort.to_dict(),
            "claim": self.claim.to_dict() if self.claim else None,
            "created_at": float(self.created_at),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Task":
        claim = payload.get("claim")
        on_abort = payload.get("on_abort") or {}
        return cls(
            task_id=str(payload["task_id"]),
            agent_id=str(payload.get("agent_id", "hero_A")),
            source=str(payload.get("source", "self")),
            goal=str(payload.get("goal", "")),
            priority=int(payload.get("priority", 0)),
            preemptible=bool(payload.get("preemptible", True)),
            preconditions=list(payload.get("preconditions") or []),
            expires_in_seconds=float(payload.get("expires_in_seconds", 5.0)),
            abort_conditions=list(payload.get("abort_conditions") or []),
            on_abort=OnAbort(
                release_stick=bool(on_abort.get("release_stick", True)),
                rollback_cooldowns=bool(on_abort.get("rollback_cooldowns", False)),
                cancel_pending_combo=bool(on_abort.get("cancel_pending_combo", True)),
            ),
            claim=Claim(
                target_id=claim.get("target_id"),
                lease_seconds=float(claim.get("lease_seconds", 0.0)),
            ) if claim else None,
            created_at=float(payload.get("created_at", time.time())),
        )
