# -*- coding: utf-8 -*-
"""在 golden（59 帧人工 ground_truth）上评估检测器：精度 + 推理耗时。

为什么需要：改 `imgsz` / 线程数是**拿精度换速度**，没有精度数字就不能改默认值。
golden 已于 2026-10-26 完成人工核验（`prefill_source=human`），可直接当基准。

指标口径（单阈值匹配，不做曲线）：
  - 逐类 TP/FP/FN（同类 + IoU≥0.5 才算命中，一对一匹配）
  - P / R / F1，宏平均
  - 推理均耗时（ms/帧，含 letterbox + NMS，不含读盘）

用法：
    python -X utf8 scripts/eval_golden_detector.py --imgsz 640 --threads 4
    python -X utf8 scripts/eval_golden_detector.py --sweep          # 640/512/416 × 4/8 线程
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
GOLDEN = ROOT.parent / "hokdata" / "golden"
CLASSES = ["enemy_hero", "ally_hero", "enemy_minion", "ally_minion",
           "enemy_turret", "ally_turret", "enemy_crystal", "ally_crystal",
           "neutral_monster", "hook_aim", "skill_effect"]


def load_gt():
    rows = []
    for line in (GOLDEN / "candidates.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if not r.get("verified") or not r.get("ground_truth"):
            continue
        rows.append(r)
    return rows


def parse_yolo(text, w, h):
    out = []
    for ln in (text or "").splitlines():
        p = ln.split()
        if len(p) != 5:
            continue
        c = int(float(p[0]))
        cx, cy, bw, bh = (float(v) for v in p[1:])
        out.append({"cls": c,
                    "x1": (cx - bw / 2) * w, "y1": (cy - bh / 2) * h,
                    "x2": (cx + bw / 2) * w, "y2": (cy + bh / 2) * h})
    return out


def iou(a, b):
    ix = max(0.0, min(a["x2"], b["x2"]) - max(a["x1"], b["x1"]))
    iy = max(0.0, min(a["y2"], b["y2"]) - max(a["y1"], b["y1"]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    aa = (a["x2"] - a["x1"]) * (a["y2"] - a["y1"])
    bb = (b["x2"] - b["x1"]) * (b["y2"] - b["y1"])
    return inter / max(1e-9, aa + bb - inter)


# 决策层已有的 UI 区判据（wzry/decision/context.py:parse_units），此处复刻用于量化
def _ui_drop(cls, cx, cy, mode):
    if mode in ("decision", "all"):
        if cls in ("enemy_turret", "ally_turret", "enemy_hero", "ally_hero") \
                and cx > 0.72 and cy < 0.20:
            return True
        if cls in ("enemy_turret", "ally_turret") and cx < 0.18 and cy < 0.32:
            return True
    if mode == "all":
        if cx < 0.185 and cy < 0.30:      # 左上小地图
            return True
        if cy > 0.80:                      # 底部技能/摇杆带
            return True
        if cy < 0.055:                     # 顶部计分条
            return True
    return False


def evaluate(rows, imgsz, threads, conf=0.35, ui_filter="off"):
    from wzry.vision.hok_yolov5 import HokYoloV5
    det = HokYoloV5(str(ROOT / "runs" / "detect" / "hok_yolov5" / "hok_yolov5s.onnx"),
                    conf=conf, imgsz=imgsz, intra_threads=threads)
    stats = {c: {"tp": 0, "fp": 0, "fn": 0} for c in CLASSES}
    infer_ms = []
    for r in rows:
        img = cv2.imread(r["image"])
        if img is None:
            continue
        h, w = img.shape[:2]
        gt = parse_yolo(r["ground_truth"], w, h)
        t0 = time.perf_counter()
        preds = det.detect(img)
        infer_ms.append((time.perf_counter() - t0) * 1000.0)
        pr = []
        for d in preds:
            if d["cls"] not in CLASSES:
                continue
            x1, y1, x2, y2 = (d["xyxy"][0], d["xyxy"][1], d["xyxy"][2], d["xyxy"][3])
            if ui_filter != "off" and _ui_drop(d["cls"], (x1 + x2) / 2 / w,
                                               (y1 + y2) / 2 / h, ui_filter):
                continue
            pr.append({"cls": CLASSES.index(d["cls"]),
                       "x1": x1, "y1": y1, "x2": x2, "y2": y2})
        used = set()
        for g in gt:
            best, best_iou = None, 0.0
            for i, p in enumerate(pr):
                if i in used or p["cls"] != g["cls"]:
                    continue
                v = iou(g, p)
                if v > best_iou:
                    best, best_iou = i, v
            if best is not None and best_iou >= 0.5:
                stats[CLASSES[g["cls"]]]["tp"] += 1
                used.add(best)
            else:
                stats[CLASSES[g["cls"]]]["fn"] += 1
        for i, p in enumerate(pr):
            if i in used:
                continue
            name = CLASSES[p["cls"]] if 0 <= p["cls"] < len(CLASSES) else "?"
            if name in stats:
                stats[name]["fp"] += 1
    per = {}
    for c, s in stats.items():
        if s["tp"] + s["fp"] + s["fn"] == 0:
            continue
        p = s["tp"] / max(1, s["tp"] + s["fp"])
        rc = s["tp"] / max(1, s["tp"] + s["fn"])
        f1 = 2 * p * rc / max(1e-9, p + rc)
        per[c] = {"tp": s["tp"], "fp": s["fp"], "fn": s["fn"],
                  "P": round(p, 3), "R": round(rc, 3), "F1": round(f1, 3)}
    # 宏平均只覆盖"有 GT 或有预测"的类；某类被过滤干净后会从字典里消失，
    # 从而把它的 0 分从平均里抹掉 —— 这会**虚高**宏平均（踩过一次）。
    # 因此主指标用**微平均**（全类 TP/FP/FN 汇总），宏平均只作参考。
    tp = sum(v["tp"] for v in stats.values())
    fp = sum(v["fp"] for v in stats.values())
    fn = sum(v["fn"] for v in stats.values())
    mic_p = tp / max(1, tp + fp)
    mic_r = tp / max(1, tp + fn)
    mic_f1 = 2 * mic_p * mic_r / max(1e-9, mic_p + mic_r)
    macro_p = sum(v["P"] for v in per.values()) / max(1, len(per))
    macro_r = sum(v["R"] for v in per.values()) / max(1, len(per))
    macro_f1 = sum(v["F1"] for v in per.values()) / max(1, len(per))
    return {
        "imgsz": imgsz, "threads": threads, "conf": conf, "ui_filter": ui_filter,
        "n_frames": len(infer_ms),
        "infer_ms_mean": round(sum(infer_ms) / max(1, len(infer_ms)), 2),
        "micro": {"tp": tp, "fp": fp, "fn": fn, "P": round(mic_p, 3),
                  "R": round(mic_r, 3), "F1": round(mic_f1, 3)},
        "macro": {"P": round(macro_p, 3), "R": round(macro_r, 3),
                  "F1": round(macro_f1, 3)},
        "per_class": per,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--ui-filter", default="off", choices=["off", "decision", "all"],
                    help="按 UI 区丢框后再算指标（只做量化，不改生产行为）")
    ap.add_argument("--json", default=None, help="把结果写到该文件")
    args = ap.parse_args()

    rows = load_gt()
    if not rows:
        print("没有 verified 的 golden 帧（先跑 verify_golden_labels.py + apply）")
        return 2
    print(f"golden verified 帧: {len(rows)}")

    combos = ([(sz, t) for sz in (640, 512, 416) for t in (4, 8)]
              if args.sweep else [(args.imgsz, args.threads)])
    out = []
    for sz, t in combos:
        r = evaluate(rows, sz, t, args.conf, args.ui_filter)
        out.append(r)
        m = r["micro"]
        print(f"\nimgsz={sz} threads={t} ui={args.ui_filter}: "
              f"推理 {r['infer_ms_mean']:.1f} ms/帧  "
              f"微 P={m['P']:.3f} R={m['R']:.3f} F1={m['F1']:.3f} "
              f"(TP={m['tp']} FP={m['fp']} FN={m['fn']})")
        for c, v in sorted(r["per_class"].items(), key=lambda kv: -kv[1]["tp"]):
            print(f"    {c:16s} TP={v['tp']:3d} FP={v['fp']:3d} FN={v['fn']:3d} "
                  f"P={v['P']:.2f} R={v['R']:.2f} F1={v['F1']:.2f}")

    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        print(f"\n已写 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
