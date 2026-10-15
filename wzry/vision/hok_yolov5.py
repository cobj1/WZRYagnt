# -*- coding: utf-8 -*-
"""王者荣耀 YOLOv5 ONNX 检测器封装 (P0 感知重建: 替代丢失的 zhongkui_r3)。

模型来源: GitHub `ironmanhcx/-YOLO-` (YOLOv5s, 640, 300epoch, best mAP@0.5≈0.689)。
权重 `gh_hok_yolov5s_best.pt` 是 YOLOv5 格式, ultralytics 8.x 无法加载, 已由
`scripts/export_detector_onnx.py` 导出为 `runs/detect/hok_yolov5/hok_yolov5s.onnx`。
本模块只依赖 onnxruntime + cv2 + numpy, 不再需要 yolov5 仓库。

模型 8 个"通用类"(不含阵营): hero / soldier / Buff / grass / wild_monster /
dragon / tower / crystal。映射到本项目 schema(见 configs/classes.json):
  hero     -> enemy_hero    / ally_hero      (阵营分流)
  soldier  -> enemy_minion  / ally_minion    (阵营分流)
  tower    -> enemy_turret  / ally_turret    (阵营分流)
  crystal  -> enemy_crystal / ally_crystal   (阵营分流)
  wild_monster / dragon / Buff -> neutral_monster
  grass    -> grass (额外保留; 供"不蹲草"约束用)
阵营分流判据与 `wzry/vision/detector.py:camp_correct` 一致: 框内蓝/红像素占比。

自测(在图上跑并画框落盘):
  venv\\Scripts\\python.exe -X utf8 -m wzry.vision.hok_yolov5 <image> [--camp blue]
"""
import argparse
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
DEF_ONNX = ROOT / "runs" / "detect" / "hok_yolov5" / "hok_yolov5s.onnx"
DEF_CLS = ROOT / "runs" / "detect" / "hok_yolov5" / "hok_yolov5s_classes.json"

# 通用类 -> 本项目 schema 的敌/我配对 (偶数=敌, 与 detector.py PAIR_ORDER 语义一致)
GENERIC_PAIRS = {
    "hero": ("enemy_hero", "ally_hero"),
    "soldier": ("enemy_minion", "ally_minion"),
    "tower": ("enemy_turret", "ally_turret"),
    "crystal": ("enemy_crystal", "ally_crystal"),
}
NEUTRAL = {"wild_monster": "neutral_monster", "dragon": "dragon",
           "Buff": "jungle_buff"}
# v15.0 资源点拆分: 原来三类被合并成 neutral_monster, 抹掉了"龙/红蓝BUFF"的区分,
#   而模型本来就有这三类 -> 直接拆开, 供记忆层做"资源点刷新/争夺"推理。
# 阵营分流: 这些通用类用框内蓝/红像素占比判定(与 detector.camp_correct 同判据)。
# hero 例外 (v14.2 B.2): 英雄框内常混蓝饰件/技能特效, 蓝/红占比判不出阵营 —— 实测本机
#   全部英雄框都落 camp_src=default, 导致下游"幻影敌人"过滤把英雄全当不可信 -> 技能不释放。
#   改判据: 英雄阵营 = 框上方**血条颜色**(绿=友/红=敌), 颜色阈值沿用
#   scripts/train/camp_autolabel.py(bar_colors); 血条位置需逐窄带扫描(见 _hero_bar_probe)。
PAIR_USES_COLOR = {"soldier", "tower", "crystal", "hero"}
CAMP_EPS = 0.02
HERO_BAR_MIN_CONF = 0.30       # 血条主色占比与其它色的差阈值(camp_autolabel --min-conf)
HERO_BAR_MIN_FRAC = 0.75       # 绿/红血条覆盖率下限: 真血条近乎满覆盖, 草地/植被 ~0.4
HERO_BAR_BLUE_MIN_FRAC = 0.55  # 蓝(队友)条覆盖率下限: 实测队友蓝条只有 0.61~0.65, 低于绿/红
HERO_BAR_BLUE_NEAR = 0.45      # 蓝条判友军仅限"交战圈"内(距屏幕中心归一化距离), 圈外保守留 default
HERO_BAR_W_FRAC = 0.70         # 扫描带宽度(框宽比例)
HERO_BAR_SCAN_PX0 = 3          # 扫描起点: 框顶上方 3px (720p 基准, 按帧高缩放)
HERO_BAR_SCAN_PX1 = 40         # 扫描终点: 框顶上方 40px (720p 基准)
HERO_BAR_BAND_PX = 4           # 窄带高度 (720p 基准)
#  注: 血条是**固定像素高度**的 UI, 与英雄远近无关 -> 用绝对像素扫描, 不能按框高比例。
#  实测(2026-10-14): 真血条在框顶上方 19~40px(1080p) 处, 绿/红覆盖率 0.86~1.00;
#  而英雄所站草地/植被的绿色覆盖率只有 0.40~0.49 -> 用覆盖率下限把草地排除掉。
#  ⚠️ 王者血条配色: **自己=绿、队友=蓝、敌人=红**。蓝条判友军必须放在红之后(见下),
#     因为实测真敌头顶上方 36px 处也有一块覆盖率 0.81 的蓝色区域(技能/位移特效),
#     若"取三色最大带"就会把真敌判成友军 -> 漏打(方向性风险不对称)。


