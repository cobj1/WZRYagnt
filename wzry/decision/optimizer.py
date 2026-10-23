# -*- coding: utf-8 -*-
"""最优决策器 v2.41: 大规模候选枚举(30-80个) + 向量化价值评估 + argmax 最优解。

候选空间:
  - 每个敌方单位(屏幕英雄框/红点) -> 支援/进攻点(≤6)
  - 每个队友蓝点 -> 跟队点(≤5)
  - 每塔(敌塔推进/己塔防守) -> 推/守点(≤4)
  - 战斗微操: 每个近敌 8 方向走位点(≤16) + 边打边撤点
  - 技术候选: 钩(敌框<0.32)/一技能/普攻/连招
  - 战术候选: 蹲草(2点)/清线/转线/撤退/原地
单次求解(全部候选) 目标 < 1ms。
"""
import json
import math
from pathlib import Path

from wzry.vision.camp_src import UNTRUSTED_CAMP_SRC   # v15.2: camp_src 口径单一事实来源
from wzry.decision import official_rules as _official  # v15.3: 官方人机 AI 常量/谓词

ROOT = Path(__file__).resolve().parents[2]

_DEFAULT_W = {
    "w_win": 1.0, "w_safe": 1.2, "w_map": 0.6, "w_risk": 2.0,
    "w_hold": 0.4, "w_skill": 1.4, "w_micro": 0.3,
    "k_win_enemy": 1.5, "k_win_support": 0.6,
}

_W = dict(_DEFAULT_W)


def load_weights():
    global _W
    try:
        p = ROOT / "configs" / "value_weights.json"
        if p.exists():
            _W = {**_DEFAULT_W, **json.loads(p.read_text(encoding="utf-8"))}
    except Exception:
        pass


# v7.0 收益标定接入(用户: 标定收益, 演算哪步收益最高做哪步)
_AV = None


def load_action_value():
    """加载 configs/action_value.json (岭回归标定的特征->收益模型)。"""
    global _AV
    try:
        p = ROOT / "configs" / "action_value.json"
        if p.exists():
            _AV = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        _AV = None


def calibrated_value(feat=None, reason=None):
    """v7.0 查表式标定收益: reason -> 事后平均收益 (n>=5)。未知 reason 回退 default。"""
    if _AV is None:
        return None
    try:
        table = _AV.get("reason_value") or {}
        if reason:
            v = table.get(reason, None)
            if v is not None:
                return float(v)
            prefix = reason
            # 前缀匹配(如 follow_ally_minimap_hold 属于 follow_ally 族)
            for k, val in table.items():
                if reason.startswith(k[:15] if len(k) > 15 else k):
                    return float(val)
        return float(_AV.get("default", 0.0))
    except Exception:
        return None


def _feat_for_eval(f):
    """从 _feat 结果转标定特征向量。"""
    try:
        return [f.get("hp", 1.0), f.get("n_enemy", 0), f.get("n_red", 0),
                f.get("n_blue", 0), f.get("n_turret", 0),
                f.get("d_turret", 1.0), f.get("in_turret_zone", 0.0)]
    except Exception:
        return None


