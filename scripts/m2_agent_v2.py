# -*- coding: utf-8 -*-
"""M2 Agent v2：M1 感知管线 + M2 动作执行器 -> 会操作的规则 Agent（钟馗 v0）。

架构（~10Hz 感知循环）：
    scrcpy 流（30fps）→ MatchStateMachine（对局状态机）
      → 确认制（in_match + 小地图连续 2 帧确认）
      → 感知（YOLO 11 类检测 + MinimapTracker）
      → decide() 规则决策（纯函数，无 IO，便于测试/换策略网络）
      → ActionExecutor 执行（--action 才真正触摸，默认 --no-action 只观察）

规则优先级（v0 简单但安全，见 decide()）：
  1. 钩子可命中（检测到 hook_aim 或 敌方英雄在 2 技能距离内）
     → skill_cast(2, 'tap')，冷却节流 > 3s；
  2. 敌人近（enemy_hero 中心距屏幕中心 < 0.25 屏宽）
     → skill_cast(1, 'tap')，节流 1.5s；
  3. 敌人贴身（< 0.20 屏宽）且技能不可用 → 普攻（攻击键），节流 1s；
  4. 敌人远 → move(朝向最近敌人, r=0.8, 400ms)；
  5. 无敌人 → 朝兵线方向移动（小地图红点质心方向）；无红点则 idle。
  防抖：任何技能释放后 50ms 内不再下发移动。
  塔：被塔攻击（塔中心 < 0.40 屏宽且无我方小兵）→ 立即反向逃离；

用法：
    venv\\Scripts\\python.exe scripts\\m2_agent_v2.py --seconds 120              # 只观察（默认，安全开关）
    venv\\Scripts\\python.exe scripts\\m2_agent_v2.py --seconds 120 --action     # 真机执行
    venv\\Scripts\\python.exe scripts\\m2_agent_v2.py --seconds 120 --action --save   # 执行 + 对局会话采集
"""
import argparse
import json
import math
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# v17.0 官方架构复刻版决策器（--ai-arch official 时惰性创建，跨帧复用）
_OFFICIAL_STATE = {"ai": None, "n": 0, "err": None}

# ---------------------------------------------------------------------------
# decision params
SKILL1_THROTTLE_S = 1.5
SUMMONER_THROTTLE_S = 30.0
SKILL3_ACTIVE_S = 3.5
SKILL_DEBOUNCE_S = 0.05
SKILL2_THROTTLE_S = 3.0      # hook throttle 3s (anti-AFK)
SKILL2_RANGE_FRAC = 0.45
SKILL1_RANGE_FRAC = 0.32   # v3.5 一技能(近身消耗)范围: 勾未中时只在一技能范围内用一技能
SKILL_HP_MIN = 0.60
HOOK_FLIGHT_S = 0.5           # 閽瓙椋炶鏈燂細閲婃斁鍚0.5s 鍐呭仠姝鍔紙绛夊嬀涓粨鏋滐紝闃茶嚜宸辫蛋杩戣鍒級
# throttle consts
NEAR_FRAC = 0.25
HOOK_CONFIRM_S = 1.0
HOOK_DIST_SHRINK = 0.72
MOVE_R = 0.8
MOVE_DURATION_MS = 450
LANE_DIR_BLUE = (0.75, 0.72)   # v2.91: 发育路中心(蓝方右下), 旧(0.5,0.5)=地图中心=河道! 用户: 别待河道
LANE_DIR_RED = (0.25, 0.28)    # v2.91: 发育路中心(红方左上镜像)
DANGER_FRAC = 0.45
# v14.3 战斗决策(用户批准): ①不占优不接战 ②回城必须无近敌
ENGAGE_HP_MIN = 0.60          # 接战血量下限: 低于此值不主动开团
ENGAGE_MAX_ENEMY = 2          # 屏内可信敌英数上限: >=3 个不打
# v15.4 收人头窗口(这是"不占优不接战"的**唯一例外**, 消费 ui.lock_target*):
#   锁定的那个目标已经快死(锁定目标血量 <= KILL_HP_MAX)且自己还够活着打完
#   (KILL_SELF_HP_MIN)时, 把接战血量门槛从 ENGAGE_HP_MIN 放宽到 KILL_SELF_HP_MIN。
#   两者缺一不可: 只看目标残血 = 无脑冲; 不看自己血量 = 送人头。敌英数上限照旧生效。
#   锁定目标血量由 hero_hp.lock_target() 提供(真机 19 帧验证, 误差 <=1pp)。
KILL_HP_MAX = 0.30            # 锁定目标血量 <= 它 -> 收人头窗口
KILL_SELF_HP_MIN = 0.35       # 放宽后的接战血量门槛
# v15.4 占优推进(消费 extra.enemy_hp_scene / ui.enemy_hp_min): 屏幕上**多个**敌人残血 =
#   敌方整体不占优 -> 自己血量略低也允许打。只放宽接战门槛, 不改移动目标。
PRESS_ENEMY_HP_MAX = 0.35     # 单个敌人算"残血"的线
PRESS_MIN_LOW_ENEMIES = 2     # 至少几个残血敌人才算"敌方整体不占优"
PRESS_SELF_HP_MIN = 0.50      # 占优推进时自己的血量门槛(比常规 0.60 低, 但高于收人头的 0.35)
PRESS_STALE_S = 0.6           # 该读数在 0.5s 节流块里 -> 允许沿用的最大陈旧秒数(防许可每拍闪断)
RECALL_ENEMY_CLEAR_FRAC = 0.65  # 回城门禁: 最近(可信)敌英/敌红条必须比这个更远才回城
                              #   (原 0.45 危险圈太松 -> 实测 recall_interrupted×2)
# 宸辨柟娉夋按鏂瑰悜堝皬鍦板浘鍧愭爣夛細钃濇柟宸笅 / 绾柟鍙充笂堥暅鍍忥級
FOUNTAIN_DIR_BLUE = (0.12, 0.88)
FOUNTAIN_DIR_RED = (0.88, 0.12)
# 浣庤閲忔挙閫堢敤鎴疯鍒v2.19 瀛範: 鐢埛 HP<40%+绾偣杩= 鎾 75%夆斺0% 鍗虫挙
LOW_HP_FRAC = 0.40
# ---- v16.0 P0-F1 敌塔威胁：改用**小地图分阵营塔**(towers_v7.enemy)，不再依赖 YOLO 塔类 ----
#   距离在小地图归一化空间量：自身估算位置 self_est(航位推算, 走过塔时被重锚纠偏)
#   vs 敌塔固定点。尺度参考(mm_rules_v7 固定点表实测)：相邻塔间距 0.14~0.21；
#   塔火力半径取 ~0.06（按 王者 防御塔射程 ~900 / 全图 ~14000 折算）。
#   **这三个半径是估计值、尚未真机标定**：先取保守小半径（宁缺勿滥：只有真的贴近敌塔
#   才触发），待 shadow 一局实测后再调；P1 的推进站位点必须 >= TURRET_MM_THREAT。
TURRET_MM_THREAT = 0.09       # 距敌塔 < 此值 -> 靠近敌塔，预先避开
TURRET_MM_ESCAPE = 0.06       # 距敌塔 < 此值 -> 判定在火力圈内，强撤
# ---- v16.0 P1 推进门控 / 目标点（纯函数；**只落地+单测，本阶段不接线**）----
#   口径：距离一律在**小地图归一化空间**（与 F1 的 TURRET_MM_* 同一把尺）；自身位置取
#   cooldowns["self_est"]（航位推算 + 走过塔被重锚纠偏）；阵营取 towers_v7.enemy / v7.minions.ally。
# v16.0 P3-A（真机数据驱动）: 原 0.55 / 2 / 0.10 在真机上 **0 次会推** —— 这个 agent 平时 hp
#   中位数只有 0.43（`_hp` 一个闸就吃掉 379/566 拍），小地图蓝点又常年 0~1 个（空旷局 `_alone`
#   吃掉 115/125 拍）。故按方案 A 放宽：血门 0.55->0.45、人数门 2->1、身边队友半径 0.10->0.15。
#   一轮只改这三个数，便于把"会推比例"的变化归因到它们；若仍近乎 0 再考虑方案 B。
PUSH_SELF_HP_MIN = 0.45       # 判据1: 自己血量下限（低于此不推）
PUSH_MIN_ALLY = 1             # 判据2: 视野内(小地图)我方点数下限
PUSH_ALLY_NEAR = 0.15         # 判据2 的"或：身边就有队友"半径（F3 的"跟队友推"也用它）
# v16.0 P3-B（真机第二轮数据驱动）: 放宽阈值后瓶颈后移到 `_gank`411 / `_outnumbered`241：
#   B′ F3 从"这一拍没敌人"改成"连续 PUSH_BLIND_GANK_S 秒都没敌人 且 身边没队友" —— 真实对局里
#      "对面不在你视野里（在别的路）"是常态，按单拍判会把正常单带的推进全挡掉。
#   B″ 判据3 从"敌点数 > 我点数"这种**相对比较**（依赖噪声极大的小地图蓝点，实测常 0~1 个）
#      改成绝对上限：可见敌人 > 2 个才不推（与 ENGAGE_MAX_ENEMY 同一套思路）。
PUSH_BLIND_GANK_S = 8.0       # B′: 连续这么多秒一个敌人都没见到, 且身边没队友 -> 才判"可能在蹲"
PUSH_MAX_ENEMY_VISIBLE = 2    # B″: 小地图可见敌人上限（超过才不推）
PUSH_CORRIDOR_K = 0.30        # 判据4/§5: 发育路走廊半径（与 decide 内 _corridor 同尺度）
PUSH_WAVE_NEAR = 0.30         # 判据5: 我方兵线离我多近才算"在推进方向上"
PUSH_STANDOFF = 0.10          # 判据6/§5: 塔外站位（必须 > TURRET_MM_THREAT，否则与 F1 打架）
PUSH_LEAD = 0.03              # §5: 跟兵线时朝敌方方向多压一点
# v16.0 E（推进优先级改造，设计见 docs/ARCH_PUSH_PRIORITY.md）：让推进也能在"支援"分支竞争
#   （真机一局 54% 的拍是 support_dps_lane，推进原来跟它零交集 -> 评估面只有 18%）。
PUSH_STAY_WITH_ALLY = 0.15    # E 硬约束: 推进点与"要跟的队友点"最大距离（超过就放弃接管，别把人甩开）
# v16.0 E-P2: 真机确认时可临时覆盖这些调参常量(不必改代码重跑), 用法 `--push-set KEY=VAL`（可重复）。
#   只允许覆盖**推进相关**的常量(白名单), 防止 `--push-set` 变成"随手改任意全局"的后门。
#   默认不覆盖 -> 与今天完全一致（零行为变化）。
_PUSH_TUNABLE = ("PUSH_SELF_HP_MIN", "PUSH_MIN_ALLY", "PUSH_ALLY_NEAR", "PUSH_CORRIDOR_K",
                 "PUSH_WAVE_NEAR", "PUSH_STANDOFF", "PUSH_LEAD", "PUSH_STAY_WITH_ALLY",
                 "PUSH_BLIND_GANK_S", "PUSH_MAX_ENEMY_VISIBLE",
                 "TURRET_MM_THREAT", "TURRET_MM_ESCAPE")


def apply_push_overrides(items):
    """`--push-set KEY=VAL`（可重复）-> 生效列表。非法键/值抛 SystemExit（**不改任何全局**）。

    先**全部校验**再统一生效：否则"前一半生效、后一半报错"会留下半吊子状态（而且只在启动时可见）。
    判据函数读的是**模块全局**，所以这里改全局即"立即生效"（不需要重载/重启逻辑）。
    """
    parsed = []
    for kv in items or []:
        key, _, val = str(kv).partition("=")
        key = key.strip()
        if key not in _PUSH_TUNABLE:
            raise SystemExit(f"--push-set 不支持 {key!r}；可调常量: {', '.join(_PUSH_TUNABLE)}")
        try:
            num = float(val)
        except ValueError:
            raise SystemExit(f"--push-set {key} 的值不是数字: {val!r}")
        parsed.append((key, num))
    for key, num in parsed:
        globals()[key] = num
    return parsed


ATTACK_THROTTLE_S = 0.8
MELEE_FRAC = 0.30
HOOK_BLOCK_FRAC = 0.05
RESTORE_THROTTLE_S = 60.0
LOW_RESOURCE_FRAC = 0.80
CHASE_BREAK_FRAC = 0.50
ALLY_TOWER_RECALL_FRAC = 0.35
SAFE_CLEAR_FRAC = 0.55
def _debounced(action: dict, now: float, cooldowns: dict) -> dict:
    """decoded docstring."""
    if now - float(cooldowns.get("skill", 0.0)) < SKILL_DEBOUNCE_S:
        return {"type": "none", "reason": "skill_debounce"}
    return action


def _mm_self(state_dict):
    """decoded docstring."""
    mm = state_dict.get("minimap") or {}
    dots = (mm.get("dots") or {}) if mm.get("found") else {}
    greens = dots.get("green") or []
    # v2.89: 无自点(未追踪到)返回 None, 调用方不作为红点距离基准(旧(0.5,0.5)兜底
    # -> 泉水期任意中点红点<0.15 触发空放技能)
    return (greens[0][0], greens[0][1]) if greens else None


def _mm_enemy_towers(state_dict):
    """小地图**分阵营敌塔** -> [(nx, ny), ...]（v16.0 P0-F1）。

    只认 MMTrackerV7 的 `towers_v7.enemy`（固定点 + 阵营判定 + 存活过滤，可靠）；
    拿不到就返回 []（**不猜**：不用未分类的 mm["towers"]，它两边混在一起，
    用它会把自家塔当敌塔 -> 无端后撤）。
    """
    mm = state_dict.get("minimap") or {}
    out = []
    for t in ((mm.get("towers_v7") or {}).get("enemy") or []):
        n = t.get("n") if isinstance(t, dict) else t
        if isinstance(n, (list, tuple)) and len(n) >= 2:
            out.append((float(n[0]), float(n[1])))
    return out


def enemy_out_of_view(state_dict):
    """视野外敌人粗判据（v16.0 P0-F3，保守近似）。

    m2 **从不主动开战绩面板** -> 拿不到"已死敌数"，所以不用"存活 = 5 - 已死 - 视野内"，
    只做保守近似：**屏内敌英(units 可信敌英 + 小地图红点)全无 且 视野内我方 >= 2 人**
    -> 对面 5 个都不在视野 = 很可能在蹲/抱团 -> 不许孤身推进（宁可跟队友/清线）。
    返回 {"red_seen","scene_seen","ally_seen","suspect_gank"}，供推进判据(P1 push_ok)消费。
    """
    mm = state_dict.get("minimap") or {}
    dots = (mm.get("dots") or {}) if mm.get("found") else {}
    red = dots.get("red") or []
    blue = dots.get("blue") or []
    try:
        from wzry.decision.optimizer import enemy_hero_trusted as _tr
    except Exception:
        _tr = None
    scene = [u for u in (state_dict.get("units") or [])
             if str(u.get("cls")) == "enemy_hero" and not u.get("dead")
             and (_tr is None or _tr(u))]
    return {"red_seen": len(red), "scene_seen": len(scene), "ally_seen": len(blue),
            "suspect_gank": bool(len(red) == 0 and not scene and len(blue) >= 2)}


# ---------------------------------------------------------------------------
# v16.0 P1 推进门控 / 目标点（纯函数）：**只落地 + 离线单测，本阶段不接线**
#   接线是 P2（`--push shadow`），位置在 decide() 的 `target is None` 分支里、
#   `dps_lane_push` 之前；条件不成立时行为与现在逐字节一致。
# ---------------------------------------------------------------------------
def _lane_center(cooldowns):
    """发育路(射手路)中心: 蓝方下路 / 红方上路 —— 复用已有 LANE_DIR_BLUE/RED（单一出处）。"""
    return LANE_DIR_BLUE if (cooldowns.get("camp") or "blue") == "blue" else LANE_DIR_RED


def _mm_self_est(state_dict, cooldowns):
    """自身在小地图上的估算位置：优先 `self_est`（航位推算, 走过塔被重锚纠偏），否则回基地。"""
    s = cooldowns.get("self_est")
    if s and len(s) >= 2:
        return (float(s[0]), float(s[1]))
    return BASE_BLUE if (cooldowns.get("camp") or "blue") == "blue" else BASE_RED


def _mm_dots(state_dict, key):
    """小地图某色点 -> [(nx, ny), ...]（`found=False` 或缺失都返回 []）。"""
    mm = state_dict.get("minimap") or {}
    dots = (mm.get("dots") or {}) if mm.get("found") else {}
    out = []
    for p in (dots.get(key) or []):
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            out.append((float(p[0]), float(p[1])))
    return out


def _mm_ally_minions(state_dict):
    """我方兵线点 -> [(nx, ny), ...]（v7.minions.ally；拿不到就返回 []，不猜）。"""
    mm = state_dict.get("minimap") or {}
    out = []
    for d in (((mm.get("v7") or {}).get("minions") or {}).get("ally") or []):
        n = d.get("n") if isinstance(d, dict) else d
        if isinstance(n, (list, tuple)) and len(n) >= 2:
            out.append((float(n[0]), float(n[1])))
    return out


def _push_enemy_dir(est, tw, cooldowns):
    """"朝敌方"的单位向量：优先 自身→最近敌塔，退化到 自身→敌方基地。"""
    if tw is not None:
        tgt = tw
    else:
        # 敌方基地 = 对面的泉水（本方是 BASE_BLUE，对面就是 BASE_RED，反之亦然）
        tgt = BASE_RED if (cooldowns.get("camp") or "blue") == "blue" else BASE_BLUE
    dx, dy = float(tgt[0]) - est[0], float(tgt[1]) - est[1]
    d = math.hypot(dx, dy) or 1e-6
    return (dx / d, dy / d)


def _push_pick_tower(towers, est, lane_c, k):
    """"最近且在我方推进走廊内"的敌塔（走廊外的塔一律不选；都不在 -> None）。"""
    pool = [t for t in towers if math.hypot(t[0] - lane_c[0], t[1] - lane_c[1]) <= k]
    if not pool:
        return None
    return min(pool, key=lambda t: math.hypot(t[0] - est[0], t[1] - est[1]))


def _push_pick_wave(waves, est, edir):
    """"离我最近且更靠敌方"的我方兵线；没有"更靠敌方"的就退回最近的一条。"""
    if not waves:
        return None
    ahead = [p for p in waves
             if (p[0] - est[0]) * edir[0] + (p[1] - est[1]) * edir[1] > 0]
    pool = ahead or waves
    return min(pool, key=lambda p: math.hypot(p[0] - est[0], p[1] - est[1]))


def _blind_s(state_dict, cooldowns):
    """"连续没见到任何敌人"的时长（秒）。见到敌人即重置窗口（P3-B′）。

    窗口存在 `cooldowns["blind_start"]`：最后一次"见到敌人"的时刻；一直没见过就从首次进入盲区算起。
    """
    oov = enemy_out_of_view(state_dict)
    now = float(state_dict.get("t") or 0.0)
    if oov["red_seen"] or oov["scene_seen"]:
        cooldowns["blind_start"] = now          # 露头 -> 重置
        return 0.0
    if "blind_start" not in cooldowns:
        cooldowns["blind_start"] = now
    return max(0.0, now - float(cooldowns["blind_start"]))


def push_ok(state_dict, cooldowns):
    """推进门控（纯函数）-> (ok, reason)。

    逐条串行：任一不满足立即 (False, "push_blocked_*")，全过 (True, "push_ok")。
    判据顺序见 docs/ARCH_MOVE_PUSH.md §4：
      hp -> 逃生/阵亡 -> 人数(够 + 不劣) -> 视野外敌人 -> 走廊内有无敌塔 -> 有无我方兵线 -> 塔外站位
    注：**不做回城门**——回城只在 hp<0.40 时发起，判据1(0.55) 已经把它挡住了（不引入新魔数）。
    """
    ui = state_dict.get("ui") or {}
    _hp_raw = ui.get("hp")
    try:
        hp = float(_hp_raw) if _hp_raw == _hp_raw else 0.0     # NaN/缺失 -> 当没血 -> 不推
    except (TypeError, ValueError):
        hp = 0.0
    if hp < PUSH_SELF_HP_MIN:                                   # 1
        return False, "push_blocked_hp"
    now = float(state_dict.get("t") or 0.0)
    # 7) 正在逃 / 刚被重击 / 刚复活 -> 不推（窗口都复用现有状态，不新造）
    if now - float(cooldowns.get("hp_drop_t", 0.0)) < 1.2:
        return False, "push_blocked_escaping"
    if now - float(cooldowns.get("escape_t", 0.0)) < 1.5:
        return False, "push_blocked_escaping"
    _rv = float(cooldowns.get("revive_t", 0.0))
    if _rv and now - _rv < 5.0:
        return False, "push_blocked_escaping"
    est = _mm_self_est(state_dict, cooldowns)
    ally_mm = _mm_dots(state_dict, "blue")
    enemy_mm = _mm_dots(state_dict, "red")
    # "身边就有队友"用小地图蓝点（队友恒在小地图上，比屏内阵营判定可靠）
    near_ally = any(math.hypot(p[0] - est[0], p[1] - est[1]) <= PUSH_ALLY_NEAR for p in ally_mm)
    if len(ally_mm) < PUSH_MIN_ALLY and not near_ally:          # 2
        return False, "push_blocked_alone"
    if len(enemy_mm) > PUSH_MAX_ENEMY_VISIBLE:                  # 3（B″: 绝对上限, 不跟蓝点数比）
        return False, "push_blocked_outnumbered"
    # F3 (B′): 单拍"看不见敌人"是正常单带的常态 -> 改成"连续 PUSH_BLIND_GANK_S 秒都没敌人
    #   且身边没队友"才判蹲（会写 cooldowns["blind_start"] 维护窗口）
    if _blind_s(state_dict, cooldowns) >= PUSH_BLIND_GANK_S and not near_ally:
        return False, "push_blocked_gank"
    tw = _push_pick_tower(_mm_enemy_towers(state_dict), est,
                          _lane_center(cooldowns), PUSH_CORRIDOR_K)
    if tw is None:                                              # 4
        return False, "push_blocked_no_tower"
    if not any(math.hypot(p[0] - est[0], p[1] - est[1]) <= PUSH_WAVE_NEAR
               for p in _mm_ally_minions(state_dict)):          # 5
        return False, "push_blocked_no_wave"
    if math.hypot(tw[0] - est[0], tw[1] - est[1]) <= PUSH_STANDOFF:    # 6
        return False, "push_blocked_too_close"
    return True, "push_ok"


def push_target(state_dict, cooldowns):
    """推进目标点（纯函数）-> (target, reason)。

    target=(nx, ny)，或 None（此时 reason 是放弃原因）。按优先级（§5）：
      1) 跟兵线（首选）: "最近且更靠敌方"的我方兵线，目标点 = 兵线再朝敌方压 PUSH_LEAD
         -> reason = "push_follow_wave"
      2) 压塔（次选）:   走廊内最近的敌塔，目标点 = 塔向我方回退 PUSH_STANDOFF
         -> reason = "push_lane_tower"（回退距离 = PUSH_STANDOFF > TURRET_MM_THREAT，故不会
            被 F1 判成"靠近敌塔"）
      3) 走廊硬约束: 算出的点必须落在发育路走廊内，否则 (None, "push_blocked_corridor")
         —— 防止"推着推着跑到河道/对抗路"。
    注：本函数**假定 push_ok 已通过**（不重复门控），但走廊约束它自己保证。
    """
    est = _mm_self_est(state_dict, cooldowns)
    lane_c = _lane_center(cooldowns)
    tw = _push_pick_tower(_mm_enemy_towers(state_dict), est, lane_c, PUSH_CORRIDOR_K)
    edir = _push_enemy_dir(est, tw, cooldowns)
    wave = _push_pick_wave(_mm_ally_minions(state_dict), est, edir)
    if wave is not None:
        pt = (wave[0] + edir[0] * PUSH_LEAD, wave[1] + edir[1] * PUSH_LEAD)
        reason = "push_follow_wave"
    elif tw is not None:
        d = math.hypot(tw[0] - est[0], tw[1] - est[1])
        k = min(PUSH_STANDOFF, d)          # gate6 已保证 d > STANDOFF，这里只是兜底
        pt = (tw[0] - (tw[0] - est[0]) / (d or 1e-6) * k,
              tw[1] - (tw[1] - est[1]) / (d or 1e-6) * k)
        reason = "push_lane_tower"
    else:
        return None, "push_blocked_no_tower"
    if math.hypot(pt[0] - lane_c[0], pt[1] - lane_c[1]) > PUSH_CORRIDOR_K:
        return None, "push_blocked_corridor"
    return (max(0.02, min(0.98, pt[0])), max(0.02, min(0.98, pt[1]))), reason


def _push_inputs(state_dict, cooldowns, support_pt=None):
    """把推进判据**真正读到的输入**原样打包（v16.0 离线回放用，见 scripts/replay_push.py）。

    为什么要存: 真机一局 7 分钟，而且"局间 hp 分布差异"已经污染过两次对照（0.557 vs 0.391）。
    存下输入就能用**同一批真实帧**离线重跑任意阈值/条件，不必再打游戏，也不再有局间方差。
    只打包判据用到的字段: hp / 自身估算位置 / 阵营 / 蓝点红点 / 敌塔 / 我方兵线 /
    逃生窗口(布尔化, 判据7 只关心"在不在窗口内") / 屏内可信敌英数(只影响 F3 盲区计时) / 支援点。
    """
    ui = state_dict.get("ui") or {}
    _hp = ui.get("hp")
    try:
        # 不取整: 判据是 `hp < PUSH_SELF_HP_MIN` 这种**精确比较**, 取整会在边界把结论翻转
        #   （实测 round(x,4) 曾让 1.7% 的拍在自检里对不上, 全都卡在 hp==门槛 那一拍）
        hp = float(_hp) if _hp == _hp else None
    except (TypeError, ValueError):
        hp = None
    now = float(state_dict.get("t") or 0.0)
    esc = bool(now - float(cooldowns.get("hp_drop_t", 0.0)) < 1.2
               or now - float(cooldowns.get("escape_t", 0.0)) < 1.5
               or (cooldowns.get("revive_t")
                   and now - float(cooldowns["revive_t"]) < 5.0))
    try:
        from wzry.decision.optimizer import enemy_hero_trusted as _tr
    except Exception:
        _tr = None
    nsc = len([u for u in (state_dict.get("units") or [])
               if str(u.get("cls")) == "enemy_hero" and not u.get("dead")
               and (_tr is None or _tr(u))])
    est = _mm_self_est(state_dict, cooldowns)

    def rl2(pts):
        return [[round(float(p[0]), 4), round(float(p[1]), 4)] for p in pts]

    return {"hp": hp, "est": [round(est[0], 4), round(est[1], 4)],
            "camp": cooldowns.get("camp") or "blue",
            "blue": rl2(_mm_dots(state_dict, "blue")),
            "red": rl2(_mm_dots(state_dict, "red")),
            "towers": rl2(_mm_enemy_towers(state_dict)),
            "waves": rl2(_mm_ally_minions(state_dict)),
            "esc": esc, "nscene": nsc,
            "sup": rl2([support_pt])[0] if support_pt is not None else None}


def _push_eval(state_dict, cooldowns, scope, support_pt=None):
    """评估一次推进门并写 `cooldowns["push_info"]`（v16.0 E-P0：**只评估+记录，不采用**）。

    scope = "fallback"（原来那支）| "support"（E 新增：支援帧上也评一次）。
    scope=="support" 时额外算 E 的三条硬约束（§ARCH_PUSH_PRIORITY 3.1），结果放 `e_ok`：
    只有 `ok and pt and gap<=PUSH_STAY_WITH_ALLY and 我和队友都在朝敌方前半场` 才为 True。
    `e_ok` 就是"E-P1 真接线后这一拍会接管"的预测量 —— 先量再决定要不要接线。
    返回 info（含 raw `target`，供接线时精确采用，不做取整）。
    """
    mode = cooldowns.get("push_mode") or "off"
    ok, why = push_ok(state_dict, cooldowns)
    pt, preason = push_target(state_dict, cooldowns) if ok else (None, why)
    info = {"mode": mode, "ok": bool(ok), "why": why, "reason": preason,
            "target": [pt[0], pt[1]] if pt else None, "scope": scope, "used": False}
    if scope == "support":
        est = _mm_self_est(state_dict, cooldowns)
        tw = _push_pick_tower(_mm_enemy_towers(state_dict), est,
                              _lane_center(cooldowns), PUSH_CORRIDOR_K)
        edir = _push_enemy_dir(est, tw, cooldowns)

        def _ahead(p):
            return (p[0] - est[0]) * edir[0] + (p[1] - est[1]) * edir[1] >= 0.0

        gap = (math.hypot(pt[0] - support_pt[0], pt[1] - support_pt[1])
               if (pt is not None and support_pt is not None) else None)
        info["gap"] = None if gap is None else round(gap, 3)
        info["e_ok"] = bool(ok and pt is not None and support_pt is not None
                            and gap <= PUSH_STAY_WITH_ALLY and _ahead(pt) and _ahead(support_pt))
    info["in"] = _push_inputs(state_dict, cooldowns, support_pt)   # 离线回放用（不参与判据）
    cooldowns["push_info"] = info
    return info


