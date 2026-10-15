# -*- coding: utf-8 -*-
"""STICK_SPEED 标定：满杆直推 1 秒 -> 小地图归一化位移。

方法（顾问设计，2026-10-14/15）：
  英雄站在本方泉水时，其小地图位置 = 固定已知点 BASE_BLUE(泉水图标实测坐标)。
  朝最近的己方塔**满杆直推**（每杆固定 900ms，用"整杆"为单位吸收 input swipe 的
  加减速损耗）；YOLO 一旦检出"塔框底边(塔基)贴到英雄屏幕锚点"，即认为英雄走到了
  该塔 —— 此刻英雄小地图位置 ≈ 该塔坐标。沿塔线继续推会依次经过下一座塔，于是得到
  多个 (累计推杆秒数, 距泉水的小地图距离) 数据点，过原点最小二乘拟合 -> STICK_SPEED：
      斜率 = Σ(t_i·d_i) / Σ(t_i²)
  用 r=MOVE_R(=0.8) 推杆，与 m2 的 map_move 完全同幅，标定值可直接使用。

真机事实与三个关键修正（2026-10-15）：
  - 小地图"绿点/self"恒定停在泉水图标附近，**不随英雄移动**（满杆推 15.3s 绿点只挪
    0.048）-> 自己位置无法直接观测，必须以"泉水"为唯一锚点。
  - 王者推杆撞墙**不会滑行** -> 方向必须沿塔线（"泉水->最近塔"射线），全程固定 theta。
  - 抵达判定不是"塔框中心到屏幕中心"（塔框中心在塔身上部、镜头又把英雄放在屏幕中部
    偏下，永远差 0.2+），而是"塔基(框底边)贴英雄屏幕锚点(self_hp_mp)"。
  - 抵达用"最近塔框距锚点的**局部极小值**"登记（距离回落再抬升），不用"先远后近"的
    迟滞 —— 上一版就死在迟滞条件上（距离已到 0.089 却没登记）。
  - 塔线必须用 MMTrackerV7 的**存活塔**过滤：大劣局外塔已毁，死塔混进序列会让
    "第 k 次抵达 = 第 k 座塔"整个错位。
  - 英雄中途阵亡会打断序列（阵亡横幅 >=2500px）-> 暂停推杆，等复活(回泉水)后从 t=0
    重新累计。

用法：
  python scripts/calib_stick_speed.py --probe        # 只看可见塔框/方向/锚点，不推杆
  python scripts/calib_stick_speed.py --seconds 45   # 真推杆标定
  python scripts/calib_stick_speed.py --r 1.0        # 测"摇杆是否饱和"(与 --r 0.8 比值)
"""
import argparse
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision.mm_rules_v7 import BLUE_TOWER_PTS, RED_TOWER_PTS  # noqa: E402

# 与 m2_agent_v2.py 保持一致
BASE_BLUE = (0.074, 0.88)      # 泉水在小地图上的实测归一化坐标(蓝方)
BASE_RED = (0.926, 0.12)
MOVE_R = 0.80                  # 与 map_move 同幅
BURST_MS = 900                 # 固定每杆时长(吸收加减速损耗)
ARRIVE_FRAC = 0.16             # 塔基(框底边)距英雄屏幕锚点 < 该值 => 有资格登记抵达
RISE_FRAC = 0.06               # 距锚点从极小值抬升超过该值 => 确认"已走过这座塔"
DEATH_PX = 2500                # death_banner_px 阈值(见 self_bars: >=2500 为真死亡)


def _lane_line(camp, theta):
    """沿推杆方向(theta)由近到远的塔点: 只保留方向接近该射线的塔(±22°), 按距泉水排序。

    单纯"按距泉水排序"会把三条路的塔混在一起(如 (0.222,0.907) 是下路塔,
    不在中路射线上) -> 抵达序列对不上; 故先按角度筛。
    """
    base = BASE_BLUE if camp == "blue" else BASE_RED
    pts = BLUE_TOWER_PTS if camp == "blue" else RED_TOWER_PTS
    out = []
    for p in pts:
        vx, vy = p[0] - base[0], p[1] - base[1]
        d = math.hypot(vx, vy)
        if d < 1e-6:
            continue
        a = math.atan2(-vy, vx)
        da = abs((a - theta + math.pi) % (2 * math.pi) - math.pi)
        if da > math.radians(22):
            continue
        out.append((d, p))
    out.sort()
    return base, [p for _, p in out]


def _turret_boxes(dets):
    return [d for d in dets if d.cls in ("enemy_turret", "ally_turret")]


def _nearest_turret(dets, w, h, ax, ay):
    """离"英雄屏幕锚点"最近的塔框(按塔基=框底边算) -> (距锚点, cls, 塔基x, 塔基y)。"""
    best = None
    for d in dets:
        if d.cls not in ("enemy_turret", "ally_turret"):
            continue
        bx = (d.xyxy[0] + d.xyxy[2]) / 2.0 / w
        by = d.xyxy[3] / h
        dist = math.hypot(bx - ax, (by - ay) * (h / w))
        if best is None or dist < best[0]:
            best = (dist, d.cls, bx, by)
    return best