def _hero_bar_probe(frame, x1, y1, x2, y2):
    """扫描框顶上方逐条窄带, 找覆盖率最高的绿/红/蓝带(血条)。

    返回 (is_enemy, camp_src, [green, red], bar_color)。
    判定优先级 **红 > 绿 > 蓝**(每条各自取自己的最佳带):
      红条成立 -> 敌(绝不可能是友军干扰, 先判红);
      绿条成立 -> 友(自己);
      蓝条成立 -> 队友, 但仅当框在交战圈内(d<=HERO_BAR_BLUE_NEAR), 圈外保守留 default;
      都不成立 -> 保守判敌 + camp_src="default"(下游按不可信过滤)。
    """
    h, w = frame.shape[:2]
    sc = h / 720.0
    bw = x2 - x1
    cx = (x1 + x2) / 2
    rx0 = max(0, int(cx - bw * HERO_BAR_W_FRAC / 2))
    rx1 = min(w, int(cx + bw * HERO_BAR_W_FRAC / 2))
    if rx1 - rx0 < 7:
        return True, "default", [0.0, 0.0], None
    band = max(2, int(round(HERO_BAR_BAND_PX * sc)))
    step = max(1, band // 2)
    y_lo = max(0, int(y1 - HERO_BAR_SCAN_PX1 * sc))
    y_hi = max(0, int(y1 - HERO_BAR_SCAN_PX0 * sc))
    bg = bgr = bb = 0.0                 # 三个颜色各自的最佳覆盖率
    fg = fr = fb = (0.0, 0.0, 0.0)      # 各自最佳带的三色占比
    fall = (0.0, 0.0)
    for top in range(y_lo, y_hi + 1, step):
        crop = frame[top:top + band, rx0:rx1]
        g, r, b = _bar_color_fracs(crop)
        if g > bg:
            bg, fg = g, (g, r, b)
        if r > bgr:
            bgr, fr = r, (g, r, b)
        if b > bb:
            bb, fb = b, (g, r, b)
        if max(g, r, b) > max(fall):
            fall = (g, r)
    if bgr >= HERO_BAR_MIN_FRAC and (fr[1] - max(fr[0], fr[2])) > HERO_BAR_MIN_CONF:
        return True, "bar", [round(fr[0], 3), round(fr[1], 3)], "red"
    if bg >= HERO_BAR_MIN_FRAC and (fg[0] - max(fg[1], fg[2])) > HERO_BAR_MIN_CONF:
        return False, "bar", [round(fg[0], 3), round(fg[1], 3)], "green"
    if bb >= HERO_BAR_BLUE_MIN_FRAC and (fb[2] - max(fb[0], fb[1])) > HERO_BAR_MIN_CONF:
        cnx, cny = cx / w, ((y1 + y2) / 2.0) / h
        d = math.hypot(cnx - 0.5, (cny - 0.5) * (h / float(w)))
        if d <= HERO_BAR_BLUE_NEAR:
            return False, "bar", [round(fb[0], 3), round(fb[1], 3)], "blue"
        return True, "default", [round(fb[0], 3), round(fb[1], 3)], "blue_far"
    return True, "default", [round(fall[0], 3), round(fall[1], 3)], None


def _bar_color_fracs(roi):
    """血条 ROI 的绿/红/蓝像素占比(HSV; 绿/红阈值同 camp_autolabel.bar_colors)。"""
    if roi is None or roi.size == 0:
        return 0.0, 0.0, 0.0
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    h_, s_, v_ = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    sat = (s_ >= 80) & (v_ >= 80)
    green = ((h_ >= 40) & (h_ <= 85) & sat).mean()
    red = (((h_ <= 10) | (h_ >= 170)) & sat).mean()
    blue = ((h_ >= 90) & (h_ <= 140) & sat).mean()
    return float(green), float(red), float(blue)


def _letterbox(img, new_shape=640, color=(114, 114, 114), stride=32, auto=True):
    """YOLOv5 官方 letterbox(auto=True): 等比缩放后只补到 stride 的倍数(最小矩形),
    不补成正方形 —— 与 detect.py 的 letterbox(..., stride=32, auto=True) 行为一致。
    返回 (图, 缩放比, pad_w, pad_h)。"""
    h0, w0 = img.shape[:2]
    if isinstance(new_shape, int):
        new_shape = (new_shape, new_shape)
    r = min(new_shape[0] / h0, new_shape[1] / w0)
    new_unpad = (int(round(w0 * r)), int(round(h0 * r)))
    dw, dh = new_shape[1] - new_unpad[0], new_shape[0] - new_unpad[1]
    if auto:                                    # 最小矩形: 只补到 stride 倍数
        dw, dh = dw % stride, dh % stride
    dw, dh = dw / 2, dh / 2                     # 居中均分
    if (w0, h0) != new_unpad:
        img = cv2.resize(img, new_unpad, interpolation=cv2.INTER_LINEAR)
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    img = cv2.copyMakeBorder(img, top, bottom, left, right,
                             cv2.BORDER_CONSTANT, value=color)
    return img, r, dw, dh


def _xywh2xyxy(x):
    y = x.copy()
    y[..., 0] = x[..., 0] - x[..., 2] / 2
    y[..., 1] = x[..., 1] - x[..., 3] / 2
    y[..., 2] = x[..., 0] + x[..., 2] / 2
    y[..., 3] = x[..., 1] + x[..., 3] / 2
    return y


def _nms(boxes, scores, iou_thres):
    """按类别做过 NMS。boxes: (n,4) xyxy, scores: (n,)"""
    if len(boxes) == 0:
        return np.zeros(0, dtype=np.int64)
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1)
        h = np.maximum(0.0, yy2 - yy1)
        inter = w * h
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= iou_thres]
    return np.array(keep, dtype=np.int64)


