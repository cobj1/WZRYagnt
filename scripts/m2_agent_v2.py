# -*- coding: utf-8 -*-
# m2_agent_v2 line 2
#鏋舵瀯垀10Hz 鎰熺煡寰幆夛細
#    scrcpy 娴侊紙30fps夆啋 MatchStateMachine堝灞鐘舵佹満      鈫纭鍒讹紙in_match + 忓湴鍥捐繛缁2 甯璁級
#      鈫鎰熺煡圷OLO 11 绫绘娴+ MinimapTracker      鈫decide() 瑙勫垯鍐崇瓥堢函鍑芥暟屾棤 IO屼究浜庢祴璇鎹瓥鐣綉缁滐級
#      鈫ActionExecutor 鎵-action 鎵嶇湡姝ｈ鎽革紝榛樿 --no-action 鍙瀵燂級

#瑙勫垯浼樺厛绾紙v0 绠鍗曚絾瀹夊叏岃 decide()夛細
#  1. 閽瓙鍙懡涓紙妫娴嬪埌 hook_aim 鎴鏁屾柟鑻遍泟鍦2 鎶鑳借窛绂诲唴     鈫skill_cast(2, 'tap')屽喎鍗磋妭娴> 3s  2. 鏁屼汉杩戯紙enemy_hero 涓績璺濆睆骞曚腑蹇< 0.25 灞忓     鈫skill_cast(1, 'tap')岃妭娴1.5s  3. 鏁屼汉璐磋韩 0.20 灞忓変笖鎶鑳戒笉鍙敤 鈫鏅敾堟敾鍑婚敭夛紝鑺傛祦 1s  4. 鏁屼汉杩鈫move(鏈濆悜鏈杩戞晫浜 r=0.8, 400ms)  5. 鏃犳晫浜鈫鏈濆叺绾挎柟鍚戠鍔紙忓湴鍥剧孩鐐硅川蹇冩柟鍚戯級涙棤绾偣鍒idle銆  闃叉姈氫换浣曟妧鑳介噴鏀惧悗 50ms 鍐呬笉鍐嶄笅鍙戠鍔  濉旓細琚鏀诲嚮堝涓績 < 0.40 灞忓涓旀棤鎴戞柟忓叺夆啋 绔嬪嵆鍙嶅悜閫冪
#鐢硶    venv\\Scripts\\python.exe scripts\\m2_agent_v2.py --seconds 120              # 鍙瀵燂紙榛樿屽畨鍏紑鍏筹級
#    venv\\Scripts\\python.exe scripts\\m2_agent_v2.py --seconds 120 --action     # 鐪熸満鎵
#    venv\\Scripts\\python.exe scripts\\m2_agent_v2.py --seconds 120 --action --save   # 鎵 + 瀵瑰眬浼氳瘽閲囬泦
#    """decoded docstring."""
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
TURRET_ESCAPE_FRAC = 0.40
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
    # v7.1 塔威胁: ① 开局 8s 禁用(泉水期自家基地 yolo 误标 enemy_turret)
    #   ② 屏中线下方(y>0.62 且 x 中)的塔 = 自家基地建筑, 不算敌塔威胁
    #   ③ 无 ally_minion 要求 (v6.0 用户: 被塔打了要撤, 不等兵线)
    def _is_base_building(t):
        return t[1] > 0.62 and abs(t[0] - 0.5) < 0.32
    _turs_f = [t for t in turrets if not _is_base_building(t)]
    _match_age = now - float(cooldowns.get("match_start_t", 0.0))
    nt = nearest(_turs_f) if _turs_f else None
    turret_threat = bool(_turs_f) and nt is not None and _match_age > 8.0 \
        and dist_width(nt[0], nt[1]) < TURRET_THREAT_FRAC
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
        _pool_esc = list(enemies) + list(_turs_f) if _turs_f else list(enemies)
        if _pool_esc:
            _esc_target = min(_pool_esc, key=lambda s: dist_width(s[0], s[1]))
        if _esc_target is not None:
            _ax3, _ay3 = _away_map(state_dict, _esc_target[0], _esc_target[1], k=0.20)
            return {"type": "map_move", "nx": _ax3, "ny": _ay3,
                    "reason": "hp_drop_escape"}
    if turret_threat:
        tx, ty = nt[0], nt[1]
        # 宸插湪濉旀敾鍑昏寖鍥村唴堝涓績 < ESCAPE_FRAC夆啋 绔嬪嵆鍙嶅悜閫冪堢敤鎴峰弽棣堬細琚鎵撲笉鐭亾鍑哄幓        if dist_width(tx, ty) < TURRET_ESCAPE_FRAC:
        # inside turret fire -> escape
        if dist_width(tx, ty) < TURRET_ESCAPE_FRAC:
            cooldowns["escape_t"] = now   # v2.15 閫冭劚鎸佺画s 鍐呬繚鎸佽繙绂诲
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
    # v2.15 閫冭劚鎸佺画歟scape 鍚1.5s 鍐呭嵆浣垮垽瀹氱煭鏆傛秷澶变篃缁画杩滅堢洿鍒扮湡姝ｅ嚭鍦堬級
    if now - float(cooldowns.get("escape_t", 0.0)) < 1.5 and turrets:
        nt2 = nearest(turrets)
        tx, ty = nt2[0], nt2[1]
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
            _down_c = (0.75, 0.72) if _camp0 == "blue" else (0.25, 0.28)
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
                else:
                    # 2) 射手走廊内红点(敌人在射手路) -> 支援那里
                    _pool_red = [p for p in mm_red if _corridor(p, _down_c, 0.35)]
                    if _pool_red and hp >= _u_eng_hp:
                        tr = min(_pool_red, key=lambda p: (p[0]-myx)**2 + (p[1]-myy)**2)
                        d_red = math.hypot(tr[0]-myx, tr[1]-myy) if tr else 9.9
                        if d_red < 0.60:
                            target = (tr[0], tr[1])
                            reason = "support_dps_lane_red"
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
                  "support_ally_engaged", "lane_develop", "follow_ally_lowhp"):
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
        pass  # 宸叉槸鍦板浘鍧愭爣
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
        if now - float(cooldowns.get("_map_t", -9.9)) < dur / 1000.0:
            return                       # 上一次推杆尚未结束: 避免 swipe 叠发
        ex.move(math.atan2(-dy, dx), MOVE_R, dur)     # 世界 Δ -> 摇杆方向(0=右, 逆时针)
        cooldowns["_map_t"] = now
        _step = STICK_SPEED * (dur / 1000.0)          # 航位推算: 自己前进 step
        cooldowns["self_est"] = (max(0.02, min(0.98, sx + dx / dist * _step)),
                                 max(0.02, min(0.98, sy + dy / dist * _step)))
    elif t == "move":
        ex.move(float(action.get("theta", 0.0)),
                float(action.get("r", MOVE_R)),
                int(action.get("duration_ms", MOVE_DURATION_MS)))
    elif t == "skill":
        ex.skill_cast(int(action.get("id", 0)), action.get("mode", "tap"))
    elif t == "summoner":
        ex.summoner()
    elif t == "recall":
        ex.recall()
    elif t == "attack":
        ex.attack(str(action.get("priority", "free")))
    elif t == "restore":
        ex.restore()
    elif t == "combo":
        for i, sub in enumerate(action.get("actions", [])):
            apply_action(ex, sub, cooldowns)
            if i < len(action["actions"]) - 1:
                time.sleep(0.12)   # combo interval
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
BASE_BLUE = (0.074, 0.88)     # 本方泉水在小地图上的归一化位置(蓝方; 真机实测 2026-10-14:
                              #   小地图绿点恒定于此 = 泉水治疗圈图标, 不是英雄; 红方镜像)
