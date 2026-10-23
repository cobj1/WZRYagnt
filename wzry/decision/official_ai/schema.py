# -*- coding: utf-8 -*-
"""特征模式：逐字对齐官方 `feature::Vec*` 的键名与归一化口径。

来源：`docs/wzry-ai-data-inputs.md` §4（键名全部取自 libGameCore.so 的 .rodata 字面量）。
官方配置里给出 `feature_name_list` / `feature_size_list` / `normType`，代码只负责按键名取值；
本模块把"键名 -> 归一化方式 -> 在向量里的下标"固化成同一套东西。

设计约束：
  - 键名**必须与官方一致**（拼错就等于换了一套特征，后续对账会失真）；
  - 每个类别的顺序即官方 `order_of_hero` / `config_id_list` 之后的字段顺序；
  - 归一化只有官方那四种：none / one_hot / min_max / min_max_one_hot。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

NORM_NONE = "none"
NORM_RATIO = "min_max"          # 官方用 min_max，这里对 [0,1] 量直接截断
NORM_ONE_HOT = "one_hot"
NORM_RATIO_ONE_HOT = "min_max_one_hot"


@dataclass(frozen=True)
class Feat:
    """单个特征维的规格。"""
    name: str
    norm: str = NORM_NONE
    lo: float = 0.0             # min_max 下界
    hi: float = 1.0             # min_max 上界
    one_hot: int = 0            # >0 时展开成 one_hot 维数

    @property
    def size(self) -> int:
        return self.one_hot if self.one_hot > 0 else 1


def _f(name: str, lo: float = 0.0, hi: float = 1.0) -> Feat:
    return Feat(name, NORM_RATIO, lo, hi)


def _i(name: str) -> Feat:
    return Feat(name, NORM_NONE)


def _b(name: str) -> Feat:
    return Feat(name, NORM_NONE)


def _oh(name: str, n: int) -> Feat:
    return Feat(name, NORM_ONE_HOT, one_hot=n)


# ---------------------------------------------------------------- 兵线
# 官方 VecSoldier 的 unit 内字段（.rodata 顺序）
VEC_SOLDIER: Tuple[Feat, ...] = (
    _f("atk", 0, 2000),
    _f("hp_rate"),
    _f("location_x", -60000, 60000),
    _f("location_z", -60000, 60000),
    _f("kill_income", 0, 500),
    _oh("soldier_type", 8),
    _b("in_main_hero_sight"),
    _b("belong_to_main_camp"),
    _f("soldier_sin", -1, 1),
    _f("soldier_cos", -1, 1),
    _f("max_hp", 0, 20000),
    _b("is_soldier_alive"),
    _f("unit_buff_num", 0, 8),
    _f("soldier_buff_marks", 0, 8),
)

# ---------------------------------------------------------------- 建筑
# 官方 VecOrgan：30 维 unit 内字段（22 个具名槽位另行索引）
VEC_ORGAN: Tuple[Feat, ...] = (
    _f("location_x", -60000, 60000),
    _f("location_z", -60000, 60000),
    _f("hp_rate"),
    _f("max_hp", 0, 20000),
    _f("kill_income", 0, 500),
    _oh("type", 8),
    _f("conti_attack", 0, 20),
    _i("attack_target"),
    _f("attack_range", 0, 2000),
    _f("atk", 0, 2000),
    _b("is_alive"),
    _f("cake_need_scds", 0, 120),
    _b("belong_to_main_camp"),
    _b("in_main_hero_view"),
    _b("whether_under_each_hero_atk"),
    _b("whether_organ_atk_each_hero"),
    _f("distance_from_each_hero", 0, 60000),
    _f("relative_location_x", -60000, 60000),
    _f("relative_location_z", -60000, 60000),
    _b("organ_invincible_state"),
    _f("organ_invincible_state_time", 0, 60),
    _b("organ_incompetent_state"),
    _f("organ_incompetent_state_time", 0, 60),
    _f("nearby_emy_soldier_num", 0, 12),
    _b("whether_under_frd_hero_atk"),
    _b("whether_under_emy_hero_atk"),
    _f("organ_emy_soldier_min_dist", 0, 60000),
    _b("is_emy_soldier_around_organ"),
    _f("organ_emy_soldier_cnt", 0, 12),
    _b("is_organ_attack_emy_soldier"),
    _b("is_organ_attack_main_hero"),
    _b("is_organ_now_outside"),
)

# 24 个具名建筑槽位（官方按语义索引，不遍历识别）—— 顺序即官方 .rodata 顺序
ORGAN_SLOTS: Tuple[str, ...] = (
    "friend_crystal", "enemy_crystal",
    "friend_spring_tower", "enemy_spring_tower",
    "friend_mid_high_tower", "enemy_mid_high_tower",
    "friend_top_high_tower", "enemy_top_high_tower",
    "friend_bottom_high_tower", "enemy_bottom_high_tower",
    "friend_mid_norm_tower_1st", "enemy_mid_norm_tower_1st",
    "friend_top_norm_tower_1st", "enemy_top_norm_tower_1st",
    "friend_bottom_norm_tower_1st", "enemy_bottom_norm_tower_1st",
    "friend_mid_norm_tower_2nd", "enemy_mid_norm_tower_2nd",
    "friend_top_norm_tower_2nd", "enemy_top_norm_tower_2nd",
    "friend_bottom_norm_tower_2nd", "enemy_bottom_norm_tower_2nd",
)

# ---------------------------------------------------------------- 英雄
# 官方 10 英雄特征（all_10_hero_feature）
VEC_HERO: Tuple[Feat, ...] = (
    _f("dist_from_each_hero", 0, 60000),
    _b("under_atk_by_each_hero"),
    _b("whether_atk_each_main_camp_hero"),
    _f("relative_location_x", -60000, 60000),
    _f("relative_location_z", -60000, 60000),
    _f("x_diff", -60000, 60000),
    _f("z_diff", -60000, 60000),
    _f("buff_marks", 0, 8),
    _b("is_in_hero_atk_range"),
    _b("is_kill_by_hero_norm_atk"),
    _f("atk_range", 0, 2000),
    _f("phy_def", 0, 3000),
    _b("is_atk_main_hero"),
    _b("past_1s_attack_soldier"),
    _f("past_1s_attack_soldier_hprate"),
    _b("past_1s_attack_soldier_lowhp"),
    _b("has_spec_buffs"),
)

# ---------------------------------------------------------------- 野怪 / 血包
VEC_MONSTER: Tuple[Feat, ...] = (
    _f("hp_rate"),
    _f("location_x", -60000, 60000),
    _f("location_z", -60000, 60000),
    _f("max_hp", 0, 40000),
    _f("distance_from_main_hero", 0, 60000),
    _b("in_main_hero_view"),
    _b("is_alive"),
    _oh("monster_type", 8),
    _f("monster_buff_marks", 0, 8),
)

VEC_CAKE: Tuple[Feat, ...] = (
    _f("location_x", -60000, 60000),
    _f("location_z", -60000, 60000),
    _f("dist_2_ckae_map", 0, 60000),      # 官方原串就是这个拼写（ckae）
    _oh("cake_type", 4),
    _b("is_visiable"),                    # 官方原串拼写
    _f("cake_need_scds", 0, 120),
)

# ---------------------------------------------------------------- 自身
# 对齐官方 AiProcess::PrintHeroInfo 打出的字段
VEC_SELF: Tuple[Feat, ...] = (
    _f("hp"),
    _f("hp_rate"),
    _f("ep"),
    _f("ep_rate"),
    _f("atk_range", 0, 2000),
    _f("level", 1, 15),
    _f("gold", 0, 30000),
    _f("skill1_cd"),
    _f("skill2_cd"),
    _f("skill3_cd"),
    _f("summoner_cd"),
    _f("heal_cd"),
    _b("skill1_ready"),
    _b("skill2_ready"),
    _b("skill3_ready"),
    _f("move_degree", 0, 360),
    _f("mov_spd", 0, 2000),
    _f("forward_degree", 0, 360),
)

# ---------------------------------------------------------------- 全局态势
# 官方 [CurrentTacticsCheck] / [FinalSelectTactics] 打印的判据 + 经济/时间
VEC_GLOBAL: Tuple[Feat, ...] = (
    _f("grid_tactics_state", 0, 8),
    _b("arrived_strategy_target"),
    _f("enemy_hero_min_dis", 0, 60000),
    _b("m_in_dangerous_place"),
    _b("m_ally_crystal_is_atked"),
    _f("clock_s", 0, 1800),
    _f("ally_gold", 0, 200000),
    _f("enemy_gold", 0, 200000),
    _f("kill_diff", -30, 30),
)


@dataclass
class VecSpec:
    """一个类别的完整规格：单位内字段 + 最大单位数。"""
    key: str                      # 官方配置键，如 vec_soldier_config
    name: str                     # 官方类名，如 VecSoldier
    feats: Tuple[Feat, ...]
    max_units: int                # 官方 units_num_list 的上限（按距离取前 N）
    sorted_by_dist: bool = True

    @property
    def unit_size(self) -> int:
        return sum(f.size for f in self.feats)

    @property
    def total_size(self) -> int:
        return self.unit_size * self.max_units


SPECS: Dict[str, VecSpec] = {
    "soldier": VecSpec("vec_soldier_config", "VecSoldier", VEC_SOLDIER, 8),
    "organ": VecSpec("vec_organ_config", "VecOrgan", VEC_ORGAN, 24),
    "monster": VecSpec("vec_monster_config", "VecMonster", VEC_MONSTER, 4),
    "cake": VecSpec("vec_cake_config", "VecCake", VEC_CAKE, 2),
    "hero": VecSpec("vec_hero_config", "VecFeatureHero", VEC_HERO, 10),
}


def layout(spec: VecSpec) -> List[Tuple[str, int, int]]:
    """返回 [(name, start_idx, size)]，等价官方日志 `key:%d start_idx %d feature_size:%d`。"""
    out, k = [], 0
    for f in spec.feats:
        out.append((f.name, k, f.size))
        k += f.size
    return out


def dims() -> Dict[str, int]:
    """各类别的向量总维数（自检/日志用）。"""
    d = {k: s.total_size for k, s in SPECS.items()}
    d["self"] = sum(f.size for f in VEC_SELF)
    d["global"] = sum(f.size for f in VEC_GLOBAL)
    return d
