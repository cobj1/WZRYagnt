# -*- coding: utf-8 -*-
"""监听式面板取证（v15.2）：等你手动点开面板，我把那一帧抓下来。

为什么这么做：UI 查询层要标定三组坐标（打开按钮 / 关闭按钮 / 面板内 ROI），
而面板只在人工点开后出现 —— 所以先"监听 + 取证"，再离线定 ROI。

判据：panel_open_score（中央亮卡片 vs 四周暗遮罩，见 wzry/uiquery/panel）。
产出：
  runs/detect/live/panel_<score>_<ts>.png    面板打开帧（按分数命名，最多存 6 张）
  runs/detect/live/panel_prev_<ts>.png       打开前最后一帧（用于对比 HUD）
  runs/detect/live/panel_zoom2_<ts>.png      面板帧 2 倍放大（供人工确认）

用法：
    venv\\Scripts\\python.exe scripts\\grab_panel.py --seconds 150
"""
import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.uiquery.panel import panel_open_score  # noqa: E402

OUT = ROOT / "runs" / "detect" / "live"
MAX_SAVE = 6


def digits_in(frame):
    """在面板帧里找数字块（面板是大字静态画面）-> [(x,y,w,h,值,置信度)]。"""
    try:
        from wzry.vision.digit_reader import _blobs, read_number, load_templates
        tpl = load_templates()
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        out = []
        # 面板通常居中：只在中央 76% 区域找，避免把 HUD 上的字也扫进来
        h, w = g.shape[:2]
        sub = g[int(h * 0.12):int(h * 0.88), int(w * 0.12):int(w * 0.88)]
        ox, oy = int(w * 0.12), int(h * 0.12)
        for b in _blobs(sub)[:24]:
            crop = sub[b["y"]:b["y"] + b["h"], b["x"]:b["x"] + b["w"]]
            v, conf = read_number(crop, tpl)
            if v is not None and conf and max(conf) > 0.5:
                out.append((b["x"] + ox, b["y"] + oy, b["w"], b["h"], v, round(max(conf), 2)))
        return out
    except Exception as e:
        print(f"  [数字块] 失败: {type(e).__name__}: {e}")
        return []


def main():
    ap = argparse.ArgumentParser(description="监听式面板取证")
    ap.add_argument("--seconds", type=float, default=150.0)
    ap.add_argument("--thr", type=float, default=0.30, help="面板打开判据阈值")
    ap.add_argument("--mode", default="diff", choices=["diff", "panel"],
                    help="diff=画面剧变即抓(外观无关, 推荐) / panel=按亮卡片判据")
    ap.add_argument("--diff-thr", type=float, default=18.0,
                    help="diff 模式下相邻帧平均差阈值（面板弹出是大幅突变）")
    ap.add_argument("--min-gap", type=float, default=3.0,
                    help="两次抓帧最小间隔秒（避免跟随镜头的运动把名额刷满）")
    ap.add_argument("--serial", default=None)
    args = ap.parse_args()

    from wzry.capture.screencap_capture import ScreencapCapture
    cap = ScreencapCapture(**(dict(serial=args.serial) if args.serial else {}))
    cap.start()
    OUT.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%H%M%S")
    print(f"监听中（{args.seconds:.0f}s, 阈值 score>={args.thr}）…")
    print(">>> 请在对局里点开【比分面板】并保持 3~5 秒；如方便再点【英雄属性面板】")
    t_end = time.time() + args.seconds
    saved, prev_frame, last_prev_at = [], None, 0.0
    best = (0.0, None)
    t_report = 0.0
    prev_g = None
    last_obj = None
    stall_t = 0.0
    try:
        while time.time() < t_end:
            frame, _ = cap.wait_frame(timeout=2.0)
            # ⚠️ wait_frame 在"没有新帧"时会返回**同一个缓存对象** -> 直接比 diff 会恒为 0
            #   （实测踩到：四分钟里 diff 一直 0.0，其实画面在缓慢变化）。必须按对象身份去重。
            if frame is None or frame is last_obj:
                if stall_t == 0.0:
                    stall_t = time.time()
                elif time.time() - stall_t > 6.0 and time.time() - t_report > 10.0:
                    t_report = time.time()
                    print("  [警告] 采集线程 6s 没有新帧（画面静止或采集卡住）")
                continue
            last_obj, stall_t = frame, 0.0
            sc = panel_open_score(frame)
            if sc > best[0]:
                best = (sc, frame.copy())
            tiny = cv2.cvtColor(cv2.resize(frame, (320, 180)), cv2.COLOR_BGR2GRAY).astype(np.int16)
            diff = float(np.abs(tiny - prev_g).mean()) if prev_g is not None else 0.0
            prev_g = tiny
            hit = (diff >= args.diff_thr) if args.mode == "diff" else (sc >= args.thr)
            if hit and (time.time() - last_prev_at) >= args.min_gap:
                last_prev_at = time.time()
                if len(saved) < MAX_SAVE:
                    tag = f"diff{diff:.0f}" if args.mode == "diff" else f"score{sc:.2f}"
                    p = OUT / f"panel_{tag}_{ts}_{len(saved)}.png"
                    cv2.imwrite(str(p), frame)
                    if prev_frame is not None:
                        cv2.imwrite(str(OUT / f"panel_prev_{ts}_{len(saved)}.png"), prev_frame)
                    big = cv2.resize(frame, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
                    cv2.imwrite(str(OUT / f"panel_zoom2_{ts}_{len(saved)}.png"), big)
                    saved.append(str(p))
                    print(f"  [抓到突变] {tag} score={sc:.2f} -> {p.name}")
                time.sleep(0.8)
            else:
                prev_frame = frame
                if time.time() - t_report > 10.0:
                    t_report = time.time()
                    print(f"  …等待中  score={sc:.3f} diff={diff:.1f}（最高 score {best[0]:.3f}）")
    except KeyboardInterrupt:
        print("手动停止")
    finally:
        try:
            cap.stop()
        except Exception:
            pass

    print(f"\n监听结束：抓到面板帧 {len(saved)} 张；最高 score={best[0]:.3f}")
    if not saved:
        print("没有抓到面板（阈值未过）。要么面板没被点开，要么它在中央不是亮卡片结构。")
        if best[1] is not None and best[0] > 0.15:
            p = OUT / f"panel_best_{ts}.png"
            cv2.imwrite(str(p), best[1])
            print(f"已把最高分的那帧存下来供人工看: {p}")
        return 2
    img = cv2.imdecode(np.fromfile(saved[0], dtype=np.uint8), cv2.IMREAD_COLOR)
    print("\n面板帧里的数字块（x,y,w,h,值,置信度）—— 用来定 panel_rois：")
    for t in digits_in(img):
        print(f"   x={t[0]:4d} y={t[1]:4d} w={t[2]:3d} h={t[3]:3d}  值={t[4]:<6} conf={t[5]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
