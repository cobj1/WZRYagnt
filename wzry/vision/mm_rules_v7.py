# -*- coding: utf-8 -*-
"""Minimap rules det v7 (rewritten, ASCII comments only).

Detects hero rings (self/ally/enemy), minion dots (template match),
fixed towers/monsters/buff points on the minimap. Key params were tuned
for: adb RAW frames (S 60+), PrintWindow frames (S 40+), and corner
decorations (bottom-right blue block / crystal / triangle).

Detector output keys: found/center/radius/size, dots{self,ally,enemy,monster},
minions{ally,enemy}, towers{ally,enemy}, buff.
"""
import json
import math
from pathlib import Path

import cv2
import numpy as np

from wzry.vision import colors   # HSV 判据单一事实来源（v15.3）

ROOT = Path(__file__).resolve().parents[2]

MM_SIZE = 242                 # 兼容保留（16:9 基准边长）
MM_SIZE_FRAC_H = 0.336        # v15.3 实测: 小地图边长 ≈ 0.336 × 帧高（16:9 与 20:9 均成立）
_MM_BOX_CACHE = {}


def _load_mm_box_table():
    """读 configs/calibration_absolute.json 的 "minimap_box"（键 "WxH"，值 [x,y,size]）。"""
    try:
        import json
        p = ROOT / "configs" / "calibration_absolute.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        t = d.get("minimap_box") or {}
        return {k: tuple(int(v) for v in val) for k, val in t.items() if len(val) == 3}
    except Exception:
        return {}


def resolve_mm_box(frame):
    """小地图方形框 -> (x0, y0, size)。v15.3。

    解析顺序: configs 的 `minimap_box["WxH"]` -> 按帧高推算 (x0=0, size=0.336*h) -> MM_SIZE。

    背景: 原实现写死 `MM_SIZE=242` 且隐含原点 `(0, 0)`，那是 **16:9(1280x720, MuMu)** 基准。
    真机 **20:9(1280x576)** 实测小地图在 **x=63 / size=194** —— 用旧框时 20 个已知塔点的
    `_tower_alive` 只过 8/10 与 5/10（录进 states 后 towers 97% 为空）；用对框 10/10 与 10/10。
    标定工具: `scripts/calibrate_minimap_box.py`（以塔点通过数为目标，不依赖人工看图）。
    """
    h, w = frame.shape[:2]
    key = f"{w}x{h}"
    if "table" not in _MM_BOX_CACHE:
        _MM_BOX_CACHE["table"] = _load_mm_box_table()
    t = _MM_BOX_CACHE["table"]
    if key in t:
        x, y, s = t[key]
    else:
        s, x, y = int(round(MM_SIZE_FRAC_H * h)), 0, 0
    s = max(40, min(int(s), w, h))
    x = max(0, min(int(x), w - s))
    y = max(0, min(int(y), h - s))
    return x, y, s


_RING = np.ones((15, 15), np.float32)
_RING[5:10, 5:10] = 0.0
_RING = _RING / _RING.sum()
_CENTER = np.zeros((11, 11), np.float32)
_CENTER[3:8, 3:8] = 1.0
_CENTER = _CENTER / _CENTER.sum()
_BAND_SEL = None


def _band_sel():
    global _BAND_SEL
    if _BAND_SEL is None:
        yy, xx = np.mgrid[-15:16, -15:16]
        dd = np.sqrt(xx ** 2 + yy ** 2)
        _BAND_SEL = (dd >= 8) & (dd <= 14)
    return _BAND_SEL


_SLOPE_SEL = None


def _slope_sels():
    """(外环掩码, 内环掩码)，33×33；预计算一次。

    原来在 `ring_feat` 里每次调用都 mgrid + sqrt + 两次比较（1089 元素 ×3），
    是 detect() 里除 percentile 之外最大的隐性开销。
    """
    global _SLOPE_SEL
    if _SLOPE_SEL is None:
        yy, xx = np.mgrid[-16:17, -16:17]
        dd = np.sqrt(xx ** 2 + yy ** 2)
        _SLOPE_SEL = ((dd >= 11.5) & (dd <= 14.5), (dd >= 6.5) & (dd <= 9.5))
    return _SLOPE_SEL


def _p90(a):
    """与 `np.percentile(a, 90)`（默认 linear 插值）**逐位等价**，但快约 8 倍。

    numpy 的 percentile 走通用 quantile 机制（类型检查/掩码/轴推断），对小数组
    调用开销远大于计算本身。这里只做它实际执行的那一步：
    idx = 0.9*(n-1) → 取 floor/ceil 两个分位值做线性插值。
    """
    n = a.size
    if n == 0:
        return 0.0
    if n == 1:
        return float(a.reshape(-1)[0])
    idx = 0.9 * (n - 1)
    lo = int(idx)
    hi = lo + 1 if lo + 1 < n else lo
    v = np.partition(a, [lo, hi]).reshape(-1)
    frac = idx - lo
    return float(v[lo] + (v[hi] - v[lo]) * frac)


RING_RATIO = {"self": 0.30, "ally": 0.35, "enemy": 0.35}
CENTER_CAP = {"self": 0.15, "ally": 0.15, "enemy": 0.15}

# fixed points (normalized 0-1 of 242px; rebuilt from s01-s12 GT cluster)
BLUE_TOWER_PTS = [(0.032, 0.300), (0.031, 0.535), (0.045, 0.717), (0.082, 0.847),
                  (0.169, 0.783), (0.222, 0.907), (0.450, 0.906), (0.301, 0.661),
                  (0.366, 0.545), (0.695, 0.906)]
RED_TOWER_PTS = [(0.293, 0.051), (0.500, 0.042), (0.711, 0.042), (0.779, 0.168),
                 (0.869, 0.092), (0.653, 0.277), (0.905, 0.223), (0.916, 0.415),
                 (0.907, 0.701), (0.578, 0.390)]