def _away_map(state_dict, tx, ty, k=0.12):
    """decoded docstring."""
    gx, gy = _mm_self(state_dict) or (0.5, 0.5)   # v2.89 None兜底(仅方位辅助)
    d = math.hypot(gx - tx, gy - ty) or 1e-6
    return (max(0.02, min(0.98, gx + (gx - tx) / d * k)),
            max(0.02, min(0.98, gy + (gy - ty) / d * k)))


def _nearest_walkable(grid, p, radius=3):
    """decoded docstring."""
    n = grid.shape[0]
    for r in range(1, radius + 1):
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                y, x = p[0] + dy, p[1] + dx
                if 0 <= y < n and 0 <= x < n and grid[y, x] > 0:
                    return (y, x)
    return None


def _hook_path_blocked(target, blockers, half_w, dist_width, aspect):
    """decoded docstring."""
#    target: [cx, cy, w, h] 灞忓箷褰掍竴鍖栵紙鎴戞柟鍦睆骞曚腑蹇(0.5, 0.5)    鍒柇歜locker 鍒扮嚎娈碉紙涓績->target夌殑璺濈 < half_w 涓blocker 璺濈 < target 璺濈
    """decoded docstring."""
    import math
    tx, ty = target[0], target[1]
    dx, dy = tx - 0.5, (ty - 0.5) * aspect
    seg_len = math.hypot(dx, dy)
    if seg_len < 1e-4:
        return False
    for b in blockers:
        bx, by = b[0], b[1]
        # 鐐瑰埌绾挎璺濈堝弬鏁t 鎶曞奖        px, py = bx - 0.5, (by - 0.5) * aspect
        t = (px * dx + py * dy) / (seg_len * seg_len)
        if t < 0.0 or t > 1.0:
            continue
        # 瀹屾暣鍨傜洿璺濈坸/y 鎶樼畻鍚庯級
        proj_x = 0.5 + dx * t
        proj_y = 0.5 + dy * t
        perp = math.hypot(bx - proj_x, (by - proj_y) * aspect)
        b_dist = math.hypot(bx - 0.5, (by - 0.5) * aspect)
        if perp < half_w and b_dist < seg_len * 0.98:
            return True
    return False


def decide(state_dict: dict, cooldowns: dict) -> dict:
    """decoded docstring."""
    # (comment)
#      0. 鍕句腑杩炴嫑氫簩鎶鑳介噴鏀惧悗绐楀彛鍐呮晫浜鸿鎷夎繎 鈫鍙敜甯堟妧鑳+ 涓夋妧鑳      1. 濉旇閬匡細鏁屾柟濉斿彲瑙佷笖鏃犳垜鏂瑰皬鍏鈫绉诲姩閬垮紑濉旀敾鍑昏寖鍥达紙鎶鑳戒笉鍙楅檺      2. 浜屾妧鑳斤細鏁屾柟鑻遍泟鍦簩鎶鑳借寖鍥村唴 鈫閽瓙
#      3. 涓鎶鑳斤細涓嶅湪澶嫑鐢熸晥鏈涓鏁屼汉/鏁屽叺鍦韩杈鈫涓鎶鑳      4. 绉诲姩氭湁鏁屼汉 鈫鏈濇渶杩戞晫浜烘寔缁嫋鍔紱鏃犳晫浜鈫甯彂鑲茶矾勬墜
#         堣窡闅忔渶杩ally_hero屽惁鍒欐湞鍙戣偛璺柟鍚戯級

#    璁板繂坈ooldowns 鎵睍瀛楁夛細
#      skill2_t / skill1_t / skill3_t / summoner_t  鏈杩戦噴鏀炬椂闂      hook_anchor_dist  浜屾妧鑳介噴鏀炬椂鏈杩戞晫浜鸿窛绂伙紙鍕句腑鍒畾鍩哄噯      hook_pending      浜屾妧鑳介噴鏀炬椂鍒伙紙鍕句腑纭绐楄捣鐐癸級
    """decoded docstring."""
    now = float(state_dict.get("t") or 0.0)
    w, h = state_dict.get("screen_size") or (1280.0, 720.0)
    aspect = (float(h) / float(w)) if w else 0.5625
    # v16.0 P2 修正: **每拍先清空 push 评估**（只在 --push != off 时；off 保持零足迹）。
    #   decide() 在很多分支会提前 return（打架/逃生/阵亡/回城…），那些拍根本走不到移动块；
    #   而 cooldowns["push_info"] 跨拍存活 -> 不清空会把**上一拍的**评估贴到这一拍上
    #   （实测污染：标着 `_alone` 的拍却记着蓝点 2~4 个）。
    if (cooldowns.get("push_mode") or "off") != "off":
        cooldowns["push_info"] = None

    # input: extra enemy bars / self pos
    extra = state_dict.get("extra") or {}
    enemy_bars = extra.get("enemy_bars") or []
    roster = extra.get("roster") or cooldowns.get("roster")
    hook_signal = extra.get("hook_signal") or []

    units = state_dict.get("units") or []
    enemies, minions, turrets, allies, ally_minions, monsters = [], [], [], [], [], []
    ally_turrets = []
    selfs = []                      # v2.45 self collection
    from wzry.decision.optimizer import enemy_hero_trusted as _eh_trusted   # v14.2
    for u in units:
        cls = str(u.get("cls", ""))
        scr = u.get("screen") or [0.5, 0.5, 0.0, 0.0]
        cx, cy = scr[0], scr[1]
        # v2.8 UI 鍖鸿繃婊細鍙充笂瑙掑鍍忓尯 (x>0.72, y<0.20) 鐨勫/鑻遍泟妫娴嬫槸 UI 璇
        if cls in ("enemy_turret", "ally_turret", "enemy_hero", "ally_hero") \
                and cx > 0.72 and cy < 0.20:
            continue
        # (comment)
        if cls in ("enemy_turret", "ally_turret") and cx < 0.18 and cy < 0.32:
            continue
        # v8.0 用户: 塔/小兵不需要判断(红方时自家塔/小兵误标敌方, 阵营同观无法区分)
        #   -> 完全屏蔽 塔/小兵 类(不参与决策), 只判断英雄(人)
        if cls in ("enemy_turret", "ally_turret", "enemy_minion", "ally_minion",
                   "enemy_crystal", "ally_crystal"):
            continue
        if cls == "self":
            pass   # v2.46 鍏睆鑷繁涓嶆娴涓嶆樉绀 鑷繁蹇呭湪灞忓箷涓, 浠皬鍦板浘缁跨幆涓哄噯
        elif cls == "enemy_hero":
            # v14.2 幻影敌人过滤(用户问题③"技能空放"): 阵营判据不显著(camp_src="default":
            #   遮挡/远景/技能特效下框内蓝/红像素占比判不出来, 后端保守兜底判成敌人)或
            #   置信不足(conf<0.55)的框不进 enemies -> 不触发 skill1/skill2/attack/连招,
            #   避免朝空处放技能。skill 是 m2 规则层直接放行(solve 白名单不覆盖), 故
            #   过滤必须做在这里, 而非 optimizer._feat。
            if _eh_trusted(u):
                enemies.append(scr)
        elif cls == "enemy_minion":
            minions.append(scr)
        elif cls == "enemy_turret":
            turrets.append(scr)
        elif cls == "ally_hero":
            allies.append(scr)
        elif cls == "ally_minion":
            ally_minions.append(scr)
        elif cls == "neutral_monster":
            monsters.append(scr)
        elif cls == "ally_turret":
            ally_turrets.append(scr)

#    # v2.45 鑷繁=鍏満鍞竴: 澶鑷繁"妗-> 绂诲睆骞曚腑蹇冩渶杩戣鐪熸垜, 鍏朵綑杞洖鎴戣嫳(闃熷弸璇爣)
    if len(selfs) > 1:
        selfs.sort(key=lambda s: (s[0] - 0.5) ** 2 + (s[1] - 0.5) ** 2)
        # (comment)
        selfs = selfs[:1]
    elif selfs:
        pass
#    # 鑷繁涓庢垜鑻遍噸鍙闃熷弸璐磋劯) -> 浠鑷繁"涓哄噯, 鍘绘帀涓庡叾閲嶅彔鐨勬垜鑻    if selfs:
        sx, sy = selfs[0][0], selfs[0][1]
        allies = [a for a in allies if math.hypot(a[0] - sx, a[1] - sy) > 0.04]

    # v2.60 敌英信任
    _mmp_yellow = ((state_dict.get("minimap") or {}).get("dots") or {}).get("yellow") or []
    # (comment)
    _ally_bars = extra.get("ally_bars") or []
    _ally_bars = extra.get("ally_bars") or []
    if allies and _ally_bars:
        allies = [a for a in allies if any(
            math.hypot(b[0] / w - float(a[0]), b[1] / h - float(a[1])) < 0.20
            for b in _ally_bars)]

    def dist_width(cx, cy):
        return math.hypot(cx - 0.5, (cy - 0.5) * aspect)

    def nearest(lst):
        if not lst:
            return None
        return min(lst, key=lambda s: dist_width(s[0], s[1]))

    def ready(key, thr):
        return now - float(cooldowns.get(key, 0.0)) > thr

    # ---- v2.72 dominant skills; v2.97 用户规则(铁): 小地图红点**禁止**触发技能/出钩
    #      (红点闪烁位置不准 -> 乱出钩); 只有小地图之外=屏幕上 敌英框/敌红条 才释放。
    #      出钩(skill2)额外要求: 屏幕敌英在二技能范围内(SKILL2_RANGE_FRAC) ----
    _mmr_pts0 = ((state_dict.get("minimap") or {}).get("dots") or {}).get("red") or []
    _mm_own0 = _mm_self(state_dict)
    # v2.89 红点距离只有在自己绿点已知时才有意义(泉水/未追踪=0.5,0.5兜底曾触发空放)
    _d_red0 = (min([math.hypot(p[0] - _mm_own0[0], p[1] - _mm_own0[1]) for p in _mmr_pts0],
                   default=9.9) if _mm_own0 else 9.9)
    if _mmr_pts0:
        cooldowns["red_stable"] = float(cooldowns.get("red_stable", 0)) + 1
    else:
        cooldowns["red_stable"] = 0
    # v2.97 释放证据 = 屏幕敌英(yolo框) 或 屏幕敌红条(yolo未检出时可作辅助);
    #          v2.98 红条独证不释放技能: 暴君/红buff等野怪红条也是红条且yolo常漏检
    #          -> 无 yolo enemy_hero 框时, 红条只算"疑似敌英", 不触发任何技能/出钩
    _has_yolo_eh = bool(enemies)
    # ---- v14.3 战斗决策(用户批准): 不占优不接战 ----
    # 实测 180s 完整对局: 3 死 0 英雄击杀、回城被打断 2 次 -> "看到谁就打谁"。
    # 门槛: HP>=ENGAGE_HP_MIN 且 屏内**可信**敌英 <=ENGAGE_MAX_ENEMY 才允许接战;
    #       不满足且近敌已进技能射程 -> 直接朝反方向撤(map_move), 不生成任何技能候选。
    _ui_now = state_dict.get("ui") or {}
    _hp_e = _ui_now.get("hp")
    _hp_e = float(_hp_e) if (_hp_e is not None and _hp_e == _hp_e
                             and 0.0 <= float(_hp_e) <= 1.0) else 0.6
    _ne0 = nearest(enemies)
    _d_ne0 = dist_width(_ne0[0], _ne0[1]) if _ne0 else 9.9
    # v15.4 两个"占优"例外(消费读数): 缺读数一律按常规门槛走 -> **不放宽**。
    #   ① 收人头窗口: 锁定的那个目标快死(ui.lock_target_hp <= KILL_HP_MAX) + 自己还够活。
    #   ② 占优推进:   屏幕上**多个**敌人残血(extra.enemy_hp_scene 里 <= PRESS_ENEMY_HP_MAX
    #      的个数 >= PRESS_MIN_LOW_ENEMIES) = 敌方整体不占优 -> 允许自己血量略低也打。
    #      它只放宽接战门槛, **不改移动目标**(压线/推进属于另一层决策, 不在这里动)。
    _lt_hp = _ui_now.get("lock_target_hp")
    _kill_confirm = bool(_ui_now.get("lock_target") and _lt_hp is not None
                         and float(_lt_hp) <= KILL_HP_MAX and _hp_e >= KILL_SELF_HP_MIN)
    # ② 的读数(extra.enemy_hp_scene)在 0.5s 节流块里, 而 state_dict 每拍重建 -> 被跳过的拍
    #   上它是缺失的; 直接用许可会每拍闪断(英雄走一步撤一步)。按 hp/mp 回填的同一思路做**有界缓存**:
    #   读到新值时记 n_low + 时间戳, 缺失时在 PRESS_STALE_S 内沿用(<=0.6s 陈旧远好过闪烁)。
    _scene = (state_dict.get("extra") or {}).get("enemy_hp_scene")
    if _scene:
        _n_low = sum(1 for r in _scene if r is not None and float(r) <= PRESS_ENEMY_HP_MAX)
        cooldowns["n_low_enemies"], cooldowns["n_low_t"] = _n_low, now
    else:
        _n_low = (int(cooldowns.get("n_low_enemies") or 0)
                  if (now - float(cooldowns.get("n_low_t", -9e9))) <= PRESS_STALE_S else 0)
    _press = bool((not _kill_confirm) and _n_low >= PRESS_MIN_LOW_ENEMIES
                  and _hp_e >= PRESS_SELF_HP_MIN)
    if _kill_confirm and (now - float(cooldowns.get("kill_log_t", -9e9)) > 5.0):
        cooldowns["kill_log_t"] = now
        print(f"[kill] 锁定目标残血 {float(_lt_hp):.2f} + 自己 HP {_hp_e:.2f}"
              f" -> 接战门槛放宽到 {KILL_SELF_HP_MIN}")
    if _press and (now - float(cooldowns.get("press_log_t", -9e9)) > 5.0):
        cooldowns["press_log_t"] = now
        print(f"[press] 敌方残血 {_n_low} 个(<= {PRESS_ENEMY_HP_MAX}) + 自己 HP {_hp_e:.2f}"
              f" -> 接战门槛放宽到 {PRESS_SELF_HP_MIN}")
    _engage_floor = (KILL_SELF_HP_MIN if _kill_confirm
                     else PRESS_SELF_HP_MIN if _press else ENGAGE_HP_MIN)
    # 接战类动作统一用这个 reason(便于日志/复盘区分"常规接战"与两个占优例外)
    _engage_reason = ("kill_confirm_attack" if _kill_confirm
                      else "press_advantage_attack" if _press else "engage_enemy")
    _engage_ok = (_hp_e >= _engage_floor and len(enemies) <= ENGAGE_MAX_ENEMY)
    # v14.3 回城门禁: 最近(可信)敌英/敌红条都得够远, 否则读条中必被近身打断
    _recall_clear = _d_ne0 > RECALL_ENEMY_CLEAR_FRAC and not any(
        math.hypot(b[0] / w - 0.5, (b[1] / h - 0.5) * aspect) <= RECALL_ENEMY_CLEAR_FRAC
        for b in enemy_bars)
    if (not _engage_ok) and _ne0 is not None and _d_ne0 <= SKILL2_RANGE_FRAC:
        _ax_e, _ay_e = _away_map(state_dict, _ne0[0], _ne0[1], k=0.20)
        return {"type": "map_move", "nx": _ax_e, "ny": _ay_e,
                "reason": "disengage_retreat"}
    if _has_yolo_eh or enemy_bars:
        _nq0 = nearest(enemies) if enemies else None
        _dq0 = dist_width(_nq0[0], _nq0[1]) if _nq0 else 0.90
        _s_ = (state_dict.get("ui") or {}).get("skill_states") or {}
        _s3_ = _s_.get("3") or {}
        _s2_ = _s_.get("2") or {}
        _s1_ = _s_.get("1") or {}
        # v10.5 用户铁律: 碰到敌人 = 先二技能勾! 大招 ONLY 在勾中后 combo!
        #   没勾到 = 只用一技能消耗 (大招绝不先手)
        if _engage_ok and _nq0 is not None and _dq0 <= SKILL2_RANGE_FRAC \
                and ready("skill2_t", SKILL2_THROTTLE_S) and _s2_.get("unlocked") is not False:
            cooldowns["skill2_t"] = now
            cooldowns["skill"] = now
            cooldowns["hook_pending"] = now
            cooldowns["hook_anchor_dist"] = _dq0
            return {"type": "skill", "id": 2, "mode": "tap",
                    "reason": "enemy_in_skill2_range"}
        if _engage_ok and ready("skill1_t", SKILL1_THROTTLE_S) \
                and _s1_.get("unlocked") is not False and _has_yolo_eh:
            cooldowns["skill1_t"] = now
            cooldowns["skill"] = now
            return {"type": "skill", "id": 1, "mode": "tap", "reason": "enemy_hero_near"}
        if _engage_ok and ready("attack_t", ATTACK_THROTTLE_S) and _has_yolo_eh:
            cooldowns["attack_t"] = now
            return {"type": "attack", "priority": "free", "reason": _engage_reason}

    # ---- v2.13 鍥炲煄璇绘潯淇濇姢氬洖鍩庡悗 RECALL_ACTIVE_S 鍐呬笉鎵鍏朵粬鍔綔 ----
    # 堜慨澶嶏細鎭閿绉诲姩鍦洖鍩庤鏉腑鎵 -> 鍥炲煄琚墦鏂紝琛涓鐩村洖涓嶆弧    # 渚嬪氭晫浜绾潯杩戣韩堝嵄闄級鏃朵腑鏂洖鍩庯紝璧版甯稿喅绛栵紙閫冭窇/鍙嶅嚮    if now - float(cooldowns.get("recall_t", 0.0)) < RECALL_ACTIVE_S:
        ne = nearest(enemies)
        danger = ne is not None and dist_width(ne[0], ne[1]) < DANGER_FRAC
        if not danger and enemy_bars:
            for b in enemy_bars:
                bd = math.hypot(b[0] / w - 0.5, (b[1] / h - 0.5) * aspect)
                if bd < DANGER_FRAC:
                    danger = True
                    break
        if not danger:
            return {"type": "none", "reason": "recall_in_progress"}

