# -*- coding: utf-8 -*-
"""英雄头顶血条检测（HP/MP/阵营）。

王者荣耀 HUD 语义（用户指导）：
  - 自己英雄头顶血条 = 绿色
  - 队友英雄头顶血条 = 蓝色
  - 敌人英雄头顶血条 = 红色
  - 绿条下方有蓝色细条 = 蓝量（MP）

实现：
  - 全画面找高亮细横条（V>140, 宽25-150, 高<=10）
  - 按颜色分类：绿=自己、蓝=队友、红=敌人
  - 自己 HP = 绿条长度比例；MP = 绿条下方蓝条比例
  - 排除 HUD 边缘（x>1230 右侧 UI、y<60 顶部、y>660 底部）

用法：
    from wzry.vision.self_bars import detect_all_bars
    bars = detect_all_bars(frame)  # {"self": [...], "allies": [...], "enemies": [...]}
    hp, mp = self_hp_mp(frame)
"""
from __future__ import annotations

import cv2
import numpy as np

from wzry.vision import colors   # HSV 判据单一事实来源（v15.3）

# 血条特征
BRIGHT_V = 140          # 血条高亮
BAR_W_MIN, BAR_W_MAX = 25, 150
BAR_H_MAX = 13
BAR_H_MIN = 5           # v2.36 野怪/小怪血条很细(1-4px), 英雄血条粗(5-9px) -> 下限过滤
                       # v2.87: 上限放宽 10->13 (PW帧抗锯齿绿条 9-11px, 旧10px丢失->假dead)
BAR_AREA_MIN = 40
# HUD 排除区（右侧 UI、顶部 HUD、底部技能栏）
EXCLUDE_X = 1230
EXCLUDE_Y_TOP = 240    # v2.13: 顶部 240px 是小地图(0-232)+头像条，无真实血条（旧 60 误检多）
EXCLUDE_Y_BOTTOM = 660

# 自己英雄搜索区（中央偏下）
HERO_REGION = (500, 300, 800, 550)
MIN_HERO_AREA = 300


def _scale(frame):
    """HUD 常量以 1280x720 为基准，按实际帧缩放（20:9 生产帧为 1280x576）。"""
    h, w = frame.shape[:2]
    return w / 1280.0, h / 720.0


def hero_bar_check(frame, cx, cy, half_w=60, y_span=40):
    """v2.75 敌英血条厚度校验: (cx,cy)附近找红条, 返回最高条高(px)。
    >=BAR_H_MIN(5)=英雄; 1-4px=野怪/小兵 -> 由调用方剔除。无条返回 None。"""
    try:
        sx, sy = _scale(frame)
        half_w = max(1, int(round(half_w * sx)))
        y_span = max(1, int(round(y_span * sy)))
        x0 = max(0, int(cx) - half_w)
        x1 = min(frame.shape[1], int(cx) + half_w)
        y0 = max(0, int(cy))
        y1 = min(frame.shape[0], int(cy) + y_span)
        roi = frame[y0:y1, x0:x1]
        if roi.size == 0:
            return None
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        S, V = hsv[..., 1].astype(int), hsv[..., 2].astype(int)
        m = ((S > 60) & (V > 100)).astype(np.uint8)
        n, lab, st, _ = cv2.connectedComponentsWithStats(m, 8)
        w_min = max(1, int(round(10 * sx)))
        w_max = max(w_min, int(round(200 * sx)))
        h_max = max(1, int(round(12 * sy)))
        best = None
        for i in range(1, n):
            x, y, w2, h2, area = st[i]
            # 红条: 宽 10-200, 高 1-12, 长宽比>=3(横条), 红色(R>G*1.4)
            if not (w_min <= w2 <= w_max and 1 <= h2 <= h_max and w2 >= 3 * h2):
                continue
            seg = roi[y:y + h2, x:x + w2]
            rmean = float(seg[..., 2].mean())
            gmean = float(seg[..., 1].mean())
            if rmean < gmean * 1.3:
                continue
            if best is None or h2 > best:
                best = h2
        return best
    except Exception:
        return None

# v2.13 血条标定（1280x720）：头顶血条满条宽 ~115px（标注帧 90-123 中位）
FULL_BAR_W = 115.0
# 血条中心到英雄位置垂直距离（血条在头顶上方）
HERO_BAR_GAP = 55