MONSTER_PTS = [(0.590, 0.178), (0.319, 0.130), (0.500, 0.307), (0.171, 0.284),
               (0.187, 0.549), (0.355, 0.742), (0.449, 0.631), (0.625, 0.801),
               (0.778, 0.656), (0.803, 0.508), (0.746, 0.388), (0.065, 0.051),
               (0.930, 0.917), (0.141, 0.432)]
BUFF_PTS = [(0.685, 0.499), (0.504, 0.743), (0.444, 0.198), (0.270, 0.440),
            (0.313, 0.282), (0.626, 0.676)]

_RING_MODEL = None


def _load_ring_model():
    global _RING_MODEL
    if _RING_MODEL is None:
        try:
            import json
            p = ROOT / "configs" / "ring_model.json"
            if p.exists():
                _RING_MODEL = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            _RING_MODEL = False
    return _RING_MODEL


def ring_model_score(mm_bgr, hsv, m, px, py, ring_map=None, cen_map=None):
    """v2.63 学到的环判别分(8维, z-score 逻辑回归)。

    ring_map / cen_map 可传入 detect() 里已算好的 filter2D 结果(核与 _RING/_CENTER
    完全一致, 数值等价); 不传则自行计算(会退化成每个候选项两次全图卷积)。
    """
    md = _load_ring_model()
    if not md:
        return None
    f = ring_feat(mm_bgr, hsv, m, px, py, ring_map=ring_map, cen_map=cen_map)
    xs = (f - np.array(md["mu"])) / np.array(md["sd"])
    return float(xs @ np.array(md["w"]))


def ring_feat(mm_bgr, hsv, m, px, py, ring_map=None, cen_map=None):
    if ring_map is None:
        ring_map = cv2.filter2D(m, -1, _RING)
    if cen_map is None:
        cen_map = cv2.filter2D(m, -1, _CENTER)
    patch_h = hsv[max(0, py - 15):py + 16, max(0, px - 15):px + 16]
    s90, v90 = 0.0, 0.0
    if patch_h.shape[0] == 31 and patch_h.shape[1] == 31:
        sel = _band_sel()
        s90 = _p90(patch_h[..., 1][sel])
        v90 = _p90(patch_h[..., 2][sel])
    g = cv2.cvtColor(mm_bgr[max(0, py - 6):py + 7, max(0, px - 6):px + 7],
                     cv2.COLOR_BGR2GRAY).astype(np.float32)
    edge = 0.0
    if g.shape == (13, 13):
        gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
        edge = float((np.sqrt(gx ** 2 + gy ** 2) > 60).mean())
    mw = m[max(0, py - 16):py + 17, max(0, px - 16):px + 17]
    slope = 0.0
    if mw.shape[0] == 33 and mw.shape[1] == 33:
        sel_out, sel_in = _slope_sels()
        slope = float(mw[sel_out].mean() - mw[sel_in].mean())
    return np.array([float(ring_map[py, px]), float(cen_map[py, px]), edge,
                     s90, v90, slope, float(m.sum()), 1.0], np.float32)


def _edge_frac(mm_bgr, cx, cy, half=6):
    patch = mm_bgr[max(0, cy - half):cy + half + 1, max(0, cx - half):cx + half + 1]
    if patch.shape[0] != 2 * half + 1 or patch.shape[1] != 2 * half + 1:
        if patch.size == 0:
            return None
        patch = cv2.copyMakeBorder(patch, 0, 2 * half + 1 - patch.shape[0],
                                   0, 2 * half + 1 - patch.shape[1],
                                   cv2.BORDER_REPLICATE)
    g = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.sqrt(gx ** 2 + gy ** 2)
    return float((mag > 60).mean())


def _has_avatar(mm_bgr, cx, cy, half=6, thr=0.75):
    ed = _edge_frac(mm_bgr, cx, cy, half)
    return ed is not None and ed > thr


def _ring_slope(m, cx, cy):
    win = m[max(0, cy - 16):cy + 17, max(0, cx - 16):cx + 17]
    if win.shape[0] != 33 or win.shape[1] != 33:
        return -1.0
    sel_out, sel_in = _slope_sels()
    return float(win[sel_out].mean() - win[sel_in].mean())


def _tower_alive(mm_bgr, cx, cy, side, search=7, min_px=3):
    ya, yb = max(0, cy - search), min(mm_bgr.shape[0], cy + search + 1)
    xa, xb = max(0, cx - search), min(mm_bgr.shape[1], cx + search + 1)
    patch = mm_bgr[ya:yb, xa:xb]
    if patch.size == 0:
        return False
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    # v15.3 阈值集中到 colors.py（BLUE_TOWER / RED_TOWER）
    spec = colors.BLUE_TOWER if side == "ally" else colors.RED_TOWER
    m = colors.mask(hsv, spec).astype(np.float32)
    conv = cv2.filter2D(m, -1, np.ones((5, 5), np.float32))
    return float(conv.max()) >= min_px


def _ring_centroid(ring_map, px, py, win=6, thr=0.85):
    y0, x0 = max(0, py - win), max(0, px - win)
    wm = ring_map[y0:py + win + 1, x0:px + win + 1]
    if wm.size == 0:
        return float(px), float(py)
    m = wm >= thr * max(1e-6, float(ring_map[py, px]))
    if not m.any():
        return float(px), float(py)
    ys, xs = np.nonzero(m)
    return int(round(x0 + float(xs.mean()))), int(round(y0 + float(ys.mean())))


def _prune_templates(arr, max_t):
    """k-means-lite clustering by position; keep best-scored per cluster."""
    if len(arr) <= max_t:
        return arr
    out = []
    for t in arr:
        merged = False
        for o in out:
            if abs(o[0] - t[0]) < 8 and abs(o[1] - t[1]) < 8:
                if t[2] > o[2]:
                    o[0], o[1], o[2] = t[0], t[1], t[2]
                merged = True
                break
        if not merged:
            out.append([t[0], t[1], t[2], t[3]])
    return out[:max_t]


