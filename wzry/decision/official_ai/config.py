# -*- coding: utf-8 -*-
"""AI 难度参数：对齐官方 `AILevel` 的四元组。

官方日志原文（`[ChangeAiLevelDynamic]`）：
    ... model_id: %d, predict_freq: %d, delay_frame: %d, delay_buy_time: %d, god_vision: %d

也就是说"简单/一般/困难/绝悟"不是换一套逻辑，而是**同一套逻辑配不同参数**：
预测频率、观测延迟、买装备延迟、上帝视野时长、可见半径。
本模块把这组旋钮显式化，便于复现官方难度曲线，也便于我们做 A/B。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict


@dataclass(frozen=True)
class AIParams:
    name: str = "normal"
    model_id: str = "default"        # 官方按 hero×ai_level×match_type 选模型；我们只有打分器
    predict_freq: float = 8.0        # 每秒决策次数（官方 predict_freq）
    delay_frames: int = 0            # 观测延迟帧（官方 delay_frame）
    delay_buy_s: float = 0.0         # 买装备延迟（官方 delay_buy_time）
    god_vision_s: float = 0.0        # 上帝视野时长（官方 god_vision）
    view_distance: float = 1600.0    # 可见半径（官方 view_distance）
    feature_interval: int = 1        # 每 N 帧抽一次特征（官方 feature_extract_interval）
    temperature: float = 1.0         # 打分 -> 概率的 softmax 温度
    top_k: int = 3                   # 官方日志里的 rank / tactics_prob_size
    # 场景化预测间隔（秒）。对齐官方 `predict_interval_*` 一族配置键
    #   （`docs/wzry-ai-data-inputs.md` §3.1）：不同场景用不同预测频率，
    #   所以官方 AI 的反应"有节奏"。数值是本项目工程标定，非官方常量。
    scene_interval: Dict[str, float] = field(default_factory=lambda: dict(SCENE_INTERVAL))


SCENE_INTERVAL: Dict[str, float] = {
    "default": 0.0,        # 0 = 用 1/predict_freq
    "return_city": 0.25,   # predict_interval_return_city
    "dy": 0.10,            # predict_interval_dy（交战/团战）
    "atk_dragon": 0.20,    # predict_interval_atk_dragon
    "atk_monster": 0.18,   # predict_interval_atk_monster_alone
    "atk_soldier": 0.18,   # predict_interval_atk_soldier_alone
    "stuck_wall": 0.33,    # predict_interval_stuck_wall（卡墙时降频，避免抖动）
}


PRESETS: Dict[str, AIParams] = {
    # 越简单的 AI：决策越慢、看得越近、延迟越大
    "easy": AIParams("easy", predict_freq=3.0, delay_frames=6, delay_buy_s=1.5,
                     god_vision_s=0.0, view_distance=900.0, feature_interval=3,
                     temperature=1.6),
    "normal": AIParams("normal", predict_freq=6.0, delay_frames=3, delay_buy_s=0.8,
                       god_vision_s=0.0, view_distance=1300.0, feature_interval=2),
    "hard": AIParams("hard", predict_freq=8.0, delay_frames=1, delay_buy_s=0.3,
                     god_vision_s=2.0, view_distance=1600.0, feature_interval=1),
    "juewu": AIParams("juewu", predict_freq=12.0, delay_frames=0, delay_buy_s=0.0,
                      god_vision_s=8.0, view_distance=2200.0, feature_interval=1,
                      temperature=0.8),
}


def get(name: str = "normal") -> AIParams:
    return PRESETS.get((name or "normal").lower(), PRESETS["normal"])


def with_overrides(name: str = "normal", **kw) -> AIParams:
    return replace(get(name), **kw)