# v16.0 分段式血条的"桥接"宽度：真机 10-18 的血条每 ~10px 有一道黑刻度，
#   绿色连续段往往 < 开运算核(21px) -> 整条被抹掉。闭运算 13px 可跨过单道刻度。
SELF_BAR_BRIDGE = 13


def _spec_mask_u8(hsv, spec, v_gt=BRIGHT_V):
    """colors 规格 -> 0/255 掩码，用 `cv2.inRange`(C++) 实现。

    v15.3 逐像素等价于旧式 `(hue_cond(H,S,V) & (V > BRIGHT_V)).astype(np.uint8) * 255`：
    阈值全 <256，inRange 的闭区间边界与 `>=`/`<=` 一一对应；`V > v_gt` 折算成 `V >= v_gt+1`
    并与规格自带的 V 下界取较大者（旧实现无条件 AND 上 `V > BRIGHT_V`，故规格里的更宽 V 门不生效）。
    """
    h_lo, h_hi, s_lo, s_hi, v_lo, v_hi = spec
    if v_gt is not None:
        v_lo = (v_gt + 1) if (v_lo is None or v_gt + 1 > v_lo) else v_lo
    s0, s1 = (0 if s_lo is None else s_lo), (255 if s_hi is None else s_hi)
    v0, v1 = (0 if v_lo is None else v_lo), (255 if v_hi is None else v_hi)
    if h_lo > h_hi:                                   # 红：色相环绕
        return cv2.bitwise_or(cv2.inRange(hsv, (0, s0, v0), (h_hi, s1, v1)),
                              cv2.inRange(hsv, (h_lo, s0, v0), (180, s1, v1)))
    return cv2.inRange(hsv, (h_lo, s0, v0), (h_hi, s1, v1))


def _find_bars(frame, hue_cond, label, hsv=None, bridge=0, y_top=None):
    """全画面找指定颜色的高亮细横条。

    v15.2 性能（逐像素等价）：
      - H/S/V 直接用 uint8 视图，不再 `astype(int)`（全帧升 int64 实测 ~11ms/次，
        而所有颜色阈值都 <256，比较结果逐像素一致）；
      - `hsv` 可由调用方传入复用 —— detect_all_bars 的三种颜色本就要各扫一遍全帧。
    v15.3：`hue_cond` 可传 colors 规格(6 元组) -> 走 `cv2.inRange` 快路径（全帧布尔运算约
      5ms/色 -> 约 0.5ms）；旧式 callable 仍兼容（脚本/调试用）。
    v16.0：`bridge` = 先做 (bridge,1) 的**闭运算**把被黑色刻度切断的色段接回去。
      真机 10-18 出现"分段式血条"（每 ~10px 一道黑刻度），绿色连续段 < 下面开运算的
      21px 核 -> 整条被抹平、连一条候选都找不到（实测 0 条）。见 self_hp_mp 调用处。
    """
    if hsv is None:
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    sx, sy = _scale(frame)
    w_min = max(1, int(round(BAR_W_MIN * sx)))
    w_max = max(w_min, int(round(BAR_W_MAX * sx)))
    h_min = max(1, int(round(BAR_H_MIN * sy)))
    h_max = max(h_min, int(round(BAR_H_MAX * sy)))
    area_min = max(1, int(round(BAR_AREA_MIN * sx * sy)))
    x_limit = EXCLUDE_X * sx
    y_bottom = EXCLUDE_Y_BOTTOM * sy
    y_top_default = EXCLUDE_Y_TOP * sy
    if isinstance(hue_cond, tuple):                   # colors 规格 -> inRange 快路径
        mask = _spec_mask_u8(hsv, hue_cond)
    else:
        H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        mask = (hue_cond(H, S, V) & (V > BRIGHT_V)).astype(np.uint8) * 255
    if bridge and bridge > 1:
        mask = cv2.morphologyEx(
            mask, cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_RECT, (int(bridge), 1)))
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (21, 1))
    bars = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
    n, lab, st, cent = cv2.connectedComponentsWithStats(bars, 8)
    found = []
    for i in range(1, n):
        w_, h_ = st[i, cv2.CC_STAT_WIDTH], st[i, cv2.CC_STAT_HEIGHT]
        area = st[i, cv2.CC_STAT_AREA]
        if not (w_min <= w_ <= w_max and h_min <= h_ <= h_max
                and area >= area_min):
            continue
        lx, ly = int(cent[i][0]), int(cent[i][1])
        _yt = y_top_default if y_top is None else y_top
        if lx > x_limit or ly < _yt or ly > y_bottom:
            continue
        found.append({"x": lx, "y": ly, "w": w_, "h": h_, "ratio": w_ / 150.0})
    found.sort(key=lambda b: (b["y"], b["x"]))
    return found


