# -*- coding: utf-8 -*-
"""面板按钮 scan-tap 标定（v15.3）：点一下 -> 截图 -> 判面板 -> 记录按钮坐标。

为什么反过来做：
  监听式取证（grab_panel.py）要求"人正好点开面板"，太被动（实测卡在结算页四分钟）。
  但面板按钮坐标是**对局 HUD 的绝对坐标**，标定它的本质就是"试点 + 看反馈"——
  那就由脚本自己点：对每个候选点，比较 **点击前/后** 两帧的**结构分**判定它是不是面板按钮。

判据（**以结构分为准**，diff 只当线索）：
  1) 结构分 panel_open_norm（wzry/uiquery/panel）：把"亮卡片面板（商店/属性/结算，中央亮+四周暗）"
     和"战绩面板（深蓝暗遮罩）"归一化到同一个标量，>=1.0 即"面板打开"。
     真机教训：对局里镜头一划 diff 就能到 50，**只靠 diff 判定必然误报**；
     而结构分是瞬时的、与镜头无关（对局 ~0.25，面板 ~1.3）。
  2) 闲置抖动：点击前先量一段"不操作的天然变化"当基线，只给 diff 兜底用。
  3) 关闭验证：开成功后再把它关回去（先试同点，再试 --close-points 给的关闭键候选）。
     真机教训：比分面板的打开键**不是 toggle** —— 开着时再点同一个点，结构分仍是 1.28；
     必须点右上角的 ✕（真机量到 (1218,93)）。谁把面板关掉了，就把它记成 ui_taps.close。
  4) 开工前先查"面板是不是已经开着"：真机踩到上一轮留下的面板没关 -> 整轮试点全白跑。

安全（四道闸，默认全部收紧）：
  - **不下发点击**除非加 --confirm-in-battle：不加参数时脚本只拍基线帧 + 打印状态就退出。
    为什么不自动判断"是否在对局"：试过 minimap.find_minimap 当对局判据 —— 它在真实
    对局帧上 found=False、却在选英雄页 found=True，判错了会更危险（在结算/大厅乱点）。
    所以这里不做伪检测，交给人拍板。
  - 基线帧若已是"面板打开"状态 -> 直接拒绝运行（return 6），不产生垃圾结论
  - 任何一次试点若判为"打开了面板"，立刻**再点同一位置**尝试复原（幂等回退）
  - 默认**不写盘**（只打印 + 存图）；人工确认帧内容后加 --write 才落 configs

用法：
    venv\\Scripts\\python.exe scripts\\probe_ui_tap.py                      # 只看基线帧（安全）
    venv\\Scripts\\python.exe scripts\\probe_ui_tap.py --confirm-in-battle  # 对局内才试点
    venv\\Scripts\\python.exe scripts\\probe_ui_tap.py --confirm-in-battle --write
    venv\\Scripts\\python.exe scripts\\probe_ui_tap.py --close-at 1197,90   # 只点一下关面板
    venv\\Scripts\\python.exe scripts\\probe_ui_tap.py --points "862,28 1098,34"
"""
import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.uiquery.panel import (  # noqa: E402
    load_cfg, panel_open_norm, panel_open_score, scoreboard_open_score,
)

OUT = ROOT / "runs" / "detect" / "live"
CALIB_PATH = ROOT / "configs" / "calibration_absolute.json"

# 对局顶部"KDA/比分"区的候选点（1280x720 帧空间，真机 11:12 帧上量出来的）：
#   对局时间 ~(865,27) | 比分 1vs0 ~(862,41) | 击杀/死亡/助攻 ~(965..1065,58)
#   三横线"≡"列表图标 ~(1098,34) | 齿轮 ~(1152,34) | 金币 336 ~(1215,35)
# 说明：不同分辨率/皮肤下位置有差，所以给一组候选由脚本逐个试、用反馈定音 —— 不靠猜。
# 真机结论：比分面板的打开键就是**对局时间/比分那块**（(862,28) 一次就打开了）。
CANDIDATES = {
    "scoreboard": [(862, 28), (862, 41), (1098, 34), (848, 60), (965, 58)],
}
# 关闭键候选（真机在 1280x720 帧上量出来）：比分面板右上角的白 ✕ = (1218,93)，
#   已验证：点它 结构分 1.28 -> 0.39（关掉了）。打开键**不是** toggle，所以必须给这个。
CLOSE_CANDIDATES = {
    "scoreboard": [(1218, 93)],
}