def camp_blue_red_fracs(frame, x1, y1, x2, y2):
    """框内蓝色/红色像素占比(BGR 帧)。与 detector.camp_blue_red_fracs 同一实现。"""
    h, w = frame.shape[:2]
    x1, y1 = max(0, int(x1)), max(0, int(y1))
    x2, y2 = min(w, int(x2)), min(h, int(y2))
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return 0.0, 0.0
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    H, S = hsv[..., 0].astype(int), hsv[..., 1].astype(int)
    blue = (((H >= 70) & (H <= 135) & (S > 60))).mean()
    red = ((((H <= 15) | (H >= 165)) & (S > 60))).mean()
    return float(blue), float(red)


class HokYoloV5:
    """ONNX 推理 + 解码 + 类别映射 + 阵营分流。"""

    def __init__(self, onnx_path=None, classes_path=None, conf=0.35, iou=0.45,
                 imgsz=640, self_camp="blue"):
        import onnxruntime as ort
        self.onnx_path = Path(onnx_path or DEF_ONNX)
        self.imgsz = int(imgsz)
        self.conf = float(conf)
        self.iou = float(iou)
        self.self_camp = self_camp        # 我方阵营: 偶数类=敌; 蓝/红占比定敌我
        cp = Path(classes_path or DEF_CLS)
        self.names = {int(k): v for k, v in
                      json.loads(cp.read_text(encoding="utf-8"))["names"].items()}
        self.sess = ort.InferenceSession(str(self.onnx_path),
                                         providers=["CPUExecutionProvider"])
        self._in = self.sess.get_inputs()[0].name
        self._outs = [o.name for o in self.sess.get_outputs()]
        self.last_ms = 0.0

    # ---- 纯解码: raw out (1,25200,5+nc) -> [(cls_idx, conf, xyxy)] (原图坐标) ----
    def decode(self, out, frame_shape, ratio, dw, dh):
        import time
        t0 = time.time()
        o = np.asarray(out)[0]                        # (25200, 5+nc)
        nc = len(self.names)
        obj = o[:, 4]
        cls_scores = o[:, 5:5 + nc]
        cls_idx = cls_scores.argmax(axis=1)
        cls_conf = cls_scores[np.arange(len(o)), cls_idx]
        conf = obj * cls_conf
        m = conf > self.conf
        if not m.any():
            self.last_ms = (time.time() - t0) * 1000
            return []
        box = _xywh2xyxy(o[m, :4])
        cidx = cls_idx[m]
        cconf = conf[m]
        # 按类别 NMS
        keep_all = []
        for ci in np.unique(cidx):
            sel = np.where(cidx == ci)[0]
            k = _nms(box[sel], cconf[sel], self.iou)
            keep_all.append(sel[k])
        keep = np.concatenate(keep_all) if keep_all else np.zeros(0, dtype=np.int64)
        box, cidx, cconf = box[keep], cidx[keep], cconf[keep]
        # letterbox 逆变换 -> 原图坐标
        box[:, [0, 2]] = (box[:, [0, 2]] - dw) / ratio
        box[:, [1, 3]] = (box[:, [1, 3]] - dh) / ratio
        h, w = frame_shape[:2]
        box[:, [0, 2]] = box[:, [0, 2]].clip(0, w)
        box[:, [1, 3]] = box[:, [1, 3]].clip(0, h)
        self.last_ms = (time.time() - t0) * 1000
        return [(self.names[int(i)], float(c), b) for i, c, b in
                zip(cidx, cconf, box)]

    # ---- 检测: BGR 帧 -> 已映射到本项目 schema 的 list[dict] ----
    def detect(self, frame):
        import time
        img, ratio, dw, dh = _letterbox(frame, self.imgsz)
        x = img[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        x = np.ascontiguousarray(x)
        t0 = time.time()
        out = self.sess.run([self._outs[0]], {self._in: x})[0]
        infer_ms = (time.time() - t0) * 1000
        raw = self.decode(out, frame.shape, ratio, dw, dh)
        dets = []
        for gname, c, box in raw:
            d = self.map_class(gname, frame, box, c)
            if d is not None:
                d["generic"] = gname
                d["cam_ms"] = round(infer_ms, 1)
                dets.append(d)
        return dets

    def map_class(self, gname, frame, box, conf):
        """通用类 -> 本项目 schema(+阵营分流)。"""
        x1, y1, x2, y2 = box
        if gname in GENERIC_PAIRS:
            enemy_c, ally_c = GENERIC_PAIRS[gname]
            bar_color = None
            if gname == "hero":
                # v14.2 B.2: 英雄阵营 = 框上方血条颜色(自己=绿 / 队友=蓝 / 敌人=红)。
                # camp_fracs 记 [绿, 红]; bar_color 再给出实际命中色(绿/红/蓝)。
                is_enemy, camp_src, fracs, bar_color = _hero_bar_probe(frame, x1, y1, x2, y2)
                blue, red = fracs
            else:
                blue, red = camp_blue_red_fracs(frame, x1, y1, x2, y2)
                if gname in PAIR_USES_COLOR:
                    # 红占比显著高 -> 敌; 蓝占比显著高 -> 我; 两者都不显著(遮挡/远景)
                    # -> 取保守默认(敌), 并标 camp_src=default 让下游可识别不确定性
                    if (red - blue) > CAMP_EPS:
                        is_enemy, camp_src = True, "color"
                    elif (blue - red) > CAMP_EPS:
                        is_enemy, camp_src = False, "color"
                    else:
                        is_enemy, camp_src = True, "default"
                else:
                    is_enemy, camp_src = True, "assumed"
            cls = enemy_c if is_enemy else ally_c
            return {"cls": cls, "conf": round(conf, 3),
                    "xyxy": [round(float(v), 1) for v in box],
                    "camp_src": camp_src,
                    "camp_fracs": [round(blue, 3), round(red, 3)],
                    "bar_color": bar_color}
        if gname in NEUTRAL:
            return {"cls": NEUTRAL[gname], "conf": round(conf, 3),
                    "xyxy": [round(float(v), 1) for v in box],
                    "camp_src": "neutral"}
        # grass 及未知: 原样保留(不在 11 类 schema 内, 供额外约束用)
        return {"cls": gname, "conf": round(conf, 3),
                "xyxy": [round(float(v), 1) for v in box], "camp_src": "extra"}


def _selftest():
    ap = argparse.ArgumentParser(description="HOK YOLOv5 ONNX 自测")
    ap.add_argument("image")
    ap.add_argument("--onnx", default=str(DEF_ONNX))
    ap.add_argument("--camp", default="blue")
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--crop", default=None, help="WxH+X+Y 只测裁剪区域(去拼接图边框)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    frame = cv2.imread(args.image)
    if frame is None:
        print("读图失败:", args.image)
        return 1
    off = (0, 0)
    if args.crop:
        w, h, x, y = [int(v) for v in args.crop.replace("+", " ").replace("x", " ").split()]
        frame, off = frame[y:y + h, x:x + w].copy(), (x, y)
    det = HokYoloV5(args.onnx, conf=args.conf, self_camp=args.camp)

    import time
    det.detect(frame)                                  # 预热 + 统计
    t0 = time.time()
    dets = det.detect(frame)
    ms = (time.time() - t0) * 1000

    from collections import Counter
    cnt = Counter(d["cls"] for d in dets)
    print(f"[selftest] {Path(args.image).name} {frame.shape[1]}x{frame.shape[0]} | "
          f"检出 {len(dets)} 个 | {ms:.0f}ms")
    print("  类别分布:", dict(cnt))
    for d in sorted(dets, key=lambda x: -x["conf"])[:12]:
        print(f"    {d['cls']:14s} {d['conf']:.2f} {d['generic']:12s} "
              f"{d['camp_src']:8s} {d['xyxy']}")

    # 画框落盘
    vis = frame.copy()
    colors = {"hero": (255, 0, 0), "soldier": (0, 165, 255), "tower": (0, 200, 0),
              "crystal": (0, 255, 255), "grass": (0, 255, 0),
              "neutral_monster": (255, 0, 255)}
    for d in dets:
        x1, y1, x2, y2 = [int(v) for v in d["xyxy"]]
        g = d.get("generic", "")
        col = colors.get(g, (120, 120, 120))
        cv2.rectangle(vis, (x1, y1), (x2, y2), col, 2)
        cv2.putText(vis, f"{d['cls']} {d['conf']:.2f}", (x1, max(12, y1 - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1)
    out = args.out or str(ROOT / "runs" / "detect" / "hok_yolov5" /
                          f"selftest_{Path(args.image).stem}.jpg")
    cv2.imwrite(out, vis)
    print("  画框结果:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
