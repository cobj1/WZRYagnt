# -*- coding: utf-8 -*-
"""官方人机 AI 的战术常量与判定谓词（从 libGameCore.so 反汇编还原）。

来源：base.apk 内 lib/arm64-v8a/libGameCore.so（.symtab 已剥离，仅剩 2498 个动态符号）。
方法：reverse/xref_strings.py 用 ADRP+ADD 交叉引用把日志串定位到代码地址；
      reverse/func_dump.py 做函数级反汇编并注解字符串/调用；
      reverse/dump_reloc_ptrs.py 从 .rela.dyn 还原运行时函数指针表。
已定位的关键规则函数：
    RuleTactics::GotoTargetPosWhenFirstBorn   0x027dd40c
    RuleTactics::CheckReturnCity              0x027df394
    RuleTactics::IsInDangerousPlace           0x027db2ac
    RuleTactics::StayWithMonster              0x027e5438
    AITargetSearcher::GetNearestEnemy(真身)    0x034de91c

换算说明：官方内部距离单位与客户端小地图归一化坐标之间没有公开换算系数，
    MAP_UNITS 取"地图宽 1000 单位"作为工程近似，可由 OFFICIAL_SCALE 覆盖，
    实测校准后再固化。
"""
import os

# 官方比较立即数还原值（括号内为汇编证据）
FRAMES_PER_SEC = 15.0            # 451 帧 ≈ 30s 反推（0x1c3 = 451）
FIRST_BORN_TIMEOUT_FRAMES = 451  # 0x27dd40c: cmp w8, #0x1c3 -> "after 30s, stop"
HERO_ABORT_DIST = 301            # 0x12d: "enemy hero around, stop"
DANGER_DIST = 301                # 0x12d: IsInDangerousPlace / CheckReturnCity 共用
DANGER_DIST_FAR = 527            # 0x20f: CheckReturnCity 远距分支
RETURN_CITY_FRAME_GATE = 70      # 0x46: CheckReturnCity 帧门限
RETURN_CITY_HP_RATE = 0.60       # 占位：官方血量分支由配置表驱动，非立即数。
                                 #   这里保持与既有 optimizer 的 hp<0.6 一致，
                                 #   使本次改造不改变生存行为；实测校准后再调
RETURN_CITY_EP_RATE = None       # 占位：官方为 cur_ep/max_ep 比例，阈值未知；
                                 #   为 None 时该分支不触发
MOVE_ACTION_ID = 2               # 0x27dd40c: cmp w8, #2 -> "not move action, stop"

MAP_UNITS = 1000.0               # 工程近似：地图宽 1000 单位
OFFICIAL_SCALE = float(os.environ.get("WZRY_OFFICIAL_SCALE", "1.0"))

# CheckReturnCity 的 7 个具名分支（日志标签原文）
RETURN_CITY_FORCE_HP = "FORCE_RETURN_CITY_HP"
RETURN_CITY_FORCE_EP = "FORCE_RETURN_CITY_EP"
RETURN_CITY_GANK_TIME = "GANK_TIME_NOT_RETURN_CITY"
RETURN_CITY_REVIVE_DIS = "REVIVE_DIS_NOT_RETURN_CITY"
RETURN_CITY_STRATEGY = "STRATEGY_RETURN_CITY"
RETURN_CITY_FUHUOJIA = "not return, fuhuojia usable"
RETURN_CITY_NONE = "NOT_RETURN_CITY"


def norm(units: float) -> float:
    """官方距离单位 -> 小地图归一化距离。"""
    return float(units) * OFFICIAL_SCALE / MAP_UNITS


def return_city_decision(hp, ep=None, revive_armor_usable=None,
                         is_gank_time=None, strategy_return=False):
    """对应 RuleTactics::CheckReturnCity(0x27df394)。

    分支名取自其日志标签。缺失字段（ep / 复活甲 / gank 时段）按"不触发该分支"
    处理——当前 GameState 未提供这些数据，接口先留好，接上数据源后即可生效。

    返回 (是否回城, 分支名)。
    """
    if ep is not None and RETURN_CITY_EP_RATE is not None and ep <= RETURN_CITY_EP_RATE:
        return True, RETURN_CITY_FORCE_EP
    if hp is not None and hp <= RETURN_CITY_HP_RATE:
        return True, RETURN_CITY_FORCE_HP
    if revive_armor_usable:
        return False, RETURN_CITY_FUHUOJIA
    if is_gank_time:
        return False, RETURN_CITY_GANK_TIME
    if strategy_return:
        return True, RETURN_CITY_STRATEGY
    return False, RETURN_CITY_NONE


def first_born_gate(elapsed_s, enemy_dist_norm, action_is_move=True,
                    has_target=True):
    """对应 RuleTactics::GotoTargetPosWhenFirstBorn(0x27dd40c)。

    官方判定顺序：
      1) 帧号 >= 451（约 30s）        -> 中止（"after 30s, stop"）
      2) 无目标点                      -> 中止（"no target pos, stop"）
      3) 当前动作 != 移动              -> 中止（"not move action, stop"）
      4) 最近敌距 < 301 单位            -> 中止（"enemy hero around, stop"）

    返回 (是否允许继续前往, 中止原因)。
    """
    if elapsed_s is not None and elapsed_s >= FIRST_BORN_TIMEOUT_FRAMES / FRAMES_PER_SEC:
        return False, "after_30s"
    if not has_target:
        return False, "no_target_pos"
    if not action_is_move:
        return False, "not_move_action"
    if enemy_dist_norm is not None and enemy_dist_norm < norm(HERO_ABORT_DIST):
        return False, "enemy_hero_around"
    return True, ""


def is_in_dangerous_place(nearest_enemy_dist_norm, nearest_soldier_dist_norm=None):
    """对应 RuleTactics::IsInDangerousPlace(0x027db2ac)。

    官方还参考 dangerous_tower_runtime_id / main_hero_atk_range / max_soldier_dist
    / model.y_action；这里只落地当前状态可得的距离项，其余留待接入。
    """
    if nearest_enemy_dist_norm is not None and nearest_enemy_dist_norm < norm(DANGER_DIST):
        return True, "danger_radius"
    if (nearest_soldier_dist_norm is not None
            and nearest_soldier_dist_norm < norm(DANGER_DIST)):
        return True, "danger_soldier_radius"
    return False, "safe"