def _alive_filter(line, mm):
    """用 MMTrackerV7 的存活塔过滤塔线(死塔混入会让抵达序列错位)。返回 (存活线, 说明)。"""
    tw = (mm or {}).get("towers") or []
    if not tw:
        return line, "无存活塔信息(不筛)"
    alive = []
    for p in line:
        d = min(math.hypot(p[0] - q[0], p[1] - q[1]) for q in tw)
        alive.append((d, p))
    keep = [p for d, p in alive if d < 0.06]
    note = "已毁/未检出塔: " + str([tuple(round(v, 3) for v in p)
                                   for d, p in alive if d >= 0.06]) if len(keep) != len(line) else "全部存活"
    return keep, note


def _dead(frame):
    try:
        from wzry.vision.self_bars import death_banner_px
        return death_banner_px(frame) >= DEATH_PX
    except Exception:
        return False


def _alive_bar(frame):
    try:
        from wzry.vision.self_bars import self_hp_mp
        hp, _mp, pos = self_hp_mp(frame)
        return (hp is not None and hp > 0.0 and pos is not None)
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="STICK_SPEED 标定")
    ap.add_argument("--camp", default="blue", choices=["blue", "red"])
    ap.add_argument("--seconds", type=float, default=45.0)
    ap.add_argument("--probe", action="store_true", help="不推杆, 只看当前塔框/方向/锚点")
    ap.add_argument("--r", type=float, default=MOVE_R, help="推杆幅度(与 map_move 同幅=0.8)")
    ap.add_argument("--model", default=str(ROOT / "runs" / "detect" / "hok_yolov5"
                                           / "hok_yolov5s.onnx"))
    ap.add_argument("--capture", default="screencap", choices=["screencap", "desktop"])
    ap.add_argument("--theta", type=float, default=None,
                    help="推杆方向(度, 0=屏幕右, 逆时针); 默认=泉水->最近塔方向")
    args = ap.parse_args()

    w, h = 1280.0, 720.0

    from wzry.vision.detector import YoloDetector
    from wzry.vision.mm_tracker_v7 import MMTrackerV7
    det = YoloDetector(args.model, conf=0.35)
    tracker = MMTrackerV7()

    if args.capture == "desktop":
        from wzry.capture.desktop_capture import DesktopCapturePrint
        cap = DesktopCapturePrint(interval=0.02)
    else:
        from wzry.capture.screencap_capture import ScreencapCapture
        cap = ScreencapCapture(interval=0.05)
    cap.start()

    def frame():
        f, _ = cap.wait_frame(timeout=3.0)
        return f

    base = BASE_BLUE if args.camp == "blue" else BASE_RED
    pts = BLUE_TOWER_PTS if args.camp == "blue" else RED_TOWER_PTS
    # 默认方向 = 泉水 -> 最近的"塔"(排除离泉水最近那颗 = 水晶)
    _ord = sorted(pts, key=lambda p: math.hypot(p[0] - base[0], p[1] - base[1]))
    _near = _ord[1]
    theta = (math.radians(args.theta) if args.theta is not None
             else math.atan2(-(_near[1] - base[1]), _near[0] - base[0]))
    print(f"泉水={base}  最近塔={tuple(round(v, 3) for v in _near)}")
    print(f"推杆方向 theta={math.degrees(theta):.1f}°  每杆 {BURST_MS}ms  r={args.r}")
    print("注意: 脚本启动时英雄必须在泉水(刚开局/刚复活)，否则锚点错误。")

    # ---- 首帧: 锚点 / 塔框 / 存活塔筛塔线 ----
    f0 = frame()
    if f0 is None:
        cap.stop()
        raise SystemExit("抓不到帧（检查 MuMu/adb/桌面窗口）")
    mm0 = tracker.update(f0)
    line_all = _lane_line(args.camp, theta)[1]
    line, note = _alive_filter(line_all, mm0)
    dists = [math.hypot(p[0] - base[0], p[1] - base[1]) for p in line]
    print(f"\n塔线(存活, 由近到远): {[tuple(round(v, 3) for v in p) for p in line]}"
          f"  [{note}]")
    print(f"对应距离: {[round(d, 3) for d in dists]}")

    hx, hy = 0.5, 0.5
    try:
        from wzry.vision.self_bars import self_hp_mp
        _hp, _mp, _pos = self_hp_mp(f0)
        if _pos:
            hx, hy = _pos[0] / w, _pos[1] / h
    except Exception:
        pass
    print(f"英雄屏幕锚点={hx:.3f},{hy:.3f} (血条推算; 抓不到则(0.5,0.5))")
    d0 = _turret_boxes(det.detect(f0))
    print(f"起始帧塔框 {len(d0)} 个:")
    for d in d0:
        bx = (d.xyxy[0] + d.xyxy[2]) / 2.0 / w
        by = d.xyxy[3] / h
        print(f"  {d.cls:13s} conf={d.conf:.2f} 塔基=({bx:.3f},{by:.3f}) "
              f"距锚点={math.hypot(bx - hx, (by - hy) * (h / w)):.3f}")
    if args.probe:
        from collections import Counter
        print("起始帧全部检测:", dict(Counter(d.cls for d in det.detect(f0))))
        print("存活塔(小地图):", [tuple(round(v, 3) for v in p)
                                 for p in (mm0.get("towers") or [])])
        cap.stop()
        return
    if not line:
        cap.stop()
        raise SystemExit("该方向上没有存活的塔点，换 --theta 或等外塔重建后重试")

    from wzry.action.executor_v2 import ActionExecutor
    from wzry.control.executor import AdbExecutor
    ex = ActionExecutor(AdbExecutor(event_log=ROOT / "data" / "calib_stick_events.jsonl"))

    print("\n开始推杆标定（Ctrl+C 可中断）...")
    t_start = time.time()
    push_s = 0.0
    burst = 0
    arrivals = []                # (累计推杆秒数, 登记时距锚点)
    run_min, run_min_t = 9.9, 0.0   # 当前"局部极小"跟踪
    paused = False

    def _reset_run():
        """复活(回泉水)后重新开始累计: 锚点回到泉水, 之前的抵达点全部作废。"""
        nonlocal push_s, run_min, run_min_t
        push_s = 0.0
        run_min, run_min_t = 9.9, 0.0
        arrivals.clear()

    try:
        while time.time() - t_start < args.seconds:
            f = frame()
            if f is None:
                continue
            # 阵亡: 暂停推杆, 等复活(复活必回泉水 -> 锚点重新有效)
            if paused:
                if _alive_bar(f):
                    paused = False
                    _reset_run()
                    print(f"  [复活] 血条恢复 -> 从泉水重新累计 (t=0)")
                else:
                    time.sleep(0.5)
                continue
            if _dead(f):
                paused = True
                burst = 0
                print(f"  杆{burst}: 检出阵亡横幅 -> 暂停推杆, 等复活")
                continue

            burst += 1
            ex.move(theta, args.r, BURST_MS)
            push_s += BURST_MS / 1000.0
            time.sleep(0.15)
            f = frame()
            if f is None:
                continue
            mm = tracker.update(f)
            ax, ay = hx, hy
            try:
                from wzry.vision.self_bars import self_hp_mp
                _hp, _mp, _pos = self_hp_mp(f)
                if _pos:
                    ax, ay = _pos[0] / w, _pos[1] / h
            except Exception:
                pass
            best = _nearest_turret(det.detect(f), w, h, ax, ay)
            if not best:
                print(f"  杆{burst}: 累计 {push_s:5.1f}s | 无塔框")
                run_min, run_min_t = 9.9, push_s
                continue
            dist = best[0]
            # 局部极小值登记: 距离先探底、再抬升 >RISE_FRAC => 确认走过这座塔
            if dist < run_min:
                run_min, run_min_t = dist, push_s
            elif dist > run_min + RISE_FRAC:
                if run_min < ARRIVE_FRAC:
                    arrivals.append((run_min_t, run_min))
                    print(f"  杆{burst}: 累计 {push_s:5.1f}s | 登记抵达 #{len(arrivals)} "
                          f"@ {run_min_t:.1f}s (最近 {run_min:.3f})")
                run_min, run_min_t = 9.9, push_s
            print(f"  杆{burst}: 累计 {push_s:5.1f}s | 锚点({ax:.3f},{ay:.3f}) "
                  f"| 塔基 {best[1]} ({best[2]:.3f},{best[3]:.3f}) 距锚点={dist:.3f}"
                  f" | 极小={run_min:.3f}")
    except KeyboardInterrupt:
        print("\n中断。")
    finally:
        cap.stop()

    print("\n=== 结果 ===")
    if not arrivals:
        print("未检出任何塔框抵达 -> 标定失败")
        print("可能原因: 英雄不在泉水(锚点错)/方向撞墙/该方向塔已毁/英雄一直没走到塔")
        return
    n = min(len(arrivals), len(dists))
    print(f"抵达 {len(arrivals)} 次; 拟合用前 {n} 次 (塔线按由近到远对应):")
    for i in range(n):
        print(f"  #{i+1} 累计推杆 {arrivals[i][0]:5.1f}s  距泉水 {dists[i]:.3f}  "
              f"单点估 {dists[i] / max(0.01, arrivals[i][0]):.4f}")
    num = sum(arrivals[i][0] * dists[i] for i in range(n))
    den = sum(arrivals[i][0] ** 2 for i in range(n))
    est = num / den
    print(f"\nSTICK_SPEED(r={args.r}, 过原点最小二乘) = {num:.3f} / {den:.3f} = {est:.4f}")
    print("（当前常量 0.045）")
    if n >= 2:
        res = max(abs(dists[i] - est * arrivals[i][0]) for i in range(n))
        print(f"最大残差 {res:.3f} (= {100.0 * res / dists[n - 1]:.0f}% 最远点) "
              f"-> 越小越可信; 残差大=中途撞墙/锚点错/抵达序列错位")
    else:
        print("只有 1 个抵达点: 单点噪声大(且可能是第 2 座塔) -> 建议再跑一次拿 2-3 点")


if __name__ == "__main__":
    main()