V_PANEL = "panel"
V_CHANGED = "changed"
V_NONE = "none"

# 只有 V_PANEL（结构分跳变）才算"打开了面板"，需要回弹验证
OPENED = (V_PANEL,)


def panel_kind(frame):
    """给存证帧标个类型（供人判）：亮卡片面板 / 战绩面板 / 都不是。"""
    if scoreboard_open_score(frame) >= 0.62:
        return "战绩面板(深蓝暗遮罩)"
    if panel_open_score(frame) >= 0.18:
        return "亮卡片面板"
    return "都不是"


def judge_tap(s0, s1, diff, thr=1.0, delta_thr=0.5, diff_thr=18.0,
              idle_diff=0.0, idle_k=2.0):
    """一次试点的判定（纯函数，便于离线单测）。返回 (verdict, 说明)。

    s0/s1 是 panel_open_norm 的归一化结构分（>=1.0 即"面板打开"），**以它为准**：
      真机踩到：对局里镜头一划 diff 就能上 50，单看 diff 会把镜头移动当成"打开了面板"。
    闲置抖动 idle_diff：点击前不操作、同样间隔量到的天然变化，只为给 diff 判定兜底。
    """
    s0, s1, diff = float(s0), float(s1), float(diff)
    d = s1 - s0
    if s1 >= float(thr) and d >= float(delta_thr):
        return V_PANEL, f"结构分达标 {s0:.2f}->{s1:.2f}（Δ{d:+.2f}, 阈值{thr}）"
    need = max(float(diff_thr), float(idle_k) * float(idle_diff))
    if diff >= need:
        return V_CHANGED, (f"画面剧变 diff={diff:.1f}（≥{need:.1f}）但结构分只 {s1:.2f}"
                           f"（阈值 {thr}）")
    return V_NONE, f"无反应（结构分 {s1:.2f} 未达 {thr}, diff={diff:.1f}）"


def frame_diff(a, b):
    """两帧平均差（缩到 320x180 灰度，抗噪声）。"""
    if a is None or b is None or a.shape != b.shape:
        return 0.0
    ga = cv2.cvtColor(cv2.resize(a, (320, 180)), cv2.COLOR_BGR2GRAY).astype(np.int16)
    gb = cv2.cvtColor(cv2.resize(b, (320, 180)), cv2.COLOR_BGR2GRAY).astype(np.int16)
    return float(np.abs(ga - gb).mean())


def fresh_frame(cap, last, timeout=2.0, budget=3.0):
    """等一张**新**帧。wait_frame 在没有新帧时会返回同一个缓存对象 -> 必须按对象身份去重。

    坑（真机踩到）：wait_frame 在缓存 1s 内会**立刻返回同一个对象**而不阻塞，
    所以不能只"重试固定次数"（6 次 × 0.05s ≈ 0.3s，比采集间隔 0.25s 还短，
    会误判"取帧失败"并白白跳过候选点）。改成按**时间预算**等。
    """
    t_end = time.time() + budget
    while time.time() < t_end:
        f, _t = cap.wait_frame(timeout=timeout)
        if f is not None and f is not last:
            return f
        time.sleep(0.03)
    return None


