"""动作协议（P0 冻结）。

所有技能/移动/面板/购买都必须转成 ``Action`` 入同一执行器；执行器负责
deadline、critical/best_effort 和占屏互斥，不依赖各模块自觉遵守。
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Any

ACTION_TYPES = {
    "map_move",
    "move",
    "skill",
    "attack",
    "summoner",
    "recall",
    "restore",
    "buy",
    "panel",
    "combo",
}
VALID_CLASSES = {"critical", "best_effort"}
VALID_ON_FAIL = {"rollback_cd", "retry_once", "abort_task", "alert"}

_action_ids = itertools.count(1)


def next_action_id() -> int:
    return next(_action_ids)


@dataclass
class Action:
    type: str
    params: dict[str, Any] = field(default_factory=dict)
    action_id: int = field(default_factory=next_action_id)
    t_decide: float = field(default_factory=time.time)
    deadline_ms: int = 250
    cls: str = "best_effort"
    idempotent: bool = True
    occupies_screen: bool = False
    requires_screen: bool = True
    on_fail: str = "abort_task"

    def __post_init__(self) -> None:
        if self.type not in ACTION_TYPES:
            raise ValueError(f"非法动作类型: {self.type}")
        if self.cls not in VALID_CLASSES:
            raise ValueError(f"非法动作类别: {self.cls}")
        if self.on_fail not in VALID_ON_FAIL:
            raise ValueError(f"非法 on_fail: {self.on_fail}")
        if int(self.deadline_ms) <= 0:
            raise ValueError("deadline_ms 必须为正")

    @property
    def critical(self) -> bool:
        return self.cls == "critical"

    def is_expired(self, now: float | None = None) -> bool:
        now = time.time() if now is None else float(now)
        return (now - float(self.t_decide)) * 1000.0 >= float(self.deadline_ms)

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "params": dict(self.params),
            "action_id": int(self.action_id),
            "t_decide": float(self.t_decide),
            "deadline_ms": int(self.deadline_ms),
            "cls": self.cls,
            "idempotent": bool(self.idempotent),
            "occupies_screen": bool(self.occupies_screen),
            "requires_screen": bool(self.requires_screen),
            "on_fail": self.on_fail,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Action":
        return cls(
            type=str(payload["type"]),
            params=dict(payload.get("params") or {}),
            action_id=int(payload.get("action_id", next_action_id())),
            t_decide=float(payload.get("t_decide", time.time())),
            deadline_ms=int(payload.get("deadline_ms", 250)),
            cls=str(payload.get("cls", "best_effort")),
            idempotent=bool(payload.get("idempotent", True)),
            occupies_screen=bool(payload.get("occupies_screen", False)),
            requires_screen=bool(payload.get("requires_screen", True)),
            on_fail=str(payload.get("on_fail", "abort_task")),
        )

    @classmethod
    def for_type(
        cls,
        action_type: str,
        params: dict[str, Any] | None = None,
        *,
        deadline_ms: int | None = None,
        cls_name: str | None = None,
        on_fail: str | None = None,
    ) -> "Action":
        """按动作类型填默认原子性/占屏/deadline，避免调用方各写一份。"""
        if action_type in {"map_move", "move"}:
            defaults = dict(idempotent=True, occupies_screen=False, requires_screen=True, deadline_ms=500)
        elif action_type in {"skill", "attack", "summoner"}:
            defaults = dict(idempotent=False, occupies_screen=False, requires_screen=True, deadline_ms=200)
        elif action_type in {"recall", "restore"}:
            defaults = dict(idempotent=False, occupies_screen=True, requires_screen=True, deadline_ms=1000)
        elif action_type in {"buy", "panel"}:
            defaults = dict(idempotent=False, occupies_screen=True, requires_screen=False, deadline_ms=1500)
        elif action_type == "combo":
            defaults = dict(idempotent=False, occupies_screen=False, requires_screen=True, deadline_ms=1500)
        else:
            raise ValueError(f"非法动作类型: {action_type}")
        if deadline_ms is not None:
            defaults["deadline_ms"] = deadline_ms
        return cls(
            type=action_type,
            params=dict(params or {}),
            cls=cls_name or "best_effort",
            on_fail=on_fail or "abort_task",
            **defaults,
        )
