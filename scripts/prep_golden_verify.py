# -*- coding: utf-8 -*-
"""golden 标注核验辅助：跑 YOLO -> 画框+坐标网格 -> 拼图，供人工（视觉）逐帧核对。

产出：
    hokdata/golden/verify/<name>.png        单帧带框（全分辨率）
    hokdata/golden/verify/sheets/sheet_XX.png  4 帧拼图（2×2，640×360 每格）

用法：
    python -X utf8 scripts/prep_golden_verify.py            # 全量
    python -X utf8 scripts/prep_golden_verify.py --limit 8  # 先试前 8 帧
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
GOLDEN = ROOT.parent / "hokdata" / "golden"
OUT = GOLDEN / "verify"
CLASSES = ["enemy_hero", "ally_hero", "enemy_minion", "ally_minion",
           "enemy_turret", "ally_turret", "enemy_crystal", "ally_crystal",
           "neutral_monster", "hook_aim", "skill_effect"]
COLORS = {0: (0, 0, 255), 1: (0, 255, 0), 2: (0, 80, 255), 3: (0, 200, 120),
          4: (255, 0, 255), 5: (180, 0, 180), 6: (0, 200, 255), 7: (120, 180, 0),
          8: (255, 180, 0), 9: (0, 255, 255), 10: (200, 200, 200)}


def draw(frame, boxes, scale=1.0):
    h, w = frame.shape[:2]
    # 坐标网格：每 1/10 画一条，标百分比，便于我口头描述"某处少了框"
    for i in range(1, 10):
        x, y = int(w * i / 10), int(h * i / 10)
        cv2.line(frame, (x, 0), (x, h), (60, 60, 60), 1)
        cv2.line(frame, (0, y), (w, y), (60, 60, 60), 1)
    for b in boxes:
        c = int(b["cls"])
        x1, y1, x2, y2 = (int(v * scale) for v in (b["x1"], b["y1"], b["x2"], b["y2"]))
        col = COLORS.get(c, (255, 255, 255))
        cv2.rectangle(frame, (x1, y1), (x2, y2), col, 2)
        tag = f"{CLASSES[c] if c < len(CLASSES) else c}"
        cv2.putText(frame, tag, (x1 + 2, max(12, y1 - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, col, 1, cv2.LINE_AA)
    return frame


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--model", default=str(ROOT / "runs" / "detect" / "hok_yolov5"
                                           / "hok_yolov5s.onnx"))
    args = ap.parse_args()

    from wzry.vision.detector import YoloDetector
    det = YoloDetector(args.model, conf=0.35)
    print(f"[prep] 检测器: {args.model}  names={getattr(det, 'names', None)}")

    rows = [json.loads(l) for l in
            (GOLDEN / "candidates.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()]
    if args.limit:
        rows = rows[:args.limit]
    (OUT / "sheets").mkdir(parents=True, exist_ok=True)

    name_to_cls = {n: i for i, n in enumerate(CLASSES)}
    results = []
    for k, r in enumerate(rows):
        img = cv2.imread(r["image"])
        if img is None:
            print(f"[prep] 读不到 {r['image']}")
            continue
        h, w = img.shape[:2]
        boxes = []
        for d in det.detect(img):
            cid = name_to_cls.get(d.cls)
            if cid is None:
                continue
            x1, y1, x2, y2 = d.xyxy
            boxes.append({"cls": cid, "x1": float(x1), "y1": float(y1),
                          "x2": float(x2), "y2": float(y2),
                          "conf": float(d.conf)})
        vis = draw(img.copy(), boxes)
        name = Path(r["image"]).stem
        cv2.imwrite(str(OUT / f"{name}.png"), vis)
        results.append({"name": name, "image": r["image"], "session": r["session"],
                        "w": w, "h": h, "boxes": boxes,
                        "prefill_boxes": r["prefill_boxes"],
                        "scenario": r["scenario"]})
        print(f"[prep] {k+1}/{len(rows)} {name} 检测 {len(boxes)} 框"
              f"（候选记录 prefill={r['prefill_boxes']}）")

    (OUT / "detections.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    # 2×2 拼图，每格 640×360
    CW, CH = 960, 540
    per = 4
    for s in range(0, len(results), per):
        sheet = np.zeros((CH * 2, CW * 2, 3), dtype=np.uint8)
        for j, rec in enumerate(results[s:s + per]):
            im = cv2.imread(str(OUT / f"{rec['name']}.png"))
            if im is None:
                continue
            im = cv2.resize(im, (CW, CH))
            cv2.putText(im, f"#{s + j} {rec['name'][-17:]}", (6, 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
            y0, x0 = (j // 2) * CH, (j % 2) * CW
            sheet[y0:y0 + CH, x0:x0 + CW] = im
        cv2.imwrite(str(OUT / "sheets" / f"sheet_{s // per:02d}.png"), sheet)
    print(f"[prep] 完成：{len(results)} 帧 / {(len(results) + 3) // 4} 张拼图 -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
