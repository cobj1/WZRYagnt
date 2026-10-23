# -*- coding: utf-8 -*-
"""官方 AI 的复刻实现（架构级）。

边界（必读）：
  官方**模型权重**与**战术配置**不随 APK 下发（见 `docs/wzry-ai-data-inputs.md` §9），
  所以本包复刻的是：数据模式、特征口径、动作空间、决策顺序、节拍/难度参数。
  唯一被替换的环节是"模型前向"——由 `policy.Scorer` 顶替，接口保持一致。

模块分工（与官方一一对应）：
  schema.py    特征键名与归一化口径（官方 feature_name_list / normType）
  snapshot.py  state_dict -> 按类别分组的单位集合（官方 BattleLogicUnit 视图）
  geo.py       世界坐标 <-> 小地图归一化的唯一口径（z 轴反序）
  organ_slots.py 22 个具名建筑槽位（位置拓扑，不依赖 cid）
  features.py  抽样 / 视野裁剪 / 拼定长向量（官方 feature::Vec*::Process）
  actions.py   双手动作：目标手 × 技能手（官方 AtkXxxSkillN 标签）
  field.py     走位价值场（官方 grid action 打分的替身）
  rules.py     规则覆盖层（官方 RuleTactics，命中即短路）
  policy.py    主管线（FeatureProcess -> 打分 -> top-k -> 规则 -> 回退）
  config.py    难度参数（官方 model_id/predict_freq/delay_frame/god_vision）
"""
from . import (actions, bridge, config, features, field, geo, organ_slots,  # noqa: F401
               policy, rules, schema, snapshot)
from .bridge import decide_legacy, to_legacy_action  # noqa: F401
from .policy import Decision, LinearScorer, OfficialAI, softmax  # noqa: F401
from .snapshot import Snapshot, Unit, from_state_dict  # noqa: F401

__all__ = [
    "OfficialAI", "Decision", "LinearScorer", "softmax",
    "Snapshot", "Unit", "from_state_dict",
    "decide_legacy", "to_legacy_action",
    "actions", "bridge", "config", "features", "field", "geo", "organ_slots",
    "policy", "rules", "schema", "snapshot",
]