def save_weights():
    try:
        (ROOT / "configs" / "value_weights.json").write_text(
            json.dumps(_W, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def refresh_weights(new_w):
    """v2.62 在线学习注入: 局中更新价值权重(内存直写, 下一决策立即生效)。"""
    global _W
    if new_w:
        _W = {**_DEFAULT_W, **new_w}


E_HERO_MIN_CONF = 0.55        # v14.1 enemy_hero 可信置信下限

# v14.2 本期范围硬边界(laya-fusion-plan §1): solve 只优化移动/战术候选。
# 技能/攻击/连招/回城/恢复留给二期, 即便候选集里枚举出了也不输出, 一律交回规则层。
# 原实现靠"存在走廊红点时 gv 下限(约1.7~3.2) > hook 基础分(2.1)"的数值巧合保证不输出
# 技能动作 —— 调低 w_map 或调高 w_skill 就会破, 故在此显式化。
_TACTICAL_ONLY_TYPES = ("skill", "attack", "combo", "recall", "restore")


def enemy_hero_trusted(u):
    """enemy_hero 框是否可信(防"幻影敌人")。

    v14.1: 阵营分流用"框内蓝/红像素占比", 遮挡/远景/技能特效下占比不显著时会
    保守地兜底判成敌人(camp_src="default")——这类框在真机上表现为"敌方英雄"
    其实是我方或幻影, 会让 skill1/skill2/attack 朝空处放技能。低置信框同理。
    非检测器来源(缺 conf/camp_src)默认可信, 保持对旧行为的兼容。
    否决集见 `wzry/vision/camp_src.UNTRUSTED_CAMP_SRC`(单一事实来源, 勿在此另写字面量)。
    """
    if u.get("camp_src") in UNTRUSTED_CAMP_SRC:
        return False
    return float(u.get("conf", 1.0)) >= E_HERO_MIN_CONF


def _feat(state, hp):
    """一次性提取环境特征(供所有候选复用)。"""
    mm = state.get("minimap") or {}
    dots = (mm.get("dots") or {}) if mm.get("found") else {}
    reds = dots.get("red") or []
    blues = dots.get("blue") or []
    greens = dots.get("green") or []
    gx, gy = greens[0] if greens else (0.5, 0.5)
    units = state.get("units") or []
    enemies_scr = []
    minions_e = []
    for u in units:
        scr = u.get("screen") or [0.5, 0.5]
        if u.get("cls") == "enemy_hero":
            if enemy_hero_trusted(u):
                enemies_scr.append(scr)
        elif u.get("cls") == "enemy_minion":
            minions_e.append(scr)
    # v7.0 标定特征
    _turs = [u for u in units if u.get("cls") == "enemy_turret"]
    _d_t = 1.0
    if _turs:
        _ts = _turs[0].get("screen") or [0.5, 0.5]
        _d_t = math.hypot(_ts[0] - 0.5, _ts[1] - 0.5)
    return {"reds": reds, "blues": blues, "g": (gx, gy), "hp": hp,
            "e_scr": enemies_scr, "e_min": minions_e,
            "n_red": len(reds), "n_blue": len(blues), "n_enemy": len(enemies_scr),
            "n_turret": len(_turs), "d_turret": _d_t,
            "in_turret_zone": 1.0 if (_turs and _d_t < 0.45) else 0.0}


def _grid_best(f, camp="blue"):
    """微地图像素级密集采样(4096 点) 向量化评估 -> 最优移动点。
       V_grid = w_win*击杀场 - w_risk*敌群场 + w_safe*协同场 + w_map*走廊场 - 距离成本
       返回 (nx, ny, score) 单个 numpy 矩阵运算, ~0.3ms。"""
    import numpy as np
    N = 64                      # 64x64 = 4096 候选
    xs = np.linspace(0.02, 0.98, N)
    gx, gy = f["g"]
    hpx, hpy = f["hp"], f["hp"]
    GX, GY = np.meshgrid(xs, xs)
    V = np.zeros((N, N))
    if f["reds"]:
        # v2.85 支援路线硬约束: 红场仅保留中/下路走廊内红点(上路红点=不支援不去)
        _c = camp or "blue"
        _down_c = (0.75, 0.72) if _c == "blue" else (0.25, 0.28)
        _mid_c = (0.5, 0.58) if _c == "blue" else (0.5, 0.42)
        _reds_corridor = [p for p in f["reds"]
                          if min(math.hypot(p[0] - _down_c[0], p[1] - _down_c[1]),
                                 math.hypot(p[0] - _mid_c[0], p[1] - _mid_c[1])) <= 0.32]
        # v2.91 支持路线硬约束: 走廊外红点(上路/河道)不进攻击场 -> 只进规避场
        reds_use = _reds_corridor
        reds_dodge = [p for p in f["reds"] if p not in reds_use]
        # v14.2 修复: 红点全部落在走廊外时 reds_use 为空, 原先直接对空数组做
        #   np.min(axis=-1) -> "zero-size array to reduction operation minimum"
        #   ValueError, 异常在 gen_candidates() 之前抛出 -> _solve_core 整体回退,
        #   规则优化器与打分器同时被静默旁路("敌人只在上路"这类状态永不产生
        #   shadow 记录)。这里短路, 只保留走廊外红点的规避场。
        if reds_use:
            rx = np.array([p[0] for p in reds_use])
            ry = np.array([p[1] for p in reds_use])
            d_red = np.sqrt((GX[..., None] - rx) ** 2 + (GY[..., None] - ry) ** 2).min(axis=-1)
            # v15.3 对齐官方 RuleTactics::IsInDangerousPlace(0x027db2ac) 的危险半径:
            #   官方常量 301 单位(0x12d)，经 official_rules.norm 换算为小地图归一化距离
            V -= _W["w_risk"] * 0.6 * (d_red < _official.norm(_official.DANGER_DIST))
            if len(reds_use) < 6:      # v2.53 敌群>=6: 红场清零(不向敌群走)
                V += _W["w_win"] * _W["k_win_enemy"] * np.exp(-d_red / 0.08)
                n_near = ((np.sqrt((GX[..., None] - rx) ** 2 + (GY[..., None] - ry) ** 2) < 0.15)
                          .sum(axis=-1))
                V -= _W["w_risk"] * (0.5 * (n_near >= 3) + 0.25 * (n_near == 2))
        if reds_dodge:
            # 走廊外红点=威胁不支援: 压接近分(不进河/不进上路团)
            rx2 = np.array([p[0] for p in reds_dodge])
            ry2 = np.array([p[1] for p in reds_dodge])
            d_dodge = np.sqrt((GX[..., None] - rx2) ** 2 + (GY[..., None] - ry2) ** 2).min(axis=-1)
            V -= _W["w_risk"] * 0.9 * np.exp(-d_dodge / 0.10)
    if f["blues"]:
        bx = np.array([p[0] for p in f["blues"]])
        by = np.array([p[1] for p in f["blues"]])
        d_blue = np.sqrt((GX[..., None] - bx) ** 2 + (GY[..., None] - by) ** 2).min(axis=-1)
        V += _W["w_safe"] * 0.5 * np.exp(-d_blue / 0.10)
    down_c = (0.75, 0.72) if camp == "blue" else (0.25, 0.28)
    mid_c = (0.5, 0.58) if camp == "blue" else (0.5, 0.42)
    V += _W["w_map"] * 2.8 * np.exp(-np.sqrt((GX - down_c[0]) ** 2 + (GY - down_c[1]) ** 2) / 0.20)
    V += _W["w_map"] * 1.2 * np.exp(-np.sqrt((GX - mid_c[0]) ** 2 + (GY - mid_c[1]) ** 2) / 0.20)
    d_self = np.sqrt((GX - gx) ** 2 + (GY - gy) ** 2)
    if f["hp"] < 0.8:
        V -= 0.8 * (d_self > 0.5)
    iy, ix = np.unravel_index(int(np.argmax(V)), V.shape)
    return float(xs[ix]), float(xs[iy]), float(V[iy, ix])


def gen_candidates(f):
    """枚举大候选空间 -> [(action, tag)]"""
    gx, gy = f["g"]
    reds, blues = f["reds"], f["blues"]
    hp = f["hp"]
    down_c = (0.75, 0.75)
    mid_c = (0.5, 0.58)
    cands = []

    def mc(x, y, tag):
        cands.append(({"type": "map_move", "nx": max(0.02, min(0.98, x)),
                       "ny": max(0.02, min(0.98, y)), "reason": tag}, tag))

    # 1) 红点=支援/进攻(≤6)  v2.63: 下路强优先(下路走廊池), mid 仅下路空时兜底
    down_c = (0.75, 0.72)
    mid_c = (0.5, 0.58)
    _down_pts = [p for p in reds[:8] if
                 math.hypot(p[0] - down_c[0], p[1] - down_c[1]) <= 0.32]
    _corridor_pts = _down_pts
    if not _down_pts:
        _corridor_pts = [p for p in reds[:8] if
                         math.hypot(p[0] - mid_c[0], p[1] - mid_c[1]) <= 0.32]
    for i, p in enumerate(_corridor_pts[:6]):
        tag = f"support_red{i}"
        mc(p[0], p[1], tag)
    # 2) 每个蓝点=跟队(≤5)
    for i, p in enumerate(blues[:5]):
        mc(p[0], p[1], f"follow_blue{i}")
    # 3) 走廊战术点: 下路/中路走廊中心
    for i, (cxn, cyn, tag) in enumerate(((down_c[0], down_c[1], "lane_down"),
                                         (mid_c[0], mid_c[1], "lane_mid"))):
        mc(cxn, cyn, tag)
    # 4) v3.1 用户铁律: 不蹲草! 删除 hold_hook 蹲草点(红己连线中段)
    # 5) 战斗微操: 每个近敌(屏内) 8 向走位(≤2敌) + 撤离
    for i, e in enumerate(f["e_scr"][:2]):
        ex, ey = float(e[0]) - gx, float(e[1]) - gy
        for ang in range(8):
            a = ang * math.pi / 4
            mc(gx + math.cos(a) * 0.10 + ex * -0.0, gy + math.sin(a) * 0.10,
               f"micro_{i}_{ang}")
        # 边打边撤: 远离该敌方向
        d = math.hypot(ex, ey) or 1e-6
        # v14.2 标签唯一化: 原先每个屏内敌人都产出同一个 tag "kite_retreat_pressure",
        #   屏内 2 敌时短标签重复 -> Laya 的 choice 字典折叠成更少选项, 但 K 仍按
        #   候选数算, 且同一份概率被复用到两个不同走位 -> 排序加分失真。加敌人索引。
        mc(gx - ex / d * 0.15, gy - ey / d * 0.15, f"kite_retreat_pressure_{i}")
    # 6) 技术候选
    if f["e_scr"]:
        ne = min(f["e_scr"], key=lambda e: math.hypot(float(e[0]) - 0.5,
                                                       float(e[1]) - 0.5))
        d = math.hypot(float(ne[0]) - 0.5, float(ne[1]) - 0.5)
        if d <= 0.32 and hp >= 0.6:
            cands.append(({"type": "skill", "id": 2, "mode": "tap",
                           "reason": "enemy_in_skill2_range"}, "hook"))
        if d <= 0.40 and hp >= 0.6:
            cands.append(({"type": "skill", "id": 1, "mode": "tap",
                           "reason": "enemy_hero_near"}, "skill1"))
        cands.append(({"type": "attack", "priority": "free", "reason":
                       "engage_enemy"}, "attack"))
    # 7) 清线(近敌兵): 朝最近敌兵方向 0.15
    if f["e_min"]:
        em = min(f["e_min"], key=lambda e: math.hypot(float(e[0]) - 0.5,
                                                      float(e[1]) - 0.5))
        dex, dey = float(em[0]) - 0.5, float(em[1]) - 0.5
        dd = math.hypot(dex, dey) or 1e-6
        mc(gx + dex / dd * 0.15, gy + dey / dd * 0.15, "clear_lane")
    # 8) 回城/撤退 & 原地
    #   v15.3 对齐官方 RuleTactics::CheckReturnCity(0x027df394) 的具名分支。
    #   当前状态只有 hp，故只有 FORCE_RETURN_CITY_HP 会命中；ep / 复活甲 / gank 时段
    #   三个分支已在 official_rules.return_city_decision 留好入参，接上数据即生效。
    _rc, _rc_reason = _official.return_city_decision(hp=hp)
    if _rc:
        # 标签保留 `retreat_low_hp` 前缀: configs/action_value.json 的标定按前缀匹配,
        #   换前缀会让历史标定静默失效并回退到 default。
        _tag = f"retreat_low_hp_{_rc_reason}"
        if gx > 0.4:
            mc(0.90, 0.90, _tag)
        else:
            mc(0.10, 0.10, _tag)
    cands.append(({"type": "none", "reason": "stand"}, "stand"))
    return cands


def evaluate(c, f, camp="blue"):
    """候选价值: 数值计算(<3us)。"""
    a = c[0]
    t = a.get("type")
    gx, gy = f["g"]
    # v7.0 标定收益基分(查表 reason->收益, 60%) + 战术权重(40%)
    _cv = None
    try:
        _cv = calibrated_value(reason=str(a.get("reason", "")))
    except Exception:
        _cv = None
    v = 0.6 * float(_cv) if _cv is not None else 0.0
    if t == "map_move":
        nx, ny = a.get("nx", 0.5), a.get("ny", 0.5)
        d_red = min([math.hypot(nx - p[0], ny - p[1]) for p in f["reds"]], default=9.9)
        if d_red < 0.12:
            v += _W["w_win"] * _W["k_win_enemy"]
        n_red_near = sum(1 for p in f["reds"] if math.hypot(nx - p[0], ny - p[1]) < 0.15)
        v -= _W["w_risk"] * (0.5 if n_red_near >= 3 else (0.25 if n_red_near == 2 else 0.0))
        d_blue = min([math.hypot(nx - p[0], ny - p[1]) for p in f["blues"]], default=9.9)
        if d_blue < 0.10:
            v += _W["w_safe"] * 0.5
        for cxy, k in (((0.75, 0.75), 1.0), ((0.5, 0.58), 0.9)):
            if camp == "red":
                cxy = (1 - cxy[0], 1 - cxy[1])
            if math.hypot(nx - cxy[0], ny - cxy[1]) <= 0.28:
                v += _W["w_map"] * k
        d_self = math.hypot(nx - gx, ny - gy)
        if d_self > 0.5 and f["hp"] < 0.8:
            v -= 0.8
        # 蹲草标记加分(短距离安全点)
        if a.get("reason", "").startswith("hold_hook"):
            v += _W["w_hold"]
        elif a.get("reason", "").startswith("micro"):
            v += _W["w_micro"] - d_self * 0.5
        elif a.get("reason", "").startswith("kite"):
            v += _W["w_safe"] * 0.6
    elif t == "skill" and a.get("id") == 2:
        v += _W["w_skill"] * _W["k_win_enemy"]
    elif t == "skill" and a.get("id") == 1:
        v += _W["w_skill"] * 0.8
    elif t == "attack":
        v += _W["w_skill"] * 0.4
    return v


def _forecast(c, f0):
    """v12.1 前瞻演算(蒙特卡洛式, 用户: 像下棋演算多步): 模拟执行后
    估计"未来2步"收益: 移动后距敌/距队变化 + 敌群趋近风险。
    v14.1 提升到模块级(laya-fusion-plan C4: 数据构建复刻同一规则分 r)。"""
    try:
        a = c[0]
        if a.get("type") != "map_move":
            return 0.0
        nx, ny = a.get("nx", 0.5), a.get("ny", 0.5)
        # 模拟: 移到此点后, 敌/队友距离变化
        _d_red_before = min([math.hypot(f0["g"][0]-p[0], f0["g"][1]-p[1]) for p in f0["reds"]], default=9.9)
        _d_red_after = min([math.hypot(nx-p[0], ny-p[1]) for p in f0["reds"]], default=9.9)
        _d_blue_before = min([math.hypot(f0["g"][0]-p[0], f0["g"][1]-p[1]) for p in f0["blues"]], default=9.9)
        _d_blue_after = min([math.hypot(nx-p[0], ny-p[1]) for p in f0["blues"]], default=9.9)
        v = 0.0
        if _d_red_after > _d_red_before + 0.05:
            v += 0.8        # 远离敌人(安全提升)
        elif _d_red_after < _d_red_before - 0.05:
            v -= 0.8        # 靠近敌人(风险)
        if _d_blue_after < _d_blue_before - 0.05:
            v += 0.5        # 靠近队友(协同)
        if f0.get("hp", 1.0) < 0.5 and _d_red_after < 0.2:
            v -= 1.5        # 残血向敌群 = 大风险
        return v
    except Exception:
        return 0.0


def _solve_core(state, fallback_action, hp, camp="blue", base_score=0.0):
    """v2.42: 像素级密采样4096移动候选(向量化) + 战术候选枚举 -> 全局最优。
        若移动最优分高 -> map_move(网格点); 否则与战术候选(技能/蹲/原地)比优。
        全程 <2ms。异常回退 fallback。"""
    try:
        f = _feat(state, hp)
        # v2.73 动作类型白名单: solve 只优化移动, 绝不覆盖技能/攻击/连招/回城/恢复
        if fallback_action and fallback_action.get("type") in (
                "skill", "attack", "combo", "recall", "restore", "none"):
            return fallback_action
        # v2.65 无敌人证据: 直接下放给规则决策(跟队/蹲草/撤退), 不做走廊乱转
        if not f["reds"]:
            return fallback_action
        gx, gy, gv = _grid_best(f, camp)
        cands = gen_candidates(f)
        # v2.55 保命硬过滤: 敌群>=6 或 血<0.5 -> 只留 原地/跟队/蹲草/撤退(删support/技能/进攻)
        if len(f["reds"]) >= 6 or hp < 0.5:
            keep_prefix = ("stand", "follow", "hold", "retreat", "stand_")
            cands = [c for c in cands
                     if any(c[0].get("reason", "").startswith(p) for p in keep_prefix)
                     or c[0].get("type") == "none"]
        # v12.1 前瞻演算(蒙特卡洛式, 用户: 像下棋演算多步): 每候选评估 + 模拟执行后
        #   估计"未来2步"收益: 移动后距敌/距队变化 + 敌群趋近风险 -> 加权 0.35
        #   (v14.1 _forecast 提升到模块级, 复刻同一规则分)
        scored = [(evaluate(c, f, camp) + 0.35 * _forecast(c, f), c) for c in cands]
        # v14.2 修复(laya-fusion-plan 评审 2.1): 阈值闸门必须用"重排前固定"的最高规则分
        #   r_max。原先用 scored[0][0] —— 重排后它是 s-argmax 的 r, 不是 r-argmax 的 r,
        #   于是 gv 比较与 <=0 回退的结果会随打分器排序变化(等价于阈值语义被 Laya 改写):
        #   Laya 把低分候选提到首位即可翻转 gv 分支, 静默丢弃自己的选择。
        #   未接 scorer 时 scored 本就按 r 降序, r_max == scored[0][0], 行为完全不变。
        r_max = max((s[0] for s in scored), default=0.0)
        # v14.0 打分器: reorder=True 表示已按混合分重排(排序用 s=r+beta*(p-1/K)),
        # 但 scored[i][0] 保留原始规则分 -> 与 gv 比较、<=0 回退的阈值语义不变。
        _by_scorer = False
        if _SCORER is not None:
            try:
                _by_scorer = _SCORER.reorder(scored, f, hp, camp)
            except Exception:
                _by_scorer = False
        if not _by_scorer:
            scored.sort(key=lambda x: -x[0])
        # 战术候选最高分 vs 网格移动分
        if scored and gv >= r_max:
            return {"type": "map_move", "nx": max(0.02, min(0.98, gx)),
                    "ny": max(0.02, min(0.98, gy)), "reason": "grid_best"}
        if scored:
            best = scored[0][1][0]
            if best.get("type") == fallback_action.get("type") and \
                    best.get("reason") == fallback_action.get("reason"):
                return fallback_action
            if r_max <= 0.0 and fallback_action.get("type") != "none":
                return fallback_action
            if best.get("type") in _TACTICAL_ONLY_TYPES:   # v14.2 本期范围硬边界
                return fallback_action
            return best
        return fallback_action
    except Exception:
        return fallback_action


_SCORER = None


def set_scorer(scorer, beta=None, mode=None):
    """v14.0 可插拔打分器(wzry.decision.laya_scorer.LayaScorer)。
    shadow=仅记录; ensemble=混合排序; 传 None 恢复纯规则。
    beta/mode 可选: 传入时同步更新 scorer 上的属性(laya-fusion-plan C2 签名)。"""
    global _SCORER
    _SCORER = scorer
    if scorer is not None:
        if beta is not None:
            scorer.beta = float(beta)
        if mode is not None:
            scorer.mode = mode


def solve(state, fallback_action, hp, camp="blue", base_score=0.0):
    """v14.0: _solve_core 的包装层 — 打分器记录最终选择; 未接 scorer 时与 v2.42 完全一致。"""
    action = _solve_core(state, fallback_action, hp, camp, base_score)
    if _SCORER is not None:
        try:
            _SCORER.log_chosen(action)
        except Exception:
            pass
    return action


# v7.0 模块加载即启用标定收益
load_action_value()