_DOT_TEMPLATES = {"ally": None, "enemy": None}
_DOT_THR = 0.70
_DOT_MAX_T = 18


def _prune_templates_legacy(arr, max_t):
    return _prune_templates(arr, max_t)


def _dot_match(mm, templates, thr=0.62, pad=8):
    res = []
    g = cv2.cvtColor(mm, cv2.COLOR_BGR2GRAY).astype(np.float32)
    for (tx, ty, score, tpl_path) in templates:
        tpl = np.load(tpl_path) if isinstance(tpl_path, str) else tpl_path
        if tpl.ndim == 3:
            tpl = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY)
        tpl = tpl.astype(np.float32)
        th, tw = tpl.shape[:2]
        if mm.shape[0] < th or mm.shape[1] < tw:
            continue
        rc = cv2.matchTemplate(g, tpl, cv2.TM_CCOEFF_NORMED)
        while True:
            _, mv, _, mloc = cv2.minMaxLoc(rc)
            if mv < thr:
                break
            bx, by = mloc
            cx, cy = bx + tw // 2, by + th // 2
            res.append((cx, cy))
            rc[max(0, by - 3):by + th + 3, max(0, bx - 3):bx + tw + 3] = -1
    # NMS 6px
    res = _nms(res, 6)
    return res


def _nms(pts, d=6):
    out = []
    for p in sorted(pts, key=lambda q: -q[0]):
        if all(math.hypot(p[0] - q[0], p[1] - q[1]) > d for q in out):
            out.append(p)
    return out


def _load_dot_templates():
    global _DOT_TEMPLATES
    if _DOT_TEMPLATES["ally"] is not None:
        return
    base = ROOT / "temp" / "mm_dot_templates"
    ally = []
    enemy = []
    for f in sorted(base.glob("ally_*.npy")):
        tpl = np.load(str(f))
        ally.append((0, 0, 0.9, tpl))
    for f in sorted(base.glob("enemy_*.npy")):
        tpl = np.load(str(f))
        enemy.append((0, 0, 0.9, tpl))
    _DOT_TEMPLATES["ally"] = _prune_templates(ally, _DOT_MAX_T)
    _DOT_TEMPLATES["enemy"] = _prune_templates(enemy, _DOT_MAX_T)


# ---- v15.3 野怪/BUFF 标记的"证据门控" ----
# 为什么需要：原实现把 MONSTER_PTS/BUFF_PTS 无条件全量输出，于是 minimap.dots.yellow
#   每帧恒为同样 14 个点（实测与常量表逐值相同）——是常量，不是观测。
# 判据：点位附近存在标记色像素（黄 = 野怪/BUFF 图标；品红/紫 = 河道之灵等）。
MM_MARKER_GATE = True        # False -> 退回旧行为（无条件全量输出）
MM_MARKER_R_FRAC = 0.033     # 证据窗口半径 = 框边长 × 该比例（标记图标随地图等比缩放）
MM_MARKER_MIN_PX = 5         # 窗口内标记色像素数下限

# ---- v15.5 BUFF 输出模式 ----
# v15.4 真机复测（1180 帧 / 会话 20261017_162901_384146）证明"颜色证据门控"对 BUFF **无效**：
#   6 个 BUFF_PTS 里有 5 个在**每一帧都同时**满足证据（点位集合 1180 帧只有 1 种），
#   剩下 1 个永远不满足。放大取像素后看清原因：BUFF 点落在**常驻的橙色营地图标**上
#   （野区图标描边，永久绘制），而野怪点是小黄点（只在单位存活时出现）。
#   => 颜色在这两个点上无法区分"营地图标"与"在野 BUFF"，输出只会是换了数值的常量。
# 因此在没有新判据（如"图标中心是否有倒计时数字"）之前，默认**不输出 BUFF**，
#   并置 `buff_supported=False` 让下游能区分"未知"与"没有"（当前生产路径无人读 buff）。
#   "gated" / "fixed" 仅保留作对照实验，不要在生产启用。
MM_BUFF_MODE = "unavailable"       # "unavailable" | "gated" | "fixed"

# ---- v15.6 野怪输出模式 ----
# v15.5 之后按点位复核（会话 20261017_164149_269145，305 帧），"野怪门控"同样是假的：
#   M0/M1/M2/M3/M5/M6/M8/M11/M13 共 9 个点证据 **100%**（常驻营地图标，与 BUFF 同款）；
#   M7/M9/M10/M12 共 4 个点 **0%**（空坐标）；只有 M4(0.187,0.549) 是 53%，
#   而那 53% 里有 85% 同时有英雄点落在 0.06 内 -> M4 是**英雄头像/光圈**，不是野怪。
#   即 dots.yellow 的"变化"{9,10} 全部来自 M4 一个点的闪烁，其余 13 点是常量。
#   门控只是给常量换皮，且更隐蔽（总数变化把逐点的恒定遮住了）。
# 结论：与 BUFF 同源同因 —— 颜色无法区分"常驻图标"与"在野单位"。
#   默认不输出 monster，并置 `monster_supported=False`。生产路径同样无人消费
#   （m2 的 `_mmp_yellow` 赋值后未使用；optimizer/laya_scorer 不读）。
#   营地**位置**另行用 `configs/jungle_points.json`（静态结构，由 calibrate_jungle_points.py
#   从真机帧自动聚类得到），经 `v7.camps` 输出 —— 注意它只表示"这里有营地"，不表示"怪活着"。
MM_MONSTER_MODE = "unavailable"    # "unavailable" | "gated" | "fixed"

_JUNGLE_CACHE = None