BASE_RED = (0.926, 0.12)
TURRET_SAFE_FRAC = 0.55
TURRET_THREAT_FRAC = 0.45
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
    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    dr = ImageDraw.Draw(pil)
    dr.text((x, y), text, font=f, fill=(color[2], color[1], color[0]))
    img[:] = cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)


_LA_DIR = None
_LA_LAST = 0.0
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
        mmcrop[:] = cv2.resize(frame[0:242, 0:242], (bsz - 4, bsz - 4))
        scale = (bsz - 4) / 242.0

        def dpos(n):
            return (int(bx + 2 + n[0] * 242 * scale), int(by + 2 + n[1] * 242 * scale))
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

    # 搴曢儴淇伅鏉    beh_label = (beh or {}).get("label", "")
    act_txt = f"鍐崇瓥: {format_decision(action)}"
    reason = action.get("reason", "")
    mmc = (mm or {}).get("dots") or {}
    info = (f"beh: {beh.get('label', '')} | {reason} | score {score:.0f} | "
            f"map blue {len(mmc.get('blue', []))}/red {len(mmc.get('red', []))}/"
            f"green {len(mmc.get('green', []))} | {datetime.now():%H:%M:%S}")
    cv2.rectangle(vis, (0, h - 64), (w, h), (0, 0, 0), -1)
    _ltext(vis, info, (10, h - 58), 17, (0, 255, 255), )

    # 钀界洏
    cv2.imwrite(str(_LA_DIR / "annot_latest.png"), vis)
    if now - _LA_LAST >= 1.0:
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
                      "green": len(mmc.get("green", []))}}
        with open(_LA_DIR / "behavior.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
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
    ap.add_argument("--capture", default="desktop", choices=["desktop", "screencap", "scrcpy"],
                    help="frame source: desktop/screencap/scrcpy")
    ap.add_argument("--seconds", type=float, default=120.0, help="run seconds")
    ap.add_argument("--forever", action="store_true",
                    help="opt")
    # v14.0 默认检测器改为 P0 替代模型(YOLOv5 ONNX, 8 通用类) —— 原 zhongkui_r3
    # 权重已丢失; 传 --model 指向 .pt 可回退 ultralytics 11 类后端
    ap.add_argument("--model", default=str(ROOT / "runs" / "detect" / "hok_yolov5"
                                           / "hok_yolov5s.onnx"),
                    help="opt")
    ap.add_argument("--action", action="store_true",
                    help="opt")
    ap.add_argument("--no-action", dest="no_action", action="store_true",
                    help="opt")
    ap.add_argument("--save", action="store_true", help="opt")
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
    ap.add_argument("--detect-hz", type=float, default=10.0, help="opt")
    ap.add_argument("--stick-speed", type=float, default=STICK_SPEED,
                    help="满杆 1 秒的小地图归一化位移(默认=真机标定值, 见 "
                         "scripts/calib_stick_speed.py; 移速随英雄/出装变化)")
    ap.add_argument("--show", action="store_true", help="opt")
    ap.add_argument("--laya", default="off", choices=["off", "shadow", "ensemble"],
                    help="opt")
    ap.add_argument("--laya-model", default=None, help="opt")
    ap.add_argument("--laya-beta", type=float, default=0.5, help="opt")
    # v15.0 BCNet 策略接线（与 --laya 同一套 shadow 模式）：
    #   off=零改动 / shadow=每拍跑模型并落盘、**不改动作流** / bc=接管（仍必须过安全层）
    ap.add_argument("--policy", default="off", choices=["off", "shadow", "bc"], help="opt")
    ap.add_argument("--policy-checkpoint", default=None, help="BCNet/BCNetMem 权重(.pt)")
    ap.add_argument("--freeze-weights", action="store_true", help="opt")
    # v14.2 ESP32 HID 桥（真机路线）: 板子插手机 USB 口当 HID 触摸屏, 电脑走 Wi-Fi 控制。
    # 默认 adb(驱动 MuMu 模拟器); 要打真机就 --backend esp32 并给板子地址。
    ap.add_argument("--backend", default="adb", choices=["adb", "esp32"],
                    help="执行通道: adb(MuMu) / esp32(真机 HID 桥)")
    ap.add_argument("--board-host", default=None, help="ESP32 板子的 Wi-Fi 地址(backend=esp32 必填)")
    ap.add_argument("--board-port", type=int, default=7300, help="板子 TCP 端口(须与固件 BRIDGE_PORT 一致)")
    ap.add_argument("--board-mode", default="touch", choices=["touch", "mouse"],
                    help="须与烧的固件一致(推荐 touch=多点触摸屏)")
    ap.add_argument("--board-size", default="1080x2340",
                    help="手机物理分辨率 WxH, 须与固件 TOUCH_SURFACE_W/H 一致")
    args = ap.parse_args()

    do_action = bool(args.action) and not args.no_action
    freeze_weights = bool(getattr(args, "freeze_weights", False))
    STICK_SPEED = float(args.stick_speed)      # v14.2 CLI 覆盖(apply_action 读模块全局)
    _laya = None  # v14.0 LayaScorer 句柄(finally 里 stop + 输出统计, P1 门槛: 异常数=0)

    # v14.0 Laya 打分器接线 (laya-fusion-plan C3): off=零改动; shadow=仅记录; ensemble=混合排序
    if getattr(args, "laya", "off") != "off":
        from wzry.decision import optimizer as _opt_laya
        from wzry.decision.laya_scorer import LayaScorer as _LayaScorer
        _laya = _LayaScorer(mode=args.laya, beta=float(args.laya_beta),
                            model_path=args.laya_model)
        _opt_laya.set_scorer(_laya, beta=float(args.laya_beta), mode=args.laya)
        _laya.start()
        print(f"[Laya] mode={args.laya} beta={args.laya_beta} "
              f"model={args.laya_model or 'default'}")

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
        cap = ScreencapCapture(interval=0.02, rotate_s=1800)
        print("启动 screencap 实时采集（adb 直读）...")
    else:
        from wzry.capture.scrcpy_stream import ScrcpyStreamCapture
        cap = ScrcpyStreamCapture(ROOT / "tools" / "scrcpy", rotate_s=1800)
        print("启动 scrcpy 流...")
    cap.start()

    calib, _ = load_calibration()
    sm = MatchStateMachine(minimap_center_norm=calib.get("minimap_center", [0.086, 0.129]))

    print(f"鍔犺浇妫娴嬫鍨{args.model} ...")
    det = YoloDetector(args.model, conf=0.35)

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
        if args.backend == "esp32":
            # v14.2 真机通道: 板子当 HID 触摸屏, 电脑走 Wi-Fi。window_coords=True
            # 让板子执行器也吃 1280x720 帧坐标(校准表/上层都在这个空间)。
            from wzry.control.esp32_hid import Esp32HidExecutor
            if not args.board_host:
                raise SystemExit("--backend esp32 需要 --board-host <板子IP>")
            _bw, _bh = (int(v) for v in str(args.board_size).lower().split("x"))
            _board = Esp32HidExecutor(host=args.board_host, port=args.board_port,
                                      mode=args.board_mode, device_size=(_bw, _bh),
                                      event_log=_evlog, window_coords=True)
            try:
                _hello = _board.connect()
            except OSError as _e:      # 连接超时/被拒 -> 清晰提示, 不抛裸 Traceback
                raise SystemExit(f"连不上板子 {args.board_host}:{args.board_port} ({_e})"
                                 "（先确认板子已连 Wi-Fi 且固件在跑）")
            if _hello is None:
                raise SystemExit(f"板子 {args.board_host}:{args.board_port} 未按约定上报 hello"
                                 "（确认烧的是 firmware/esp32_hid_* 固件）")
            ex = ActionExecutor(_board)
            print(f"执行模式: ESP32 HID 桥 {args.board_host}:{args.board_port} "
                  f"mode={args.board_mode} hello={_hello!r} "
                  f"(触摸事件记入 data/action_events.jsonl)")
        else:
            from wzry.control.executor import AdbExecutor
            ex = ActionExecutor(AdbExecutor(event_log=_evlog))
            print("执行模式: 真实操作(触摸事件记入 data/action_events.jsonl)")
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
                 "roster": None, "roster_pending": 0.0}

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
                if _act.get("type") == "stick_move":   # 连续摇杆保持(不碰 apply_action 语义)
                    ex.move(float(_act.get("theta", 0.0)), float(_act.get("r", MOVE_R)),
                            int(_act.get("duration_ms", 200)))
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
    # yolo every 3 ticks
    yolo_every = 3
    yolo_count = 0
    cached_dets = []
    t_end = time.time() + args.seconds if not args.forever else float("inf")

    print("M2 Agent v2 杩愯涓紙Ctrl+C 閫鍑猴級...\n")
    try:
        while time.time() < t_end:
            frame, lag_ms = cap.wait_frame(timeout=2.0)
            if frame is None:
