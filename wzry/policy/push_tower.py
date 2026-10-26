# -*- coding: utf-8 -*-
"""推塔 / 兵线启发式（P1 从 scripts/m2_agent_v2.py 抽出，逐字节复制）。

为什么单独成模块
----------------
这块是 m2 里唯一「只吃 dict、出 dict」的决策子集（不碰 IO、不碰执行器），
但它和 3 863 行的大文件绑在一起，任何一次改动都只能靠真机跑回归。
拆出来后可以独立单测。

边界（搬迁时确认过）
--------------------
- 本模块**不 import m2**，避免循环依赖；它自带的常量是唯一真源。
- m2 反向 `from wzry.policy.push_tower import ...` 拿到同名对象，
  因此 `--push-set` 必须**同时写两个命名空间**（见 m2 的 apply_push_overrides）。
- `TURRET_MM_THREAT` / `TURRET_MM_ESCAPE` **没有搬**：它们被 `decide()` 直接使用，
  留在 m2 作为原真源。
- `BASE_BLUE` / `BASE_RED` 原本位于导航常量区（1655–1657），但被本模块的
  `_mm_self_est` / `_push_enemy_dir` 读取；暂随推塔块搬迁，m2 反向 import 后
  导航侧读到的仍是同一对象。
"""
from __future__ import annotations

import math  # noqa: F401  (被 _push_enemy_dir/_push_pick_* 等使用)

SKILL_DEBOUNCE_S = 0.05

LANE_DIR_BLUE = (0.75, 0.72)   # v2.91: 发育路中心(蓝方右下), 旧(0.5,0.5)=地图中心=河道! 用户: 别待河道
LANE_DIR_RED = (0.25, 0.28)    # v2.91: 发育路中心(红方左上镜像)

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

BASE_BLUE = (0.074, 0.88)     # 本方泉水在小地图上的归一化位置(蓝方; 真机实测 2026-10-14:
                              #   小地图绿点恒定于此 = 泉水治疗圈图标, 不是英雄; 红方镜像)
BASE_RED = (0.926, 0.12)


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
        # 点到线段距离参数 t 的投影
        px, py = bx - 0.5, (by - 0.5) * aspect
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