# ---- v15.7 小地图上的"头像停靠带"（v16.0 已停用）----
# v15.7 背景：旧环判据下，左边缘停靠的队友头像（红/蓝圆环 + 头像）被误判成敌英。
# v16.0 实测(会话 20261018_144334_007412, 776 帧)：这些误报的像素是**头像皮肤/发色**
#   （暗红，亮红占比 brR≤0.03），而真敌英红圈是亮红(brR≥0.21) —— 颜色门已完整接管此职责；
#   同时该带里确有**真敌人**（红圈头像，见 brR 门后的 71/27/23 帧簇，敌英压到己方基地/蓝区），
#   带开启反而丢召回。故停用。保留几何定义以备回滚对照（rect = x0,y0,x1,y1 归一化）。
MM_UI_BAND_ON = False
MM_UI_BAND = ((0.00, 0.50, 0.26, 1.00),)

# ---- v15.8 敌英检测的三个闸门（A/B 用；默认值 = 改动前行为）----
# 背景：用 YOLO 屏内敌英框做"必要条件"测召回代理，实测仅 4%(162901) / 11%(165546) ——
#   屏幕上明确有敌人时，小地图 90%+ 抓不到。下面三项是候选原因，做成开关以便逐个 A/B。
MM_ENEMY_NEAR_BAD_TOWERS = True    # 距塔点 17px 内剔除（塔点并集占小地图 43.1% 面积）
MM_ENEMY_NEAR_BAD_MONSTER = True   # 距野点 17px 内剔除（野怪点占 32.9%）
MM_ENEMY_AVATAR_THR = 0.62         # _has_avatar 相似度门

# ---- v15.8 敌英红点：显式声明"不可用" ----
# 60 帧连续帧的候选漏斗（temp/stream_0002.mkv）：
#   ring 候选 1073 -> _has_avatar 拒 844 -> UI停靠带 拒 218 -> 余 11 -> ring_model 全拒 -> 0
# 即这条路径要求"敌英点长得像头像"，而真机小地图上的敌人并非头像形态；
# 之前唯一的"检出"全是左边缘停靠的头像（精确率 0%）。同时用 YOLO 屏内敌英做必要条件，
# 召回代理仅 4%(162901) / 11%(165546)。
# 结论：当前判据在该画风下既无精度也无召回。输出置空并声明 red_supported=False，
# 让下游能区分"未知"与"这里没有敌人"——不要把空集当成"安全"。
# 要真正恢复，需要在**正常对局**里采一批带真值的小地图帧重做判据（不是调参）。
# v15.9/v16.0 更新：blob 判据 + 亮红门在真机对局（776 帧）上重做后已恢复可用，
#   详见下方 MM_ENEMY_MODE 块；此处保留历史漏斗作为"旧环判据不可用"的存档。
MM_RED_SUPPORTED = True

# ---- v15.9 敌英改用「红圈大盘」判据（真机对局验证）----
# 真机（10-18，1792 帧真实对局）确认：小地图上的敌人 = **红圈包着头像的大圆盘**
#   （半径 ~10px@190，RED_MM 掩码面积约 300~1500 px），位置随移动变化。
# 旧的环判据要求"红环 + 中心为空"（cen_map < 0.15），而头像中心本来就红
#   （实测真值点 cen_map=0.76）-> **每个真敌人都被拒**。A/B 调参最高只能到
#   精确 40% / 召回 33%（CAP=0.95 + 关 near_bad），漏检发生在更上游的环候选阶段。
# 直接按"红圈大块"取：4 帧目视全部精确命中敌英头像，无塔/装饰误检（temp/_blob_check.png）。
#
# ---- v16.0 亮红/橙 双色门（会话 20261018_144334_007412 776 帧全量复检）----
# 纯"面积大块"仍有三类系统性误报，全部由**逐像素颜色**分开（temp/_brr_scan/）：
#   真敌英红圈(brR≥0.21) vs ①队友/自己头像皮肤·发色(暗红 brR≤0.03)
#   ②主宰图标红色描边(brR≤0.19 但橙 orgf≥0.35) ③暴君金环·金色特效(orgf 0.84)
# 门限 brR≥0.15 且 orgf<0.35：332 个通过 comp 逐簇目检 28 簇全部是真红圈，0 FP；
#   两门余量：真圈 orgf≤0.21 / 主宰 orgf≥0.35；真圈 brR≥0.21 / 主宰 brR≤0.19。
# 面积下限按实测下调：红塔图标(含顶边裁剪件) ≤148px@190，细红圈(敌压基地时)最低 210px，
#   170@190 落在两簇之间的空隙上（[150,209] 全量扫描为空）。
# 同时停用 UI 停靠带（见 MM_UI_BAND_ON），该带内的"暗红头像"误报由 brR 门接管。
MM_ENEMY_MODE = "blob"          # "blob"(新) | "ring"(旧环判据，留作对照)
# 面积阈值以 **190px 小地图**为基准标定（红圈圆盘 ~210~1600px，红塔图标 ≤148）。
# v16.0: 换算成面积占比，随小地图边长平方缩放 —— 否则换分辨率（20:9 / 平板 / 缩放设置）
#   阈值就全错，等于又一次"看起来有位、其实是废值"。
MM_ENEMY_BLOB_REF = 190
MM_ENEMY_BLOB_MIN_FRAC = 170.0 / (190.0 ** 2)
MM_ENEMY_BLOB_MAX_FRAC = 1600.0 / (190.0 ** 2)
# 亮红像素占比下限（H<=8|H>=172 且 S>=120 且 V>=110 的像素比例）
MM_ENEMY_BRIGHT_MIN = 0.15
MM_ENEMY_BRIGHT_S = 120
MM_ENEMY_BRIGHT_V = 110
# 橙色占比上限（H 12~35；主宰图标红描边 orgf≥0.35，真圈≤0.21）
MM_ENEMY_ORG_MAX = 0.35
# 圆盘半径上限：112px@190（=1.18*地图半径），随小地图边长线性缩放
MM_ENEMY_R_FRAC = 112.0 / 190.0