##                print("  鏃犲抚堟祦寮傚父)
                continue
            n_frames += 1
            phase = sm.update(frame)
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
                _last_move[:] = (None, None, 0.0)   # 闈炲灞鏉惧紑鎽囨潌
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
                continue
            # v2.26 杩炵画鎽囨潌: 鍐崇瓥闂撮殭涓嶈鎽囨潌鏉炬墜(琛紳 200ms)
            # v14.2 改投递到执行队列(由 worker 串行发 adb, 主循环不再直接碰 ex)
            if do_action and _last_move[0] is not None and time.time() < _last_move[2]:
                if _exec_idle.is_set():
                    _action_q.put(({"type": "stick_move", "theta": _last_move[0],
                                    "r": _last_move[1], "duration_ms": 200}, cooldowns))
            if now - last_detect < detect_interval:
                continue
            last_detect = now

            # ---- 纭鍒----
            if tracker is None:
                tracker = make_tracker(frame)
            mm = tracker.update(frame)
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
                if not confirmed:
                    confirm_streak = 0
                    print(f"[{datetime.now():%H:%M:%S}] 対局确认中（小地图暂未定位）...")
                continue
            confirm_streak += 1
            if confirm_streak < CONFIRM_FRAMES:
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
                            print(f"[roster] modlens 澶辫触: {r.stderr[:150]}")
                            return
                        txt = r.stdout.strip()
                        start, end = txt.find("{"), txt.rfind("}")
                        if start < 0 or end < 0:
                            print(f"[roster] 瑙ｆ瀽澶辫触: {txt[:150]}")
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
                        print(f"[roster] 鎴戞柟 {ally} | 鏁屾柟 {enemy}")
                    except Exception as e:
                        print(f"[roster] 寮傚父: {e}")
                    finally:
                        cooldowns["roster_pending"] = 0.0

                _th.Thread(target=_roster_worker, args=(_frame_cp,), daemon=True).start()

            # ---- 鎰熺煡坴2.7歒OLO 闄嶉岀紦瀛樺鐢級----
            yolo_count += 1
            if yolo_count >= yolo_every:
                yolo_count = 0
                cached_dets = det.detect(frame)
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
                _h0, _s0 = _hsv0[..., 0].astype(int), _hsv0[..., 1].astype(int)
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
            st = build_state(frame, dets, phase.value, minimap={                "found": mm["found"], "center": mm["center"], "radius": mm["radius"],
                "dots": mm["dots"], "towers": mm["towers"],
            }, frame_id=frame_id)
            frame_id += 1
            st.t = time.time()  # 鍐崇瓥鏃跺埢
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
                            "screen": [_d.center[0] / w, _d.center[1] / h,
                                       (_d.xyxy[2] - _d.xyxy[0]) / w,
                                       (_d.xyxy[3] - _d.xyxy[1]) / h],
                            "conf": float(_d.conf),                            # v14.2
                            "camp_src": getattr(_d, "camp_src", None)})        # v14.2
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
                # 鎶鑳界姸鎬侊紙鏍噯鍧愭爣                try:
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
            n_ticks += 1
            infer_sum += det.last_infer_ms

            # ---- 瑙勫垯鍐崇瓥 + v2.40 鏈浼樺喅绛栧櫒(鏋氫妇鍊欓>浠峰艰瘎浼>閫夋渶浼 <2ms) ----
            action = decide(state_dict, cooldowns)
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
            try:
                from wzry.decision.optimizer import solve as _solve
                _hp0 = float((state_dict.get("ui") or {}).get("hp") or 1.0)
                _camp0 = cooldowns.get("camp") or "blue"
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
                print(f"[{datetime.now():%H:%M:%S}] [鍐崇瓥] {format_decision(action)}")
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
                # 闃典骸氱瓑寰呮硥姘村娲伙紝涓嶅仛浠讳綍鎿嶄綔堢敤鎴疯鍒欙級
                action = {"type": "none", "reason": "dead_waiting"}
                sig = (action.get("type"), action.get("id"))
            if last_dead and not beh.get("dead"):
                cooldowns["revive_t"] = now   # v2.49 璁板綍澶嶆椿鏃跺埢(5s 鍐呬笉鎺垬)
                # v14.2 复活=回泉水: 航位推算的"自己"必须一起归位, 否则估计位置还停在
                # 死前/目标处 -> map_move 认为"已到达"而不再推杆, 英雄永久僵在基地。
                _cb = cooldowns.get("camp") or "blue"
                cooldowns["self_est"] = BASE_BLUE if _cb == "blue" else BASE_RED
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

            # ---- 鎵 / 妯嫙 ----
            if action.get("type") != "none":
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
                        _action_q.put((action, cooldowns))
                        n_actions += 1
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
            try:
                vis_out = render_live_analysis(frame, dets, mm, state_dict, action, beh,
                                               reward.total, now)
            except Exception as e:
                print(f"[annot] 渲染异常: {e}")
                vis_out = frame.copy()

            # v2.81 self-diagnose: 带标注帧 + 血条读数 一起送 AI 审检
            try:
                if self_check is not None:
                    self_check.feed(vis_out, dets,
                                    ui=(state_dict.get("ui") or {}))
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

        print(f"\n完成: {n_frames}帧 感知{n_ticks} 次 "
              f"平均检测 {infer_sum / max(1, n_ticks):.0f}ms "
              f"动作 {n_actions} 次 模式: {'执行' if do_action else '模拟'}")
    except KeyboardInterrupt:
        print("\n手动退出。")
    finally:
        # v14.2 退出前 drain 动作队列: 让已入队的动作执行完(或在 5s 后放弃), 再关 recorder,
        #   否则"动作记录"与"实际执行"错位(最后几个动作没发出去却已落盘)。
        if do_action:
            try:
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
                _ls = _laya.stats()
                _laya.stop()
                print(f"[Laya] 统计: {_ls} | shadow 日志: {_laya.shadow_path}")
                if _ls.get("errors"):
                    print(f"[Laya][警告] 异常数={_ls['errors']} > 0, P1 门槛未过: "
                          f"{_ls.get('last_error')}")
            except Exception:
                pass


if __name__ == "__main__":
    main()
