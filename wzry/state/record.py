"""状态记录协议（P0 冻结）。

设计目标：把"某个字段在什么时刻、由谁、以多大置信度观测到"显式化，
避免缺失值、估计值和实测值混在一起进入硬过滤。

三条硬规则：
1. 不可观测 != 观测为 0：缺失必须是 ``value=None, source="unavailable"``。
2. 硬过滤只消费 ``conf >= min_conf`` 且 ``now - t_obs < max_age_s`` 的记录；
   ``source == "estimate"`` 只允许做软打分。
3. 派生量必须自认来源：冷却/位置/敌方技能等一律 ``source="estimate"``；
   其中敌方技能这类没有像素来源的量永远不能进硬过滤。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

VALID_SOURCES = {
    "detector",
    "ocr",
    "minimap",
    "panel",
    "memory",
    "estimate",
    "unavailable",
}
VALID_STALENESS = {"expire", "decay", "persist"}


@dataclass(frozen=True)
class StateRecord:
    key: str                       # "self.hp" / "enemy_hero.0.pos" / "skill.2.cd"
    value: Any                     # None = 不可观测
    agent_id: str = "self"         # 预留多英雄
    t_obs: float = 0.0             # 观测时刻（不是写入时刻）
    source: str = "unavailable"    # detector|ocr|minimap|panel|memory|estimate|unavailable
    conf: float = 0.0              # 0..1
    staleness: str = "expire"      # expire|decay|persist

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("StateRecord.key 不能为空")
        if self.source not in VALID_SOURCES:
            raise ValueError(f"非法 source: {self.source}")
        if self.staleness not in VALID_STALENESS:
            raise ValueError(f"非法 staleness: {self.staleness}")
        if not 0.0 <= float(self.conf) <= 1.0:
            raise ValueError(f"conf 必须在 0..1: {self.conf}")
        if self.value is None and self.source != "unavailable":
            raise ValueError("value=None 时 source 必须是 unavailable")
        if self.source == "unavailable" and self.value is not None:
            raise ValueError("source=unavailable 时 value 必须为 None")
        if self.t_obs < 0:
            raise ValueError("t_obs 不能为负")

    @property
    def observed(self) -> bool:
        return self.value is not None and self.source != "unavailable"

    def age(self, now: float) -> float:
        return max(0.0, float(now) - float(self.t_obs))

    def fresh(self, now: float, max_age_s: float) -> bool:
        return self.observed and self.age(now) < max_age_s

    def hard_filterable(self, now: float, *, max_age_s: float, min_conf: float) -> bool:
        """硬过滤唯一入口：新鲜 + 置信度达标 + 非 estimate。"""
        return self.fresh(now, max_age_s) and self.conf >= min_conf and self.source != "estimate"

    @classmethod
    def unavailable(cls, key: str, *, agent_id: str = "self", t_obs: float = 0.0) -> "StateRecord":
        return cls(key=key, value=None, agent_id=agent_id, t_obs=t_obs, source="unavailable", conf=0.0)

    @classmethod
    def observed_value(
        cls,
        key: str,
        value: Any,
        *,
        agent_id: str = "self",
        t_obs: float,
        source: str,
        conf: float,
        staleness: str = "expire",
    ) -> "StateRecord":
        return cls(
            key=key,
            value=value,
            agent_id=agent_id,
            t_obs=t_obs,
            source=source,
            conf=conf,
            staleness=staleness,
        )

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "value": self.value,
            "agent_id": self.agent_id,
            "t_obs": self.t_obs,
            "source": self.source,
            "conf": self.conf,
            "staleness": self.staleness,
        }