#    # ---- 0) 鍕句腑杩炴嫑氫簩鎶鑳介噴鏀惧悗绐楀彛鍐咃紝鏁屼汉琚鎷夎繎" = 鍕惧埌浜----
    hook_pending = float(cooldowns.get("hook_pending", 0.0))
    if hook_pending > 0 and now - hook_pending <= HOOK_CONFIRM_S:
        anchor = float(cooldowns.get("hook_anchor_dist", 0.0))
        ne = nearest(enemies)
        if ne is not None and anchor > 0:
            d = dist_width(ne[0], ne[1])
            # hook hit: distance shrink (anchor-d)>0.12
            if d < anchor * HOOK_DIST_SHRINK and (anchor - d) > 0.12:
                cooldowns["hook_pending"] = 0.0
                combo = []
                if ready("summoner_t", SUMMONER_THROTTLE_S):
                    combo.append({"type": "summoner"})
                if ready("skill3_t", SKILL3_ACTIVE_S):
                    combo.append({"type": "skill", "id": 3, "mode": "tap"})
                if combo:
                    cooldowns["summoner_t"] = now
                    cooldowns["skill3_t"] = now
                    cooldowns["skill"] = now
                    return {"type": "combo", "actions": combo, "reason": "hook_confirmed"}
        if now - hook_pending > HOOK_CONFIRM_S:
            cooldowns["hook_pending"] = 0.0

    # ---- v11.11 低血优先(用户: 残血先退安全→回城, 不打架!): 在技能块前 ----
    _hp_low_first = float((state_dict.get("ui") or {}).get("hp") or 1.0)
    _hp_low_first = _hp_low_first if 0 <= _hp_low_first <= 1 else 1.0
    if (_hp_low_first < 0.30) and (now - float(cooldowns.get("recall_t", 0.0)) > 15.0):
        _near_low = [s for s in (enemies + minions + monsters)
                     if dist_width(s[0], s[1]) < 0.42]
        if _near_low:
            _esc_low = min(_near_low, key=lambda s: dist_width(s[0], s[1]))
            _ax_low, _ay_low = _away_map(state_dict, _esc_low[0], _esc_low[1], k=0.22)
            return {"type": "map_move", "nx": _ax_low, "ny": _ay_low,
                    "reason": "low_hp_fallback_safe"}
        # 安全: 回城(自动回泉水补满)
        if _recall_clear and ready("recall_t", 20.0):
            cooldowns["recall_t"] = now
            return {"type": "recall", "reason": "low_hp_recall_first"}
    # ---- brainless fire (skills if enemy present) ----
    # v10.11 血条修复: hp=None(检测失败)不默认满血 -> 用估计/保守值; 检测帧缺失也保守
    _hp_raw = (state_dict.get("ui") or {}).get("hp")
    if _hp_raw is not None and _hp_raw == _hp_raw and 0.0 <= float(_hp_raw) <= 1.0:
        _hpq = float(_hp_raw)
    else:
        # hp 未知: 保守估计 = 0.6 (避免"没血不回家"; 若持续检测失败会用 hp_drop 兜底)
        _hpq = 0.6
    # v2.82 mana: measurement missing -> skill-cost estimate (0.10/skill, 12+ => recall)
    _mp_raw = (state_dict.get("ui") or {}).get("mp")
    _skill_cnt = 0
    for _sk in ("skill1_t", "skill2_t", "skill3_t"):
        _st = float(cooldowns.get(_sk, 0.0) or 0.0)
        if _st and now - _st < 90.0:
            _skill_cnt += 1
    _mp_est = max(0.0, round(1.0 - 0.10 * _skill_cnt, 2))
    if _skill_cnt >= 12 and _mp_est > 0.15:
        _mp_est = 0.15
    _mpq = float(_mp_raw) if (_mp_raw is not None and _mp_raw == _mp_raw
                              and 0.0 <= float(_mp_raw) <= 1.0) else _mp_est
    skill_states = (state_dict.get("ui") or {}).get("skill_states") or {}
    # v2.70 鏁屼汉璇佹嵁=灞忓箷鏁岃嫳妗鎴忓湴鍥剧孩鐐浠绘剰璺濈, 鐢埛璇箟: 鍑虹幇鍗虫墦)
    # v2.97 释放证据 = 屏幕敌英/敌红条(小地图红点不参与出钩与技能)
    #     v2.98 红条独证不释放: 暴君/野怪红条 yolo 漏检时算"疑似", 技能必须 yolo 敌英框
    _mmr_pts = ((state_dict.get("minimap") or {}).get("dots") or {}).get("red") or []
    _mm_own = _mm_self(state_dict)
    _d_red_near = (min([math.hypot(p[0] - _mm_own[0], p[1] - _mm_own[1]) for p in _mmr_pts],
                       default=9.9) if _mm_own else 9.9)
    _has_yolo_eh_q = bool(enemies)
    if enemies or enemy_bars:
        _nq = nearest(enemies) if enemies else None
        _dq = dist_width(_nq[0], _nq[1]) if _nq else 0.90
        _s3q = skill_states.get("3") or {}
        _s2q = skill_states.get("2") or {}
        _s1q = skill_states.get("1") or {}
        # v10.6 用户连招铁律: 勾中=接召唤师+三技能; 没勾中=一技能消耗
        #   勾中判定(可靠): 勾后敌人被拉近 = 当前距离 < 出钩时距离-0.08 (敌人被拽到身前)
        hook_pending = float(cooldowns.get("hook_pending", 0.0))
        hook_anchor = float(cooldowns.get("hook_anchor_dist", 0.45))
        hook_missed = (hook_pending > 0 and now - hook_pending > 2.5)
        # v10.7 勾中判定放宽+血条证据: 勾中=敌英被拉近(距离<锚点) OR 敌英血条出现/下降
        _hook_hit = False
        if hook_pending and 0 < now - hook_pending <= 1.2 and _nq is not None:
            _shrunk = _dq < min(0.45, hook_anchor - 0.04)   # 被拉近
            _nbar = None
            try:
                _bars = state_dict.get("extra") or {}
                _eb = _bars.get("enemy_bars") or []
                if _eb:
                    _nbar = True
            except Exception:
                _nbar = None
            _hit_any = _shrunk or (_nbar and _dq < 0.42)
            if _hit_any:
                _hook_hit = True
        if _hook_hit:
            # 勾中 -> 召唤师 + 三技能 (用户明确小连招=召唤师+三技能, 不接一技能)
            combo = []
            if ready("summoner_t", 5.0):
                combo.append({"type": "summoner"})
            if ready("skill3_t", 20.0) and _s3q.get("unlocked") is not False:
                combo.append({"type": "skill", "id": 3, "mode": "tap"})
            if combo:
                for c in combo:
                    if c.get("type") == "skill":
                        cooldowns[f"skill{c['id']}_t"] = now
                    else:
                        cooldowns["summoner_t"] = now
                cooldowns["skill"] = now
                cooldowns["hook_pending"] = 0.0   # 消耗掉
                return {"type": "combo", "actions": combo, "reason": "hook_confirmed_combo"}
        # 勾未中: 只用一技能消耗(不再接三技能/召唤师/攻击), 一技能进入范围即放
        if hook_missed:
            cooldowns["hook_pending"] = 0.0
            if ready("skill1_t", SKILL1_THROTTLE_S) and _nq is not None \
                    and _dq <= SKILL1_RANGE_FRAC and _s1q.get("unlocked") is not False:
                cooldowns["skill1_t"] = now
                cooldowns["skill"] = now
                return {"type": "skill", "id": 1, "mode": "tap", "reason": "hook_missed_harass"}
        # v10.5 用户铁律: 大招只在勾中 combo! 这里绝无先手大招
        in_ult = now - float(cooldowns.get("skill3_t", 0.0)) <= 3.5
        # v2.98 出钩铁律: yolo 敌英框 + 二技能范围内才出钩; 纯红条(暴君)不出钩
        if not in_ult and _nq is not None and _dq <= SKILL2_RANGE_FRAC \
                and ready("skill2_t", SKILL2_THROTTLE_S) \
                and _s2q.get("unlocked") is not False:
            cooldowns["skill2_t"] = now
            cooldowns["skill"] = now
            cooldowns["hook_pending"] = now
            cooldowns["hook_anchor_dist"] = _dq
            return {"type": "skill", "id": 2, "mode": "tap",
                    "reason": "enemy_in_skill2_range"}
        # 一技能消耗(平时也允许, 敌英在一技能范围内)
        if _engage_ok and not in_ult and ready("skill1_t", SKILL1_THROTTLE_S) \
                and _nq is not None and _dq <= SKILL1_RANGE_FRAC \
                and _s1q.get("unlocked") is not False and _has_yolo_eh_q:
            cooldowns["skill1_t"] = now
            cooldowns["skill"] = now
            return {"type": "skill", "id": 1, "mode": "tap", "reason": "enemy_hero_near"}
        # v2.65 ②敌人存在即普攻
        if _engage_ok and ready("attack_t", ATTACK_THROTTLE_S) and _has_yolo_eh_q:
            cooldowns["attack_t"] = now
            return {"type": "attack", "priority": "free", "reason": _engage_reason}
        # v2.71 鎶鑳介棬鑷洕(1s鑺傛祦): 鏈夋晫璇佹嵁鍗存湭鍑轰换浣曟妧鑳芥椂鎵撳嵃鍏抽敭閲        if enemies or enemy_bars:
            _sg = cooldowns.get("sg_t", 0.0)
            if now - float(_sg) > 1.0:
                cooldowns["sg_t"] = now
                print(f"[SkillGate] enemies={len(enemies)} "
                      f"reds={len(_mmr_pts)} d_red={_d_red_near:.2f} dq={_dq:.2f} "
                      f"in_ult={in_ult} s2={_s2q.get('unlocked', '')} "
                      f"s3={_s3q.get('unlocked', '')} s1={_s1q.get('unlocked', '')} "
                      f"hook_pending={round(now - float(cooldowns.get('hook_pending', 0)), 1)}")

    # ---- v10.11 低血回家(用户: 没血必须回家!): HP<25%或MP<20% -> 必撤向泉水(敌近也撤) ----
    if _hpq < 0.25 or _mpq < 0.20:
        _camp_hp = cooldowns.get("camp") or "blue"
        _fx_hp, _fy_hp = FOUNTAIN_DIR_BLUE if _camp_hp == "blue" else FOUNTAIN_DIR_RED
        _near_pool = [s for s in (enemies + minions + monsters)
                      if dist_width(s[0], s[1]) < 0.45]
        if _near_pool:
            # 残血 + 敌近: 先退到安全位置(远离敌人, 向后退) - 不是回泉水!
            _esc = min(_near_pool, key=lambda s: dist_width(s[0], s[1]))
            _ax_hp, _ay_hp = _away_map(state_dict, _esc[0], _esc[1], k=0.22)
            return {"type": "map_move", "nx": _ax_hp, "ny": _ay_hp,
                    "reason": "low_hp_fallback_safe"}
        # 残血 + 安全: 回城(自动回泉水), 满血后回来
        if _recall_clear and ready("recall_t", 20.0):
            cooldowns["recall_t"] = now
            cooldowns["low_hp_recall_t"] = now   # 标记(回城后满血再出)
            return {"type": "recall", "reason": "low_hp_safe_recall_15"}

    # ---- 0.5) 浣庤閲忔挙閫堢敤鎴疯鍒欙級欻P<20% 鍥炶嚜瀹跺涓嬪啀鍥炲煄 ----
    hp = float((state_dict.get("ui") or {}).get("hp") or 1.0)
    if hp < LOW_HP_FRAC:
        dangerous = any(dist_width(s[0], s[1]) < DANGER_FRAC
                        for s in (enemies + minions))
        camp = cooldowns.get("camp") or "blue"
        fx, fy = FOUNTAIN_DIR_BLUE if camp == "blue" else FOUNTAIN_DIR_RED
        # v2.5氭畫琛鍥炲煄瑕佸湪鑷濉斾笅堣嚜瀹跺涓績 < 0.35 灞忓夋墠瀹夊叏鍥炲煄
        nt_ally = nearest(ally_turrets)
        under_ally_tower = (nt_ally is not None
                            and dist_width(nt_ally[0], nt_ally[1]) < ALLY_TOWER_RECALL_FRAC)
        if _recall_clear and not dangerous and under_ally_tower \
                and ready("recall_t", RECALL_THROTTLE_S):
            cooldowns["recall_t"] = now
            return {"type": "recall", "reason": "low_hp_safe_recall"}
        # 涓嶅湪濉斾笅 -> 鏈嚜瀹跺璧帮紙鏃犲鍙鍒欐湞娉夋按> 忓湴鍥剧偣鍑 鑷娉夌偣
        # 残血安全: 回城(自动回泉水), 满血再出
        if _recall_clear and ready("recall_t", RECALL_THROTTLE_S):
            cooldowns["recall_t"] = now
            return {"type": "recall", "reason": "low_hp_safe_recall"}

    # ---- 0.6) 鎭閿紙鐢埛瑙勫垯夛細HP 鎴MP < 80% 涓旇韩杈瑰畨鍏鈫鎸夋仮澶嶉敭 ----
    ui = state_dict.get("ui") or {}
    mp = float(ui.get("mp") or 1.0)
    if (hp < LOW_RESOURCE_FRAC or mp < LOW_RESOURCE_FRAC) \
            and ready("restore_t", RESTORE_THROTTLE_S):
        near_danger = any(dist_width(s[0], s[1]) < DANGER_FRAC
                          for s in (enemies + minions + monsters))
        if not near_danger:
            cooldowns["restore_t"] = now
            return {"type": "restore", "reason": "low_resource_safe_restore"}

    # ---- 1) 濉旇閬匡細鏁屾柟濉斿湪杩戝鍙涓旀棤鎴戞柟忓叺(ally_minion) 鈫涓嶈繘鍏鏀诲嚮鑼冨洿 ----
    # v7.1 历史: 开局 8s 禁用 / 无 ally_minion 要求 (v6.0 用户: 被塔打了要撤, 不等兵线)
    # v16.0 P0-F1: 敌塔不再来自 YOLO 塔框（屏幕空间判据 + "自家基地建筑"过滤都已删）,
    #   改由小地图分阵营塔 towers_v7.enemy + 自身 self_est 的**小地图距离**判定。
    # P0-F1: 敌塔改由小地图分阵营塔给出（YOLO 塔类已被 v8.0 屏蔽 -> 旧 screen 空间判据是死代码）
    _turs_f = _mm_enemy_towers(state_dict)
    _s_est = _mm_self_est(state_dict, cooldowns)
    _d_tur = min([math.hypot(t[0] - float(_s_est[0]), t[1] - float(_s_est[1]))
                  for t in _turs_f], default=None)
    _match_age = now - float(cooldowns.get("match_start_t", 0.0))
    nt = min(_turs_f, key=lambda t: math.hypot(t[0] - float(_s_est[0]),
                                               t[1] - float(_s_est[1]))) if _turs_f else None
    turret_threat = bool(_match_age > 8.0 and _d_tur is not None
                         and _d_tur < TURRET_MM_THREAT)
    # v8.0 被塔打 = 血条急剧下降(用户铁律): 0.6s 内 hp 降 >0.18 -> 塔/单位重击 -> 强撤
    #   不依赖阵营塔判断(塔/小兵已屏蔽), 直接看血量骤降(被打的信号)
    _hp_prev_t = float(cooldowns.get("hp_prev_t", 0.0))
    _hp_prev_v = float(cooldowns.get("hp_prev_v", 1.0))
    if _hp_prev_t and now - _hp_prev_t <= 0.6 and _hp_prev_t > 0:
        _hp_drop = _hp_prev_v - _hpq
        if _hp_drop > 0.18 and _hpq < 0.85:
            cooldowns["escape_t"] = now
            cooldowns["hp_drop_t"] = now   # 标记血条急降(下方行为层读)
    cooldowns["hp_prev_t"] = now
    cooldowns["hp_prev_v"] = _hpq
    # v8.0 血条急降执行: 无论有无塔, 血条骤降=被重击 -> 远离最近敌人(塔/英雄不分)
    _hp_drop_now = (now - float(cooldowns.get("hp_drop_t", 0.0))) < 1.2
    if _hp_drop_now and _hpq < 0.85:
        _esc_target = None
        _pool_esc = list(enemies)          # 屏内敌人优先（screen 空间距离才有意义）
        if _pool_esc:
            _esc_target = min(_pool_esc, key=lambda s: dist_width(s[0], s[1]))
        elif nt is not None:               # P0-F1: 屏内无敌人 -> 朝最近敌塔反向撤（塔伤也是伤）
            _esc_target = nt
        if _esc_target is not None:
            _ax3, _ay3 = _away_map(state_dict, _esc_target[0], _esc_target[1], k=0.20)
            return {"type": "map_move", "nx": _ax3, "ny": _ay3,
                    "reason": "hp_drop_escape"}
    if turret_threat:
        tx, ty = nt[0], nt[1]
        # P0-F1: 距敌塔 < TURRET_MM_ESCAPE = 已在火力圈内 -> 立刻反向逃
        # inside turret fire -> escape
        if _d_tur is not None and _d_tur < TURRET_MM_ESCAPE:
            cooldowns["escape_t"] = now   # P0-F1: 在敌塔火力圈内 -> 强撤
            cooldowns["turret_esc"] = (tx, ty)   # 记住从哪座塔逃，供下方 1.5s hold 用
            ax, ay = _away_map(state_dict, tx, ty)
            return {"type": "map_move", "nx": ax, "ny": ay, "reason": "escape_turret"}
        ne = nearest(enemies)
        if ne is None:
            # 无敌人时也不朝敌方方向走（最远离敌方方向）
            ax, ay = _away_map(state_dict, tx, ty)
            return {"type": "map_move", "nx": ax, "ny": ay, "reason": "avoid_turret"}
        # 鏈夋晫浜烘椂氳嫢鏁屼汉涓庡鍚屽悜涓斿濞佽儊屼粛鍙挬堥挬瀛愪笉杩涘満夛紝绉诲姩鏂瑰悜淇濇寔浣嗘爣璁        cooldowns["turret_threat"] = 1.0
    else:
        cooldowns["turret_threat"] = 0.0
    # v2.15 历史: 逃出敌塔后 1.5s 内即使"塔威胁"判定短暂消失也继续远离（真出圈为止）
    _tesc = cooldowns.get("turret_esc")   # P0-F1: 只对"真的从敌塔逃过"的拍保持 1.5s 远离
    if _tesc and now - float(cooldowns.get("escape_t", 0.0)) < 1.5:
        tx, ty = float(_tesc[0]), float(_tesc[1])
        ax, ay = _away_map(state_dict, tx, ty)
        return {"type": "map_move", "nx": ax, "ny": ay, "reason": "escape_turret_hold"}

    # ---- 2) 浜屾妧鑳斤紙閽瓙夛細鏁屾柟鑻遍泟妗嗘垨鏁屼汉绾潯鍦寖鍥村唴鍗抽挬坴2.15: 绾潯=鍙鏁岃嫳闆勯搧璇侊級----
    skill_states = (state_dict.get("ui") or {}).get("skill_states") or {}
    # v2.12 MP 绠＄悊歁P < 20% 鏃剁鐢妧鑳斤紙鐪佽摑夛紝鍙敤鏅敾
    mp = float((state_dict.get("ui") or {}).get("mp") or 1.0)
    MP_SAVE_THRESHOLD = 0.20
    mp_saving = mp < MP_SAVE_THRESHOLD
    # hook signal lives only from screen enemies
    hook_signal = list(enemies)
    _mmr0 = ((state_dict.get("minimap") or {}).get("dots") or {}).get("red") or []
    if not _mmr0:
        pass
    # v2.20 开团血线
    hp = float((state_dict.get("ui") or {}).get("hp") or 1.0)
    # v2.31 hook signal
    if hook_signal and not mp_saving and hp >= SKILL_HP_MIN:
        ne = nearest(hook_signal)
        d = dist_width(ne[0], ne[1])
        if d <= SKILL2_RANGE_FRAC and ready("skill2_t", SKILL2_THROTTLE_S):
            # 鎶鑳借閿佹鏌紙v2.5夛細鏈閿侊紙鐏版殫変笉閲婃斁
            s2 = skill_states.get("2") or {}
            if s2.get("unlocked") is False:
                pass   # locked -> skip hook
            else:
#                # 閽瓙閬尅妫鏌紙鐢埛瑙勫垯 007夛細鏁屼汉杩炵嚎"绮楃洿绾涓婃湁鏁屽叺/閲庢-> 涓嶉挬
                blocked = _hook_path_blocked(
                    ne, minions + monsters, HOOK_BLOCK_FRAC, dist_width, aspect)
                if _engage_ok and not blocked:
                    cooldowns["skill2_t"] = now
                    cooldowns["skill"] = now
                    cooldowns["hook_pending"] = now
                    cooldowns["hook_anchor_dist"] = d
                    return {"type": "skill", "id": 2, "mode": "tap",
                            "reason": "enemy_in_skill2_range"}
                cooldowns["hook_blocked"] = 1.0
            cooldowns["hook_blocked"] = cooldowns.get("hook_blocked", 0.0)
        else:
            cooldowns["hook_blocked"] = 0.0

    # ---- 4) 绉诲姩氭寔缁嫋鍔----
    # v2.7 鐩爣婊炲洖氱洰鏍囬绻佽烦鍙橈紙钃濈偣鍣０夊鑷翠贡杞-> 鐩爣闇绋冲畾 HOLD_S 绉掓墠鍒囨崲
    TARGET_HOLD_S = 1.5
    if enemies and hp < CHASE_BREAK_FRAC:
        camp = cooldowns.get("camp") or "blue"
        fx, fy = FOUNTAIN_DIR_BLUE if camp == "blue" else FOUNTAIN_DIR_RED
        target = (fx, fy)
        reason = "stop_chase_low_hp_retreat"
        if now - float(cooldowns.get("skill2_t", 0.0)) < HOOK_FLIGHT_S:
            return {"type": "none", "reason": "hook_flight"}
        return {"type": "map_move", "nx": fx, "ny": fy, "reason": reason}
    target = None
    reason = None
    if enemies:
        ne = nearest(enemies)
        # v2.21 鎺垬寮曟搸氭晫浜哄湪 0.35 灞忓鍐-> 浼樺厛璐磋韩缂犳枟(绉诲姩+鏀诲嚮閿繛鍙
        d_ne = dist_width(ne[0], ne[1])
        if _engage_ok and d_ne < ENGAGE_FRAC and ready("attack_t", ATTACK_THROTTLE_S):
            cooldowns["attack_t"] = now
            return {"type": "attack", "priority": "free", "reason": _engage_reason}
        target = (ne[0], ne[1])
        reason = "chase_enemy"
    else:
        # v8.2 用户铁律: 追血条=负收益(-0.48)且小地图红点不可信 -> 绝不因红点去追!
        #   仅当屏内已有真敌英(yolo enemy_hero)时, 血条追可作为持续跟踪; 无屏内敌英=不追
        mm0 = state_dict.get("minimap") or {}
        mm_red0 = (mm0.get("dots") or {}).get("red") or [] if mm0.get("found") else []
        bar_hold = now - float(cooldowns.get("bar_chase_t", 0.0)) < 1.5
        if enemy_bars and enemies:
            # 有屏内敌英才允许血条追踪(近敌视野内), 且追踪方向=敌英方向(非红点)
            nearest_bar = min(enemy_bars, key=lambda b: math.hypot(
                b[0] / w - 0.5, (b[1] / h - 0.5) * (h / w)))
            bx, by = nearest_bar[0] / w, nearest_bar[1] / h
            _ne = nearest(enemies)
            if _ne is not None:
                bx, by = _ne[0], _ne[1]   # 用真敌英位置(修正血条坐标偏差)
            if 0.0 <= bx <= 1.0 and 0.0 <= by <= 1.0:
                target = (bx, by)
                reason = "chase_enemy_bar"
                cooldowns["bar_chase_t"] = now
                cooldowns["bar_target"] = (bx, by)
        elif bar_hold and cooldowns.get("bar_target") and enemies:
            target = tuple(cooldowns["bar_target"])
            reason = "chase_enemy_bar"
        if target is None:
            # movement decision
            camp = cooldowns.get("camp") or "blue"
            lane = LANE_DIR_BLUE if camp == "blue" else LANE_DIR_RED
            mm = state_dict.get("minimap") or {}
            mm_blue, mm_red = [], []
            if mm.get("found"):
                mm_blue = (mm.get("dots") or {}).get("blue") or []
                mm_red = (mm.get("dots") or {}).get("red") or []

            # opening safe: first 6s follow lane
            match_t = float(cooldowns.get("match_start_t", 0.0))
            in_opening = match_t > 0 and now - match_t < 6.0
            # v2.33 鎬佸娍闂帶(瀛敤鎴: 鍧囧娍鏁墦(0.55)/鍔ｅ娍榫熺缉(0.65)/浼樺娍鍘嬭蛋(0.60); 寮灞鏇寸
            _u_eng_hp = 0.55 if len(mm_blue) == len(mm_red) else (
                0.65 if len(mm_red) > len(mm_blue) else 0.60)
            if in_opening:
                _u_eng_hp = max(_u_eng_hp, 0.70)
            if len(mm_red) >= 6:      # enemy herd: no support
                _u_eng_hp = 1.01
            # revive 5s: follow only
            if now - float(cooldowns.get("revive_t", 0.0)) < 5.0 and cooldowns.get("revive_t"):
                _u_eng_hp = 1.01
            # v2.39 杈呭姪璺嚎(鐢埛: 涓笅璺悇45%): 涓嬭矾璧板粖(钃濇柟鍙充笅/绾柟宸笂) 浼樺厛,
            # (comment)
            _mid_c = (0.5, 0.58) if camp == "blue" else (0.5, 0.42)
            def _corridor(p, c, k=0.30):
                return math.hypot(p[0] - c[0], p[1] - c[1]) <= k
            def _pick_lane(down_only=None):
                return None
            _camp0 = cooldowns.get("camp") or "blue"
            _down_c = _lane_center(cooldowns)   # 与 P1 push_target 同一出处，防两处漂移
            _mid_c = (0.5, 0.58) if _camp0 == "blue" else (0.5, 0.42)
            na = nearest(allies)
            # v3.2 支援=跟随队友(用户铁律): 小地图红点不可作为支援目标(检测不准);
            #      队友蓝点在身边(走廊内/任意)就去队友处; 只有完全无蓝点才考虑走廊红点支援
            # v10.3 用户铁律: 支援=射手路(蓝方=下路/红方=上路), 射手在哪跟哪, 不去对抗路/河道
            if not in_opening:
                self_g = (mm.get("dots") or {}).get("green") or []
                myx, myy = (self_g[0][:2] if self_g else (0.5, 0.5))
                # 1) 射手走廊内队友蓝点(射手在哪跟哪) - 只跟射手走廊的队友
                _pool_blue = [p for p in mm_blue if _corridor(p, _down_c, 0.35)]
                if _pool_blue:
                    _trb = min(_pool_blue, key=lambda p: (p[0]-myx) ** 2 + (p[1]-myy) ** 2)
                    target = (_trb[0], _trb[1])
                    reason = "support_dps_lane"
                    # v16.0 E（设计 docs/ARCH_PUSH_PRIORITY.md）: 支援帧上也评一次推进门。
                    #   E-P0=只记录不采用；E-P1=`--push on` 且 e_ok 时接管 —— 目标从"队友脚下"
                    #   换成"推着线走"（队友前方的兵线 / 敌塔塔外站位）。仍在射手路走廊内、
                    #   仍贴着队友（gap<=PUSH_STAY_WITH_ALLY），所以没违背"射手在哪跟哪"。
                    _pem = cooldowns.get("push_mode") or "off"
                    if _pem != "off":
                        _fie = _push_eval(state_dict, cooldowns, "support", (_trb[0], _trb[1]))
                        if _pem == "on" and _fie.get("e_ok"):
                            target = tuple(_fie["target"])
                            reason = _fie["reason"]
                            _fie["used"] = True      # push_info 与返回值同一个 dict
                else:
                    # 2) 射手走廊内红点(敌人在射手路) -> 支援那里
                    _pool_red = [p for p in mm_red if _corridor(p, _down_c, 0.35)]
                    if _pool_red and hp >= _u_eng_hp:
                        tr = min(_pool_red, key=lambda p: (p[0]-myx)**2 + (p[1]-myy)**2)
                        d_red = math.hypot(tr[0]-myx, tr[1]-myy) if tr else 9.9
                        if d_red < 0.60:
                            target = (tr[0], tr[1])
                            reason = "support_dps_lane_red"
                    # 2.5) v16.0 P2 推进(Push Policy): 占优且安全时朝"最前兵线/最近敌塔(塔外站位)"压线。
                    #   只在 target is None 时才有机会接管 -> 支援/追敌/逃生等高优先级动作照旧优先。
                    #   条件不成立时行为与 --push off 完全一致（这是本方案最重要的安全性质）。
                    _pmode = cooldowns.get("push_mode") or "off"
                    if target is None and _pmode != "off":
                        _fi = _push_eval(state_dict, cooldowns, "fallback")
                        if _pmode == "on" and _fi["ok"] and _fi["target"] is not None:
                            target, reason = tuple(_fi["target"]), _fi["reason"]
                            _fi["used"] = True          # push_info 与返回值是同一个 dict
                        elif _pmode == "shadow" \
                                and now - float(cooldowns.get("push_log_t", 0.0)) > 1.0:
                            cooldowns["push_log_t"] = now
                            print(f"[push-shadow] {'会推' if _fi['target'] else '挡'} {_fi['why']}"
                                  + (f" -> {_fi['reason']} {_fi['target']}"
                                     if _fi["target"] else "")
                                  + f" (蓝{len(_mm_dots(state_dict, 'blue'))}"
                                    f"/红{len(_mm_dots(state_dict, 'red'))} hp{hp:.2f})")
                    # 3) 无队友/无敌人也去射手推进线(不空转/不等分)
                    if target is None:
                        _push = (_down_c[0] + (0.10 if _camp0 == "blue" else -0.10),
                                 _down_c[1] + (0.10 if _camp0 == "blue" else -0.10))
                        target = (_push[0], _push[1])
                        reason = "dps_lane_push"
            if target is None and not in_opening and hp < _u_eng_hp:
                na_tmp = nearest(allies)
                if na_tmp is not None:
                    target = (na_tmp[0], na_tmp[1])
                    reason = "follow_ally_lowhp"
            # v10.8 删旧跟队覆盖块: DPS路支援(上方)是唯一支持逻辑, 旧v3.0/v3.1覆盖已删除
            if target is None and not in_opening:
                _sg3 = (mm.get("dots") or {}).get("green") or []
                _px3, _py3 = (_sg3[0][:2] if _sg3 else (0.5, 0.5))
                # 无走廊队友/敌人 -> 射手推进线
                _push = (_down_c[0] + (0.10 if _camp0 == "blue" else -0.10),
                         _down_c[1] + (0.10 if _camp0 == "blue" else -0.10))
                target = (_push[0], _push[1])
                reason = "dps_lane_push"
    # v3.2 用户兜底铁律: 决策不明时跟随队友去清兵线(不空转/不河道游荡)
    if reason == "lane_develop" or reason == "lane_develop_hold":
        if ally_minions:
            _am = min(ally_minions, key=lambda s: dist_width(s[0], s[1]))
            target = (_am[0], _am[1])
            reason = "follow_ally_clearlane"
        elif mm_blue:
            _sgf = (mm.get("dots") or {}).get("green") or []
            _fx, _fy = (_sgf[0][:2] if _sgf else (0.5, 0.5))   # v5.1 模型注入点含conf
            _nbf = min(mm_blue, key=lambda p: (p[0]-_fx)**2 + (p[1]-_fy)**2)
            target = (_nbf[0], _nbf[1])
            reason = "follow_ally_minimap"
    if target is None:
        return {"type": "none", "reason": "no_target"}
    # v2.7 鐩爣婊炲洖氳摑鐐瑰櫔澹板鑷寸洰鏍囨瘡甯烦鍙-> 鐩爣闇绋冲畾 HOLD_S 绉掓墠鍒囨崲
    # 堜粎瀵瑰皬鍦板浘绫荤洰鏍囩敓鏁堬紱chase_enemy 杩芥晫浜轰笉婊炲洖岄伩鍏嶈拷涓級
    if reason in ("follow_ally_minimap", "support_red_centroid", "support_red_nearest",
                  "support_ally_engaged", "lane_develop", "follow_ally_lowhp",
                  "push_follow_wave", "push_lane_tower"):
        prev = cooldowns.get("nav_target")
        prev_t = float(cooldowns.get("nav_target_t", 0.0))
        if prev is not None:
            dist = math.hypot(target[0] - prev[0], target[1] - prev[1])
            if dist > 0.12 and now - prev_t < TARGET_HOLD_S:
                # 鐩爣璺冲彉浣嗘湭鍒板垏鎹湡 -> 缁存寔鏃洰鏍囷紙鍒锋柊鏃堕棿鎴筹紝閬垮厤姘镐箙 hold                target = tuple(prev)
                reason = f"{reason}_hold"
                cooldowns["nav_target"] = tuple(target)
                cooldowns["nav_target_t"] = now
            else:
                cooldowns["nav_target"] = tuple(target)
                cooldowns["nav_target_t"] = now
        else:
            cooldowns["nav_target"] = tuple(target)
            cooldowns["nav_target_t"] = now
    # 閽瓙椋炶鏈熷仠姝鍔紙绛夊嬀涓粨鏋滐紝闃茶嚜宸辫蛋杩戣鍒繛鎷涳級
    if now - float(cooldowns.get("skill2_t", 0.0)) < HOOK_FLIGHT_S:
        return {"type": "none", "reason": "hook_flight"}
    # v2.27 忓湴鍥剧偣鍑荤鍔鐜嬭呰緟鍔鍔: 鍦板浘绫荤洰鏍囩洿鎺tap 忓湴鍥剧偣, 寮曟搸鑷姩瀵昏矾
    if reason in ("follow_ally_minimap", "support_red_centroid", "support_red_nearest",
                  "support_ally_engaged", "lane_develop", "follow_ally_lowhp",
                  "support_red_centroid_hold", "follow_ally_minimap_hold",
                  "support_red_nearest_hold", "support_ally_engaged_hold",
                  "lane_develop_path", "follow_ally_lowhp_hold",
                  "follow_ally_minimap_path", "support_red_nearest_path",
                  "support_ally_engaged_path", "lane_develop_hold",
                  "push_follow_wave", "push_lane_tower",
                  "push_follow_wave_hold", "push_lane_tower_hold",
                  "follow_ally_clearlane", "follow_ally",
                  "support_dps_lane", "support_dps_lane_red", "dps_lane_push"):
        return {"type": "map_move", "nx": max(0.02, min(0.98, target[0])),
                "ny": max(0.02, min(0.98, target[1])), "reason": reason}
    # dual-track movement
    STICK_REASONS = ("chase_enemy", "chase_enemy_bar", "follow_ally",
                     "escape_turret", "escape_turret_hold")
    mm = state_dict.get("minimap") or {}
    if reason in STICK_REASONS:
        theta = math.atan2(-(target[1] - 0.5) * aspect, target[0] - 0.5)
        return {"type": "move", "theta": theta, "r": MOVE_R,
                "duration_ms": MOVE_DURATION_MS, "reason": reason}
    # 鍏朵綑=忓湴鍥剧偣鍑 灞忓箷绫荤洰鏍囨槧勫埌鏈杩戠孩/钃濈偣; 閫閬垮鏄犲皠鍙嶅悜鐐 鎾=娉夌偣
    mmp_dots = (mm.get("dots") or {}) if mm.get("found") else {}
    greens = mmp_dots.get("green") or []
    blues = mmp_dots.get("blue") or []
    reds_ = mmp_dots.get("red") or []
    gx, gy = (greens[0][:2] if greens else (0.5, 0.5))
    nx, ny = target[0], target[1]
    if reason.startswith(("follow_ally_minimap", "support", "lane", "follow_ally_lowhp")):
        pass  # 已是地图坐标
    elif reason.startswith(("retreat",)):
        fx, fy = FOUNTAIN_DIR_BLUE if (cooldowns.get("camp") or "blue") == "blue" \
            else FOUNTAIN_DIR_RED
        nx, ny = fx, fy
    elif reason.startswith(("escape_turret", "avoid_turret")):
        tx, ty = nt[0], nt[1]
        ax, ay = _away_map(state_dict, tx, ty)
        nx, ny = ax, ay
    return {"type": "map_move", "nx": max(0.02, min(0.98, nx)),
            "ny": max(0.02, min(0.98, ny)), "reason": reason}


# ---------------------------------------------------------------------------
# 鎵杈呭姪堜粎涓诲惊鐜娇鐢紝淇濇寔 decide 绾嚱鏁帮級
# ---------------------------------------------------------------------------

def _is_operate_action(action: dict) -> bool:
    """操作层命令（type=operate 或官方命令名，如 MoveDir）。"""
    from wzry.action.operate_commands import is_operate_action
    return is_operate_action(action)


