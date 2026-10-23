# -*- coding: utf-8 -*-
"""GameState 统一状态契约（v1.1）。

内存 / 视觉 / 融合三种来源共用同一份结构：迁移时只换 StateSource，不换下游。

坐标口径（三者分开存，禁止复用同一字段）：
    screen      [cx, cy, w, h]  屏幕归一化像素，原点左上
    pos_world   [x, z]          世界坐标（量级约 ±60000）
    pos_minimap [u, v]          小地图归一化坐标，原点左上

血量口径：
    Unit.hp        0..1 比例（兼容 v0；视觉血条天然是比例）
    Unit.hp_abs    绝对当前血量；hp_max 为绝对值
    SelfState       hp/mp 及上限均为绝对值，另提供 hp_rate/mp_rate

来源与可见性：
    src         memory / vision / inferred / unknown
    visible     True=视觉可见；False=明确不可见；None=未知（内存全图单位）
    conf        视觉置信度；内存通常 1.0
    last_seen_t 最后一次看到该单位的时间（秒）
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "1.1"


def _rate(cur: Optional[float], mx: Optional[float]) -> Optional[float]:
    if cur is None or not mx:
        return None
    return max(0.0, min(1.0, float(cur) / float(mx)))


@dataclass
class Unit:
    """单个单位（英雄/小兵/建筑/野怪/血包）。"""
    # --- v0 兼容字段（顺序保持不变）---
    id: Optional[int] = None              # 跟踪 ID（视觉）；内存侧无跟踪时为 None
    cls: str = ""                         # enemy_hero / ally_hero / enemy_minion / ...
    camp: Optional[str] = None            # blue / red
    screen: Optional[List[float]] = None  # [cx, cy, w, h] 屏幕归一化
    hp: Optional[float] = None            # 0..1 比例（见模块说明）
    dead: bool = False
    conf: Optional[float] = None
    camp_src: Optional[str] = None

    # --- v1.1 统一字段 ---
    uid: Optional[str] = None             # 稳定唯一键，如 "blue:hero:106"
    kind: str = ""                        # hero / minion / organ / monster / cake
    hero_cid: Optional[int] = None        # 英雄配置 ID（内存精确；视觉识别时填）
    visible: Optional[bool] = None        # None=未知（内存全图单位）
    pos_world: Optional[List[float]] = None    # [x, z]
    pos_minimap: Optional[List[float]] = None  # [u, v]
    coord_src: Optional[str] = None       # memory / screen / inferred
    hp_abs: Optional[float] = None
    hp_max: Optional[float] = None
    level: Optional[int] = None
    skills: List[Dict[str, Any]] = field(default_factory=list)
    equip: List[int] = field(default_factory=list)
    respawn_s: Optional[float] = None
    src: str = "unknown"                  # memory / vision / inferred
    last_seen_t: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def t_last_seen(self) -> Optional[float]:
        """Runtime 协议别名：最后一次观测时刻。"""
        return self.last_seen_t

    @t_last_seen.setter
    def t_last_seen(self, value: Optional[float]) -> None:
        self.last_seen_t = value

    @property
    def hp_rate(self) -> Optional[float]:
        if self.hp is not None:
            return max(0.0, min(1.0, float(self.hp)))
        return _rate(self.hp_abs, self.hp_max)

    @property
    def is_hero(self) -> bool:
        return self.kind == "hero" or self.cls.endswith("hero")


@dataclass
class HeroStats:
    """计分板口径的英雄全量数据（10 人，自己也在内）。"""
    cid: int
    camp: Optional[str] = None
    gold: Optional[int] = None            # 可用金币（HUD 口径）
    gold_total: Optional[int] = None      # 总经济
    gold_spent: Optional[int] = None
    level: Optional[int] = None
    exp: Optional[int] = None
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    hp: Optional[float] = None            # 绝对值
    hp_max: Optional[float] = None
    equip: List[int] = field(default_factory=list)
    respawn_s: Optional[float] = None
    dead: bool = False
    x: Optional[float] = None
    z: Optional[float] = None
    src: str = "unknown"
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def kda(self) -> List[int]:
        return [self.kills, self.deaths, self.assists]

    @property
    def hp_rate(self) -> Optional[float]:
        return _rate(self.hp, self.hp_max)


@dataclass
class SelfState:
    """本地英雄的精确状态（内存/HUD 都往这里填）。"""
    cid: Optional[int] = None
    camp: Optional[str] = None
    hp: Optional[float] = None            # 绝对值
    hp_max: Optional[float] = None
    mp: Optional[float] = None            # 绝对值
    mp_max: Optional[float] = None
    gold: Optional[int] = None
    gold_total: Optional[int] = None
    gold_spent: Optional[int] = None
    level: Optional[int] = None
    exp: Optional[int] = None
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    atk_range: Optional[float] = None
    skills: List[Dict[str, Any]] = field(default_factory=list)
    summoner: Optional[Dict[str, Any]] = None
    heal: Optional[Dict[str, Any]] = None
    recall: Optional[Dict[str, Any]] = None
    equip: List[int] = field(default_factory=list)
    respawn_s: Optional[float] = None
    dead: bool = False
    x: Optional[float] = None
    z: Optional[float] = None
    visible: Optional[bool] = None
    src: str = "unknown"
    ts: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def hp_rate(self) -> Optional[float]:
        return _rate(self.hp, self.hp_max)

    @property
    def mp_rate(self) -> Optional[float]:
        return _rate(self.mp, self.mp_max)

    @property
    def kda(self) -> List[int]:
        return [self.kills, self.deaths, self.assists]


@dataclass
class GameState:
    """一帧完整游戏状态。"""
    # --- v0 兼容字段（顺序保持不变）---
    t: float = 0.0
    phase: str = "unknown"
    frame_id: int = 0
    source: str = "live"                  # live / replay / memory
    screen_size: List[int] = field(default_factory=lambda: [0, 0])
    units: List[Unit] = field(default_factory=list)
    minimap: Dict[str, Any] = field(default_factory=dict)
    ui: Dict[str, Any] = field(default_factory=dict)   # 旧口径，保留兼容；新代码读 self_state/scoreboard
    last_action: Optional[Dict[str, Any]] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    # --- v1.1 统一字段 ---
    schema_version: str = SCHEMA_VERSION
    game_version: Optional[str] = None
    t_capture: Optional[float] = None     # 截图/采集时刻
    t_state: Optional[float] = None       # 内存/状态读取时刻
    clock_s: Optional[float] = None       # 局内时钟（秒）
    sources: Dict[str, str] = field(default_factory=dict)  # 字段组 -> memory/vision/inferred
    self_state: Optional[SelfState] = None
    scoreboard: List[HeroStats] = field(default_factory=list)

    @property
    def heroes(self) -> List[Unit]:
        return [u for u in self.units if u.is_hero]

    @property
    def visible_units(self) -> List[Unit]:
        return [u for u in self.units if u.visible is True]

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        import json
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)