def detect_all_bars(frame):
    """检测所有英雄血条：绿=自己、蓝=队友、红=敌人。

    蓝条（队友）过滤：
      - 宽度 <= 80（场景/UI 宽条误检排除）
      - 远离自己绿条（自己蓝量条不算队友）
    v2.92: 敌人红条专属过滤 y>=300 (顶部头像条 y240-300 红边=队友头像误判) + x>=380
            (左上角小队头像条列 x 220-380 全为 UI)
    """
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)   # v15.2: 三色共用一次全帧转换
    sx, sy = _scale(frame)
    result = {   # v15.3 阈值集中到 wzry/vision/colors.py；传规格 -> 走 inRange 快路径
        "self": _find_bars(frame, colors.GREEN_SCAN, "self", hsv=hsv),
        "allies": _find_bars(frame, colors.BLUE_SCAN, "ally", hsv=hsv),
        "enemies": _find_bars(frame, colors.RED_CAMP, "enemy", hsv=hsv),
    }
    # v2.92 敌人红条 UI 带排除: 顶部头像条区(y<300, x<380)与左列小队头像条(x<380)
    result["enemies"] = [b for b in result["enemies"]
                         if b["y"] >= 300 * sy and b["x"] >= 380 * sx]
    # 蓝条过滤：宽条（>80）排除；自己绿条旁 60px 内的蓝条=自己蓝量，排除
    if result["self"]:
        self_bars = result["self"]
        my_x = self_bars[0]["x"] if self_bars else None
        my_y = self_bars[0]["y"] if self_bars else None
        if my_x is not None:
            result["allies"] = [
                b for b in result["allies"]
                if b["w"] <= 80 * sx
                and not (abs(b["x"] - my_x) < 60 * sx and abs(b["y"] - my_y) < 40 * sy)
            ]
    else:
        result["allies"] = [b for b in result["allies"] if b["w"] <= 80 * sx]
    return result