def _digits():
    """复用 grab_panel 的数字块定位（面板是大字静态画面，供定 panel_rois）。"""
    try:
        spec = importlib.util.spec_from_file_location("grab_panel", ROOT / "scripts" / "grab_panel.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m.digits_in
    except Exception as e:
        print(f"  [数字块] 模块加载失败: {type(e).__name__}: {e}")
        return lambda _f: []


def write_taps(panel, xy, close_xy=None):
    """把按钮坐标写进 configs/calibration_absolute.json 的 ui_taps 段（保留原内容）。"""
    d = json.loads(CALIB_PATH.read_text(encoding="utf-8-sig"))
    taps = d.setdefault("ui_taps", {})
    taps[panel] = [int(xy[0]), int(xy[1])]
    if close_xy:
        taps["close"] = [int(close_xy[0]), int(close_xy[1])]
    CALIB_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return taps


def main():
    ap = argparse.ArgumentParser(description="面板按钮 scan-tap 标定")
    ap.add_argument("--panel", default="scoreboard", choices=sorted(CANDIDATES))
    ap.add_argument("--points", default=None, help='自定义候选点，如 "1200,45 1245,30"')
    ap.add_argument("--wait", type=float, default=0.7, help="点击后等待秒数（等 UI 动画落定）")
    ap.add_argument("--thr", type=float, default=1.0, help="panel_open_norm 达标阈值（>=1.0 即打开）")
    ap.add_argument("--delta-thr", type=float, default=0.5, help="结构分相对基线的跳变阈值")
    ap.add_argument("--diff-thr", type=float, default=18.0, help="画面剧变阈值（只作兜底线索）")
    ap.add_argument("--write", action="store_true", help="把确认后的按钮坐标写进 configs")
    ap.add_argument("--confirm-in-battle", action="store_true",
                    help="确认游戏在正常对局内；不加则只拍基线帧、不下发任何点击")
    ap.add_argument("--close-at", default=None, metavar="X,Y",
                    help="收尾用：只点这一个点（关掉卡住的面板），不做试点")
    ap.add_argument("--close-points", default=None, metavar='"X,Y X,Y"',
                    help="关闭键候选（真机量到比分面板 ✕ 在 1218,93）；开了面板后按顺序试它们")
    ap.add_argument("--no-require-toggle", action="store_true",
                    help="不要求'同点能关掉'也算数（默认要求，用于排除设置菜单）")
    ap.add_argument("--serial", default=None)
    args = ap.parse_args()

    pts = CANDIDATES[args.panel]
    if args.points:
        pts = []
        for tok in args.points.replace(";", " ").split():
            x, y = tok.split(",")
            pts.append((int(x), int(y)))

    close_pts = list(CLOSE_CANDIDATES.get(args.panel, []))
    if args.close_points:
        close_pts = []
        for tok in args.close_points.replace(";", " ").split():
            x, y = tok.split(",")
            close_pts.append((int(x), int(y)))

    cfg_taps, _ = load_cfg()
    print(f"当前 ui_taps: {cfg_taps or '（未标定）'}")

    from wzry.capture.screencap_capture import ScreencapCapture
    from wzry.control.executor import AdbExecutor
    cap = ScreencapCapture(**(dict(serial=args.serial) if args.serial else {}))
    cap.start()
    ex = AdbExecutor(serial=cap.serial)
    OUT.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%H%M%S")
    digits_in = _digits()

    print(f"设备 {cap.serial} | 尝试 {len(pts)} 个候选点（panel={args.panel}）")
    print(">>> 请确认游戏在【正常对局内】！不在对局里这些坐标不会有效果。")

    hits = []
    try:
        pre = fresh_frame(cap, None)
        if pre is None:
            print("拿不到画面（模拟器/采集异常）")
            return 3
        s_pre = panel_open_norm(pre)
        print(f"基线帧：均值亮度 {cv2.cvtColor(pre, cv2.COLOR_BGR2GRAY).mean():.1f}  "
              f"结构分={s_pre:.2f}（亮卡片 {panel_open_score(pre):.3f}"
              f" / 战绩深蓝 {scoreboard_open_score(pre):.3f}）"
              f"  -> {OUT / f'probe_pre_{ts}.png'}")
        cv2.imwrite(str(OUT / f"probe_pre_{ts}.png"), pre)

        if args.close_at:
            cx, cy = [int(v) for v in args.close_at.split(",")]
            print(f"\n收尾模式：只点 ({cx},{cy}) 关面板（不做试点）…")
            ex.tap(cx, cy, source="probe_ui_tap")
            time.sleep(args.wait)
            back = fresh_frame(cap, pre)
            print(f"  点后结构分 = "
                  f"{panel_open_norm(back) if back is not None else float('nan'):.2f}"
                  f"（<1.0 即已关掉）")
            return 0

        if s_pre >= 1.0:
            # 真机踩到：上一轮"changed"判定不回收，把比分面板留在屏幕上，
            #   下一轮所有试点都在面板上点 -> 全部"无反应"，白跑一轮。
            print(f"\n⚠ 基线帧上面板**已经开着**（结构分 {s_pre:.2f} ≥ 1.0，类型："
                  f"{panel_kind(pre)}）—— 此时试点没有意义（点在面板上不会开新面板）。")
            print("  请先关掉（点右上角 ✕ / 再点一次打开键），或用："
                  f" --close-at X,Y 点一下关闭键。")
            return 6

        if not args.confirm_in_battle:
            print("\n保护模式：未加 --confirm-in-battle -> 不下发任何点击"
                  "（避免在结算/大厅/死亡回放页乱点）。")
            print(f"请确认上面这张基线帧是**正常对局**（有小地图、技能键、血条）。")
            print(f"确认后重跑： .\\venv\\Scripts\\python.exe scripts\\probe_ui_tap.py "
                  f"--panel {args.panel} --confirm-in-battle")
            return 5

        for i, (px, py) in enumerate(pts):
            before = fresh_frame(cap, pre)
            if before is None:
                print(f"  [{i}] ({px},{py}) 取帧失败，跳过")
                continue
            # 先量"闲置抖动"：不点击、同样间隔取两帧 —— 对局里镜头一直在动，
            #   固定 diff 阈值会把一次镜头划动(diff≈53, 真机实测)误判成"打开了面板"。
            idle_a = fresh_frame(cap, before)
            time.sleep(args.wait)
            idle_b = fresh_frame(cap, idle_a) if idle_a is not None else None
            if idle_b is None:
                idle_b, idle_diff = before, 0.0
            else:
                idle_diff = frame_diff(idle_a, idle_b)
            s_base = panel_open_norm(idle_b)
            if not ex.tap(px, py, source="probe_ui_tap"):
                print(f"  [{i}] ({px},{py}) 点击下发失败（adb 异常），跳过")
                continue
            time.sleep(args.wait)
            after = fresh_frame(cap, idle_b)
            if after is None:
                print(f"  [{i}] ({px},{py}) 点击后取帧失败，跳过")
                continue
            s1 = panel_open_norm(after)
            diff = frame_diff(idle_b, after)
            verdict, note = judge_tap(s_base, s1, diff, thr=args.thr,
                                      delta_thr=args.delta_thr, diff_thr=args.diff_thr,
                                      idle_diff=idle_diff)
            print(f"  [{i}] ({px:>4},{py:>4}) {verdict:<8} {note}  闲置抖动={idle_diff:.1f}")

            if verdict not in OPENED:
                if verdict == V_CHANGED:
                    # changed 是最需要人看的一类（可能是真面板、也可能是镜头/团战）-> 存证
                    tag = f"{args.panel}_changed_{px}_{py}_{ts}"
                    cv2.imwrite(str(OUT / f"probe_{tag}.png"), after)
                    cv2.imwrite(str(OUT / f"probe_before_{tag}.png"), idle_b)
                    print(f"       已存证（changed，需人工判）: probe_{tag}.png + probe_before_{tag}.png")
                pre = after
                continue

            # ---- 打开了面板：存证 + 回弹验证（同点再点一次看能否复原）----
            tag = f"{args.panel}_{verdict}_{px}_{py}_{ts}"
            cv2.imwrite(str(OUT / f"probe_{tag}.png"), after)
            big = cv2.resize(after, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
            cv2.imwrite(str(OUT / f"probe_zoom2_{tag}.png"), big)
            print(f"       面板类型：{panel_kind(after)}")
            print(f"       已存证帧: probe_{tag}.png (+ 2x 放大 probe_zoom2_{tag}.png)")
            for t in digits_in(after):
                print(f"       x={t[0]:4d} y={t[1]:4d} w={t[2]:3d} h={t[3]:3d} "
                      f"值={t[4]:<6} conf={t[5]}")

            # ---- 关闭验证：先试同点，再试"专门的关闭点" ----
            # 真机教训：比分面板的打开键(对局时间/比分那块) **不是 toggle** —— 开着时再点它
            #   关不掉（实测结构分仍是 1.28），必须点右上角的 ✕(真机量到 (1218,93))。
            #   所以这里按顺序试：同点 -> 各个 close_point，谁关掉了就记它当 ui_taps.close。
            closed, close_xy, back = False, None, None
            for j, cpt in enumerate([(px, py)] + list(close_pts)):
                way = "同点" if j == 0 else f"关闭点{j}"
                ex.tap(cpt[0], cpt[1], source="probe_ui_tap")
                time.sleep(args.wait)
                fr = fresh_frame(cap, back if back is not None else after)
                if fr is None:
                    print(f"       关闭尝试[{way}] ({cpt[0]},{cpt[1]}) 取帧失败，跳过")
                    continue
                s2 = panel_open_norm(fr)
                print(f"       关闭尝试[{way}] ({cpt[0]},{cpt[1]}) -> 结构分 {s2:.2f}"
                      f"（面板时 {s1:.2f}） {'已关掉' if s2 < 1.0 else '仍开着'}")
                back = fr
                if s2 < 1.0:
                    closed, close_xy = True, (int(cpt[0]), int(cpt[1]))
                    break
            if not closed:
                print("       ⚠ 没能关掉面板（它会一直挡着屏幕）——建议手动点 ✕，"
                      "或用 --close-points 给出关闭键候选。")
            pre = back if back is not None else after

            hits.append({"xy": (px, py), "verdict": verdict, "s0": s_base, "s1": s1,
                         "diff": diff, "closed": closed, "close_xy": close_xy,
                         "frame": f"probe_{tag}.png"})
            if closed:
                break          # 找到了打开键、也关掉了 -> 收工（后面的候选没必要再点）

    finally:
        try:
            ex.close()
            cap.stop()
        except Exception:
            pass

    # ---------------- 汇总 ----------------
    print("\n=== 汇总 ===")
    if not hits:
        print("没有任何候选点打开面板。"
              "可能原因：① 当前不在正常对局内（结算/大厅/死亡回放）；"
              "② 候选点位置不对（请给 --points 指定）。")
        print(f"基线帧已存 {OUT / f'probe_pre_{ts}.png'}，请确认画面状态。")
        return 2

    ok = [h for h in hits if h["closed"] or args.no_require_toggle]
    for h in hits:
        print(f"  ({h['xy'][0]},{h['xy'][1]}) {h['verdict']:<8} 已关掉={h['closed']}"
              f"  关闭点={h['close_xy']}  帧={h['frame']}")
    if not ok:
        print("候选点能打开 UI，但**没能关掉** -> 关闭键没给对（用 --close-points），"
              "或者这其实是设置菜单之类（不该记为 scoreboard）。")
        return 4

    best = ok[0]
    print(f"\n建议按钮：{args.panel} = {list(best['xy'])}；"
          f"close = {list(best['close_xy']) if best['close_xy'] else '（没找到）'}")
    if args.write:
        if not best["close_xy"]:
            print("没找到关闭点，拒绝写盘（否则面板会把屏幕锁死）。")
            return 4
        taps = write_taps(args.panel, best["xy"], close_xy=best["close_xy"])
        print(f"已写入 {CALIB_PATH} -> ui_taps: {taps}")
    else:
        print("干跑结束（未写盘）。确认存证帧确实是【比分面板】后，加 --write 重跑。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
