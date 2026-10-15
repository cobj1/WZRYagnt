# -*- coding: utf-8 -*-
"""真机在线验证：从 MuMu 截图 -> YoloDetector(ONNX 替代检测器) -> 统计 + 落盘带框图。

用途：P0 感知重建验证（替代检测器在真机对局画面上的召回/精度/耗时）。
对齐生产：抓原图后缩放到 1280x720（等同 m2 `--capture desktop` 的裁剪路径），
再走 `YoloDetector.detect()`，这样 `hero_dedup_ui` 里的像素阈值语义才一致。

运行：
  venv\\Scripts\\python.exe -X utf8 scripts/probe_detector_live.py --seconds 60
  [--serial 127.0.0.1:16384] [--conf 0.3] [--save-every 10] [--native]
"""
import argparse
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEF_ADB = r"D:\app\MuMuPlayer\nx_main\adb.exe"
DEF_MODEL = ROOT / "runs" / "detect" / "hok_yolov5" / "hok_yolov5s.onnx"
PROD_SIZE = (1280, 720)          # m2 生产路径的帧尺寸


def shot(adb, serial, timeout=60):
    r = subprocess.run([adb, "-s", serial, "exec-out", "screencap", "-p"],
                       capture_output=True, timeout=timeout)
    if r.returncode != 0 or len(r.stdout) < 1024:
        return None
    img = cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR)
    return img


def draw(frame, dets):
    vis = frame.copy()
    colors = {"enemy_hero": (0, 0, 255), "ally_hero": (255, 120, 0),
              "enemy_minion": (0, 80, 255), "ally_minion": (255, 200, 0),
              "enemy_turret": (0, 0, 160), "ally_turret": (160, 0, 0),
              "enemy_crystal": (0, 0, 220), "ally_crystal": (220, 0, 0),
              "neutral_monster": (200, 0, 200), "grass": (0, 200, 0)}
    for d in dets:
        x1, y1, x2, y2 = [int(v) for v in d.xyxy]
        col = colors.get(d.cls, (140, 140, 140))
        cv2.rectangle(vis, (x1, y1), (x2, y2), col, 2)
        cv2.putText(vis, f"{d.cls} {d.conf:.2f}", (x1, max(14, y1 - 5)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1)
    return vis


def main():
    ap = argparse.ArgumentParser(description="live detector probe on MuMu")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--serial", default="127.0.0.1:16384")
    ap.add_argument("--adb", default=DEF_ADB)
    ap.add_argument("--model", default=str(DEF_MODEL))
    ap.add_argument("--conf", type=float, default=0.3)
    ap.add_argument("--save-every", type=int, default=10,
                    help="每 N 帧落盘一张带框图")
    ap.add_argument("--native", action="store_true",
                    help="不缩放, 按设备原生分辨率检测(仅供对比, 坐标阈值与生产不一致)")
    ap.add_argument("--out", default=str(ROOT / "runs" / "detect" / "live"))
    args = ap.parse_args()

    from wzry.vision.detector import YoloDetector

    det = YoloDetector(args.model, conf=args.conf)
    print(f"[live] backend={det.backend} model={Path(args.model).name} conf={args.conf}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    frame0 = shot(args.adb, args.serial)
    if frame0 is None:
        print("[live] 截图失败: 检查 adb 连接与设备号")
        return 1
    h0, w0 = frame0.shape[:2]
    print(f"[live] 设备原生 {w0}x{h0} | 生产对齐 {PROD_SIZE[0]}x{PROD_SIZE[1]}")

    counts = Counter()
    ms_list = []
    n = 0
    saved = 0
    t_end = time.time() + args.seconds
    while time.time() < t_end:
        frame = shot(args.adb, args.serial)
        if frame is None:
            print("[live] 截图丢帧, 跳过")
            time.sleep(0.2)
            continue
        if not args.native and frame.shape[:2] != (PROD_SIZE[1], PROD_SIZE[0]):
            frame = cv2.resize(frame, PROD_SIZE, interpolation=cv2.INTER_AREA)
        dets = det.detect(frame)
        n += 1
        ms_list.append(det.last_infer_ms)
        for d in dets:
            counts[d.conf >= 0.5 and d.cls or (d.cls + "@低置信")] += 1
        if n % args.save_every == 0:
            p = out / f"live_{int(time.time())}_{n:04d}.jpg"
            cv2.imwrite(str(p), draw(frame, dets))
            saved += 1
        time.sleep(0.05)

    print(f"\n[live] 共 {n} 帧, 落盘 {saved} 张带框图 -> {out}")
    if ms_list:
        ms = sorted(ms_list)
        print(f"[live] 推理耗时 p50={ms[len(ms)//2]:.0f}ms "
              f"p95={ms[int(0.95*(len(ms)-1))]:.0f}ms  (adb 截图另计)")
    print("[live] 类别命中统计(>=0.5 与 <0.5 分开):")
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"    {k:22s} {v}")
    print("\n[live] 判据提醒: 期望至少看到 enemy_hero / ally_hero / soldier(minion) / "
          "tower 等; 全空则说明画面不在对局内或检测器不适配")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