def find_color_bars(frame, is_col, y_top=70, y_bot=635, x_max=1230,
                    wmin=25, wmax=150, gap=9, exclude_mm=True, thick_min=4, thick_max=14):
    """找指定颜色的分段血条（刻度将条切成多段 → 行内 gap 合并 + 行间堆叠成条）。

    返回 [{x0, x1, y, w, cx}]，y 为条顶部行，w 为最大行宽。
    v2.36: thick 过滤——野怪/小怪血条很细(2-3px), 英雄血条较粗(5-9px)
           条厚 < thick_min 或 > thick_max 的条剔除(野怪白给, 英雄误杀).
    排除：小地图区(x<245,y<245)、右上/右下 UI(x>=x_max)、底部(y>y_bot)。
    """
    h, w = frame.shape[:2]
    # v15.3 修既有缺陷: y_bot/x_max 是 720p 默认值, 在 20:9(1280x576) 帧上会越界 IndexError
    y_bot = min(int(y_bot), h)
    x_max = min(int(x_max), w)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    if isinstance(is_col, tuple):
        # v15.3 colors 规格 -> inRange 快路径；不加 V>BRIGHT 约束（与旧实现一致），
        #   `> 0` 是为了保持旧实现的 0/1 口径（下游只用真值性，但 0/1 便于逐像素比对）
        mask = (_spec_mask_u8(hsv, is_col, v_gt=None) > 0).astype(np.uint8)
    else:
        H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        mask = is_col(H, S, V).astype(np.uint8)
    rows = []  # (y, x0, x1)
    for y in range(y_top, y_bot):
        if exclude_mm:
            row = mask[y, :x_max].copy()
            if y < 245:
                row[:245] = 0
        else:
            row = mask[y, :x_max]
        idx = np.nonzero(row)[0]
        if len(idx) < wmin // 2:
            continue
        groups = []
        start = prev = idx[0]
        for i in idx[1:]:
            if i - prev > gap:
                if prev - start + 1 >= wmin:
                    groups.append((start, prev))
                start = i
            prev = i
        if prev - start + 1 >= wmin:
            groups.append((start, prev))
        for g0, g1 in groups:
            if g1 - g0 + 1 <= wmax:
                rows.append((y, int(g0), int(g1)))
    if not rows:
        return []
    # 行间堆叠：y 差<=2 且 x 区间重叠 -> 同一条
    rows.sort()
    bars = []
    for y, x0, x1 in rows:
        placed = False
        for b in bars:
            if y - b["yb"] <= 2 and not (x1 < b["x0"] - 10 or x0 > b["x1"] + 10):
                b["yb"] = y
                b["x0"] = min(b["x0"], x0)
                b["x1"] = max(b["x1"], x1)
                placed = True
                break
        if not placed:
            bars.append({"y": y, "yb": y, "x0": x0, "x1": x1})
    out = []
    for b in bars:
        w = b["x1"] - b["x0"] + 1
        thick = b["yb"] - b["y"] + 1
        if w < wmin or thick < thick_min or thick > thick_max:
            continue
        out.append({"x0": b["x0"], "x1": b["x1"], "y": b["y"],
                    "w": w, "cx": (b["x0"] + b["x1"]) // 2, "thick": thick})
    return out


def find_ally_bars(frame):
    """队友蓝条（分段合并版）。阈值: colors.BLUE_SEG。"""
    return find_color_bars(frame, colors.BLUE_SEG)


def find_enemy_bars(frame):
    """敌人红条（分段合并版）。阈值: colors.RED_SEG。"""
    return find_color_bars(frame, colors.RED_SEG)
    """在头顶区域找指定色相的横条，返回 (ratio, 条长, 区域宽)。

    v2.13：改用连通域找最长水平条（逐行 run 对血条渐变/缺口不鲁棒）。
    """
    if head_roi is None or head_roi.size == 0:
        return None, 0, 0
    hsv = cv2.cvtColor(head_roi, cv2.COLOR_BGR2HSV)
    H, S, V = hsv[..., 0].astype(int), hsv[..., 1].astype(int), hsv[..., 2].astype(int)
    mask = (hue_cond(H, S, V)).astype(np.uint8) * 255
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 1))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
    n, lab, st, cent = cv2.connectedComponentsWithStats(mask, 8)
    best_w, best_x, best_y = 0, 0, 0
    for i in range(1, n):
        w_, h_ = st[i, cv2.CC_STAT_WIDTH], st[i, cv2.CC_STAT_HEIGHT]
        if h_ > 14 or w_ < 12:  # 横条：高 <=14，宽 >=12
            continue
        if w_ > best_w:
            best_w = int(w_)
            best_x, best_y = int(cent[i][0]), int(cent[i][1])
    if best_w > 0:
        return best_w / max(1, mask.shape[1]), best_w, mask.shape[1], (best_x, best_y)
    return None, 0, mask.shape[1], None


def kill_banner_px(frame):
    """v2.96 击杀播报检测: 屏幕顶部中央('你击败了xx'金色横幅, 0-46, 500-790)。
    金色: H 15-40, S>120, V>140。击杀瞬间横幅出现即可作为 kill/assist 铁证
    (旧"敌人消失=击杀"是假事件: 敌人进草/走掉也算 -> 战绩0杀却记一堆kill)。"""
    try:
        roi = frame[0:46, 500:790]
        if roi.size == 0:
            return 0
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        return colors.count(hsv, colors.YELLOW_KILL)   # v15.3 阈值见 colors.py
    except Exception:
        return 0


def death_banner_px(frame):
    """v2.87 死亡横幅检测: 顶部中央(0-46, 500-790)暗红色像素数。
    真死亡('查看死亡回放'深红横幅)>=2500; 击杀播报/存活时<=1800 -> 阈值2500分离。"""
    try:
        sx, sy = _scale(frame)
        roi = frame[0:int(round(46 * sy)), int(round(500 * sx)):int(round(790 * sx))]
        if roi.size == 0:
            return 0
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        return colors.count(hsv, colors.RED_DEATH)   # v15.3 阈值见 colors.py
    except Exception:
        return 0