# ---- v16.1 蓝环包裹门（MuMu 画风的队友头像误报）----
# 背景（会话 20261015_122552_016278，63 帧 1280x720 / 小地图 242px，MuMu）：
#   该画风下部分队友头像的圆盘底是**深红/绛红人像底**，亮红占比 brR 0.16~0.24
#   落在亮红门内（33 个 comp，a=276~431@242），被当成敌英红圈 -> FP。
# 判据：真敌英红圈的外围是地图背景/邻位头像边缘；队友头像被**自己的蓝圈**360° 包裹。
#   统计 comp 膨胀环带（k=round(2*s/190)，dev(190)k=2 / MuMu(242)k=3）内亮蓝像素
#   (H85-135,S>=90,V>=140) 的**角度覆盖**（36 个 10° 扇区）：
#   真圈 dev 323 comps max 67%；真圈 MuMu 80 comps max 78%；队友头像 FP 81~100%。
#   阈值 80%（含）-> MuMu 33 个 FP 全灭，dev/MuMu 真圈全保留（余量 13/2 个百分点）。
# 例外：大面积合并团块（>=280px@190）可能叠着真敌，不做蓝环否决
#   （MuMu a=1199@242 真敌与队友贴叠，cov=81%，被此例外救回；FP 最大仅 431@242）。
MM_ENEMY_BLUE_COV_MAX = 0.80
MM_ENEMY_BLUE_H = (85, 135)
MM_ENEMY_BLUE_S = 90
MM_ENEMY_BLUE_V = 140
MM_ENEMY_BLUE_K_FRAC = 2.0 / 190.0
MM_ENEMY_BLUE_EXEMPT_FRAC = 280.0 / (190.0 ** 2)


def _enemy_blobs(hsv, mw, mh):
    """红圈大盘 = 敌英雄（亮红占比 brR 门 + 橙占比上限门 + 蓝环包裹门）。返回 [(px, py, conf), ...]。"""
    m = colors.mask(hsv, colors.RED_MM).astype(np.uint8)
    n, lab, st, cent = cv2.connectedComponentsWithStats(m, 8)
    # 面积/半径阈值随当前小地图边长缩放（按 190px 标定）
    _a2 = float(mw) * float(mw)
    a_min = MM_ENEMY_BLOB_MIN_FRAC * _a2
    a_max = MM_ENEMY_BLOB_MAX_FRAC * _a2
    r_max = MM_ENEMY_R_FRAC * float(mw)
    # 逐像素双色统计：一次 bincount 得到每个 comp 的亮红/橙计数（避免逐 comp 全图扫描）
    nz = np.flatnonzero(m)
    if nz.size == 0:
        return []
    Hf = hsv[:, :, 0].ravel()[nz].astype(np.int32)
    Sf = hsv[:, :, 1].ravel()[nz].astype(np.int32)
    Vf = hsv[:, :, 2].ravel()[nz].astype(np.int32)
    cf = lab.ravel()[nz]
    red = ((Hf <= 8) | (Hf >= 172)) & (Sf >= MM_ENEMY_BRIGHT_S) & (Vf >= MM_ENEMY_BRIGHT_V)
    org = (Hf >= 12) & (Hf <= 35)
    red_c = np.bincount(cf, weights=red.astype(np.float64), minlength=n)
    org_c = np.bincount(cf, weights=org.astype(np.float64), minlength=n)
    # v16.1 蓝环包裹门预计算（惰性）：亮蓝掩码 + 膨胀核 + 免检面积
    blue = None
    _kk = max(2, int(round(MM_ENEMY_BLUE_K_FRAC * float(mw))))
    _bker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * _kk + 1, 2 * _kk + 1))
    _exempt_a = MM_ENEMY_BLUE_EXEMPT_FRAC * _a2
    out = []
    for i in range(1, n):
        a = int(st[i, cv2.CC_STAT_AREA])
        if not (a_min <= a <= a_max):
            continue
        brR = red_c[i] / max(1.0, float(a))
        if brR < MM_ENEMY_BRIGHT_MIN:
            continue          # 暗红 = 头像皮肤/发色，不是红圈
        if org_c[i] / max(1.0, float(a)) >= MM_ENEMY_ORG_MAX:
            continue          # 橙主导 = 主宰红描边/暴君金环等装饰
        px, py = int(cent[i][0]), int(cent[i][1])
        if not (5 <= px <= mw - 5 and 5 <= py <= mh - 5):
            continue
        if math.hypot(px - mw / 2.0, py - mh / 2.0) > r_max:
            continue          # 圆盘外 = 角饰/塔饰
        if _in_ui_band(px, py, mw, mh):
            continue          # 头像停靠带（v16.0 默认关闭）
        # v16.1 蓝环包裹门：环带内亮蓝像素角度覆盖 >=80% -> 队友头像（自己的蓝圈包裹）
        if a < _exempt_a:
            if blue is None:
                Hc, Sc, Vc = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
                blue = ((Hc >= MM_ENEMY_BLUE_H[0]) & (Hc <= MM_ENEMY_BLUE_H[1]) &
                        (Sc >= MM_ENEMY_BLUE_S) & (Vc >= MM_ENEMY_BLUE_V))
            _x, _y = int(st[i, cv2.CC_STAT_LEFT]), int(st[i, cv2.CC_STAT_TOP])
            _w, _h = int(st[i, cv2.CC_STAT_WIDTH]), int(st[i, cv2.CC_STAT_HEIGHT])
            _pad = _kk + 2
            ya, yb = max(0, _y - _pad), min(mh, _y + _h + _pad)
            xa, xb = max(0, _x - _pad), min(mw, _x + _w + _pad)
            _comp = (lab[ya:yb, xa:xb] == i)
            _band = (cv2.dilate(_comp.astype(np.uint8), _bker) > 0) & (~_comp)
            bys, bxs = np.nonzero(_band & blue[ya:yb, xa:xb])
            if bys.size:
                ang = np.arctan2(bys - (cent[i][1] - ya), bxs - (cent[i][0] - xa))
                nb = np.unique(((ang + np.pi) / (2.0 * np.pi) * 36.0).astype(np.int32) % 36).size
                if nb / 36.0 >= MM_ENEMY_BLUE_COV_MAX:
                    continue
        out.append((px, py, round(min(1.0, 0.5 + brR), 2)))
    return out