def update_cooldowns(action: dict, cooldowns: dict, now: float):
    """decoded docstring."""
    t = action.get("type")
    if t == "skill":
        sid = int(action.get("id", 0))
        cooldowns[f"skill{sid}_t"] = now
        cooldowns["skill"] = now
    elif t == "summoner":
        cooldowns["summoner_t"] = now
        cooldowns["skill"] = now
    elif t == "recall":
        cooldowns["recall_t"] = now
        cooldowns["skill"] = now
    elif t == "attack":
        cooldowns["attack_t"] = now
    elif t == "restore":
        cooldowns["restore_t"] = now
    elif t == "combo":
        for sub in action.get("actions", []):
            update_cooldowns(sub, cooldowns, now)
    elif _is_operate_action(action):
        from wzry.action.operate_commands import OperateType, command_of
        try:
            cmd = command_of(action)
        except Exception:
            return
        if cmd in (OperateType.POS_SKILL, OperateType.DIR_SKILL,
                   OperateType.OBJ_SKILL, OperateType.CHARGE_SKILL,
                   OperateType.USE_JOINT_SKILL):
            sid = action.get("id", action.get("skill_id"))
            try:
                sid = int(sid)
            except (TypeError, ValueError):
                sid = None
            if sid is not None:
                cooldowns[f"skill{sid}_t"] = now
            cooldowns["skill"] = now
        elif cmd in (OperateType.ATTACK_COMMON, OperateType.ATTACK_ACTOR,
                     OperateType.ATTACK_TO_POS):
            cooldowns["attack_t"] = now
        elif cmd is OperateType.MOVE_STOP:
            cooldowns.pop("_map_t", None)


def _policy_action_to_m2(pa: dict) -> dict:
    """BCNet 输出 -> m2 动作 dict（与 apply_action 口径一致）。

    move 走 **target 路径**：规则数据训出来的模型 target 有监督、theta 没有
    （theta 由执行器在真机上用 self_est 现算），所以交给 map_move 而不是直接给摇杆角。
    """
    a = pa.get("act_name")
    if a == "move":
        tgt = pa.get("target")
        if isinstance(tgt, (list, tuple)) and len(tgt) == 2:
            return {"type": "map_move", "nx": max(0.02, min(0.98, float(tgt[0]))),
                    "ny": max(0.02, min(0.98, float(tgt[1]))), "reason": "bc:target"}
        return {"type": "move", "theta": float(pa.get("theta", 0.0)),
                "r": float(pa.get("r", 0.5)), "duration_ms": MOVE_DURATION_MS,
                "reason": "bc:theta"}
    if a in ("skill1", "skill2", "skill3"):
        return {"type": "skill", "id": int(a[-1]), "mode": "tap", "reason": f"bc:{a}"}
    if a == "attack":
        return {"type": "attack", "priority": "free", "reason": "bc:attack"}
    if a in ("recall", "restore", "summoner"):
        return {"type": a, "reason": f"bc:{a}"}
    return {"type": "none", "reason": f"bc:{a}"}


def _policy_guard(cand: dict, state_dict: dict, cooldowns: dict, now: float):
    """BC 接管时的安全层（与规则栈同源）：幻影敌人过滤 + 接战门槛 + 回城门禁 + 冷却节流。

    模型输出**必须**过这一层；任何一项不满足就丢弃 -> 本拍退回规则动作。
    返回 (ok, reason)。
    """
    if not cand or cand.get("type") == "none":
        return False, "none/空动作"
    t = cand.get("type")
    ui = state_dict.get("ui") or {}
    hp = float(ui.get("hp") or 1.0)
    enemies = [u for u in (state_dict.get("units") or [])
               if u.get("cls") == "enemy_hero"
               and u.get("camp_src") != "default"
               and float(u.get("conf", 1.0)) >= 0.55]
    _hp_min = float(globals().get("ENGAGE_HP_MIN", 0.60))
    if t in ("skill", "attack"):
        if not enemies:
            return False, "无可信敌人(幻影过滤)"
        if t == "skill":
            sid = int(cand.get("id") or 0)
            thr = 3.0 if sid == 2 else 1.5
            if now - float(cooldowns.get(f"skill{sid}_t", 0.0)) <= thr:
                return False, f"skill{sid} 冷却节流"
        elif now - float(cooldowns.get("attack_t", 0.0)) <= 0.8:
            return False, "attack 节流"
        if hp < _hp_min:
            return False, f"接战门槛未过(hp={hp:.2f}<{_hp_min})"
    if t == "recall" and now - float(cooldowns.get("recall_t", 0.0)) <= 20.0:
        return False, "回城节流"
    if t == "map_move" and now - float(cooldowns.get("skill", 0.0)) < SKILL_DEBOUNCE_S:
        return False, "技能后防抖"
    return True, "ok"


def _async_result_detect(frame, reward):
    """decoded docstring."""
    import threading as _th

    def worker(fr):
        try:
            import subprocess as _sp
            import sys as _sys
            import cv2 as _cv2
            tmp = ROOT / "temp" / "result_live.png"
            _cv2.imwrite(str(tmp), fr)
            prompt = ("This is the WZRY match result screen. "
                      "If victory (gold text) answer victory. "
                      "If defeat answer defeat. "
                      "Otherwise answer unknown. "
                      "Output one word only.")
            r = _sp.run(
                [_sys.executable, "-X", "utf8",
                 str(ROOT / "scripts" / "train" / "modlens_ask.py"),
                 str(tmp), prompt],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=90)
            txt = (r.stdout or "").strip().lower()
            if "victory" in txt or "victory" in txt:
                sc = reward.on_event("victory")
                print(f"[{datetime.now():%H:%M:%S}] [反馈] 事件 victory {sc:+.0f} 分 "
                      f"(总分 {reward.total:.0f})")
            elif "defeat" in txt:
                sc = reward.on_event("defeat")
                print(f"[{datetime.now():%H:%M:%S}] [反馈] 事件 defeat {sc:+.0f} 分 "
                      f"(总分 {reward.total:.0f})")
        except Exception:
            pass

    _th.Thread(target=worker, args=(frame.copy(),), daemon=True).start()


def _rec_hook_measure(cooldowns: dict, state_dict: dict, action: dict, hit: bool):
    """decoded docstring."""
    try:
        import json
        from pathlib import Path
        now = float(state_dict.get("t") or time.time())
        dist = float(cooldowns.get("hook_anchor_dist", 0.0))
        rec = {"t": now, "dist_frac": round(dist, 4), "hit": hit,
               "reason": action.get("reason", "")}
        p = ROOT / "data" / "measure_hook.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _drive_parse_line(raw, prev_t=None, pace_max=5.0):
    """解析一行外部驱动输入（见 `--drive-stdin`）。返回 (action_or_None, wait_s, prev_t)。

    - action=None 表示该行应跳过（空行 / `#` 注释 / 非法 JSON / 无 `type` / `type=="none"`）。
    - 节奏来源优先级：行内 `delay_ms` > 行内 `t`（绝对秒，取与前一条的差值）> 不等待。
      用 `t` 是为了能**直接喂** `data/matches/*/actions.jsonl` —— 它的 schema 就是"动作 dict
      + t"。差值 clamp 到 [0, pace_max]，防第一条或中途长空档一次性睡很久。
    """
    s = (raw or "").strip()
    if not s or s.startswith("#"):
        return None, 0.0, prev_t
    try:
        obj = json.loads(s)
    except Exception:
        return None, 0.0, prev_t
    if not isinstance(obj, dict):
        return None, 0.0, prev_t

    wait = 0.0
    _d = obj.pop("delay_ms", None)
    _tf = None
    _t = obj.pop("t", None)
    if _t is not None:
        try:
            _tf = float(_t)
        except Exception:
            _tf = None
    if _d is not None:
        try:
            wait = max(0.0, float(_d) / 1000.0)
        except Exception:
            wait = 0.0
    elif _tf is not None and prev_t is not None:
        wait = min(max(_tf - float(prev_t), 0.0), float(pace_max))
    if _tf is not None:
        prev_t = _tf

    if not obj.get("type") or obj.get("type") == "none":
        return None, wait, prev_t
    return obj, wait, prev_t


def _drive_run(lines, put_action, is_idle=None, pace_max=5.0, is_done=None,
               sleep=time.sleep, idle_poll_s=0.005):
    """外部驱动主循环（`--drive-stdin` 的实现；抽出来是为了能离线单测）。

    lines      : 文本行可迭代对象（生产 = `sys.stdin`）
    put_action : 投递一条动作 dict 的回调（生产 = 把 `(action, cooldowns)` 放进执行队列）
    is_idle    : 无参回调，True 表示执行线程空闲；None = 不等（背压交给调用方）
    is_done    : 无参回调，True 表示应提前收尾；None = 不检查
    返回 (下发条数, 跳过条数, 首条被跳过的原文)

    "跳过"只统计**非空且非注释**却没被执行的行 —— 管道编码错/字段写错时的表现就是
    "什么都没发生"，必须让它可见，否则极难排查。
    """
    prev_t = None
    n = skip = 0
    first_skip = None
    for raw in lines:
        if is_done is not None and is_done():
            break
        obj, wait, prev_t = _drive_parse_line(raw, prev_t, pace_max)
        if obj is None:
            s = (raw or "").strip()
            if s and not s.startswith("#"):
                skip += 1
                if first_skip is None:
                    first_skip = s[:100]
            continue
        if wait > 0:
            sleep(wait)
        if is_idle is not None:
            while not is_idle():
                if is_done is not None and is_done():
                    break
                sleep(idle_poll_s)
        put_action(obj)
        n += 1
    return n, skip, first_skip


def apply_action(ex, action: dict, cooldowns: dict):
    """decoded docstring."""
    now = time.time()
    t = action.get("type")
    if t == "map_move":
        # v14.2 点小地图在真机不移动英雄(只移镜头) -> 改走摇杆推杆朝向目标。
        _nx, _ny = float(action.get("nx", 0.5)), float(action.get("ny", 0.5))
        _camp = cooldowns.get("camp") or "blue"
        _s = cooldowns.get("self_est")
        if not _s:
            _s = BASE_BLUE if _camp == "blue" else BASE_RED
        sx, sy = float(_s[0]), float(_s[1])
        dx, dy = _nx - sx, _ny - sy
        dist = math.hypot(dx, dy)
        if dist < 1e-9:                  # 与估计位置重合: 无法定方向, 直接跳过(防除零)
            return
        # v16.2 "到达即停"闸门(仅持久摇杆模式)。非 hold 模式不查, 保持原行为逐字节一致。
        #   仅在 dist<near 时才有意义 —— 到点后旧逻辑会重锚回基地继续推 -> 连续移动下
        #   变成从目标往外走(过冲); 这里改为到点即松手停住, 由保险丝兜防失真僵死。
        if dist < MAPMOVE_NEAR and ex.hold_enabled:
            _gate = cooldowns.get("_map_gate")
            if _gate is None:
                from wzry.decision.nav_gate import ArrivalGate
                _gate = ArrivalGate(MAPMOVE_NEAR, MAPMOVE_IDLE_MAX)
                cooldowns["_map_gate"] = _gate
            _g = _gate.decide(now, dist)
            if _g == "hold":
                ex.release_stick()                 # 到了就真松手(持久摇杆是物理按住)
                cooldowns.pop("_map_t", None)      # 停着不累计"走过了多久"
                return
            if _g == "fuse":
                print(f"[map_move] 到达闸门保险丝(第 {_gate.fuse_count} 次): 目标处停推超 "
                      f"{MAPMOVE_IDLE_MAX:g}s, 按旧逻辑重锚回基地恢复推进")
        if dist < MAPMOVE_NEAR:
            # v14.2 航位推算没有真实位置反馈: 估计位置一旦"到点"就再也离不开, 于是永远
            # 不推杆 -> 英雄站立不动(实测 180s 只发 19 次 swipe, 然后被连杀 4 次、0 击杀)。
            # 到点即重锚回本方基地, 下一拍继续朝目标推进(方向仍正确)。
            _sb = BASE_BLUE if _camp == "blue" else BASE_RED
            cooldowns["self_est"] = _sb
            sx, sy = float(_sb[0]), float(_sb[1])
            dx, dy = _nx - sx, _ny - sy
            dist = math.hypot(dx, dy)
        dur = int(max(MAPMOVE_MIN_MS, min(MAPMOVE_MAX_MS, dist / STICK_SPEED * 1000)))
        _theta = math.atan2(-dy, dx)     # 世界 Δ -> 摇杆方向(0=右, 逆时针)
        if ex.hold_enabled:
            # v16.2 持久摇杆: 按住不放 + 每次只更新朝向。单条 ≈20ms, 不必等上一次 dur
            #   走完(原 swipe 守卫会因 dur 过长而跳拍); 这里只限一个最小间隔防同拍抖发。
            #   收益: 松手即停消失(移动连续) + worker 占用从 200~1000ms 降到 ≈20ms。
            _prev_t = cooldowns.get("_map_t")
            if now - float(cooldowns.get("_map_t", -9.9)) < MAPMOVE_HOLD_GAP:
                return
            ex.move_hold(_theta, MOVE_R)
            # v16.2 航位推算改按**实际经过时间**推进。旧实现用 dur 名义值, 在 swipe 模式下
            #   因"两次推杆间隔 ≥ dur"的守卫而自洽; 持久摇杆去掉了该守卫, 间隔变成主循环
            #   周期(≈0.53s)而 dur 恒被钳到上限 900ms -> 估计前进量约为实际的 1.7 倍,
            #   会让战术层(接战距离/塔威胁/逃生判据)以为自己离目标更近、离塔更远。
            _el = 0.0 if _prev_t is None else min(max(now - float(_prev_t), 0.0), MAPMOVE_EL_MAX)
        else:
            if now - float(cooldowns.get("_map_t", -9.9)) < dur / 1000.0:
                return                       # 上一次推杆尚未结束: 避免 swipe 叠发
            ex.move(_theta, MOVE_R, dur)
            _el = dur / 1000.0               # 非 hold: 英雄确实走了 dur, 口径不变
        cooldowns["_map_t"] = now
        _step = STICK_SPEED * _el            # 航位推算: 自己前进 step
        cooldowns["self_est"] = (max(0.02, min(0.98, sx + dx / dist * _step)),
                                 max(0.02, min(0.98, sy + dy / dist * _step)))
    elif t == "move":
        # v16.2 与 map_move 一致: 持久摇杆模式下改走"按住不放 + 只改朝向"。
        #   原实现恒走 swipe -> ①每次都会松手(追击/逃生时最要命) ②worker 被占 450ms,
        #   其间决策出来的动作(含攻击/技能)会被丢弃。无多点能力时自动回退 swipe。
        _th = float(action.get("theta", 0.0))
        _rr = float(action.get("r", MOVE_R))
        if not ex.move_hold(_th, _rr):
            ex.move(_th, _rr, int(action.get("duration_ms", MOVE_DURATION_MS)))
    elif t == "skill":
        # v16.2 透传 target/duration: 原先只传 (id, mode) -> mode="drag" 必抛
        #   ValueError(需要 target) 且被 worker 吞成 "[exec] 异常(跳过)", 等于手动瞄准
        #   这条路从决策层根本走不通。target 必须与技能按钮同空间(帧坐标 1280x720)。
        #   `or 150`: duration 显式给 null 时用默认值, 避免 int(None) 抛 TypeError。
        ex.skill_cast(int(action.get("id", 0)), action.get("mode", "tap"),
                      action.get("target"), int(action.get("duration") or 150))
    elif t == "summoner":
        ex.release_stick()                # v16.2 持久摇杆: 先松手, 否则英雄在走(施法/回城需站定)
        cooldowns.pop("_map_t", None)     # 已停住: 航位推算不该把这段"静止时间"算成走过
        ex.summoner()
    elif t == "recall":
        ex.release_stick()                # 回城读条会被移动打断 -> 必须先松摇杆
        cooldowns.pop("_map_t", None)
        ex.recall()
    elif t == "attack":
        ex.attack(str(action.get("priority", "free")))
    elif t == "restore":
        ex.release_stick()
        cooldowns.pop("_map_t", None)
        ex.restore()
    elif t == "combo":
        for i, sub in enumerate(action.get("actions", [])):
            apply_action(ex, sub, cooldowns)
            if i < len(action["actions"]) - 1:
                time.sleep(0.12)   # combo interval
    elif _is_operate_action(action):
        from wzry.action.operate_commands import execute_operate
        execute_operate(ex, action, cooldowns)
    elif t != "none":
        # v16.2 可见性: 未知 type 原先会**静默 no-op**(本函数无 else 兜底), 在日志上与
        #   "这拍确实无事可做"无法区分 —— 拼错 type 的唯一表现就是"英雄没反应", 极难排查。
        #   这里只加可见性(计数 + 5s 节流打印), **行为不变**: 未知 type 依旧不执行任何动作。
        #   `none` 是文档化的合法占位类型, 不在此列(不计不打印)。
        cooldowns["_act_unknown"] = int(cooldowns.get("_act_unknown", 0)) + 1
        if now - float(cooldowns.get("_unknown_log_t", -9.9)) > 5.0:
            cooldowns["_unknown_log_t"] = now
            print(f"[exec] 未知动作 type={t!r} 已忽略(累计 {cooldowns['_act_unknown']} 次); "
                  f"支持: map_move/move/skill/attack/summoner/recall/restore/combo/none/"
                  f"operate(官方命令名 MoveDir/PosSkill/...)")
    update_cooldowns(action, cooldowns, now)


# ---------------------------------------------------------------------------
# v14.3 运行时塔重锚: 把 map_move 的开环航位推算变成"每经过一座塔纠一次偏"的闭环
# ---------------------------------------------------------------------------
TOWER_SNAP_FRAC = 0.16        # YOLO 塔框底边(塔基)距英雄屏幕锚点 < 该值 => 英雄就在这座塔上
TOWER_SNAP_MAX_ERR = 0.25     # 估计位置离该塔超过这个距离就不吸附(防吸错塔; 约等于一条路的塔距)
SNAP_ALREADY_EPS = 0.02       # v15.1 已在塔上(=误差极小) -> 视为无需纠偏，直接退出
SNAP_SAME_TOWER_COOLDOWN_S = 6.0   # v15.1 同一座塔重复吸附的冷却（偏差不够大时不重复吸）


def _snap_self_est_to_tower(frame, dets, mm, cooldowns, w=1280.0, h=720.0):
    """英雄走到某座塔上时, 把航位推算的 self_est 吸附到该塔的小地图坐标(闭环纠偏)。

    为什么需要: map_move 用 STICK_SPEED 航位推算"自己在哪", STICK_SPEED 只要不准(或
      中途撞墙/被拉回), 估计就越漂越远 -> 目标方向算错 -> 英雄原地打转/撞墙。真机实测
      小地图绿点恒定在泉水图标处, **不随英雄移动**(满杆推 15.3s 绿点只挪 0.048), 所以
      自身位置无法直接观测, 只能在"经过已知塔"时校正。
    判据: YOLO 塔框的底边(塔基落地处)贴到英雄屏幕锚点(self_hp_mp 的 hero_pos, 王者镜头
      把英雄放在屏幕中部偏下, 不是屏幕中心); 且该塔在小地图上仍存活(MMTrackerV7 的
      towers 只输出存货塔)。吸附目标 = 存活塔里离当前估计最近的那座。
    返回 (x, y, 估计误差) 便于日志核对; 未命中返回 None。
    """
    if not (mm and mm.get("found")):
        return None
    towers = mm.get("towers") or []
    if not towers:
        return None
    try:
        from wzry.vision.self_bars import self_hp_mp
        _hp, _mp, _pos = self_hp_mp(frame)
    except Exception:
        return None
    if not _pos:
        return None
    ax, ay = _pos[0] / w, _pos[1] / h
    hit = None
    for d in (dets or []):
        if str(getattr(d, "cls", "")) not in ("enemy_turret", "ally_turret"):
            continue
        bx = (d.xyxy[0] + d.xyxy[2]) / 2.0 / w
        by = d.xyxy[3] / h                     # 塔框底边 = 塔基
        dist = math.hypot(bx - ax, (by - ay) * (h / w))
        if dist < TOWER_SNAP_FRAC and (hit is None or dist < hit[0]):
            hit = (dist, bx, by)
    if hit is None:
        return None
    est = cooldowns.get("self_est")
    if not est:
        est = BASE_BLUE if (cooldowns.get("camp") or "blue") == "blue" else BASE_RED
    t = min(towers, key=lambda p: math.hypot(p[0] - est[0], p[1] - est[1]))
    d_est = math.hypot(t[0] - est[0], t[1] - est[1])
    if d_est > TOWER_SNAP_MAX_ERR:
        return None
    _key = (round(float(t[0]), 2), round(float(t[1]), 2))
    _now = time.time()
    # v15.1 修"重锚空转"：真机实测日志每 0.3s 刷一次同一座塔、偏差恒为 0 —— 因为
    #   self_est 被吸到塔上后下一次 d_est 就是 0，还会再"重锚"一次；而每次都要调
    #   self_hp_mp（全画面血条检测）-> 主循环被拖到 ~2.8fps、英雄推进也失去反馈。
    #   ① 已在该塔上 -> 无需纠偏，直接退出（不再空转、不再白跑 self_hp_mp）
    if d_est < SNAP_ALREADY_EPS:
        return None
    #   ② 同一座塔的重复吸附加冷却（偏差不够大时不重复吸，避免把估计钉死在塔上）
    if (cooldowns.get("snap_key") == _key
            and _now - float(cooldowns.get("snap_key_t", 0.0)) < SNAP_SAME_TOWER_COOLDOWN_S
            and d_est < TOWER_SNAP_MAX_ERR * 0.5):
        return None
    cooldowns["self_est"] = (_key[0], _key[1])
    cooldowns["snap_key"], cooldowns["snap_key_t"] = _key, _now
    return (round(_key[0], 3), round(_key[1], 3), round(d_est, 3))


# ---------------------------------------------------------------------------
# 瀹炴椂鍒嗘瀽鏍囨敞坴2.15: 姣忓抚鍏绱犳爣娉-> temp/live_annot/屼緵浜哄伐鏍告煡 AI 鐞嗚# ---------------------------------------------------------------------------
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

# recovered consts (from v3.1)
RECALL_THROTTLE_S = 20.0
RECALL_ACTIVE_S = 8.0
# v14.2 移动通道修正: 实测"点小地图"在真机**不移动英雄**(点前后整屏均差仅 7.8, 几乎无
#   变化), 而"摇杆拖动"有效(均差 70.0, 整屏滚动) —— 王者的小地图只移动镜头, 不走人。
#   故 map_move 由"点小地图目标点"改为"摇杆推杆"。相机朝向固定且与小地图同向, 世界方向
#   (小地图 Δnx,Δny) 即摇杆屏幕方向。theta 用 executor 约定(0=右, 逆时针, y 向上)。
#   "自己"在小地图上的位置无法从视觉可靠定位(绿点=自家基地固定图标), 用航位推算:
#   从本方基地出发, 按每次推杆时长前进。方向为主 + 定时重发纠偏, 偏差可接受。
STICK_SPEED = 0.018           # 满杆 1 秒 -> 小地图归一化位移(在 map_move 实际工作点 r=0.8 实测)
                              # 真机标定 2026-10-15 (英雄=钟馗, 蓝方, scripts/calib_stick_speed.py):
                              #   ① 干净锚点(新开局 0:08 英雄刚在泉水) 沿下路满杆直推 900ms/杆:
                              #      #1 到 0.150 用 9.0s -> 0.0167 (同塔另一次 8.1s -> 0.0186)
                              #   ② 残局里从基地(离泉水约 0.03~0.05, 锚点偏)测的 2 点过原点拟合 = 0.0214,
                              #      锚点偏会让距离偏大 -> 该值偏高, 以 ① 为准则取 0.018。
                              #   结论: 真值区间 0.017~0.020, 取 0.018。
                              # 与旧值 0.045 的关系: 0.045 是"满舵"量级; ActionExecutor.stick_radius=0.12
                              #   -> r=1.0 只有 86px(帧空间), 而校准表 dir_* 满杆点约 130~160px, 故 r=0.8
                              #   只到满舵 ~50%, 0.045*0.5≈0.0225 与实测同量级 -> 旧值其实是"满舵数"被
                              #   当成了 r=0.8 的数, 这才是"英雄推进慢/到点太早"的一个来源。
                              # 移速随英雄/出装/减速效果变化: 换英雄或改 r 都要重标, 或 CLI 覆盖:
                              #   --stick-speed <值>; 标定法见 scripts/calib_stick_speed.py
MAPMOVE_MIN_MS = 350
MAPMOVE_MAX_MS = 900
MAPMOVE_NEAR = 0.04           # 距目标 < 该值视为已到达, 停推(避免原地抖)
# v16.2 持久摇杆模式下两次"改朝向"的最小间隔(秒)。swipe 模式靠 dur 守卫防叠发,
#   但持久摇杆单条只 ≈20ms, 不需要等 dur; 这里只防同一拍内重复下发(≈8Hz 上限)。
MAPMOVE_HOLD_GAP = 0.12
# v16.2 航位推算的时间口径上限(秒): 持久摇杆按"实际经过时间"推进估计, 若两拍之间隔了
#   很久(切屏/卡顿/长时间只放技能没改朝向)要设上限, 否则会一次性把估计推进过头。
MAPMOVE_EL_MAX = 1.5
# v16.2 "到达即停"闸门的保险丝时长(秒): 在同一位置连续停推超过它仍没换目标, 认为
#   开环估计已失真 -> 回退到旧的"重锚回基地 + 继续推进", 防永久僵住(见 nav_gate.py)。
MAPMOVE_IDLE_MAX = 5.0
BASE_BLUE = (0.074, 0.88)     # 本方泉水在小地图上的归一化位置(蓝方; 真机实测 2026-10-14:
                              #   小地图绿点恒定于此 = 泉水治疗圈图标, 不是英雄; 红方镜像)
BASE_RED = (0.926, 0.12)
TURRET_SAFE_FRAC = 0.55
ENGAGE_FRAC = 0.30

_FONT_CACHE = {}


def _lfont(size):
    if size not in _FONT_CACHE:
        for fp in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc",
                   r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simsun.ttc"):
            if Path(fp).exists():
                _FONT_CACHE[size] = ImageFont.truetype(fp, size)
                break
        else:
            _FONT_CACHE[size] = None
    return _FONT_CACHE[size]


def _ltext(img, text, org, size=16, color=(255, 255, 0)):
    f = _lfont(size)
    if f is None:
        return
    x, y = int(org[0]), int(org[1])
    h, w = img.shape[:2]
    if x >= w or y >= h:
        return
    # v14.3 只转换文字所在的小块: 原实现对整幅图做 BGR->PIL->BGR 往返
    #   (1280x576 = 0.7M 像素 x 两次 cvtColor + 一次全图拷贝), 而 m2 每拍要为每个
    #   检测框/小地图点/信息行都调一次本函数(约 20-30 次) -> 实测 553ms/拍, 占主循环 24%。
    #   渲染结果逐像素不变, 但每调用从 ~18ms 降到 ~0.1ms。
    try:
        bx0, by0, bx1, by1 = f.getbbox(text)
    except Exception:
        bx0 = by0 = 0
        bx1, by1 = f.getsize(text)
    pad = max(2, size // 4)
    x0, y0 = max(0, x + bx0 - pad), max(0, y + by0 - pad)
    x1, y1 = min(w, x + bx1 + pad), min(h, y + by1 + pad)
    if x1 <= x0 or y1 <= y0:
        return
    patch = img[y0:y1, x0:x1]
    pil = Image.fromarray(cv2.cvtColor(patch, cv2.COLOR_BGR2RGB))
    dr = ImageDraw.Draw(pil)
    dr.text((x - x0, y - y0), text, font=f, fill=(color[2], color[1], color[0]))
    img[y0:y1, x0:x1] = cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)


_LA_DIR = None
_LA_LAST_WRITE = [0.0]      # v14.3 PNG 实时预览限流时间戳
_LA_LAST = 0.0
# v16.3 标注图落盘开关（纯人工预览，不参与任何决策/执行）。
#   真机(screencap/scrcpy)下每拍要写 2 张 PNG，实测单张 20–70ms，
#   且与采集线程/视频落盘争用同一磁盘时会被放大到数百 ms —— 追帧率时用 --no-annot 关掉。
_LA_ANNOT = True
MM_LABELS_CN = {"self": "self", "ally": "ally", "enemy": "enemy",
                "monster": "monster", "buff": "buff", "minion": "minion"}
CLS_CN = {"enemy_hero": "enemy", "ally_hero": "ally", "enemy_minion": "enemy_minion",
          "ally_minion": "ally_minion", "enemy_turret": "enemy_turret", "ally_turret": "ally_turret",
          "enemy_crystal": "enemy_crystal", "ally_crystal": "ally_crystal",
          "neutral_monster": "monster", "hook_aim": "hook", "skill_effect": "skill",
          "self": "self"}


