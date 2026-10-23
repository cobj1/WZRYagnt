# -*- coding: utf-8 -*-
"""战术配置 schema：复刻官方 `tactics_config_file` 的字段结构。

官方证据（`docs/wzry-ai-data-inputs.md` §4.7 / §3.6）：
  - 加载路径 `AILab/ai_config/5v5/tactics/tactics_config_file.txt`（**不随包**）；
  - 日志 `[CurrentTacticsCheck] tactics_size:%d select_tactics:%d rank:%d
          grid_tactics_state:%d arrived_strategy_target:%d enemy_hero_min_dis:%d`
  - 日志 `[FinalSelectTactics] rank:%d grid_tactics_state:%d arrived_strategy_target:%d
          enemy_hero_min_dis:%d`
  - 选战术时 `[SelectTactics] max prob:%f, min prob score:%f, select:%d`

由此可确定：每条战术（tactics）至少有
    **名字 / 触发条件 / 打分维度 / 优先级 rank / 概率阈值** 五类字段。
我们拿不到具体表，但可以把 **schema 固化**，并允许用户用 JSON 覆盖
（`configs/tactics.json`），这样一旦拿到官方表就能直接灌进来。

字段类型与取值范围的推断依据：
  - `grid_tactics_state`：日志里是 %d，且与"网格战术状态"对应 → 枚举 0..N；
  - `arrived_strategy_target`：%d → 布尔；
  - `enemy_hero_min_dis`：%d，与 301 危险半径同量级 → 距离（世界单位）；
  - `rank`：%d，越小越优先；
  - `max prob / min prob score`：%f → 概率/分数，0..1 与实数两套。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[3]
CFG = ROOT / "configs" / "tactics.json"

# 官方日志里出现过的打分维度（字段名逐字对齐）
DIMENSIONS: Dict[str, dict] = {
    "grid_tactics_state": {"type": "int", "range": (0, 8), "desc": "网格战术状态"},
    "arrived_strategy_target": {"type": "bool", "desc": "是否已到战略目标点"},
    "enemy_hero_min_dis": {"type": "dist", "range": (0, 60000), "desc": "最近敌英雄距离"},
    "rank": {"type": "int", "range": (0, 64), "desc": "战术优先级（越小越优先）"},
    "max_prob": {"type": "float", "range": (0.0, 1.0), "desc": "模型概率上界"},
    "min_prob_score": {"type": "float", "desc": "概率分数下界"},
    "in_organ_attacked_flag": {"type": "bool", "desc": "我方建筑被攻击"},
    "organ_conti_attack_val": {"type": "int", "range": (0, 20), "desc": "建筑连击值"},
    "dist_organ": {"type": "dist", "desc": "到目标建筑距离"},
    "organ_hp_rate": {"type": "float", "range": (0.0, 1.0)},
    "hero_hp_rate": {"type": "float", "range": (0.0, 1.0)},
    "m_in_dangerous_place": {"type": "bool", "desc": "处于危险区（半径 301）"},
    "m_ally_crystal_is_atked": {"type": "bool", "desc": "我方水晶被攻击"},
}


@dataclass
class TacticsEntry:
    """一条战术。字段名对齐官方日志里的维度。"""
    name: str
    rank: int = 0
    enabled: bool = True
    dims: Dict[str, float] = field(default_factory=dict)   # 维度 -> 权重/阈值
    min_prob: float = 0.0


def load() -> List[TacticsEntry]:
    """读 `configs/tactics.json`（若存在）。结构见 `dump_schema()`。"""
    if not CFG.exists():
        return []
    raw = json.loads(CFG.read_text(encoding="utf-8"))
    out = []
    for e in raw.get("tactics") or []:
        out.append(TacticsEntry(name=str(e.get("name") or ""),
                                rank=int(e.get("rank") or 0),
                                enabled=bool(e.get("enabled", True)),
                                dims=dict(e.get("dims") or {}),
                                min_prob=float(e.get("min_prob") or 0.0)))
    return sorted(out, key=lambda x: x.rank)


def dump_schema() -> dict:
    """导出 schema（给外部工具/文档用；也作为 JSON 模板）。"""
    return {
        "version": 1,
        "note": "官方 tactics_config_file 不随包；本 schema 由日志字段反推，字段名逐字对齐",
        "dimensions": DIMENSIONS,
        "tactics": [
            {"name": "BackTower", "rank": 10, "enabled": True,
             "dims": {"m_in_dangerous_place": 1.0, "hero_hp_rate": -2.0}, "min_prob": 0.2},
            {"name": "AtkTower", "rank": 20, "enabled": True,
             "dims": {"enemy_hero_min_dis": 1.0, "arrived_strategy_target": 1.0}},
            {"name": "AtkSoldier", "rank": 30, "enabled": True,
             "dims": {"grid_tactics_state": 1.0}},
            {"name": "TacticsAttackMonster", "rank": 40, "enabled": True,
             "dims": {"enemy_hero_min_dis": 0.5}},
            {"name": "ReturnCity", "rank": 5, "enabled": True,
             "dims": {"hero_hp_rate": -3.0}},
            {"name": "NoneAction", "rank": 90, "enabled": True, "dims": {}},
        ],
    }


def score(entry: TacticsEntry, dims: Dict[str, float]) -> float:
    """线性打分：Σ(权重 × 维度值)。缺失维度按 0 处理（与官方"缺字段不参与"一致）。"""
    return sum(float(w) * float(dims.get(k, 0.0)) for k, w in entry.dims.items())


def validate(dims: Dict[str, float]) -> Tuple[bool, List[str]]:
    """按 schema 校验维度取值，返回 (是否合法, 问题列表)。"""
    bad: List[str] = []
    for k, v in dims.items():
        spec = DIMENSIONS.get(k)
        if spec is None:
            bad.append(f"unknown_dim:{k}")
            continue
        t = spec["type"]
        if t == "bool" and v not in (0, 1, True, False):
            bad.append(f"bool:{k}={v}")
        if t in ("int", "dist") and not float(v).is_integer():
            bad.append(f"int:{k}={v}")
        rng = spec.get("range")
        if rng and not (rng[0] <= float(v) <= rng[1]):
            bad.append(f"range:{k}={v}")
    return (not bad), bad