def _in_ui_band(px, py, mw, mh):
    if not MM_UI_BAND_ON:
        return False
    nx, ny = px / max(1.0, float(mw)), py / max(1.0, float(mh))
    return any(x0 <= nx <= x1 and y0 <= ny <= y1 for (x0, y0, x1, y1) in MM_UI_BAND)


def jungle_pts():
    """标定后的野区营地点位 [(x, y, big), ...]（归一化到小地图方形框）。

    来源 `configs/jungle_points.json`，由 `scripts/calibrate_jungle_points.py` 从真机帧
    自动聚类得到（16 个点，已目视核对；旧 MONSTER_PTS/BUFF_PTS 只有 5/20 能对上真实图标）。
    注意：**只表示"此处有营地"，不表示野怪存活** —— 颜色无法区分常驻图标与在野单位。
    已知缺口：暴君/主宰两个大图标不在表内（灰色主宰不是金色；橙色暴君面积超出过滤窗口）。
    """
    global _JUNGLE_CACHE
    if _JUNGLE_CACHE is None:
        try:
            d = json.loads((ROOT / "configs" / "jungle_points.json").read_text(encoding="utf-8"))
            _JUNGLE_CACHE = [(float(p["n"][0]), float(p["n"][1]), bool(p.get("big")))
                             for p in d["points"]]
        except Exception:
            _JUNGLE_CACHE = [(x, y, False) for x, y in MONSTER_PTS]
    return _JUNGLE_CACHE


def _marker_evidence(hsv, px, py, size, radius=None, min_px=MM_MARKER_MIN_PX):
    """点位附近是否有野怪/BUFF 标记色证据（hsv 为小地图裁剪的 HSV）。

    黄 H15-38/S>=80/V>=90（野怪小圆点、BUFF/暴君主宰图标）
    品红 H140-170/S>=70/V>=80（河道之灵等特殊中立单位）
    """
    r = int(radius if radius is not None else max(3, round(MM_MARKER_R_FRAC * size)))
    y0, y1 = max(0, py - r), min(hsv.shape[0], py + r + 1)
    x0, x1 = max(0, px - r), min(hsv.shape[1], px + r + 1)
    if y1 <= y0 or x1 <= x0:
        return False
    p = hsv[y0:y1, x0:x1]
    # v15.3 阈值集中到 colors.py（YELLOW_MARKER / MAGENTA_MARKER）
    return (colors.count(p, colors.YELLOW_MARKER)
            + colors.count(p, colors.MAGENTA_MARKER)) >= min_px


