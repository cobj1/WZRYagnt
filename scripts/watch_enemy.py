# -*- coding: utf-8 -*-
""""看到敌人"那一刻的取证（v15.4）：只读harvester，用来解锁两个卡住的感知项。

卡住的两项都只在**敌人可见**时才存在数据：
  1) 敌方血量比例（场景内头顶红条）——没有敌人就没有红条；
  2) 顶部"锁定目标头像"（选中/正在攻击敌方英雄时才出现的那个实时血量）——不仅要敌人，
     还要"正在打他"。
上一次真机驱车两次都是 enemy_hero 全程 0 次检出（敌人不进口），所以缺的从来不是算法，
是**素材**。本脚本就干一件事：在对局里蹲着，凡是检测到敌方英雄的帧就落盘 + 打印，
顺带把红条读音一起记下来。之后 3/4 两项都能在这些帧上离线标定/验证。

只读、不发任何点击（对局安全），也不接主循环。

用法：
    venv\\Scripts\\python.exe scripts\\watch_enemy.py            # 蹲 150s
    venv\\Scripts\\python.exe scripts\\watch_enemy.py --seconds 300 --conf 0.30
"""
import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "runs" / "detect" / "live" / "enemy_watch"
MAX_SAVE = 40
MOVE_PX = 30.0            # 同一个敌人框挪动超过这么多像素 -> 当成新的一次"露头"


def _xyxy(d):
    return [float(v) for v in (d.get("xyxy") or (0, 0, 0, 0))]


def main():
    ap = argparse.ArgumentParser(description="敌人露头取证（只读）")
    ap.add_argument("--seconds", type=float, default=150.0)
    ap.add_argument("--conf", type=float, default=0.35, help="检测器置信度阈值")
    ap.add_argument("--serial", default=None)
    args = ap.parse_args()

    from wzry.capture.screencap_capture import ScreencapCapture
    from wzry.vision.hero_hp import enemy_hp_from_dets, enemy_hp_ratio
    from wzry.vision.hok_yolov5 import HokYoloV5

    det_model = HokYoloV5(conf=args.conf)
    cap = ScreencapCapture(**(dict(serial=args.serial) if args.serial else {}))
    cap.start()
    OUT.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%H%M%S")
    print(f"蹲守 {args.seconds:.0f}s（只读，不发点击）…")
    print(">>> 请在【正常对局】里正常游玩，遇到敌人时脚本会自动落盘证据帧")

    t_end = time.time() + args.seconds
    n_saved, n_seen, n_raw = 0, 0, 0
    last_boxes = []
    last_obj = None
    t_report = 0.0
    try:
        while time.time() < t_end:
            frame, _ = cap.wait_frame(timeout=2.0)
            if frame is None or frame is last_obj:      # wait_frame 无新帧会返回同一缓存对象
                continue
            last_obj = frame
            dets = det_model.detect(frame)
            eh = [d for d in dets if d.get("cls") == "enemy_hero"]
            if not eh:
                if time.time() - t_report > 15.0:
                    t_report = time.time()
                    print(f"  …{time.strftime('%H:%M:%S')} 视野内无敌方英雄"
                          f"（已落盘 {n_saved} 帧）")
                continue
            n_raw += len(eh)
            # 可信的（camp_src != default）与全部都要记：前者是链路口径，后者只是线索
            ratios = enemy_hp_from_dets(frame, eh)
            boxes = [_xyxy(d) for d in eh]
            # 只有"新露头/明显换位"才落盘，避免同一个敌人连续占满名额
            moved = True
            if last_boxes:
                moved = any(
                    min(abs(b[0] - o[0]) + abs(b[1] - o[1]) for o in last_boxes) > MOVE_PX
                    for b in boxes)
            if not moved:
                continue
            last_boxes = boxes
            n_seen += 1
            if n_saved < MAX_SAVE:
                p = OUT / f"enemy_{ts}_{n_saved:02d}.png"
                cv2.imwrite(str(p), frame)
                top = OUT / f"enemy_{ts}_{n_saved:02d}_top.png"
                cv2.imwrite(str(top), cv2.resize(frame[0:70, :], None, fx=2, fy=2,
                                                 interpolation=cv2.INTER_NEAREST))
                n_saved += 1
                print(f"  [{time.strftime('%H:%M:%S')}] 露头 #{n_seen}: enemy_hero x{len(eh)}"
                      f" -> {p.name}（+顶部条带 {top.name}）")
                for d in eh:
                    bx = _xyxy(d)
                    print(f"       box=({bx[0]:.0f},{bx[1]:.0f},{bx[2]:.0f},{bx[3]:.0f})"
                          f" conf={d.get('conf')} camp_src={d.get('camp_src')}"
                          f" 红条比例={enemy_hp_ratio(frame, bx)}")
                print(f"       可信口径(enemy_hp_from_dets) = {ratios}")
    except KeyboardInterrupt:
        print("手动停止")
    finally:
        try:
            cap.stop()
        except Exception:
            pass

    print(f"\n蹲守结束：落盘 {n_saved} 帧；露头次数 {n_seen}；原始 enemy_hero 检出 {n_raw} 次")
    if n_seen == 0:
        print("这一段时间里**没有敌方英雄进入视野** —— 素材仍然缺失，"
              "这两项（敌方血量比例 / 顶部锁定目标头像）只能等下一次遇到敌人。")
    else:
        print(f"证据帧在 {OUT}")
        print("下一步：在这些帧上量 '头顶红条长度' 与 '顶部锁定目标头像' 的位置。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