def self_hp_mp(frame):
    """检测自己英雄 HP/MP（头顶绿条 + 蓝条）。

    返回 (hp_ratio, mp_ratio, hero_pos) 或 (None, None, None)。

    v2.13 重构（血条驱动）：
      - 旧版用"中央高饱和大块"定位英雄，场景（水域/草丛/金币提示）常误判
        → 血条位置全错，HP/MP 交替缺失
      - 新版：全画面绿条中**最宽的** = 自己血条（队友蓝/敌人红/场景绿短条）
        → HP = 条宽 / 满条宽(115px)；MP = 绿条正下方蓝条
      - 修复旧 ratio 用区域宽(200px)作分母导致满血只显示 0.55 的低估 bug
    """
    h, w = frame.shape[:2]
    sx, sy = _scale(frame)
    full_bar_w = FULL_BAR_W * sx
    hero_gap = HERO_BAR_GAP * sy

    # v16.0 两处放宽（真机 10-18 出现"分段式血条 + 镜头偏高"，原判据一条都找不到）：
    #   1) bridge=SELF_BAR_BRIDGE：黑色刻度把绿条切成 <21px 的短段，会被开运算(21,1)整条抹掉
    #      （实测该帧 0 条候选）
    #   2) y 上下界改比例：原来固定 [250,550]，而该帧自己血条在 y≈206，被直接排除
    greens = _find_bars(frame, colors.GREEN_SELF_LOOSE, "self",
                        bridge=SELF_BAR_BRIDGE, y_top=int(0.30 * h))
    if not greens:
        return None, None, None
    # 排除小地图方形区域 (x<240, y<240) 里的绿点/绿圈
    greens = [b for b in greens if not (b["x"] < 240 and b["y"] < 240)]
    # 排除 HUD 区误检：自己英雄镜头跟随一般在画面中部
    greens = [b for b in greens if b["y"] >= 0.30 * h]
    # 底部按钮区(战队/结算界面)绿色按钮误检排除
    greens = [b for b in greens if b["y"] <= 0.96 * h]
    if not greens:
        return None, None, None
    # 自己血条 = 最宽的绿条（血条 90-123px，场景绿 <60px）
    greatest = max(greens, key=lambda b: b["w"])
    bx, by, bw = greatest["x"], greatest["y"], greatest["w"]
    hp = min(1.0, bw / max(1.0, full_bar_w))
    hero_pos = (bx, by + hero_gap)

    # MP：绿条正下方 3-25px。蓝条左端与 HP 条左端对齐、填充连续；
    # 用"最右蓝色列位置"换算抗遮挡（金币飘字/血条被截断时左段法会低估）
    mp = None
    y0 = max(0, by + max(1, int(round(3 * sy))))
    y1 = min(h, by + max(1, int(round(27 * sy))))
    if y1 > y0:
        bar_left = bx - bw // 2
        x_lo = max(0, bar_left - 2)
        x_hi = min(w, bar_left + int(full_bar_w) + 4)
        if x_hi > x_lo:
            sub = frame[y0:y1, x_lo:x_hi]
            hsv2 = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
            m2 = colors.mask(hsv2, colors.BLUE_MP)   # v15.3 阈值见 colors.py
            cols = m2.any(axis=0)
            if cols.any():
                last = int(np.nonzero(cols)[0].max())
                if last >= max(1, int(round(12 * sx))):
                    mp = min(1.0, (last + 1) / max(1.0, full_bar_w))
                else:
                    mp = None
    if hp is not None and bw < max(1, int(round(25 * sx))):
        hp = None
    return hp, mp, hero_pos


# 兼容旧接口
def detect_self_bars(frame):
    return self_hp_mp(frame)


if __name__ == "__main__":
    import numpy as np
    for f in ["s01.png", "s05.png", "s08.png", "s11.png"]:
        data = np.fromfile(rf"E:\WZRYagent\temp\ann\{f}", dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if img is None:
            print(f"{f}: 无法读取")
            continue
        hp, mp, pos = self_hp_mp(img)
        bars = detect_all_bars(img)
        print(f"{f}: HP={hp} MP={mp} 自己绿条{len(bars['self'])} "
              f"队友蓝条{len(bars['allies'])} 敌人红条{len(bars['enemies'])}")
        if bars["self"]:
            print(f"  自己绿条: {[(b['x'], b['y'], b['w']) for b in bars['self'][:3]]}")
        if bars["enemies"]:
            print(f"  敌人红条: {[(b['x'], b['y'], b['w']) for b in bars['enemies'][:3]]}")