class MMDetectorV7:
    def detect(self, frame, mm_box=None):
        h, w = frame.shape[:2]
        if mm_box is None:
            # v15.3: 框随分辨率解析（原为写死 0,0 + MM_SIZE=242，仅在 16:9 正确）
            x0, y0, mw = resolve_mm_box(frame)
            mh = mw
        else:
            x0, y0, x1, y1 = mm_box
            mw, mh = x1 - x0, y1 - y0
        mm = frame[y0:y0 + mh, x0:x0 + mw]
        if mm.size == 0 or mm.shape[0] < 30 or mm.shape[1] < 30:
            return self._empty(mw)
        hsv = cv2.cvtColor(mm, cv2.COLOR_BGR2HSV)
        H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]   # v15.2: uint8 视图（阈值均 <256）

        # ring masks (S>40 for PrintWindow; H windows tuned)
        # v15.3 阈值集中到 wzry/vision/colors.py（GREEN_MM / BLUE_MM / RED_MM）
        color_masks = {
            "self": colors.mask(hsv, colors.GREEN_MM).astype(np.float32),
            "ally": colors.mask(hsv, colors.BLUE_MM).astype(np.float32),
            "enemy": colors.mask(hsv, colors.RED_MM).astype(np.float32),
        }
        res = {}
        for name, m in color_masks.items():
            ring_map = cv2.filter2D(m, -1, _RING)
            cen_map = cv2.filter2D(m, -1, _CENTER)
            out_pts = []
            for ptag, thr_ring, cen_ok in (("A", RING_RATIO[name], True),
                                           ("B", 0.62, False)):
                if ptag == "A":
                    hits = ((ring_map > RING_RATIO[name]) & (cen_map < CENTER_CAP[name])).astype(np.uint8) * 255
                else:
                    hits = (ring_map > 0.62).astype(np.uint8) * 255
                n, lab, st, cent = cv2.connectedComponentsWithStats(hits, 8)
                for i in range(1, n):
                    cx, cy = int(cent[i][0]), int(cent[i][1])
                    if not (5 <= cx <= mw - 5 and 5 <= cy <= mh - 5):
                        continue
                    win = ring_map[max(0, cy - 6):cy + 7, max(0, cx - 6):cx + 7] \
                        - cen_map[max(0, cy - 6):cy + 7, max(0, cx - 6):cx + 7] * 2.0
                    if win.size == 0 or win.shape[0] < 3 or win.shape[1] < 3:
                        continue
                    dy, dx = np.unravel_index(np.argmax(win), win.shape)
                    px, py = max(0, cx - 6) + dx, max(0, cy - 6) + dy
                    if ring_map[py, px] <= thr_ring:
                        continue
                    if cen_ok and cen_map[py, px] >= CENTER_CAP[name]:
                        continue
                    if ptag == "B":
                        px, py = _ring_centroid(ring_map, px, py)   # B 通道质心精化
                    # v2.63 学到的环判别: 模型分过低 -> 剔除(替代启发式倾向)
                    #   v14.3 复用上面已算好的 ring_map/cen_map(核相同, 数值等价),
                    #   免掉每个候选又各做一次全图卷积 -> detect() 快 ~5x
                    _mscore = ring_model_score(mm, hsv, m, px, py,
                                               ring_map=ring_map, cen_map=cen_map)
                    _hi = (_mscore is not None and
                           _mscore >= float(_RING_MODEL["b"]))
                    if name == "enemy":
                        # v2.77 四道硬闸防误检(野怪/红塔/场景红->不是敌英雄)
                        if not _has_avatar(mm, px, py, thr=MM_ENEMY_AVATAR_THR):
                            continue
                        patch = mm[max(0, py - 3):py + 4, max(0, px - 3):px + 4]
                        if patch.shape[0] == 7 and patch.shape[1] == 7:
                            hsv_p = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
                            hh_, ss_ = hsv_p[..., 0].astype(int), hsv_p[..., 1].astype(int)
                            pure_red = (((hh_ <= 15) | (hh_ >= 170)) & (ss_ > 90)).sum()
                            if pure_red / 49.0 >= 0.55:
                                continue
                        # 固定塔点/野怪点排除(这些是塔图标/野怪, 非敌英雄):
                        # v2.87 半径 13->17 (塔图标边缘环检出偏移13.9px 曾逃逸 -> 幽灵红点)
                        # v15.8: 拆成两个开关做 A/B —— 该并集实测覆盖 70.1% 的小地图面积，
                        #   而英雄本就常年贴着塔活动，可能是召回只有 ~4% 的主因。
                        near_bad = False
                        if MM_ENEMY_NEAR_BAD_TOWERS:
                            for (fx_, fy_) in BLUE_TOWER_PTS + RED_TOWER_PTS:
                                if math.hypot(px - fx_ * mw, py - fy_ * mh) < 17:
                                    near_bad = True
                                    break
                        if not near_bad and MM_ENEMY_NEAR_BAD_MONSTER:
                            for (fx_, fy_) in MONSTER_PTS:
                                if math.hypot(px - fx_ * mw, py - fy_ * mh) < 17:
                                    near_bad = True
                                    break
                        if near_bad:
                            continue
                        # v2.87 圆盘边界: 真英雄头像在小地图圆盘内(r<=112), 盘外=红塔S图标/角饰
                        # (实测真头像最小r=106@(60,26), 幽灵塔饰r=118-132 -> 112分离)
                        if math.hypot(px - mw / 2.0, py - mh / 2.0) > 112:
                            continue
                        # v15.7 排除"头像停靠带"：真机上队友/敌方头像会**停靠在小地图左边缘**，
                        #   带红圈的头像会通过 _has_avatar 与环检测 -> 被当成敌英雄。
                        #   实测（会话 20261017_162901/165546，47 帧）：45 个"敌点"全部落在
                        #   仅 2 个位置 (0.184,0.663) x26 与 (0.121,0.616) x19，**离散度 0.0000**，
                        #   即完全不动 = UI 叠层，不是世界坐标。精确率 0%。
                        if _in_ui_band(px, py, mw, mh):
                            continue
                    if name in ("self", "ally") and px > 216 and py > 216:
                        continue   # corner decor (blue block/crystal/triangle)
                    if name == "self":
                        patch = hsv[max(0, py - 15):py + 16, max(0, px - 15):px + 16]
                        pv_ok = False
                        if patch.shape[0] == 31 and patch.shape[1] == 31:
                            sel = _band_sel()
                            s90 = _p90(patch[..., 1][sel])
                            v90 = _p90(patch[..., 2][sel])
                            pv_ok = (s90 >= 145 and v90 >= 145)
                        elif patch.size > 0:
                            pad = cv2.copyMakeBorder(patch, 0, 31 - patch.shape[0],
                                                     0, 31 - patch.shape[1],
                                                     cv2.BORDER_REPLICATE)
                            s90 = _p90(pad[..., 1][_band_sel()])
                            v90 = _p90(pad[..., 2][_band_sel()])
                            pv_ok = (s90 >= 145 and v90 >= 145)
                        if not _hi and not pv_ok and ring_map[py, px] <= 0.70:
                            continue
                        if not _hi and not _has_avatar(mm, px, py, thr=0.55):
                            continue
                    elif name == "ally":
                        patch2 = hsv[max(0, py - 15):py + 16, max(0, px - 15):px + 16]
                        if patch2.shape[0] != 31 or patch2.shape[1] != 31:
                            continue
                        s90 = _p90(patch2[..., 1][_band_sel()])
                        avail = _edge_frac(mm, px, py)
                        if avail is None or avail <= 0.62 or s90 <= 140:
                            continue
                        if _ring_slope(m, px, py) < 0.05:
                            continue
                    if any(math.hypot(px - op[0], py - op[1]) < 8 for op in out_pts):
                        continue   # 两通道重复候选
                    out_pts.append((px, py, float(ring_map[py, px])))
            # v2.77 红点数量帽: 5v5 至多 5 个(防洪水误检)
            if name == "enemy":
                out_pts = out_pts[:5]
            res[name] = out_pts

        # v15.9 敌英：新版判据直接取"红圈大盘"，覆盖上面环路径的结果。
        #   环路径在本画风下把每个真敌人都拒了（见 MM_ENEMY_MODE 说明），仅留作对照。
        if MM_ENEMY_MODE == "blob":
            res["enemy"] = _enemy_blobs(hsv, mw, mh)[:5]

        # v2.78 红环互斥: 与队友/自己环同点(<9px)的红点=队友图标红色纹理 -> 删
        # v2.94 9->15px: 队友蓝圈头像的红色纹理环实测偏移 13px, 旧线漏删 -> 红点团假敌英
        # v15.9: 仅在旧环路径启用。blob 判据下这条会**团灭真敌人** ——
        #   团战时敌人几乎总在队友 15px 内（实测 117 个有敌帧只剩 41 帧检出）。
        if MM_ENEMY_MODE == "ring" and res.get("enemy"):
            _ally_pts = [p for pp in (res.get("ally", []) + res.get("self", []))
                         for p in (pp[:2],)]
            if _ally_pts:
                kp = []
                for e in res["enemy"]:
                    if any(math.hypot(e[0] - a[0], e[1] - a[1]) < 15 for a in _ally_pts):
                        continue
                    kp.append(e)
                res["enemy"] = kp

        # minion dots (template matching)
        _load_dot_templates()
        minions_ally = _dot_match(mm, _DOT_TEMPLATES["ally"] or [], thr=_DOT_THR)
        minions_enemy = _dot_match(mm, _DOT_TEMPLATES["enemy"] or [], thr=_DOT_THR)

        # fixed points
        towers_ally, towers_enemy = [], []
        for x, y in BLUE_TOWER_PTS:
            px, py = int(x * mw), int(y * mh)
            if _tower_alive(mm, px, py, "ally"):
                towers_ally.append((px, py))
        for x, y in RED_TOWER_PTS:
            px, py = int(x * mw), int(y * mh)
            if _tower_alive(mm, px, py, "enemy"):
                towers_enemy.append((px, py))
        # v15.3 野怪/BUFF：由"无条件常量"改为"有证据才输出"；
        # v15.6: 逐点复核证明**野怪这一路同样是假的**（9 点恒亮=常驻图标、4 点恒灭=空坐标、
        #   1 点=英雄头像），详见 MM_MONSTER_MODE 说明。默认不输出，"gated"/"fixed" 仅作对照。
        monster, buff = [], []
        if MM_MONSTER_MODE != "unavailable":
            # v15.6: "gated" 用**标定后**的点位（jungle_pts），旧 MONSTER_PTS 只有 5/14 对得上
            _pts = jungle_pts() if MM_MONSTER_MODE == "gated" else MONSTER_PTS
            for _p in _pts:
                x, y = _p[0], _p[1]
                px, py = int(x * mw), int(y * mh)
                if MM_MONSTER_MODE == "fixed" or _marker_evidence(hsv, px, py, mw):
                    monster.append((px, py))
        # v15.5: BUFF 默认不输出 —— 颜色证据在 BUFF 点上是常驻营地图标，无法区分在野状态。
        #   详见 MM_BUFF_MODE 处的说明（含真机实测数据）。"gated"/"fixed" 仅作对照实验。
        if MM_BUFF_MODE != "unavailable":
            for x, y in BUFF_PTS:
                px, py = int(x * mw), int(y * mh)
                if MM_BUFF_MODE == "fixed" or _marker_evidence(hsv, px, py, mw):
                    buff.append((px, py))
        in_match_evidence = bool(res["self"] or res["ally"] or res["enemy"]
                                 or minions_ally or minions_enemy)
        # v3.3 用户铁律: 5v5 上限 = 敌英最多5, 队友最多4(自己1) -> 按置信度截断
        _sort_k = lambda pts: sorted(pts, key=lambda q: -float(q[2]) if len(q) > 2 else 0.0)
        self_r = _sort_k(res["self"])[:1]
        ally_r = _sort_k(res["ally"])[:4]
        enemy_r = _sort_k(res["enemy"])[:5]
        if not MM_RED_SUPPORTED:
            # v15.8: 判据在该画风下无精度无召回（见 MM_RED_SUPPORTED 处漏斗数据）——
            # 宁可输出空 + 显式标记 unsupported，也不输出"看起来像敌人"的 UI 头像。
            enemy_r = []

        def norm(pts):
            return [{"n": [round(p[0] / mw, 4), round(p[1] / mh, 4)],
                     "conf": round(float(p[2]), 2) if len(p) > 2 else 0.7,
                     "src": "v7rw"} for p in pts]

        return {"found": in_match_evidence,
                "center": ((x0 + mw / 2, y0 + mh / 2)), "radius": mw / 2,
                "size": mw,
                "dots": {"self": norm(self_r), "ally": norm(ally_r),
                         "enemy": norm(enemy_r),
                         "monster": [{"n": [round(p[0] / mw, 4), round(p[1] / mh, 4)],
                                      "conf": 0.8, "src": "fixed"} for p in monster]},
                "minions": {"ally": norm(minions_ally), "enemy": norm(minions_enemy)},
                "towers": {"ally": norm(towers_ally), "enemy": norm(towers_enemy)},
                "buff": [{"n": [round(p[0] / mw, 4), round(p[1] / mh, 4)],
                          "conf": 0.8, "src": "fixed"} for p in buff],
                # v15.5/15.6: False = 该字段"未知"而非"没有"（下游据此区分，勿当空集用）
                "buff_supported": MM_BUFF_MODE != "unavailable",
                "monster_supported": MM_MONSTER_MODE != "unavailable",
                "red_supported": bool(MM_RED_SUPPORTED)}

    def _empty(self, size=None):
        return {"found": False, "center": None, "radius": None,
                "size": int(size) if size else MM_SIZE,
                "dots": {"self": [], "ally": [], "enemy": [], "monster": []},
                "minions": {"ally": [], "enemy": []},
                "towers": {"ally": [], "enemy": []}, "buff": [],
                "buff_supported": MM_BUFF_MODE != "unavailable",
                "monster_supported": MM_MONSTER_MODE != "unavailable",
                "red_supported": bool(MM_RED_SUPPORTED)}