def render_live_analysis(frame, dets, mm, state_dict, action, beh, score, now):
    """decoded docstring."""
    global _LA_DIR, _LA_LAST
    if _LA_DIR is None:
        _LA_DIR = ROOT / "temp" / "live_annot"
        _LA_DIR.mkdir(parents=True, exist_ok=True)
    vis = frame.copy()
    h, w = vis.shape[:2]

    # 鍏睆 12 绫绘 (v2.56: self 妗嗗交搴曚笉鐢烩斺旂敤鎴峰凡纭鑷繁鍦睆骞曚腑澶
    for d in dets:
        if d.cls == "self":
            continue
        x1, y1, x2, y2 = (int(v) for v in d.xyxy)
        col = {"enemy_hero": (0, 0, 255), "ally_hero": (255, 160, 0),
               "enemy_minion": (0, 100, 255), "ally_minion": (120, 220, 0),
               "enemy_turret": (0, 0, 200), "ally_turret": (220, 140, 0),
               "enemy_crystal": (255, 0, 200), "ally_crystal": (200, 255, 0),
               "neutral_monster": (0, 255, 255), "hook_aim": (255, 0, 255),
               "skill_effect": (255, 255, 0)}.get(d.cls, (255, 255, 255))
        cv2.rectangle(vis, (x1, y1), (x2, y2), col, 2)
        _ltext(vis, f"{CLS_CN.get(d.cls, d.cls)} {d.conf:.2f}",
               (x1 + 2, max(2, y1 - 20) if y1 > 22 else y2 + 2), 14, col)

    # 鑷繁 HP/MP
    ui = state_dict.get("ui") or {}
    hp = float(ui.get("hp") or 0.0)
    mp = float(ui.get("mp") or 0.0)
    # v2.80 hero 全屏标记已删: 自己在屏幕中央固定, 不需要检测/显示 (仅下方血量数字)
    _dummy_hero = None

    # 忓湴鍥惧叏瑕佺礌 2x堝彸涓婏級v2.25: 鍏箙 0-242 (鍚笅杈规娉夋按鍖
    v7 = (mm or {}).get("v7") or {}
    if v7:
        bx, by, bsz = w - 490, 34, 480
        cv2.rectangle(vis, (bx, by), (bx + bsz, by + bsz), (40, 40, 40), -1)
        mmcrop = vis[by + 2:by + bsz - 2, bx + 2:bx + bsz - 2]
        # v15.3 小地图框随分辨率变（20:9 真机实测 x=63/size=194），不再写死 (0,0,242)
        _c, _r = (mm or {}).get("center"), (mm or {}).get("radius")
        if _c and _r:
            msz = max(40, int(round(float(_r) * 2)))
            mx = max(0, int(round(float(_c[0]) - float(_r))))
            my = max(0, int(round(float(_c[1]) - float(_r))))
        else:
            msz, mx, my = 242, 0, 0
        mmcrop[:] = cv2.resize(frame[my:my + msz, mx:mx + msz], (bsz - 4, bsz - 4))
        scale = (bsz - 4) / float(msz)

        def dpos(n):
            return (int(bx + 2 + n[0] * msz * scale), int(by + 2 + n[1] * msz * scale))
        for d in v7.get("self", []):
            px, py = dpos(d["n"]); cv2.circle(vis, (px, py), 12, (0, 255, 0), 3)
            _ltext(vis, "self", (px + 13, py - 8), 13, (0, 255, 0))
        for d in v7.get("ally", []):
            px, py = dpos(d["n"]); cv2.circle(vis, (px, py), 10, (255, 150, 0), -1)
            _ltext(vis, "ally", (px + 11, py - 8), 13, (255, 150, 0))
        for d in v7.get("enemy", []):
            px, py = dpos(d["n"]); cv2.circle(vis, (px, py), 10, (0, 0, 255), -1)
            _ltext(vis, "enemy", (px + 11, py - 8), 13, (255, 80, 80))
        for d in v7.get("monster", []):
            px, py = dpos(d["n"]); cv2.circle(vis, (px, py), 7, (0, 255, 255), -1)
        for d in v7.get("buff", []):
            px, py = dpos(d["n"]); cv2.circle(vis, (px, py), 7, (255, 0, 255), -1)
        tws = (mm or {}).get("towers_v7") or {}
        for d in tws.get("ally", []):
            px, py = dpos(d["n"]); cv2.rectangle(vis, (px - 8, py - 8), (px + 8, py + 8),
                                                 (255, 180, 0), -1)
        for d in tws.get("enemy", []):
            px, py = dpos(d["n"]); cv2.rectangle(vis, (px - 8, py - 8), (px + 8, py + 8),
                                                 (0, 80, 255), -1)
        for d in (v7.get("minions") or {}).get("ally", []):
            px, py = dpos(d["n"]); cv2.circle(vis, (px, py), 4, (150, 255, 150), -1)
        for d in (v7.get("minions") or {}).get("enemy", []):
            px, py = dpos(d["n"]); cv2.circle(vis, (px, py), 4, (150, 150, 255), -1)

    # 底部信息条
    beh_label = (beh or {}).get("label", "")
    act_txt = f"决策: {format_decision(action)}"
    reason = action.get("reason", "")
    mmc = (mm or {}).get("dots") or {}
    info = (f"beh: {beh.get('label', '')} | {reason} | score {score:.0f} | "
            f"map blue {len(mmc.get('blue', []))}/red {len(mmc.get('red', []))}/"
            f"green {len(mmc.get('green', []))} | {datetime.now():%H:%M:%S}")
    cv2.rectangle(vis, (0, h - 64), (w, h), (0, 0, 0), -1)
    _ltext(vis, info, (10, h - 58), 17, (0, 255, 255), )

    # 落盘
    # v14.3 PNG 编码实测 23.5ms/帧(1280x576), 而这只是给人看的实时预览, 每拍都写纯属浪费。
    #   限到 ~4Hz; 返回值 vis 不变, 不影响任何下游(决策/执行/test)。
    global _LA_LAST_WRITE
    if _LA_ANNOT and now - _LA_LAST_WRITE[0] >= 0.25:
        _LA_LAST_WRITE[0] = now
        cv2.imwrite(str(_LA_DIR / "annot_latest.png"), vis)
    if _LA_ANNOT and now - _LA_LAST >= 1.0:
        _LA_LAST = now
        cv2.imwrite(str(_LA_DIR / f"annot_{now:.0f}.png"), vis)
        # 娓呯悊鏃枃浠淇濈暀200)
        old = sorted(_LA_DIR.glob("annot_*.png"))
        for f in old[:-210]:
            try:
                f.unlink()
            except OSError:
                pass
    # 琛屼负瀹
    try:
        rec = {"t": now, "ts": datetime.now().isoformat(timespec="seconds"),
               "label": beh_label, "action": action.get("type"),
               "id": action.get("id"), "reason": reason,
               "hp": round(hp, 3), "mp": round(mp, 3), "score": round(float(score), 1),
               "mm": {"blue": len(mmc.get("blue", [])), "red": len(mmc.get("red", [])),
                      "green": len(mmc.get("green", []))},
               # v16.0 P2 推进: 跑了 shadow 一局就能统计"会推的帧占比 / 被各闸挡掉的占比"
               "push": (state_dict.get("extra") or {}).get("push")}
        with open(_LA_DIR / "behavior.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        # 以前是静默 pass -> behavior.jsonl 一直没生成也没人知道（P2 依赖它落数据）
        print(f"[annot-record] 行为记录写入失败(不影响决策): {e!r}")
    return vis


def format_decision(action: dict) -> str:
    t = action.get("type")
    reason = action.get("reason", "")
    if t == "none":
        return f"none [{reason}]"
    if t == "skill":
        return f"skill{action.get('id')} ({action.get('mode', 'tap')}) [{reason}]"
    if t == "summoner":
        return f"summoner [{reason}]"
    if t == "recall":
        return f"recall [{reason}]"
    if t == "restore":
        return f"restore [{reason}]"
    if t == "combo":
        names = [f"{a.get('type')}{a.get('id', '')}" for a in action.get("actions", [])]
        return f"combo {'+'.join(names)} [{reason}]"
    if t == "move":
        return f"move theta={action.get('theta', 0.0):+.2f} r={action.get('r', 0.0)} " \
               f"{action.get('duration_ms', 0)}ms [{reason}]"
    return f"{t} [{reason}]"


def main():
    global STICK_SPEED          # v14.2 (声明须在任何使用之前)
    ap = argparse.ArgumentParser(description="doc")
    ap.add_argument("--capture", default="desktop",
                    choices=["desktop", "screencap", "scrcpy", "screenrecord"],
                    help="frame source: desktop/screencap/scrcpy/screenrecord(裸H264经TCP,真机推荐)")
    ap.add_argument("--seconds", type=float, default=120.0, help="run seconds")
    ap.add_argument("--forever", action="store_true",
                    help="opt")
    # v14.3 主循环分阶段计时: 定位"每拍到底慢在哪"(之前只能靠单点测量猜)
    ap.add_argument("--timing", action="store_true",
                    help="收尾打印主循环各阶段累计耗时(含 wait_frame 阻塞等待)")
    # v14.0 默认检测器改为 P0 替代模型(YOLOv5 ONNX, 8 通用类) —— 原 zhongkui_r3
    # 权重已丢失; 传 --model 指向 .pt 可回退 ultralytics 11 类后端
    ap.add_argument("--model", default=str(ROOT / "runs" / "detect" / "hok_yolov5"
                                           / "hok_yolov5s.onnx"),
                    help="opt")
    # v16.0 P3 性能: YOLO ONNX intra_op 线程数。默认 0 = onnxruntime 按物理核数开满
    #   (本机 16 核) —— 推理突发期间把采集 reader 饿死(wait_frame 占墙钟 88-97%)。
    #   限到 2-4 给采集让 CPU; 同局内 A/B 用不同值各跑一段再比。
    ap.add_argument("--yolo-threads", type=int, default=0,
                    help="YOLO ONNX intra_op 线程数(0=ORT 默认占满物理核; 建议试 2-4)")
    ap.add_argument("--yolo-every", type=int, default=3,
                    help="每 N 个主循环拍做一次 YOLO 检测(默认 3, 保持原行为)。"
                         "实测真机主循环 ~21Hz 而感知块 ~2.3Hz, 故 N=3 时 YOLO 仅 ~0.8Hz;"
                         "调小可提高检测刷新率, 代价是主循环变慢且决策下发更密(执行线程可能丢弃)")
    ap.add_argument("--action", action="store_true",
                    help="opt")
    ap.add_argument("--no-action", dest="no_action", action="store_true",
                    help="opt")
    ap.add_argument("--save", action="store_true", help="opt")
    ap.add_argument("--no-annot", dest="no_annot", action="store_true",
                    help="不写 temp/live_annot 的标注预览 PNG（纯人工看图，不参与决策；"
                         "真机追帧率时用，省 2 张 PNG/拍的编码与磁盘开销）")
    # v15.1 取证用：关键帧落盘间隔 + "看到敌方英雄就额外落一帧"
    #   （顶部目标头像只在锁定/攻击敌方时出现，5s 节流会正好错过那个瞬间）
    ap.add_argument("--frame-every", type=float, default=5.0, help="关键帧间隔秒(默认5)")
    ap.add_argument("--frame-on-enemy", action="store_true",
                    help="检测到敌方英雄时额外落一帧(取证目标头像/敌方血条)")
    ap.add_argument("--hunt", action="store_true",
                    help="主动找敌: 看不见敌人且小地图有红点时, 无视走廊约束径直去找(默认关)")
    # v15.1 UI导航/信息查询层（与主战斗感知解耦的支线）：
    #   off=零改动 / read=只读游戏自动弹出的面板(阵亡回放, 零点击) / full=允许点击打开面板
    ap.add_argument("--ui-nav", default="off", choices=["off", "read", "full"],
                    help="面板类信息查询: off/read/full")
    # v16.3 内存直读（Route C，只读 /proc/<pid>/mem，需 adb root + /data/local/tmp/memscan）：
    #   补视觉层读不到的对局字段 —— 可用金币/总经济/等级/经验/KDA/局内时间/单位血量与死亡/
    #   在场野怪。读在后台线程（~2Hz），主循环只做一次 dict 合并，不加 adb 往返。
    ap.add_argument("--mem", action="store_true",
                    help="开启内存直读（经济/等级/局内时间/单位血量/野怪在场）；"
                         "默认关。需 adb root，见 docs/wzry-ai-reverse.md §13")
    ap.add_argument("--mem-self-id", type=int, default=None,
                    help="本地英雄配置 ID（106=小乔…）。不给则用数组 slot0（实测两局均为本地玩家）")
    ap.add_argument("--mem-hz", type=float, default=2.0, help="内存刷新频率(默认2Hz)")
    # S1-4 shadow: 只读回放 hok-decision-ai 的宏观决策，不改变任何下发动作。
    #   默认关；开启时用独立 daemon 线程 + 队列深度 1，主循环只做非阻塞提交。
    ap.add_argument("--hok-shadow", action="store_true",
                    help="开启 hok 宏观决策 shadow 日志（只读，不改动作流；默认关）")
    ap.add_argument("--hok-shadow-hz", type=float, default=1.0,
                    help="shadow 决策频率(默认1Hz)")
    ap.add_argument("--hok-shadow-model", choices=["base", "linear"], default="base",
                    help="base=规则权重(默认)；linear=Tencent 线性价值(未做 WZRY 域校准)")
    ap.add_argument("--hok-shadow-out", default=None,
                    help="shadow JSONL 路径；默认 temp/hok_shadow/live.jsonl")
    # v17.0 决策架构：legacy=价值场 argmax（默认，行为不变）；official=官方架构复刻版
    #   （按类别特征向量 -> 打分 -> top-k -> 规则覆盖 -> 回退）。建议与 --mem 同用。
    ap.add_argument("--ai-arch", choices=["legacy", "official"], default="legacy",
                    help="决策架构：legacy(默认) / official(官方复刻版)")
    ap.add_argument("--ai-level", choices=["easy", "normal", "hard", "juewu"], default="hard",
                    help="official 架构的难度参数档（predict_freq/delay_frame/god_vision）")
    # v17.1 节拍与规则覆盖（对齐官方 predict_interval_* / RuleTactics 141 条链）
    ap.add_argument("--ai-rules", choices=["all", "disasm"], default="all",
                    help="official 架构规则覆盖层：all=全部已实现规则（含名字级推断）"
                         " / disasm=只跑有反汇编证据的 4 条")
    ap.add_argument("--ai-predict-interval", type=float, default=0.0,
                    help="覆盖基础决策间隔（秒）；0=按档位 predict_freq。"
                         "场景化间隔见 official_ai/config.py::SCENE_INTERVAL")
    ap.add_argument("--ai-delay-frames", type=int, default=-1,
                    help="观测延迟帧（拟人/抗抖动）；-1=按档位 delay_frame")
    # 局内时间定位的"窗口锚点"。生产场景（agent 从开局就跑）用 match_start_t 就够；
    #   中途接入时 match_start_t 是"接入时刻"，会与真实局内时间差出整整一段，
    #   这时用 HUD 上看到的分秒换算成秒传进来（例：17:26 -> 1046）。
    ap.add_argument("--mem-clock-hint", type=int, default=None,
                    help="中途接入时的局内秒数锚点（HUD 时钟换算）；不给则用 match_start_t")
    ap.add_argument("--detect-hz", type=float, default=10.0, help="opt")
    ap.add_argument("--stick-speed", type=float, default=STICK_SPEED,
                    help="满杆 1 秒的小地图归一化位移(默认=真机标定值, 见 "
                         "scripts/calib_stick_speed.py; 移速随英雄/出装变化)")
    # v16.2 摇杆下发方式。on(默认)=持久触点: t_down 按住 + 每次 t_move 只改朝向, 英雄连续
    #   移动, 单条 ≈20ms; off=回退成原来的 t_swipe(推一次就松手, 占执行线程 200~1000ms)。
    #   仅对具备 touch 多点能力的执行器生效(--backend esp32 且 --board-mode touch);
    #   adb / mouse 固件会自动回退 swipe, 行为与改造前完全一致。
    #   要 A/B 归因就 off 跑一段、on 跑一段(其余参数保持一致)。
    ap.add_argument("--stick-hold", default="on", choices=["on", "off"],
                    help="摇杆持久触点 on/off(默认 on; off=回退 swipe 原行为)")
    # v16.2 外部/文件驱动：从 stdin 逐行读 JSON 动作并执行，**不经过 decide()**。
    #   用途① 外部程序实时驱动（写一行 JSON 就执行一条）；
    #   用途② 回放 data/matches/*/actions.jsonl（schema 就是"动作 + t"，节奏按 t 差值）。
    #   开启后主循环自己的动作下发会被关掉（避免与外部驱动打架），感知/录制照常。
    ap.add_argument("--drive-stdin", action="store_true",
                    help="从 stdin 逐行读 JSON 动作并执行(外部/文件驱动); 默认关")
    ap.add_argument("--drive-pace-max", type=float, default=5.0,
                    help="按 t 重放时单条最大等待秒数(默认5; 防长空档一次性睡很久)")
    ap.add_argument("--show", action="store_true", help="opt")
    # v14.3 真机 / 非 MuMu 支持: 本机同时装了 MuMu 自带 adb(D://app//MuMuPlayer//nx_main)
    #   与项目自带 tools/scrcpy/adb.exe, 两者版本不同会轮流占 5037 端口 -> 每次切换都重启
    #   daemon(>5s), 表现为"连着设备却报未发现 ADB 设备"。用 --adb 显式指定唯一 adb。
    ap.add_argument("--adb", default=None,
                    help="adb.exe 路径(真机/非 MuMu 场景); 等价于设 WZRY_ADB")
    ap.add_argument("--serial", default=None,
                    help="目标设备序列号(如真机 USB 序列号); 默认自动发现")
    ap.add_argument("--laya", default="off", choices=["off", "shadow", "ensemble"],
                    help="opt")
    ap.add_argument("--laya-model", default=None, help="opt")
    # v14.2 beta 默认 0.5 -> 0.05: 实测 shadow 探针里 top1-top2 规则分间距只有
    #   0.25(457条)/0.004(229条), 而 beta=0.5,K=16 时 bonus 极差 = 0.469 > 0.25,
    #   足以无条件翻转任意一次排序 —— 不是"小扰动", 与方案"beta 从小开始"矛盾。
    ap.add_argument("--laya-beta", type=float, default=0.05, help="opt")
    # v14.2 P1 缓解旋钮(方案 §6 P1 不通过时: 降频 / 换 ONNX fp16 / 减候选)
    ap.add_argument("--laya-topk", type=int, default=16,
                    help="送进 Laya 的候选数(方案上限 20); 推理耗时随选项数增长")
    ap.add_argument("--laya-hz", type=float, default=0.0,
                    help="提交推理的最小间隔(秒), 0=不限。模型吞吐跟不上时用它降频")
    ap.add_argument("--laya-max-errors", type=int, default=20,
                    help="运行期熔断: 累计异常数达此值即本局降级 off; 0=不熔断")
    ap.add_argument("--laya-max-dups", type=int, default=5,
                    help="运行期熔断: 标签重复次数达此值即本局降级 off; 0=不熔断")
    # v14.3 推理后端(方案 §6.2): 本机无独显 -> 用 OpenVINO 跑 Arc 140T 核显(实测 92.5ms,
    #   CPU 876ms / NPU 403ms)。需要先跑 scripts/export_laya_ov.py 生成 IR。
    ap.add_argument("--laya-backend", default="auto", choices=["auto", "onnx", "torch", "openvino"],
                    help="auto=原行为; openvino=用 OpenVINO IR 跑核显/NPU")
    ap.add_argument("--laya-device", default="GPU", help="OpenVINO 设备: GPU(核显)/NPU/CPU/AUTO")
    ap.add_argument("--laya-ov-ir", default=None,
                    help="OpenVINO IR 路径; 默认 runs/laya/laya_ov/laya_qfreeze.xml")
    ap.add_argument("--laya-max-len", type=int, default=320,
                    help="IR 编译时的固定序列长度, 须与导出时一致(实测需 274 token)")
    ap.add_argument("--laya-offline", action="store_true",
                    help="HF 缓存离线模式(无外网时必开)")
    # v15.0 BCNet 策略接线（与 --laya 同一套 shadow 模式）：
    #   off=零改动 / shadow=每拍跑模型并落盘、**不改动作流** / bc=接管（仍必须过安全层）
    ap.add_argument("--policy", default="off", choices=["off", "shadow", "bc"], help="opt")
    ap.add_argument("--policy-checkpoint", default=None, help="BCNet/BCNetMem 权重(.pt)")
    # v16.0 P2 推进(Push Policy)接线（方案 docs/ARCH_MOVE_PUSH.md）：
    #   off=完全不评估（零开销、零行为变化）/ shadow=照常评估但**不采用**推进目标
    #   （只落盘 behavior.jsonl + 节流打印，动作流与 off 逐字节一致）/ on=真正接管移动
    ap.add_argument("--push", default="off", choices=["off", "shadow", "on"], help="opt")
    # v16.0 E-P2: 临时覆盖推进调参常量（真机确认时用，避免"改一个数就改代码"）。
    #   例: --push-set PUSH_STAY_WITH_ALLY=0.20 --push-set PUSH_SELF_HP_MIN=0.40
    ap.add_argument("--push-set", action="append", default=[], metavar="KEY=VAL",
                    help="临时覆盖 PUSH_*/TURRET_MM_* 常量（可重复；默认不覆盖）")
    ap.add_argument("--freeze-weights", action="store_true", help="opt")
    # v14.2 ESP32 HID 桥（真机路线）: 板子插手机 USB 口当 HID 触摸屏, 电脑走 Wi-Fi 控制。
    # v16.1 执行通道是**互斥**的(只产出一个 executor), 故这里做成"选择策略"而非并行:
    #   auto(默认) = 优先板子, 探测失败回落 adb; adb/esp32 = 强制某一方。
    #   关键安全性质: auto 在"从未配置过板子地址"时不发起探测、直接 adb ->
    #   与旧版 --backend adb 行为一致, 既有 MuMu 工作流零影响。
    #   板子地址来源: --board-host > 环境变量 WZRY_BOARD_HOST > configs/board.json
    ap.add_argument("--backend", default="auto", choices=["auto", "adb", "esp32"],
                    help="执行通道: auto(优先板子,不通回落adb) / adb(强制adb) / esp32(强制板子)")
    ap.add_argument("--board-host", default=None,
                    help="ESP32 板子的 Wi-Fi 地址(backend=esp32 必填; auto 下可不填, 走环境变量/已存配置)")
    ap.add_argument("--board-port", type=int, default=7300, help="板子 TCP 端口(须与固件 BRIDGE_PORT 一致)")
    ap.add_argument("--board-mode", default=None, choices=["touch", "mouse"],
                    help="须与烧的固件一致(默认 touch=多点触摸屏; 未填则用 configs/board.json 的值)")
    ap.add_argument("--board-size", default=None,
                    help="手机物理分辨率 WxH, 须与固件 TOUCH_SURFACE_W/H 一致"
                         "(未填则用 configs/board.json 的值, 都没有才用 1080x2340)")
    # 探测超时须短: auto 是默认路径, 探一个不在线的地址不该让启动卡住
    ap.add_argument("--board-probe-timeout", type=float, default=0.8,
                    help="auto 模式下探测板子的连接超时秒(默认0.8; 板子在线实测往返19-22ms)")
    args = ap.parse_args()
    if getattr(args, "no_annot", False):
        global _LA_ANNOT
        _LA_ANNOT = False
        print("[annot] 标注预览 PNG 落盘已关闭（temp/live_annot/annot_*.png 不再写入）")
    _ovr = apply_push_overrides(getattr(args, "push_set", None) or [])
    if _ovr:
        print("[push-set] 临时覆盖常量: " + ", ".join(f"{k}={v}" for k, v in _ovr))

    # v16.2 --drive-stdin 等价于"要驱动": 不传 --action 也允许(但仍受 --no-action 否决)
    do_action = (bool(args.action) or bool(args.drive_stdin)) and not args.no_action
    freeze_weights = bool(getattr(args, "freeze_weights", False))
    STICK_SPEED = float(args.stick_speed)      # v14.2 CLI 覆盖(apply_action 读模块全局)
    _laya = None  # v14.0 LayaScorer 句柄(finally 里 stop + 输出统计, P1 门槛: 异常数=0)

    # v14.0 Laya 打分器接线 (laya-fusion-plan C3): off=零改动; shadow=仅记录; ensemble=混合排序
    if getattr(args, "adb", None):                # v14.3 --adb 覆盖(须 patch 模块常量,
        import os as _os2                         #   因为 executor.ADB 在 import 时就已解析)
        _os2.environ["WZRY_ADB"] = args.adb
        try:
            from wzry.control import executor as _exec_mod
            _exec_mod.ADB = args.adb
        except Exception:
            pass

    if getattr(args, "laya_offline", False):      # v14.3 无外网时强制用 HF 本地缓存
        import os as _os
        _os.environ["HF_HUB_OFFLINE"] = "1"
        _os.environ["TRANSFORMERS_OFFLINE"] = "1"

    if getattr(args, "laya", "off") != "off":
        from wzry.decision import optimizer as _opt_laya
        from wzry.decision.laya_scorer import LayaScorer as _LayaScorer
        _laya = _LayaScorer(mode=args.laya, beta=float(args.laya_beta),
                            model_path=args.laya_model,
                            top_k=int(getattr(args, "laya_topk", 16)),
                            min_submit_interval=float(getattr(args, "laya_hz", 0.0)),
                            max_errors=int(getattr(args, "laya_max_errors", 20)),
                            max_label_dups=int(getattr(args, "laya_max_dups", 5)),
                            backend=getattr(args, "laya_backend", "auto"),
                            ov_device=getattr(args, "laya_device", "GPU"),
                            ov_ir=getattr(args, "laya_ov_ir", None),
                            ov_max_len=int(getattr(args, "laya_max_len", 320)))
        _opt_laya.set_scorer(_laya, beta=float(args.laya_beta), mode=args.laya)
        _laya.start()
        _bdev = (f"{_laya.backend}:{_laya.ov_device}"
                 if _laya.backend == "openvino" else _laya.backend)
        print(f"[Laya] mode={args.laya} beta={args.laya_beta} "
              f"topk={_laya.top_k} 节流={_laya.min_submit_interval or '不限'}s "
              f"后端={_bdev} model={args.laya_model or 'default'}")

    from wzry.calib import load_calibration
    from wzry.capture.scrcpy_stream import ScrcpyStreamCapture
    from wzry.data.collector import MatchRecorder
    from wzry.state.fuser import build_state
    from wzry.state.match_state import MatchPhase, MatchStateMachine
    # ---- v15.0 BCNet 策略接线 + 事件记忆层 + Hero Profile ----
    _policy = None
    _policy_mode = getattr(args, "policy", "off")
    _policy_log = ROOT / "runs" / "bc" / "policy_shadow.jsonl"
    if _policy_mode != "off":
        if not getattr(args, "policy_checkpoint", None):
            print("[policy] 未给 --policy-checkpoint -> 等价 off")
            _policy_mode = "off"
        else:
            try:
                from wzry.policy.inference import BCNetInference as _BCInf
                _policy = _BCInf(args.policy_checkpoint)
                print(f"[policy] mode={_policy_mode} ckpt={args.policy_checkpoint} "
                      f"memory={_policy.has_memory}")
            except Exception as _pe:
                print(f"[policy] 加载失败 -> 等价 off: {type(_pe).__name__}: {_pe}")
                _policy, _policy_mode = None, "off"
    from wzry.memory.event_log import EventLog as _EventLog
    _events = _EventLog(persist=bool(args.save))
    # ---- v15.1 UI导航/信息查询层（按 wzry/uiquery/panel.py 的接口）：
    #   点击=执行层 UI 触发动作 / 面板=辅助UI感知 / 内容回填状态层+记忆层。
    #   模态锁保证"同一时刻只有一个面板占用整屏"，且 run() 必定 close + release。
    _uiq = None
    _is_panel_open = None
    _uiq_mode = getattr(args, "ui_nav", "off")
    if _uiq_mode != "off":
        try:
            from wzry.uiquery.panel import PanelNavigator as _PanelNav
            from wzry.uiquery.panel import is_panel_open as _ipo
            _is_panel_open = _ipo
            _btns = ((json.loads((ROOT / "configs" / "calibration_absolute.json")
                                 .read_text(encoding="utf-8-sig"))).get("panel_buttons") or {})
            _uiq = _PanelNav(tap=getattr(ex, "tap", None), taps=_btns, timeout=3.0)
            print(f"[ui-nav] mode={_uiq_mode} panel_buttons={list(_btns.keys()) or '无'}"
                  f"（无标定则只读游戏自动弹出的面板）")
        except Exception as _ue:
            print(f"[ui-nav] 初始化失败(不影响主链): {type(_ue).__name__}: {_ue}")
    _profile = None
    try:
        from wzry.decision import hero_profile as _hp0
        _profile = _hp0.load("zhongkui")
        if _profile:
            for _sid in (1, 2, 3):
                _sk = (_profile.get("skills") or {}).get(str(_sid)) or {}
                _cd = _sk.get("max_cd_s")
                if not _cd:
                    try:
                        from wzry.knowledge import skill_cd_prior
                        _cd = skill_cd_prior(_sk.get("name"), slot=_sid) if _sk.get("name") else None
                    except Exception:
                        _cd = None
                if _cd:
                    _events.set_max_cd(_sid, _cd)
            print(f"[profile] {_profile.get('hero')} 加点={_profile.get('level_order')} "
                  f"出装 {len((_profile.get('items') or {}).get('route') or [])} 件 "
                  f"(status={_profile.get('status')})")
    except Exception as _pf:
        print(f"[profile] 加载失败(不影响运行): {type(_pf).__name__}: {_pf}")
    from wzry.vision.detector import YoloDetector
    from wzry.vision.mm_tracker_v7 import MMTrackerV7

#    # v2.8歳otate_s 璋冨鍒30 鍒嗛挓堥伩鍏嶅灞涓疆杞鑷鏃犲抚"鍋滈】    # v2.47 閲囬泦: desktop=MuMu绐楀彛PrintWindow骞虫粦娴榛樿, ~12fps鏃犲垎娈
    if args.capture == "desktop":
        from wzry.capture.desktop_capture import DesktopCapturePrint
        cap = DesktopCapturePrint(interval=0.02)
        print("启动 MuMu 窗口平滑视频流(PrintWindow, ~84ms/帧)...")
    elif args.capture == "screencap":
        from wzry.capture.screencap_capture import ScreencapCapture
        # v16.0 修: 以前没把 --serial 传进来 -> 真机(USB)时它恒连 127.0.0.1:16384,
        #   抓不到帧(实测 0 帧)。传 None 时行为与以前完全一致(内部仍兜底到该地址)。
        cap = ScreencapCapture(interval=0.02, rotate_s=1800,
                               serial=getattr(args, "serial", None))
        print("启动 screencap 实时采集（adb 直读）...")
    elif args.capture == "screenrecord":
        # v16.4: scrcpy --record 的 mkv 有 ~5s muxer 缓冲且 cv2 尾读会 EOF 锁死;
        #   换 screenrecord 裸 H264 经 TCP 喂 cv2: 32.6fps 交付、无卡顿、无启动白等。
        from wzry.capture.screenrecord_capture import ScreenrecordCapture
        cap = ScreenrecordCapture(ROOT / "tools" / "scrcpy", rotate_s=170,
                                  serial=getattr(args, "serial", None))
        print("启动 screenrecord 裸 H264 -> TCP 流...")
    else:
        from wzry.capture.scrcpy_stream import ScrcpyStreamCapture
        cap = ScrcpyStreamCapture(ROOT / "tools" / "scrcpy", rotate_s=1800,
                                  serial=getattr(args, "serial", None))
        print("启动 scrcpy 流...")
    cap.start()

    calib, _ = load_calibration()
    sm = MatchStateMachine(minimap_center_norm=calib.get("minimap_center", [0.086, 0.129]))

    print(f"鍔犺浇妫娴嬫鍨{args.model} ...")
    _yolo_threads = int(getattr(args, "yolo_threads", 0) or 0)
    det = YoloDetector(args.model, conf=0.35,
                       intra_threads=(_yolo_threads if _yolo_threads > 0 else None))
    print(f"[YOLO] intra_op 线程: {'ORT 默认(占满物理核)' if _yolo_threads <= 0 else _yolo_threads}")

    # v11.9 自动学习守护: 每120s检查 selfplay 新数据 -> 变化则 evolve_policy (防POST_MATCH漏触)
    try:
        import threading as _thg
        import subprocess as _spg
        import sys as _syg
        def _auto_learn_watch():
            _last_learn = 0.0
            _last_sp_mtime = 0.0
            while True:
                try:
                    import time as _tg
                    _spr = ROOT / "data" / "selfplay"
                    if _spr.exists():
                        _fs = sorted(_spr.glob("*.jsonl"), key=lambda f: f.stat().st_mtime)
                        if _fs:
                            _mt = _fs[-1].stat().st_mtime
                            _now = _tg.time()
                            if _mt > _last_sp_mtime:
                                if _now - _last_learn > 300.0:
                                    _last_learn = _now
                                    _r = _spg.run([_syg.executable, "-X", "utf8", "-u",
                                                   "scripts/evolve_policy.py"],
                                                  cwd=str(ROOT), capture_output=True, text=True, timeout=300)
                                    print(f"[自动学习守护] evolve: {_r.stdout.strip()[-100:]}")
                                # v12.8 每次新数据都生成中文学习网页 (实时更新)
                                _rg = _spg.run([_syg.executable, "-X", "utf8", "-u",
                                                "scripts/growth_report.py"],
                                               cwd=str(ROOT), capture_output=True, text=True, timeout=120)
                                print(f"[自动学习守护] 网页: {_rg.stdout.strip()[-50:]}")
                            _last_sp_mtime = _mt
                    _tg.sleep(60)
                except Exception as _e3:
                    print(f"[自动学习守护] err {_e3}")
                    import time as _tg2
                    _tg2.sleep(120)
        if not freeze_weights:  # v14.0 --freeze-weights: A/B 评估期间冻结规则权重
            _thg.Thread(target=_auto_learn_watch, daemon=True).start()
    except Exception as _e4:
        print(f"[自动学习守护] init err {_e4}")

    # 忓湴鍥捐窡韪細鍏堥獙 = 鏍噯鐐癸紙褰掍竴鍖-> 棣栦釜瀹為檯甯昂瀵告崲绠楋級
    mm_prior = None

    def make_tracker(frame):
        nonlocal mm_prior
        if mm_prior is None:
            h, w = frame.shape[:2]
            mc = calib.get("minimap_center", [0.086, 0.129])
            mm_prior = [int(mc[0] * w), int(mc[1] * h)]
        from wzry.vision.mm_tracker_v7 import MMTrackerV7
        return MMTrackerV7()

    tracker = None
    from wzry.vision.wall_sensor import WallSensor
    wall_sensor = WallSensor()

    if do_action:
        from wzry.action.executor_v2 import ActionExecutor
        # v14.0 原始触摸事件单独落盘(data/action_events.jsonl): 便于核对
        # "帧坐标 -> 设备像素"换算是否真的把触点送到正确位置(决策流 actions.jsonl
        # 只记 m2 语义动作 map_move{nx,ny}, 看不到实际发送的设备坐标)
        _evlog = ROOT / "data" / "action_events.jsonl"
        # v16.1 通道选择策略(见 wzry/control/channel.py): 执行互斥, 一次只产出一个
        # executor。auto = 优先板子, 探测不通回落 adb; --backend adb/esp32 为强制。
        from wzry.control.channel import ChannelResolutionError, resolve_channel, save_board_cfg
        try:
            _ch = resolve_channel(args.backend, board_host=args.board_host,
                                  board_port=args.board_port, board_mode=args.board_mode,
                                  board_size=args.board_size,
                                  probe_timeout=args.board_probe_timeout)
        except ChannelResolutionError as _e:
            raise SystemExit(str(_e))
        if _ch["channel"] == "esp32":
            # v14.2 真机通道: 板子当 HID 触摸屏, 电脑走 Wi-Fi。window_coords=True
            # 让板子执行器也吃 1280x720 帧坐标(校准表/上层都在这个空间)。
            from wzry.control.esp32_hid import Esp32HidExecutor
            _bw, _bh = (int(v) for v in str(_ch["size"]).lower().split("x"))
            _board = Esp32HidExecutor(host=_ch["host"], port=_ch["port"],
                                      mode=_ch["mode"], device_size=(_bw, _bh),
                                      event_log=_evlog, window_coords=True)
            try:
                _hello = _board.connect()
            except OSError as _e:      # 连接超时/被拒 -> 清晰提示, 不抛裸 Traceback
                raise SystemExit(f"连不上板子 {_ch['host']}:{_ch['port']} ({_e})"
                                 "（先确认板子已连 Wi-Fi 且固件在跑）")
            if _hello is None:
                raise SystemExit(f"板子 {_ch['host']}:{_ch['port']} 未按约定上报 hello"
                                 "（确认烧的是 firmware/esp32_hid_* 固件）")
            # 连成功才落盘: 下次 auto 不必再敲 --board-host(板子走 DHCP, IP 会变)
            save_board_cfg(_ch["host"], _ch["port"], _ch["mode"], _ch["size"])
            _hold_en = (args.stick_hold == "on")
            ex = ActionExecutor(_board, hold_enabled=_hold_en)
            print(f"执行模式: ESP32 HID 桥 {_ch['host']}:{_ch['port']} "
                  f"mode={_ch['mode']} size={_ch['size']} hello={_hello!r} "
                  f"摇杆={'持久触点' if ex.hold_enabled else 'swipe(回退)'} "
                  f"[{_ch['reason']}] (触摸事件记入 data/action_events.jsonl)")
        else:
            from wzry.control.executor import AdbExecutor
            ex = ActionExecutor(AdbExecutor(serial=getattr(args, "serial", None),
                                            event_log=_evlog),
                                hold_enabled=(args.stick_hold == "on"))
            # adb 无多点能力 -> hold_enabled 会被自动降级为 False(与改造前逐字节一致)
            print(f"执行模式: 真实操作 adb [{_ch['reason']}] "
                  f"摇杆={'持久触点' if ex.hold_enabled else 'swipe(回退)'} "
                  f"(触摸事件记入 data/action_events.jsonl)")
    else:
        ex = None
        print("鍔綔妯紡: 浠呰瀵燂紙榛樿 --no-action屼笉娉叆浠讳綍瑙懜涘姞 --action 鎵嶆墽琛岋級")

    recorder = MatchRecorder(base_dir=ROOT / "data" / "matches",
                             frame_every_s=float(getattr(args, "frame_every", 5.0)))
    from wzry.understanding.behavior import BehaviorTagger
    from wzry.feedback.reward import RewardSystem
    tagger = BehaviorTagger()
    reward = RewardSystem()
    cooldowns = {"skill1_t": 0.0, "skill2_t": 0.0, "skill3_t": 0.0,
                 "summoner_t": 0.0, "recall_t": 0.0, "hp_t": 0.0,
                 "restore_t": 0.0, "attack_t": 0.0,
                 "skill": 0.0, "hook_pending": 0.0,
                 "hook_anchor_dist": 0.0, "turret_threat": 0.0,
                 "hook_blocked": 0.0, "match_start_t": 0.0,
                 "roster": None, "roster_pending": 0.0,
                 # v16.0 P2 推进开关: off 时 decide() 完全不评估 push_ok/push_target
                 "push_mode": str(getattr(args, "push", "off"))}

    # ---- v16.3 内存直读（Route C）: 后台线程 ~2Hz 刷新，主循环零 adb 往返 ----
    #   补的是视觉层结构上读不到的字段（金币/等级/局内时间/单位血量/野怪在场）。
    #   只在 --mem 时启动；任何异常都降级为"没有内存数据"，不影响原有视觉链路。
    _memw = None
    if getattr(args, "mem", False):
        try:
            from wzry.vision.mem_battle import MemBattle, MemWorker
            _mb = MemBattle(serial=getattr(args, "serial", None) or "127.0.0.1:16384")
            _mb.connect()

            def _exp_clock():                      # 用墙钟推局内秒数，只作扫描窗口锚点
                hint = getattr(args, "mem_clock_hint", None)
                if hint is not None:
                    return int(hint + max(0.0, time.time() - _mem_t0))
                t0 = float(cooldowns.get("match_start_t") or 0.0)
                return int(max(0.0, time.time() - t0)) if t0 else None

            _mem_t0 = time.time()

            _memw = MemWorker(_mb, self_id=getattr(args, "mem_self_id", None),
                              interval=1.0 / max(0.2, float(getattr(args, "mem_hz", 2.0))),
                              expected_clock_fn=_exp_clock).start()
            print(f"[mem] 内存直读已开启: pid={_mb.pid} "
                  f"self_id={getattr(args, 'mem_self_id', None) or 'slot0'}")
        except Exception as _e:
            print(f"[mem] 内存直读启动失败(降级为纯视觉): {type(_e).__name__}: {_e}")
            _memw = None

    # ---- v14.2 执行不阻塞感知: 动作投递到单消费者 daemon 线程 ----
    # 症状: 同步 apply_action 让主循环在 adb input swipe(最长 900ms) 上等完才继续抓帧,
    #   实测感知频率掉到 1.23Hz(目标 >=2.5Hz), 决策"看到的是旧画面"。
    # 方案(顾问设计): 主循环只做"感知+决策+入队", 由唯一 worker 线程串行执行动作。
    #   单线程 FIFO = 与改造前完全相同的"无并发 adb 输入流"语义(executor 内部锁
    #   会串行化 adb 进程, 若主循环与 worker 同时发 swipe 会互相阻塞 -> 故主循环
    #   不再直接调用 ex, 全部经队列)。
    # 背压: 只在 worker 空闲时投递(队列最大深度 1) —— 若放开 FIFO, 感知 ~3Hz 而
    #   动作 ~1Hz, 队列会无限堆积, 动作会在决策很久之后才执行(状态已过期)。
    _action_q = queue.Queue()
    _exec_idle = threading.Event()
    _exec_idle.set()

    def _action_worker():
        while True:
            item = _action_q.get()
            if item is None:                 # 退出哨兵: finally 里 put(None) drain
                _action_q.task_done()
                break
            _exec_idle.clear()
            try:
                _act, _cds = item
                _t = _act.get("type")
                if _t == "stick_move":      # 连续摇杆保持(不碰 apply_action 语义)
                    # v16.2 优先持久触点(不松手, 单条 ≈20ms); 无多点能力时回退 swipe(原行为)
                    _th, _rr = float(_act.get("theta", 0.0)), float(_act.get("r", MOVE_R))
                    if not ex.move_hold(_th, _rr):
                        ex.move(_th, _rr, int(_act.get("duration_ms", 200)))
                elif _t == "stick_release":  # 主循环要求松手(对局阶段切换等)
                    ex.release_stick()
                    _cds.pop("_map_t", None)  # 松手即停: 停止期间不该累计"走过了多久"
                else:
                    apply_action(ex, _act, _cds)
            except Exception as _e:
                print(f"[exec] 异常(跳过): {_e}")
            finally:
                _action_q.task_done()
                _exec_idle.set()

    if do_action:
        threading.Thread(target=_action_worker, daemon=True).start()
        print("执行线程: 动作入队串行执行(主循环不阻塞)")

    # ---- S1-4 hok shadow（只读旁路，默认关）----
    _hok_shadow = None
    if getattr(args, "hok_shadow", False):
        try:
            from wzry.decision.hok_bridge import HokShadowWorker
            _shadow_out = Path(args.hok_shadow_out) if args.hok_shadow_out else (
                ROOT / "temp" / "hok_shadow" / "live.jsonl")
            _hok_shadow = HokShadowWorker(
                out_path=_shadow_out,
                hz=float(args.hok_shadow_hz),
                model=args.hok_shadow_model,
            ).start()
            if _hok_shadow.enabled:
                print(f"[hok-shadow] 已开启: model={args.hok_shadow_model} "
                      f"hz={args.hok_shadow_hz} -> {_shadow_out}")
            else:
                print(f"[hok-shadow] 启动失败(降级为关闭): {_hok_shadow.error}")
        except Exception as _e:
            print(f"[hok-shadow] 启动失败(降级为关闭): {type(_e).__name__}: {_e}")
            _hok_shadow = None

    # ---- v16.2 外部/文件驱动（--drive-stdin）----
    #   读 stdin 逐行动作 -> 同一套 _action_q/_action_worker（所以 hold/release 语义一致）。
    #   投递前等 _exec_idle，保持与主循环相同的"队列深度 ≤1"背压，避免动作过期。
    #   stdin 结束即置 _drive_done，让主循环收尾退出（否则纯文件回放会一直等到 --seconds）。
    _drive_done = threading.Event()
    if args.drive_stdin and do_action:
        def _stdin_driver():
            _n, _skip, _first_skip = _drive_run(
                sys.stdin,
                lambda _o: _action_q.put((_o, cooldowns)),
                is_idle=_exec_idle.is_set,
                pace_max=float(getattr(args, "drive_pace_max", 5.0)),
                is_done=_drive_done.is_set)
            print(f"[drive] stdin 结束, 共下发 {_n} 条动作, 跳过 {_skip} 行")
            if _first_skip is not None:
                print(f"[drive] 首条被跳过的行(检查 JSON 是否合法/是否缺 type): {_first_skip}")
            _drive_done.set()
        threading.Thread(target=_stdin_driver, daemon=True).start()
        print("驱动模式: --drive-stdin —— 从 stdin 读 JSON 动作执行；"
              "主循环自己的动作下发已关闭")
    elif args.drive_stdin:
        print("警告: --drive-stdin 与 --no-action 同时给出，驱动不会生效")


    # 鎶鑳芥寜閽儚绱犲潗鏍囷紙read_ui 鐢級
    with open(ROOT / "configs" / "calibration_absolute.json", encoding="utf-8") as _f:
        _pts = json.load(_f)["points"]
    skills_pts = {1: _pts["skill1"], 2: _pts["skill2"], 3: _pts["skill3"]}

    # confirm frames
    CONFIRM_FRAMES = 2
    confirm_streak = 0
    confirmed = False

    detect_interval = 1.0 / max(0.5, args.detect_hz)
    last_detect = 0.0
    last_log = 0.0
    last_sig = ("none", None)

    # ---- v14.3 主循环分阶段计时 ----
    _TIMING = bool(getattr(args, "timing", False))
    _T_ACC = {}          # 阶段名 -> [次数, 累计ms]
    _T_LOOP = [0, 0.0]   # [迭代次数, 主循环累计ms]

    def _t0():
        return time.perf_counter() if _TIMING else 0.0

    def _t1(t, name):
        if not _TIMING or not t:
            return
        _a = _T_ACC.setdefault(name, [0, 0.0])
        _a[0] += 1
        _a[1] += (time.perf_counter() - t) * 1000.0

    if _TIMING:
        print("[timing] 已开启主循环分阶段计时(收尾打印汇总)")
    frame_id = 0
    prev_phase = None
    n_frames = 0
    # v2.81 自我诊断(截图留存, 供 DeepSeek 视觉审检)
    try:
        from wzry.diagnose.self_check import SelfCheckDiagnostician
        self_check = SelfCheckDiagnostician(interval_s=20.0)
        self_check.start()
    except Exception:
        self_check = None
    # v2.83 行为习惯学习器(对面/队友/自己)
    try:
        from wzry.learning.behavior_mine import BehaviorMiner
        behavior_miner = BehaviorMiner()
        behavior_miner.start()
    except Exception:
        behavior_miner = None
    n_ticks = 0
    n_actions = 0
    infer_sum = 0.0
    _last_move = [None, None, 0.0]
    _hero_cls_mem = {}
    last_dead = False
    try:
        from wzry.learning.online_learner import OnlineLearner
        learner = OnlineLearner()
    except Exception:
        learner = None
    # yolo every N ticks（v16.3 改为可配，默认 3 = 原行为）
    yolo_every = max(1, int(getattr(args, "yolo_every", 3) or 3))
    yolo_count = 0
    cached_dets = []
    t_end = time.time() + args.seconds if not args.forever else float("inf")

    # v14.3 主循环(密集 cv2/numpy 调用)会把 GIL 抓得太紧, 采集 reader 线程被饿死 ->
    #   wait_frame 均 335ms(仅 ~3fps), 而空闲时采集能给 25-127fps。缩小切换间隔让出 CPU。
    #   实测 setswitchinterval 0.005->0.001 后 wait_frame 335->234ms, 总帧率 2.1->2.8fps。
    sys.setswitchinterval(0.001)

    print("M2 Agent v2 杩愯涓紙Ctrl+C 閫鍑猴級...\n")
    try:
        # v16.3 诊断: 统计主循环各提前退出点的分布（**纯计数，不改任何判定**）。
        #   背景: 真机实测感知只有 ~1Hz，而 --detect-hz 调到 100 也无变化 —— 说明闸门
        #   在更上游。这里把 6 个 continue 点分别计数，退出时打印占比，用数据点名闸门。
        _skips = {"no_frame": 0, "not_in_match": 0, "detect_interval": 0,
                  "mm_not_found": 0, "confirm_pending": 0, "no_hp_bar": 0}
        _phases = {}
        _nm_why = {"not_dark": 0, "green_low": 0, "blue_high": 0}
        _nm_vals = []
        while time.time() < t_end and not _drive_done.is_set():
            _tw = _t0()
            frame, lag_ms = cap.wait_frame(timeout=2.0)
            _t1(_tw, "wait_frame阻塞取帧")
            if frame is None:
                _skips["no_frame"] += 1
##                print("  鏃犲抚堟祦寮傚父)
                continue
            n_frames += 1
            _ts = _t0()
            phase = sm.update(frame)
            _phases[getattr(phase, "value", str(phase))] = \
                _phases.get(getattr(phase, "value", str(phase)), 0) + 1
            if __import__("os").environ.get("WZRY_DBG_PHASE") == "1" and n_frames <= 20:
                _el = (time.time() - sm._in_since) if getattr(sm, "_in_since", None) else -1.0
                print(f"[dbg] f={n_frames} phase={getattr(phase, 'value', phase)} "
                      f"streak={getattr(sm, '_in_match_streak', '?')} since={_el:.2f}s "
                      f"hold_s={getattr(sm, 'hold_s', '?')} "
                      f"feat={getattr(sm, 'last_feat', None)}")
            _t1(_ts, "状态机sm.update")
            now = time.time()
            # v2.81 self-diagnose feed (AI 审检截图留存)
            try:
                if self_check is not None:
                    self_check.feed(frame, dets)
            except Exception:
                pass
            if phase != MatchPhase.IN_MATCH:
                # v10.13 金名牌 ONLY 在真选人界面: 顶部中央有"等待其他玩家选择"白字
                #   且金色名牌在名排区才判; 开局/结算/大厅的画面不误判
                # v11.8 用户极简规则: 选人界面(未进游戏) 金色名字上=蓝/下=红
                #   检测: 金色像素(名字高亮 V>180 S<120) 上半屏 vs 下半屏, 多者为名排
                _sel_ok = False
                try:
                    # 选人页 = 顶部中央白字标题"等待…选择" + 无对局比分(右上无白字数字)
                    _sel_roi = frame[8:36, 380:900]
                    _hsv_sel = cv2.cvtColor(_sel_roi, cv2.COLOR_BGR2HSV)
                    _wsel = ((_hsv_sel[..., 1] < 80) & (_hsv_sel[..., 2] > 185)).sum()
                    _score_roi = frame[0:30, 1060:1280]
                    _srv = float(_score_roi.max()) if _score_roi.size else 255.0
                    _sel_ok = (_wsel > 40 and _srv < 240)
                except Exception:
                    _sel_ok = False
                # v12.7 阵营根治: 仅"选完英雄展示阶段"(上下排英雄卡+VS) 判金色名字上下
                _row_lock = None
                try:
                    from wzry.vision.camp_detect import detect_show_stage, gold_name_row
                    if detect_show_stage(frame):
                        _row2 = gold_name_row(frame)
                        _row_lock = "blue" if _row2 == "up" else ("red" if _row2 == "down" else None)
                except Exception:
                    _row_lock = None
                if _row_lock:
                    _prev_g = cooldowns.get("gold_row_prev")
                    _g_streak = int(cooldowns.get("gold_row_streak", 0))
                    if _prev_g == _row_lock:
                        _g_streak += 1
                    else:
                        _g_streak = 1
                    cooldowns["gold_row_prev"] = _row_lock
                    cooldowns["gold_row_streak"] = _g_streak
                    if _g_streak >= 2 and not cooldowns.get("camp"):
                        cooldowns["camp"] = _row_lock
                        cooldowns.pop("camp_votes", None)
                        print(f"[{datetime.now():%H:%M:%S}] 阵营判断(展示阶段金名牌): "
                              f"{'蓝方' if _row_lock == 'blue' else '红方'} → "
                              f"支援{'下路(射手)' if _row_lock == 'blue' else '上路(射手)'}")
                else:
                    cooldowns["gold_row_streak"] = 0
                # (旧金色检测已移除)
                # post match detect
                if prev_phase == MatchPhase.IN_MATCH and phase == MatchPhase.POST_MATCH:
                    _async_result_detect(frame, reward)
                    try:
                        import threading as _th2
                        def _auto_learn():
                            try:
                                import subprocess as _sp2, sys as _sy2
                                _r = _sp2.run([_sy2.executable, "-X", "utf8", "-u",
                                               "scripts/evolve_policy.py"],
                                              cwd=str(ROOT), capture_output=True, text=True, timeout=300)
                                print(f"[自动学习] evolve: {_r.stdout.strip()[-100:]}")
                            except Exception as _e:
                                print(f"[自动学习] evolve err {_e}")
                            try:
                                _al = ROOT / "data" / "auto_labeled"
                                if _al.exists() and len(list(_al.glob("*.png"))) >= 200:
                                    import subprocess as _sp3, sys as _sy3
                                    _r2 = _sp3.run([_sy3.executable, "-X", "utf8", "-u",
                                                    "scripts/train_detector_v4.py", "--skip-merge"],
                                                   cwd=str(ROOT), capture_output=True, text=True, timeout=1200)
                                    print(f"[自动学习] retrain: {_r2.stdout.strip()[-100:]}")
                            except Exception as _e2:
                                print(f"[自动学习] train err {_e2}")
                        if not freeze_weights:  # v14.0 冻结期间不自动 evolve
                            _th2.Thread(target=_auto_learn, daemon=True).start()
                    except Exception:
                        pass
                prev_phase = phase
                confirm_streak = 0
                confirmed = False
                _last_move[:] = (None, None, 0.0)   # 非对局松开摇杆
                if do_action:
                    # v16.2 持久摇杆是"物理按住": 只清 _last_move 不会松手, 英雄会继续走。
                    #   必须经队列让 worker 真正 t_up(主循环按设计不直接碰 ex)。
                    _action_q.put(({"type": "stick_release"}, cooldowns))
                if recorder.active:
                    recorder.close()
                    print(f"[{datetime.now():%H:%M:%S}] 瀵瑰眬缁撴潫屼細璇濆凡褰掓。")
                # learning log dump (throttled: cooldown 60s + learner 60s)
                try:
                    if learner is not None and \
                            now - float(cooldowns.get("post_dump_t", 0.0)) > 60.0:
                        cooldowns["post_dump_t"] = now
                        _dk = learner.dump_log(str(ROOT / "docs" / "LEARNING_LOG.md"))
                        if _dk:
                            print(f"[{datetime.now():%H:%M:%S}] [学习] 本局学习日志 -> "
                                  f"docs/LEARNING_LOG.md")
                except Exception:
                    pass
                _skips["not_in_match"] += 1
                try:
                    _d, _g, _b, _im = sm.last_feat
                    if not _d:
                        _nm_why["not_dark"] += 1
                    if _g <= sm.green_thr:
                        _nm_why["green_low"] += 1
                    if _b >= sm.blue_max:
                        _nm_why["blue_high"] += 1
                    _nm_vals.append((_g, _b))
                except Exception:
                    pass
                continue
            # v2.26 连续摇杆: 决策间隔里不让摇杆松手(续推 200ms)
            # v14.2 改投递到执行队列(由 worker 串行发 adb, 主循环不再直接碰 ex)
            # v16.2 持久摇杆模式下**不需要**续推: 原来的"续推"是为了对抗 swipe 的"松手即停",
            #   而按住不放本身就保持着方向。继续发只是白占一个执行槽 —— 队列深度 ≤1, 每多
            #   一条续推就挤掉一次真正的攻击/技能机会。故 hold 模式跳过。
            if (do_action and not ex.hold_enabled and not args.drive_stdin
                    and _last_move[0] is not None and time.time() < _last_move[2]):
                if _exec_idle.is_set():
                    _action_q.put(({"type": "stick_move", "theta": _last_move[0],
                                    "r": _last_move[1], "duration_ms": 200}, cooldowns))
            if now - last_detect < detect_interval:
                _skips["detect_interval"] += 1
                continue
            last_detect = now

            # ---- 纭鍒----
            if tracker is None:
                tracker = make_tracker(frame)
            _tm = _t0()
            mm = tracker.update(frame)
            _t1(_tm, "小地图tracker.update")
            # v11.4 一边打一边学: 采集地图内+外自标帧 (约2s)
            try:
                if now - float(cooldowns.get("auto_cap_t", 0.0)) >= 2.0:
                    cooldowns["auto_cap_t"] = now
                    from scripts.auto_capture import capture_once
                    capture_once(frame, det)
            except Exception:
                pass
            # v5.0 模型小地图点融合: yolo v5 直接检测小地图上的 mm_red/mm_blue/mm_green
            #   -> 模型输出作为权威红/蓝/绿点 (mm_rules 硬规则退为兜底)
            try:
                if hasattr(det, "detect_minimap"):
                    _mm_crop = frame[0:232, 0:232]
                    _mmdets = det.detect_minimap(_mm_crop)
                    _md = _mmdets or []
                    _mred = [d for d in _md if d.cls == "mm_red" and d.conf >= 0.60]
                    _mblue = [d for d in _md if d.cls == "mm_blue" and d.conf >= 0.60]
                    _mgreen = [d for d in _md if d.cls == "mm_green" and d.conf >= 0.60]
                    if _mred or _mblue or _mgreen:
                        _msz = 232.0
                        # v5.2 统一2元组输出(弃conf): 老代码全部按(x,y)解包, 带conf会崩
                        def _mm_n(d):
                            return [round(d.center[0] / _msz, 4),
                                    round(d.center[1] / _msz, 4)]
                        mm["dots"]["red"] = [_mm_n(d) for d in _mred] or mm["dots"]["red"]
                        mm["dots"]["blue"] = [_mm_n(d) for d in _mblue] or mm["dots"]["blue"]
                        mm["dots"]["green"] = [_mm_n(d) for d in _mgreen] or mm["dots"]["green"]
            except Exception:
                pass
            if not mm["found"]:
                _skips["mm_not_found"] += 1
                if not confirmed:
                    confirm_streak = 0
                    print(f"[{datetime.now():%H:%M:%S}] 対局确认中（小地图暂未定位）...")
                continue
            confirm_streak += 1
            if confirm_streak < CONFIRM_FRAMES:
                _skips["confirm_pending"] += 1
                confirmed = False
                print(f"[{datetime.now():%H:%M:%S}] 瀵瑰眬纭涓{confirm_streak}/{CONFIRM_FRAMES} ...")
                continue
            # v3.0 缁挎潯閾佽瘉氶娆＄璁姹傛弧琛缁挎潯0.5 = 鏉>57px夆斺            # 瀵瑰眬鍦硥姘村嚭鐢熷繀婊涗富椤缁撶畻璇缁挎潯浠26-29px 琚帓闄紱
            # 宸茬璁悗涓嶅啀妫鏌紙闃典骸/娈嬭鏃舵棤婊鏉絾浠嶅湪瀵瑰眬涓級
            # v2.51 蹇熼氶亾: 忓湴鍥惧凡妫鍑哄崟浣钃绾缁跨偣) => 瀵瑰眬閾佽瘉, 鐩存帴纭(涓嶇瓑缁挎潯)
            if not confirmed:
                from wzry.vision.self_bars import self_hp_mp
                _hp, _mp, _pos = self_hp_mp(frame)
                _fast = False
                try:
                    _dots = (mm.get("dots") or {})
                    _fast = bool(_dots.get("blue") or _dots.get("red") or
                                 _dots.get("green")) and mm.get("found")
                except Exception:
                    _fast = False
                if _fast:
                    print(f"[{datetime.now():%H:%M:%S}] 瀵瑰眬纭(忓湴鍥鹃搧璇")
                    confirmed = True
                    confirm_streak = CONFIRM_FRAMES
                elif _hp is None or _hp < 0.5:
                    _skips["no_hp_bar"] += 1
                    confirm_streak = 0
                    print(f"[{datetime.now():%H:%M:%S}] 瀵瑰眬纭涓紙鏃犳弧琛缁挎潯岄潪瀵瑰眬鐢婚潰..")
                    continue
            confirmed = True
            # match start time
            if not cooldowns.get("match_start_t"):
                cooldowns["match_start_t"] = now
                # v2.95 新对局开始: 清零行为层死亡残留(上局 _dead_since 污染 -> 开局假died)
                try:
                    tagger.reset_match()
                except Exception:
                    pass

            # ---- v10.12 用户铁律: 阵营 ONLY by 选人界面金名牌!(绿点检测完全删除, 不可信) ----
            #   金名牌已在上方 phase!=IN_MATCH 锁定 (上排=蓝/下排=红), 这里不覆盖/不投票
            if not cooldowns.get("camp") and not cooldowns.get("camp_from_gold"):
                # 金名牌还没锁(选人页未过) -> 不判, 默认蓝(等选人页或下一步); 绝不用绿点
                pass

            # ---- 寮灞闃靛璇嗗埆坴2.10歮odlens 璇婚夎嫳闆勭晫闈枃瀛楋紝鍚挓棣椾晶=鎴戞柟---
            # 鐢悗鍙扮嚎绋嬮伩鍏嶉樆濉炰富寰幆涚粨鏋滃瓨鍏cooldowns["roster"]
            if not cooldowns.get("roster") and not cooldowns.get("roster_pending"):
                cooldowns["roster_pending"] = now
                import threading as _th
                _frame_cp = frame.copy()

                def _roster_worker(fr):
                    try:
                        import subprocess as _sp
                        import sys as _sys
                        tmp = ROOT / "temp" / "roster_live.png"
                        import cv2 as _cv2
                        _cv2.imwrite(str(tmp), fr)
                        prompt = ('List heroes seen above: JSON {"upper": [], "lower": []}')
                        r = _sp.run(
                            [_sys.executable, "-X", "utf8",
                             str(ROOT / "scripts" / "train" / "modlens_ask.py"),
                             str(tmp), prompt],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=180)
                        if r.returncode != 0:
                            print(f"[roster] modlens 失败: {r.stderr[:150]}")
                            return
                        txt = r.stdout.strip()
                        start, end = txt.find("{"), txt.rfind("}")
                        if start < 0 or end < 0:
                            print(f"[roster] 解析失败: {txt[:150]}")
                            return
                        import json as _json
                        data = _json.loads(txt[start:end + 1])
                        upper, lower = data.get("upper", []), data.get("lower", [])
                        if "閽熼" in upper:
                            ally, enemy = upper, lower
                        else:
                            ally, enemy = lower, upper
                        cooldowns["roster"] = {"ally": ally, "enemy": enemy,
                                               "self_hero": "閽熼"}
                        print(f"[roster] 我方 {ally} | 敌方 {enemy}")
                    except Exception as e:
                        print(f"[roster] 异常: {e}")
                    finally:
                        cooldowns["roster_pending"] = 0.0

                _th.Thread(target=_roster_worker, args=(_frame_cp,), daemon=True).start()

            # ---- 鎰熺煡坴2.7歒OLO 闄嶉岀紦瀛樺鐢級----
            yolo_count += 1
            if yolo_count >= yolo_every:
                yolo_count = 0
                _ty = _t0()
                cached_dets = det.detect(frame)
                _t1(_ty, "YOLO检测")
            dets = list(cached_dets)
            # v2.91 野怪互斥(源头): neutral_monster 框边距20px内重叠的 enemy_hero = 误标野怪 -> 剔除;
            #    低置信 enemy_hero (0.35-0.55) 且无 monster 佐证也有风险, 置信门槛 0.55 放行
            # v2.98 体型上限: 暴君/主宰 box 宽~276px >> 英雄(<=200px); 暴君血条厚 10-12px
            #    (英雄 5-11px). 宽>215 或 高>270 的 enemy_hero = 巨兽(暴君/主宰/大龙), 剔除
            try:
                _mon_b = [(d.xyxy[0], d.xyxy[1], d.xyxy[2], d.xyxy[3])
                          for d in dets if d.cls == "neutral_monster"]
                if _mon_b:
                    def _near_mon(d):
                        cx, cy = d.center[0], d.center[1]
                        for (mx0, my0, mx1, my1) in _mon_b:
                            if cx >= mx0 - 20 and cx <= mx1 + 20 \
                                    and cy >= my0 - 20 and cy <= my1 + 20:
                                return True
                        return False
                    dets = [d for d in dets
                            if not (d.cls == "enemy_hero" and _near_mon(d))]
                def _not_beast(d):
                    if d.cls != "enemy_hero":
                        return True
                    _bw = d.xyxy[2] - d.xyxy[0]
                    _bh = d.xyxy[3] - d.xyxy[1]
                    return not (_bw > 215 or _bh > 270)
                dets = [d for d in dets if _not_beast(d)]
                # v4.1 屏幕候选过 ROI 分类器: enemy_hero 抠图必须被判 enemy_hero(>=0.60)
                #   neutral_monster/self/ally -> 剔除(野怪/队友/自己误标为敌英的根治)
                try:
                    from wzry.vision.roi_cls_gate import classify_roi
                    _dets2 = []
                    for _d in dets:
                        if _d.cls == "enemy_hero":
                            x1, y1, x2, y2 = [int(v) for v in _d.xyxy]
                            _pad = max(6, (y2 - y1) // 8)
                            roi = frame[max(0, y1 - _pad):y2 + _pad,
                                        max(0, x1 - _pad):x2 + _pad]
                            if roi.size == 0:
                                continue
                            roi = cv2.resize(roi, (96, 96), interpolation=cv2.INTER_AREA)
                            _nm, _nc = classify_roi(roi)
                            if _nm == "enemy_hero" and _nc >= 0.60:
                                _dets2.append(_d)
                            elif _nm is None:
                                _dets2.append(_d)   # 分类器故障不拦截
                            elif _nm == "neutral_monster" and _nc >= 0.60:
                                continue            # 野怪(暴君/龙) -> 剔除, 不打野怪
                            # else: self/ally/蓝绿 -> 保留(场上有自己/队友, 不误剔)
                        else:
                            _dets2.append(_d)
                    dets = _dets2
                except Exception:
                    pass
            except Exception:
                pass
            # hero identity bar-top color guard
            try:
                _hsv0 = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                # v15.2: uint8 视图(阈值均 <256, 与原 astype(int) 逐像素等价) —— 原写法每帧
                #   把两个全帧通道升成 int64, 实测 ~6-8ms/帧
                _h0, _s0 = _hsv0[..., 0], _hsv0[..., 1]
                for _d in dets:
                    if _d.cls not in ("enemy_hero", "ally_hero"):
                        continue
                    # v14.2 B.2: 检测器已用"框上方血条颜色"给出高置信阵营(camp_src="bar")
                    # -> 不要再用不可靠的"框内蓝/红像素"复核覆盖它(那会把判对的英雄翻回去)。
                    if getattr(_d, "camp_src", None) == "bar":
                        continue
                    x1, y1, x2, y2 = _d.xyxy
                    _cy0 = int(y1); _cy1 = int(min(frame.shape[0], y1 + max(2, (y2 - y1) // 6)))
                    _cx = int((x1 + x2) // 2)
                    if _cy1 - _cy0 < 2 or _cx - 18 < 0 or _cx + 18 >= frame.shape[1]:
                        continue
                    _ph = _h0[_cy0:_cy1, _cx - 18:_cx + 18]
                    _ps = _s0[_cy0:_cy1, _cx - 18:_cx + 18]
                    _nr = int((((_ph <= 15) | (_ph >= 165)) & (_ps > 60)).sum())
                    _nb = int((((_ph >= 90) & (_ph <= 135)) & (_ps > 60)).sum())
                    _side = None
                    if _nb > _nr + 4 and _nb >= 4:
                        _side = "ally"
                    elif _nr > _nb + 4 and _nr >= 4:
                        _side = "enemy"
                    if _side is None:
                        continue
                    # hysteresis: 2-frame same side
                    _bk = (int(_cx // 40), int(_cy0 // 40))
                    _mem = _hero_cls_mem.get(_bk)
                    if _mem and _mem[0] == _side:
                        _hero_cls_mem[_bk] = (_side, _mem[1] + 1)
                        if _mem[1] + 1 >= 2:
                            if _side == "ally" and _d.cls == "enemy_hero":
                                _d.cls = "ally_hero"
                            elif _side == "enemy" and _d.cls == "ally_hero":
                                _d.cls = "enemy_hero"
                    elif _mem is None:
                        _hero_cls_mem[_bk] = (_side, 1)
            except Exception:
                pass
            _tb = _t0()
            st = build_state(frame, dets, phase.value, minimap={
                "found": mm["found"], "center": mm["center"], "radius": mm["radius"],
                "dots": mm["dots"], "towers": mm["towers"],
                # v16.0 P0-F2: 分阵营键也要注入（旧键保留，向后兼容）——
                #   否则 decide() 看不到敌/我阵营的塔与兵线（mm_tracker_v7 早就产出了）
                "towers_v7": mm.get("towers_v7"), "v7": mm.get("v7"),
                "minions": mm.get("minions"),
            }, frame_id=frame_id)
            _t1(_tb, "build_state")
            frame_id += 1
            st.t = time.time()  # 决策时刻
            state_dict = st.to_dict()
            # v2.65 鏁岃嫳寮哄埗娉叆 (涓嶄緷璧build/filter: dets 绾殑鏁岃嫳蹇呰繘 units)
            try:
                # v2.91 野怪互斥: neutral_monster 框与 enemy_hero 框重叠(IoU>0.15/中心距<40px)
                #     -> 该 enemy_hero 实为野怪(yolo 常把野猪/龙误标 enemy_hero 0.35-0.55)
                _mon_b = []
                for _d in dets:
                    if _d.cls == "neutral_monster":
                        _mon_b.append((_d.xyxy[0], _d.xyxy[1], _d.xyxy[2], _d.xyxy[3]))
                def _overlaps_monster(_d):
                    if not _mon_b:
                        return False
                    _cx, _cy = _d.center[0], _d.center[1]
                    _w2, _h2 = (_d.xyxy[2] - _d.xyxy[0]) / 2.0 + 15, (_d.xyxy[3] - _d.xyxy[1]) / 2.0 + 15
                    for (mx0, my0, mx1, my1) in _mon_b:
                        if abs(_cx - (mx0 + mx1) / 2) < _w2 + (mx1 - mx0) / 2 \
                                and abs(_cy - (my0 + my1) / 2) < _h2 + (my1 - my0) / 2:
                            return True
                    return False
                _de = [d for d in dets if d.cls == "enemy_hero"
                       and d.conf >= 0.55        # v2.91 敌英置信下限(野怪误标通常 0.35-0.52)
                       and not _overlaps_monster(d)]
                if _de and not any(u.get("cls") == "enemy_hero" for u in state_dict["units"]):
                    for _d in _de:
                        state_dict["units"].append({
                            "cls": "enemy_hero",
                            "kind": "hero",
                            "screen": [_d.center[0] / w, _d.center[1] / h,
                                       (_d.xyxy[2] - _d.xyxy[0]) / w,
                                       (_d.xyxy[3] - _d.xyxy[1]) / h],
                            "conf": float(_d.conf),                            # v14.2
                            "camp_src": getattr(_d, "camp_src", None),         # v14.2
                            "visible": True,
                            "coord_src": "screen",
                            "src": "vision",
                            "last_seen_t": state_dict.get("t") or time.time()})
                # v2.78 鏁岃嫳蹇呴』甯孩鏉>=5px): None(鏃犳潯)=閲庢璇 -> 鍓旈櫎(鎵撹嚜瀹堕噹鎬墸鍒嗗皝鍫
                from wzry.vision.self_bars import hero_bar_check
                _kept_u = []
                for _u in state_dict.get("units") or []:
                    _ucls = str(_u.get("cls", ""))
                    # v8.1 塔/小兵/水晶 一律屏蔽(红方误标自家为敌方, 阵营同观不分) -> 只留英雄
                    if _ucls in ("enemy_turret", "ally_turret", "enemy_minion",
                                 "ally_minion", "enemy_crystal", "ally_crystal"):
                        continue
                    if _ucls != "enemy_hero":
                        _kept_u.append(_u)
                        continue
                    _s = _u.get("screen") or [0.5, 0.5, 0.05, 0.05]
                    _cx = float(_s[0]) * w
                    _cy = (float(_s[1]) + float(_s[3]) * 0.6) * h
                    _bh = hero_bar_check(frame, _cx, _cy)
                    if _bh is None or _bh < 5:
                        continue          # 鏃犳潯/钖勬潯=閲庢垨璇, 鍓旈櫎(涓嶆墦浜
                    if _bh >= 9:
                        _kept_u.append(_u)   # 条厚>=9px=英雄(野怪条通常<=8px, 但龙/暴君更粗
                    else:
                        # v2.91 薄条(5-8px)需二次证据: 小地图同侧有红点或 yolo 置信>=0.85
                        _mm_red_u = ((mm.get("dots") or {}).get("red") or [])
                        _hi_c = any(
                            (abs(d.center[0] / w - _s[0]) < 0.30 and
                             abs(d.center[1] / h - _s[1]) < 0.30 and d.conf >= 0.85)
                            for d in dets if d.cls == "enemy_hero")
                        if _hi_c or _mm_red_u:
                            _kept_u.append(_u)
                state_dict["units"] = _kept_u
            except Exception:
                pass
            # v14.3 类屏蔽必须**无条件**生效: 上面那段(含血条复核)整块在 try 里, 一旦抛
            #   异常就被静默跳过 -> units 混入 塔/小兵/水晶, 与 decide() 的输入口径不一致,
            #   也污染 P2 数据集。实测同一版代码: 有些对局录到 enemy_turret/ally_crystal,
            #   有些没有 -> 就是这条偶发路径。
            _BLOCK_CLS = ("enemy_turret", "ally_turret", "enemy_minion",
                          "ally_minion", "enemy_crystal", "ally_crystal")
            state_dict["units"] = [u for u in (state_dict.get("units") or [])
                                   if str(u.get("cls")) not in _BLOCK_CLS]
            # ---- 鐞嗚灞傚寮猴紙v2.12夛細鑷繁HP/MP + 鏁屼汉绾潯 + 闃熷弸鏉+ 闃靛 ----
            if now - float(cooldowns.get("hp_t", 0.0)) >= 0.5:
                cooldowns["hp_t"] = now
                try:
                    from wzry.vision.self_bars import self_hp_mp, detect_all_bars, death_banner_px, kill_banner_px
                    # v2.87 死亡横幅铁证 -> ui.death_banner (行为层判死依据)
                    # v2.96 击杀播报金条 -> ui.kill_banner (kill/assist 铁证, 旧"消失=击杀"为假事件)
                    _ui = state_dict.setdefault("ui", {})
                    _ui["death_banner"] = death_banner_px(frame)
                    _ui["kill_banner"] = kill_banner_px(frame)
                    hp, mp, hero_pos = self_hp_mp(frame)
                    if hp is not None:
                        state_dict.setdefault("ui", {})["hp"] = hp
                    if mp is not None:
                        state_dict.setdefault("ui", {})["mp"] = mp
                    if hero_pos:
                        state_dict.setdefault("extra", {})["hero_pos"] = list(hero_pos)
                    # v16.0 自己是否在场：行为层用它区分"我在打"与"我在看别人/已死亡"。
                    #   hp 读不到 且 小地图无自身绿点 -> 非本人视角（观战/跨局/刚复活）。
                    _self_dots = (((state_dict.get("minimap") or {}).get("dots") or {})
                                  .get("green") or [])
                    _ui["self_present"] = bool(hp is not None or _self_dots)
                    # v2.37 绾钃濇潯妫娴嬮檷棰韬唤鍒畾宸蹭互忓湴鍥句负璇 鏉娴嬩粎杈呭姪杩藉嚮)
                    if now - float(cooldowns.get("bars_t", 0.0)) > 0.4:
                        bars = detect_all_bars(frame)
                        cooldowns["bars_t"] = now
                        if bars["enemies"]:
                            state_dict.setdefault("extra", {})["enemy_bars"] = [
                                (b["x"], b["y"], b["w"]) for b in bars["enemies"]]
                        if bars["allies"]:
                            state_dict.setdefault("extra", {})["ally_bars"] = [
                                (b["x"], b["y"], b["w"]) for b in bars["allies"]]
                        state_dict.setdefault("extra", {})["ally_bars"] = [
                            (b["x"], b["y"], b["w"]) for b in bars["allies"]]
                except Exception:
                    pass
                # 技能状态（校准坐标）
                try:
                    from wzry.vision.ui_reader import skill_ready_state
                    ss = skill_ready_state(frame, skills_pts)
                    state_dict.setdefault("ui", {})["skill_states"] = ss
                    # 平铺一份 0/1 到 ui（编码器直接读；skill_states 本身是嵌套 dict）
                    for _sid in (1, 2, 3):
                        _r = (ss.get(_sid) or ss.get(str(_sid)) or {}).get("ready")
                        if isinstance(_r, bool):
                            state_dict["ui"][f"skill{_sid}_ready"] = _r
                except Exception as _e:
                    # v15.0: 原来是裸 pass -> 真机上 skill_states 从没进过数据也没人发现
                    #   （实测录到的 states.jsonl 里 100% 缺 skill_states）。改成只报一次。
                    if not cooldowns.get("_skill_state_warned"):
                        cooldowns["_skill_state_warned"] = True
                        print(f"[ui] 技能图标状态读取失败(只报一次): "
                              f"{type(_e).__name__}: {_e}")
                # v15.0 敌方/队友血量比例(顶部头像条): 原来整条链路都没有这项读数
                try:
                    from wzry.vision.top_status_bars import read as _tsb_read
                    _tb = _tsb_read(frame)
                    if _tb.get("found"):
                        _u2 = state_dict.setdefault("ui", {})
                        if _tb.get("enemy_hp_min") is not None:
                            _u2["enemy_hp_min"] = _tb["enemy_hp_min"]
                        if _tb.get("ally_hp_min") is not None:
                            _u2["ally_hp_min"] = _tb["ally_hp_min"]
                        state_dict.setdefault("extra", {})["top_bars"] = _tb["bars"]
                except Exception:
                    pass
                # v15.1 敌方血量比例(场景内头顶红条长度): 顶部头像条是**条件性元素**
                #   (只在锁定/攻击敌方时出现), 而场景内红条只要敌人可见就有 -> 用它填
                #   enemy_hp_min。顶部条若存在则优先(那是"当前锁定目标"的精确读数)。
                try:
                    from wzry.vision.hero_hp import enemy_hp_from_dets as _ehp
                    _rs = _ehp(frame, dets)
                    if _rs:
                        _u3 = state_dict.setdefault("ui", {})
                        if _u3.get("enemy_hp_min") is None:
                            _u3["enemy_hp_min"] = min(_rs)
                        state_dict.setdefault("extra", {})["enemy_hp_scene"] = _rs
                except Exception:
                    pass
                # 阵容已识别（roster 来自外部 modlens 服务，可能为 None）
                if cooldowns.get("roster"):
                    state_dict.setdefault("extra", {})["roster"] = cooldowns["roster"]
            # v14.3 hp/mp 回填: 上面那块有 0.5s 节流, 而 state_dict 每拍重建 -> 被节流跳过的
            #   那一拍 ui.hp/mp 直接缺失(实测缺失率 30%), 决策层(接战门槛/回城门禁)只能退到
            #   保守值 0.6。用上一拍读数回填(陈旧 <=0.5s, 远好于退到 0.6); 读到新值时更新缓存。
            _uic = state_dict.setdefault("ui", {})
            for _k in ("hp", "mp"):
                if _uic.get(_k) is None:
                    _lastv = cooldowns.get("last_" + _k)
                    if _lastv is not None:
                        _uic[_k] = _lastv
                else:
                    cooldowns["last_" + _k] = _uic[_k]
            # v15.4 顶部"锁定目标条"(选中/正在攻击敌方英雄时才出现) -> 状态层:
            #   上面那块读的是"每个可见敌人"的最小血量; 这条是"我正在打的那一个"的实时血量,
            #   是"该收/该撤"最直接的依据。放在**每拍**(不跟上面 0.5s 节流): 只扫 187x17 的小条,
            #   开销可忽略; 而"有没有锁定目标"必须每拍都新 —— 拿上一拍的残血值当现在会误导决策。
            #   约定: 没有锁定目标时 lock_target_hp **不写**(缺失), 用它的人必须先看 lock_target。
            try:
                from wzry.vision.hero_hp import lock_target as _lt
                _ltr = _lt(frame)
                _u5 = state_dict.setdefault("ui", {})
                _u5["lock_target"] = bool(_ltr.get("present"))
                if _ltr.get("present") and _ltr.get("ratio") is not None:
                    _u5["lock_target_hp"] = _ltr["ratio"]
                state_dict.setdefault("extra", {})["lock_target"] = _ltr
            except Exception:
                pass
            # ---- v16.3 内存直读合并（零 adb 往返，只读后台线程的最新快照）----
            #   写入 ui.gold/level/exp/KDA/match_clock（视觉层没有的），
            #   并在视觉没读到 enemy_hp_min/ally_hp_min 时用精确血量比例补上。
            #   陈旧（>3s 未刷新）时 apply() 直接返回 {}，不给过期值。
            if _memw is not None:
                try:
                    _memw.apply(state_dict)
                except Exception:
                    pass
            n_ticks += 1
            infer_sum += det.last_infer_ms

            # ---- 瑙勫垯鍐崇瓥 + v2.40 鏈浼樺喅绛栧櫒(鏋氫妇鍊欓>浠峰艰瘎浼>閫夋渶浼 <2ms) ----
            _td = _t0()
            action = decide(state_dict, cooldowns)
            _t1(_td, "规则决策decide")
            if _hok_shadow is not None:
                try:
                    _hok_shadow.submit(state_dict)
                except Exception:
                    pass
            # v16.0 P2 推进: 把本拍的 push 评估结果挂到状态上（render_live_analysis 落盘用）
            _exd = state_dict.get("extra")
            if isinstance(_exd, dict):
                _exd["push"] = cooldowns.get("push_info")
            # v11.4 操作-状态-得分记录 (selfplay)
            try:
                from wzry.learn.state_action_recorder import record as _sarec
                _sarec(state_dict, cooldowns, action, reward.total, now)
            except Exception:
                pass
            try:
                from wzry.ui.osd import show as _osd2
                _hp_pct = int(((state_dict.get("ui") or {}).get("hp") or 0) * 100)
                _mp_pct = int(((state_dict.get("ui") or {}).get("mp") or 0) * 100)
                _osd2(cooldowns.get("camp") or "?",
                      ("支援下路(射手)" if cooldowns.get("camp") == "blue"
                       else "支援上路(射手)" if cooldowns.get("camp") == "red" else "判定中"),
                      f"HP{_hp_pct}% MP{_mp_pct}% [{str(action.get('type',''))}]",
                      str(action.get("reason", ""))[:24])
            except Exception:
                pass
            try:
                from wzry.ui.osd import show as _osd2
                _osd2(cooldowns.get("camp") or "?",
                      ("支援下路(射手)" if cooldowns.get("camp") == "blue"
                       else "支援上路(射手)" if cooldowns.get("camp") == "red" else "判定中"),
                      str(action.get("type", "")), str(action.get("reason", ""))[:24])
            except Exception:
                pass
            # ---- v17.0 --ai-arch official：官方架构复刻版决策 ----
            #   特征（按类别定长向量）-> 打分 -> top-k -> 规则覆盖 -> 回退。
            #   异常不阻断：任何失败都退回下面的 legacy 路径（保底=既有规则/优化器）。
            if getattr(args, "ai_arch", "legacy") == "official":
                try:
                    from wzry.decision.official_ai import OfficialAI, decide_legacy
                    if _OFFICIAL_STATE["ai"] is None:
                        _dly = int(getattr(args, "ai_delay_frames", -1))
                        _OFFICIAL_STATE["ai"] = OfficialAI(
                            level=getattr(args, "ai_level", "hard"),
                            rule_mode=getattr(args, "ai_rules", "all"),
                            predict_interval=float(
                                getattr(args, "ai_predict_interval", 0.0) or 0.0),
                            delay_frames=(None if _dly < 0 else _dly))
                    _ui0 = state_dict.get("ui") or {}
                    _oact, _odec = decide_legacy(
                        state_dict, _OFFICIAL_STATE["ai"],
                        self_id=getattr(args, "mem_self_id", None),
                        atk_range=float(_ui0.get("atk_range") or 300.0))
                    action = _oact
                    _OFFICIAL_STATE["n"] += 1
                    if _odec.hits or _odec.scene != "default":
                        # 规则命中/场景落成**旁挂字段**，不改 `reason`：
                        #   `reason` 会被 state_action_recorder 当标签字符串写进数据集，
                        #   往里拼规则名会污染训练标签。
                        action = dict(action)
                        if _odec.hits:
                            action["ai_rule"] = f"{_odec.hits[0].name}:{_odec.hits[0].verdict}"
                        action["ai_scene"] = _odec.scene
                    if _laya is not None:
                        _laya.set_context(frame_id=state_dict.get("frame_id"))
                except Exception as _oae:
                    _OFFICIAL_STATE["err"] = f"{type(_oae).__name__}: {_oae}"
            else:
                try:
                    from wzry.decision.optimizer import solve as _solve
                    _hp0 = float((state_dict.get("ui") or {}).get("hp") or 1.0)
                    _camp0 = cooldowns.get("camp") or "blue"
                    if _laya is not None:      # v14.2 决策点帧号, 便于离线按帧对齐
                        _laya.set_context(frame_id=state_dict.get("frame_id"))
                    action = _solve(state_dict, action, _hp0, _camp0)
                except Exception as _oe:
                    pass  # 浼樺寲鍣紓甯镐笉闃绘柇(淇濆簳=瑙勫垯鍐崇瓥)
            # ---- v15.1 --hunt: 主动找敌方（默认关；规则栈被"只走中/下路走廊"的硬约束
            #   锁住, 红点在上路/河道时永远走不到 -> 取证"顶部目标头像/敌方血条"需要它）----
            if getattr(args, "hunt", False):
                try:
                    _cls_of2 = (lambda d: d.get("cls") if isinstance(d, dict)
                                else getattr(d, "cls", None))
                    _has_eh = any(_cls_of2(d) == "enemy_hero" for d in dets)
                    _reds = (((mm.get("dots") or {}).get("red") or [])
                             if mm.get("found") else [])
                    if (not _has_eh) and _reds and action.get("type") in ("none", "map_move"):
                        _g = (((mm.get("dots") or {}).get("green") or [(0.5, 0.5)])[0])
                        _t = min(_reds, key=lambda p: (p[0] - _g[0]) ** 2 + (p[1] - _g[1]) ** 2)
                        action = {"type": "map_move", "nx": float(_t[0]), "ny": float(_t[1]),
                                  "reason": "hunt_red_dot"}
                except Exception:
                    pass
            # ---- v15.0 事件记忆层：喂当前帧 -> 事件/冷却/朝向（u4） ----
            _mem = None
            try:
                _mem = _events.update(state_dict, now)
                if action.get("type") in ("skill", "summoner"):
                    _events.note_cast(int(action.get("id") or 0), now, source="rule")
                if _profile is not None and now - float(cooldowns.get("_advise_t", 0.0)) > 10.0:
                    cooldowns["_advise_t"] = now
                    from wzry.decision import upgrade_policy as _up
                    from wzry.decision import item_policy as _ip
                    _ug = _up.suggest(state_dict, _profile)
                    _it = _ip.plan(state_dict, _profile)
                    _gate = _ip.purchase_gate(
                        state_dict, _it.get("price"),
                        in_combat=bool((_mem or {}).get("n_enemy_scr")),
                        in_fountain=False)
                    print(f"[决策支线] {_up.format_line(_ug)} | {_ip.format_line(_it, _gate)}")
            except Exception:
                pass
            # ---- v15.1 UI导航层：阵亡时读"游戏自动弹出的"面板（零点击=最安全路径）
            #   面板内容回填到状态层(ui，走 uiquery 的 ALLOWED_FIELDS 白名单)与记忆层
            if _uiq is not None and now - float(cooldowns.get("_uiq_t", 0.0)) > 1.0:
                try:
                    if _is_panel_open is not None and _is_panel_open(frame):
                        cooldowns["_uiq_t"] = now
                        _f = _uiq.read("death_recap", frame) or {}
                        if _f:
                            state_dict.setdefault("extra", {})["panel_death"] = _f
                            _uu = state_dict.setdefault("ui", {})
                            for _k, _v in _f.items():
                                _uu[_k] = _v
                            _events.note_panel("death_recap", _f, now)
                except Exception:
                    pass
            # ---- v15.0 BCNet 策略：shadow=只记录不接管 / bc=接管但必须过安全层 ----
            if _policy is not None:
                try:
                    _dpx = (state_dict.get("ui") or {}).get("death_banner")
                    _dead_now = bool(isinstance(_dpx, (int, float)) and _dpx >= 2500)
                    _reborn = bool(cooldowns.get("_last_dead")) and not _dead_now
                    if _reborn:
                        _policy.reset("respawn")
                    cooldowns["_last_dead"] = _dead_now
                    _pa = _policy.decide(state_dict, reset=_reborn)
                    if _policy_mode == "shadow":
                        _policy_log.parent.mkdir(parents=True, exist_ok=True)
                        with open(_policy_log, "a", encoding="utf-8") as _pf2:
                            _pf2.write(json.dumps({
                                "t": round(now, 3), "fid": state_dict.get("frame_id"),
                                "act": _pa.get("act_name"),
                                "rule_type": action.get("type"),
                                "rule_reason": action.get("reason"),
                                "theta": round(float(_pa.get("theta", 0.0)), 3),
                                "r": round(float(_pa.get("r", 0.0)), 3),
                                "target": _pa.get("target"),
                                "ms": round(_policy.last_infer_ms, 1),
                            }, ensure_ascii=False) + "\n")
                    else:
                        _cand = _policy_action_to_m2(_pa)
                        _ok, _why = _policy_guard(_cand, state_dict, cooldowns, now)
                        cooldowns["_policy_last"] = ("accepted", _cand.get("type")) if _ok \
                            else ("rejected", _why)
                        if _ok:
                            action = _cand
                except Exception as _pe:
                    cooldowns["_policy_last"] = ("error", f"{type(_pe).__name__}: {_pe}")
            sig = (action.get("type"), action.get("id"))
            if sig != last_sig:
                print(f"[{datetime.now():%H:%M:%S}] [决策] {format_decision(action)}")
                last_sig = sig

            # ---- v3.0 鐞嗚灞傦細琛屼负鏍囩 + 鍙嶉灞傚鎯----
            beh = tagger.update(state_dict, cooldowns, action, now)
            state_dict["behavior"] = beh
            # v2.69 浜嬩欢鍘婚噸: 鍚屼竴浜嬩欢 30s/10s 鑺傛祦(姝墠澶氬抚閲嶅瑙彂閫犳垚'10鍔敾'鍋囪处)
            _evgate = {"assist": 30.0, "kill": 30.0, "hook": 15.0,
                       "died": 6.0, "be_tower_attacked": 8.0,
                       "tower_kill": 10.0, "minion_clear": 6.0}
            _ev = beh.get("event")
            if _ev:
                _lg = _evgate.get(_ev, 10.0)
                _lt = float(cooldowns.get(f"evt_{_ev}_t", 0.0))
                if _lt and now - _lt < _lg:
                    _ev = None
                else:
                    cooldowns[f"evt_{_ev}_t"] = now
            if _ev:
                sc = reward.on_event(_ev)
                if sc:
                    try:
                        from wzry.learn.state_action_recorder import set_event as _setev
                        _setev(_ev, sc, now)
                    except Exception:
                        pass
                if sc:
                    print(f"[{datetime.now():%H:%M:%S}] [反馈] 事件 {_ev} "
                          f"{sc:+.0f} 分（总分 {reward.total:.0f}）")
                # online learning
                try:
                    _hp_now = float((state_dict.get("ui") or {}).get("hp") or 1.0)
                    _mm_now = (state_dict.get("minimap") or {}).get("dots") or {}
                    _n_enemy = len(_mm_now.get("red") or [])
                    _n_ally = len(_mm_now.get("blue") or [])
                    _w_before = dict(learner.weights())
                    if not freeze_weights:  # v14.0 冻结期间不做在线权重学习
                        learner.observe(_ev, near_enemies=_n_enemy,
                                        hp_low=_hp_now < 0.4, ally_near=_n_ally > 0)
                    _w_after = learner.weights()
                    _delta = " ".join(f"{k}:{_w_before[k]:.2f}->{_w_after[k]:.2f}"
                                      for k in _w_after
                                      if abs(_w_before[k] - _w_after[k]) > 1e-3)
                    if _delta:
                        print(f"[学习] {_ev} | {_delta} | "
                              f"累计 k/a/d={learner.stats().get('kill',0)}/"
                              f"{learner.stats().get('assist',0)}/{learner.stats().get('died',0)}")
                    from wzry.decision.optimizer import refresh_weights as _rw
                    # v14.2 --freeze-weights 也关掉权重注入(方案 C3 字面要求)。
                    #   observe 已被冻结 -> _w_after 与 _w_before 相同, 注入本无副作用;
                    #   但显式跳过更符合"冻结=A/B 期间规则权重完全不变"的语义。
                    if not freeze_weights:
                        _rw(_w_after)
                except Exception:
                    pass
            if now - float(cooldowns.get("dump_t", 0.0)) > 60.0:
                # learning log rolling 60s
                cooldowns["dump_t"] = now
                try:
                    if learner is not None:
                        _dk = learner.dump_log(str(ROOT / "docs" / "LEARNING_LOG.md"))
                        if _dk:
                            print(f"[{datetime.now():%H:%M:%S}] [学习] 日志滚动已写入 "
                                  f"docs/LEARNING_LOG.md")
                except Exception:
                    pass
            if beh.get("dead"):
                # 阵亡等待复活，不做任何操作（用户规则）
                action = {"type": "none", "reason": "dead_waiting"}
                sig = (action.get("type"), action.get("id"))
                # v16.2 持久摇杆是"物理按住": 阵亡时必须真的松手, 否则复活瞬间会先沿旧方向
                #   走约一拍(~0.5s)才被新的 map_move 纠正。只在"刚阵亡"那一拍投递一次。
                if not last_dead:
                    _last_move[:] = [None, None, 0.0]
                    if do_action and not args.drive_stdin:
                        # 驱动模式下摇杆归外部控制, 不代它松手
                        _action_q.put(({"type": "stick_release"}, cooldowns))
            if last_dead and not beh.get("dead"):
                cooldowns["revive_t"] = now   # v2.49 璁板綍澶嶆椿鏃跺埢(5s 鍐呬笉鎺垬)
                # v14.2 复活=回泉水: 航位推算的"自己"必须一起归位, 否则估计位置还停在
                # 死前/目标处 -> map_move 认为"已到达"而不再推杆, 英雄永久僵在基地。
                _cb = cooldowns.get("camp") or "blue"
                cooldowns["self_est"] = BASE_BLUE                 if _cb == "blue" else BASE_RED
                cooldowns.pop("_map_t", None)   # v16.2 阵亡期间英雄没动, 别把这段时间算进航位推算
                cooldowns.pop("_map_gate", None)  # 复活后重新判定"是否已到达"
            last_dead = bool(beh.get("dead"))

            # ---- v2.43 琚鎵撹嚜鐪 鑷繁(绱湀)鍦晫濉旀敾鍑诲湀鍐-> 寮哄埗鑴辩 ----
            try:
                _hp_v = (state_dict.get("extra") or {}).get("hero_pos")
                if _hp_v and action.get("type") not in ("recall", "restore", "skill"):
                    _hx, _hy = _hp_v[0] / w, _hp_v[1] / h
                    for _tu in (state_dict.get("units") or []):
                        if str(_tu.get("cls")) != "enemy_turret":
                            continue
                        _ts = _tu.get("screen") or [0.5, 0.5]
                        _td = math.hypot(_ts[0] - _hx, (_ts[1] - _hy) * aspect)
                        if _td < 0.24:
                            _ax, _ay = _away_map(state_dict, _ts[0], _ts[1])
                            action = {"type": "map_move", "nx": _ax, "ny": _ay,
                                      "reason": "under_tower_fire"}
                            sig = ("map_move", None)
                            break
            except Exception:
                pass

            # ---- 撞墙感知 v14.2: 停用"小地图绿点当自己"的撞墙判定 ----
            # 实测(2026-10-14, 76 帧全程): 小地图绿点恒定在 (0.074, 0.88) 不变 —— 它是
            # 自家基地/水晶的固定图标, 不是英雄。原逻辑拿它当"自己", WallSensor 永远判定
            # "没移动", 每 6 帧触发一次"目标点旁移 0.06 -> 重发", 与 map_move 节流叠加成
            # 死循环: 英雄被反复改向 -> 原地打转/推进极慢, 正是用户反馈的问题①(卡住不动)。
            # 定位不到真实自己(hero_pos=None)时传感器自行复位, 不再误判; 且 map_move 走的是
            # 游戏自带寻路(会自己绕墙), 我们的旁移重发不仅多余, 还有害。
            wall_sensor.update(now, False, None)

            # ---- 閽瓙勭娴嬮噺璁板綍堣嚜鍔爣瀹氫簩鎶鑳借寖鍥达級----
            if action.get("type") == "skill" and action.get("id") == 2:
                _rec_hook_measure(cooldowns, state_dict, action, hit=False)
            if action.get("type") == "combo":
                _rec_hook_measure(cooldowns, state_dict, action, hit=True)

            # ---- 执行 / 模拟 ----
            # v16.2 --drive-stdin 时整块跳过: 动作由外部/文件驱动提供, 主循环不再自己下发
            #   （否则两路都会往同一个 _action_q 投递, 互相抢深度=1 的槽位、互相打架）。
            #   感知/学习/录制仍照常运行。
            if action.get("type") != "none" and not args.drive_stdin:
                if do_action:
                    # v14.3 塔重锚(闭环纠偏): 英雄正踩在某座塔上 -> self_est 吸附到该塔坐标。
                    #   放在入队前(此处有 frame/dets/mm), 0.3s 节流(每拍调一次 self_hp_mp 太贵)。
                    if now - float(cooldowns.get("snap_t", 0.0)) >= 1.0:
                        # v15.1 0.3 -> 1.0: 每拍都要跑 self_hp_mp(全画面血条检测)，太贵
                        cooldowns["snap_t"] = now
                        _snap = _snap_self_est_to_tower(frame, dets, mm, cooldowns)
                        if _snap:
                            print(f"[{datetime.now():%H:%M:%S}] [重锚] self_est <- 塔 "
                                  f"({_snap[0]},{_snap[1]}) 原估计偏差 {_snap[2]}")
                    # v14.2 入队(不等待执行); worker 空闲时才投递 -> 队列深度 <=1, 动作不过期
                    if _exec_idle.is_set():
                        _ta = _t0()
                        _action_q.put((action, cooldowns))
                        _t1(_ta, "动作入队")
                        n_actions += 1
                        # v14.2 记录执行层实际下发的动作(与 solve 的 chosen 不同
                        #   即说明被安全层改写) -> shadow 记录新增 executed 字段
                        if _laya is not None:
                            _laya.log_executed(action)
                    else:
                        # v16.2 可见性: worker 正忙(一次 swipe 350~1000ms)时本拍决策**会被丢弃**,
                        #   而原实现无 else / 无计数 / 无重试 -> 丢失被 n_actions 掩盖
                        #   (它只统计入队成功的)。持久摇杆把 worker 占用压到 ≈20ms 后,
                        #   这里的计数应显著下降 —— 这正是本改动的可观测验收点。
                        cooldowns["_act_drop"] = int(cooldowns.get("_act_drop", 0)) + 1
                        if now - float(cooldowns.get("_drop_log_t", -9.9)) > 5.0:
                            cooldowns["_drop_log_t"] = now
                            print(f"[{datetime.now():%H:%M:%S}] [exec] 执行线程忙, 丢弃 "
                                  f"{action.get('type')} (累计 {cooldowns['_act_drop']} 次)")
                    # continuous stick: move 0.55s; instant actions release
                    if action.get("type") == "move":
                        _last_move[:] = [action.get("theta"), action.get("r", MOVE_R),
                                         now + max(0.55, action.get("duration_ms", 380) / 1000.0)]
                    elif action.get("type") in ("recall", "restore", "summoner"):
                        _last_move[:] = [None, None, 0.0]
                else:
                    pass   # simulation mode: no execution

            # ---- capture ----
            if args.save:
                if not recorder.active:
                    recorder.start(meta={"agent": "m2_agent_v2", "model": str(args.model),
                                         "action": "on" if do_action else "off"})
                    # v14.2 shadow 记录带上会话号, 便于与 data/matches/<session>/ 关联
                    if _laya is not None and recorder.session_dir is not None:
                        _laya.set_context(session_id=recorder.session_dir.name)
                # v2.10氶樀瀹瑰啓鍏姸鎬佹祦堝喅绛栧眰鍙                if cooldowns.get("roster"):
                    state_dict.setdefault("extra", {})["roster"] = cooldowns["roster"]
                recorder.on_state(state_dict)
                # v15.0 关键帧落盘：Recorder.on_frame 从来没被调用过 -> data/matches/*/frames/
                #   一直是空目录（离线标定/复现无图可用，实测 22 场 0 张）。这里接上；
                #   Recorder 内部按 frame_every_s(默认 5s) 节流，不会拖慢主循环。
                recorder.on_frame(frame)
                # v15.1 取证：检测到敌方英雄的那一拍额外落一帧（不受 5s 节流），
                #   用来抓"顶部锁定目标头像"以及场景内敌方血条。
                if getattr(args, "frame_on_enemy", False):
                    try:
                        _cls_of = (lambda d: d.get("cls") if isinstance(d, dict)
                                   else getattr(d, "cls", None))
                        if any(_cls_of(d) == "enemy_hero" for d in dets):
                            recorder.save_frame(frame, tag="enemy")
                    except Exception:
                        pass
                if do_action and action.get("type") != "none":
                    rec = dict(action)
                    rec["t"] = time.time()
                    if rec.get("type") == "combo":
                        # 杩炴嫑鎷嗘垚瀛愬姩浣滃瓨妗ｏ紙encode_action 鍏煎                        for sub in rec.get("actions", []):
                            sub_rec = dict(sub)
                            sub_rec["t"] = rec["t"]
                            sub_rec["reason"] = rec.get("reason", "hook_confirmed")
                            recorder.on_action(sub_rec)
                    else:
                        recorder.on_action(rec)

            # ---- v2.15 实时分析标注落盘(temp/live_annot/ 窗口同源显示) ----
            # v14.3 注: 曾把渲染移到后台线程, 实测反而更慢(218/284 帧 < 同步 334 帧) ——
            #   渲染线程与主循环抢 GIL 形成"护航效应", 把采集 reader 饿得更狠。故保持同步。
            #   render_live_analysis 本身已优化(_ltext 只转文字小块 55x + PNG 限流 4Hz)。
            try:
                _tr = _t0()
                vis_out = render_live_analysis(frame, dets, mm, state_dict, action, beh,
                                               reward.total, now)
                _t1(_tr, "渲染标注render_live")
            except Exception as e:
                print(f"[annot] 渲染异常: {e}")
                vis_out = frame.copy()

            # v2.81 self-diagnose: 带标注帧 + 血条读数 一起送 AI 审检
            try:
                if self_check is not None:
                    _tc = _t0()
                    self_check.feed(vis_out, dets,
                                    ui=(state_dict.get("ui") or {}))
                    _t1(_tc, "self_check落盘")
            except Exception:
                pass
            # v2.83 局内行为习惯学习(对面/队友/自己)
            try:
                if behavior_miner is not None:
                    behavior_miner.feed(mm.get("dots") or {})
            except Exception:
                pass

            # ---- 鍛湡鏃織 / 棰勮 ----
            if now - last_log >= 0.5:
                last_log = now
                objs = ", ".join(f"{d.cls}:{d.conf:.2f}" for d in dets[:5]) or "none"
                mm_txt = (f"map blue {len(mm['dots']['blue'])}/red {len(mm['dots']['red'])} "
                          f"({tracker.last_ms:.0f}ms)") if mm["found"] else "map not found"
                print(f"[{datetime.now():%H:%M:%S}] 检测 {det.last_infer_ms:5.0f}ms | "
                      f"{objs} | {mm_txt} | 行为: {beh.get('label', '')} "
                      f"| 总分: {reward.total:.0f} | 决策: {format_decision(action)}")
            if args.show:
                # v2.22 绐楀彛涓庤惤鐩樺悓婧 鍏绱犳爣娉鏂瑰舰忓湴鍥鹃潰鏉涓枃+琛屼负鏉, 涓嶅啀鐢棫鍦嗙洏
                try:
                    from PIL import ImageFont  # noqa: F401
                except Exception:
                    pass
                vis = vis_out if vis_out is not None else frame.copy()
                # v10.4 阵营显示在窗口上 (用户: 蓝方红方写在行为/得分旁边)
                try:
                    _oc = cooldowns.get("camp") or ""
                    _osd_txt = "蓝方 BLUE" if _oc == "blue" else ("红方 RED" if _oc == "red" else "判定中")
                    _ost_col = (255, 120, 0) if _oc == "blue" else ((0, 60, 255) if _oc == "red" else (200, 200, 200))
                    cv2.rectangle(vis, (10, 60), (230, 100), (0, 0, 0), -1)
                    cv2.putText(vis, _osd_txt, (20, 88), cv2.FONT_HERSHEY_SIMPLEX,
                                0.9, _ost_col, 2)
                    # 支援方向
                    _dps_txt2 = "支援: 下路(射手)" if _oc == "blue" else ("支援: 上路(射手)" if _oc == "red" else "支援: 判定中")
                    cv2.putText(vis, _dps_txt2.encode("ascii", "ignore").decode() or "support",
                                (20, 122), cv2.FONT_HERSHEY_SIMPLEX, 0.6, _ost_col, 1)
                except Exception:
                    pass
                cv2.imshow("m2-agent-v2", vis)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

        _drops = int(cooldowns.get("_act_drop", 0))
        _unknown = int(cooldowns.get("_act_unknown", 0))
        print(f"\n完成: {n_frames}帧 感知{n_ticks} 次 "
              f"平均检测 {infer_sum / max(1, n_ticks):.0f}ms "
              f"动作 {n_actions} 次 丢弃 {_drops} 次 未知类型 {_unknown} 次 "
              f"模式: {'执行' if do_action else '模拟'}")
        if _drops:
            print(f"  注: 丢弃 = 执行线程正忙时做出的决策未下发（原实现无记录）。"
                  f"占本应下发动作的 {_drops / max(1, n_actions + _drops) * 100:.1f}%；"
                  f"持久摇杆应使其显著下降。")
        if _unknown:
            print(f"  注: 未知类型 {_unknown} 次 = 有动作的 type 不在支持列表里, 被静默忽略。"
                  f"这几乎总是**拼写错误**, 请按 §3.8(g) 的清单核对。")
        _tot_skip = sum(_skips.values())
        if _phases:
            print("[phase] 各相位拍数: " + " | ".join(
                f"{k} {v}" for k, v in sorted(_phases.items(), key=lambda kv: -kv[1])))
            print(f"[phase] sm.hold_frames={sm.hold_frames} sm.hold_s={getattr(sm, 'hold_s', None)}"
                  f" sm._in_since={getattr(sm, '_in_since', None)}"
                  f" sm._in_match_streak={getattr(sm, '_in_match_streak', None)}")
        if _nm_vals:
            _gs = sorted(v[0] for v in _nm_vals)
            _bs = sorted(v[1] for v in _nm_vals)
            _mid = len(_gs) // 2
            print(f"[phase] 非对局帧判据归因: {_nm_why}"
                  f" | green 中位 {_gs[_mid]} / blue 中位 {_bs[_mid]}"
                  f"（阈值 green>{sm.green_thr} blue<{sm.blue_max}）")
        if _tot_skip:
            _pct = " | ".join(
                f"{k} {v * 100.0 / _tot_skip:.0f}%" for k, v in
                sorted(_skips.items(), key=lambda kv: -kv[1]) if v)
            print(f"[skip] 共 {_tot_skip} 拍提前退出: {_pct}")
            print(f"       感知执行 {n_ticks} 次 / {n_frames} 拍"
                  f"（{n_ticks / max(1, n_frames) * 100:.0f}% 的拍子走到了感知）")
        if _TIMING and _T_ACC:
            _tot = sum(v[1] for v in _T_ACC.values())
            print("\n[timing] 主循环分阶段累计耗时(按累计占比降序):")
            for _k, (_n, _ms) in sorted(_T_ACC.items(), key=lambda kv: -kv[1][1]):
                if _ms < 1.0:
                    continue
                print(f"  {_k:<26} {_ms:9.1f}ms  {_ms / max(1.0, _tot) * 100:5.1f}%  "
                      f"({_n}次, 均 {_ms / max(1, _n):.1f}ms/次)")
            print(f"  {'—' * 26} {'':>9} {'':>6}")
            print(f"  {'合计已埋点':<26} {_tot:9.1f}ms  "
                  f"占运行时长 {min(100.0, _tot / max(1.0, args.seconds) / 10.0):5.1f}%"
                  f"  → 未埋点(循环开销/其它) {max(0.0, args.seconds * 1000 - _tot):.0f}ms")
    except KeyboardInterrupt:
        print("\n手动退出。")
    finally:
        if _hok_shadow is not None:
            try:
                _hok_shadow.close()
                print(f"[hok-shadow] 统计: {_hok_shadow.status()}")
            except Exception:
                pass
        # v14.2 退出前 drain 动作队列: 让已入队的动作执行完(或在 5s 后放弃), 再关 recorder,
        #   否则"动作记录"与"实际执行"错位(最后几个动作没发出去却已落盘)。
        if do_action:
            try:
                # v16.2 退出前必须真正松开摇杆(持久触点是物理按住, 不松手英雄会一直走)
                _action_q.put(({"type": "stick_release"}, cooldowns))
                _action_q.put(None)          # 哨兵排在剩余动作之后
                _action_q.join(timeout=5.0)
            except Exception:
                pass
        if recorder.active:
            recorder.close()
        cap.stop()
        if args.show:
            cv2.destroyAllWindows()
        if _laya is not None:  # v14.0 P1 门槛验收: 异常数=0 / p95 延迟(shadow.jsonl latency_ms)
            try:
                _laya.stop()        # v14.2 先 stop(会把最后一个决策点落盘), 再取统计
                _ls = _laya.stats()
                print(f"[Laya] 统计: {_ls} | shadow 日志: {_laya.shadow_path}")
                if _ls.get("errors"):
                    print(f"[Laya][警告] 异常数={_ls['errors']} > 0, P1 门槛未过: "
                          f"{_ls.get('last_error')}")
                if _ls.get("label_dups"):
                    print(f"[Laya][警告] 标签重复 {_ls['label_dups']} 次 -> 该拍已 fail-open; "
                          f"检查 optimizer.gen_candidates 的 tag 唯一性")
                if _ls.get("tripped"):
                    print(f"[Laya][熔断] 本局已降级为 off: {_ls.get('trip_reason')} "
                          f"(可用 --laya-max-errors/--laya-max-dups 调整阈值)")
                _n_lat = _ls.get("latency_logged", 0)
                if _ls.get("shadow_logged") and not _n_lat:
                    print("[Laya][警告] shadow 记录无延迟样本 -> P1 p95 门槛无法评估")
                if _ls.get("throttled"):
                    print(f"[Laya] 提交节流跳过 {_ls['throttled']} 次 "
                          f"(模型吞吐 < 决策频率; 见 --laya-hz)")
            except Exception:
                pass


if __name__ == "__main__":
    main()
