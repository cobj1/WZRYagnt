# -*- coding: utf-8 -*-
"""顶部头像血条条带自标定：从一张真机【对局内】帧里找出敌方/队友血条所在的 y 条带。

背景：顶部头像条在整个链路上从没被读过（self_bars 的 EXCLUDE_Y_TOP=240 直接排除了
顶部 HUD）。像素位置随机型/分辨率变化，所以不写死，改为用本脚本扫描候选条带，
把命中结果写回 configs/calibration_absolute.json 的 "top_bar"。

判定标准：候选条带内，红条（敌）或蓝条（友）的数量 >= --min-bars，且 x 间距相对均匀
（同一行头像条特征），取"条数最多 + 间距最均匀"的条带。

用法：
    venv\\Scripts\\python.exe scripts\\calibrate_top_bar.py runs\\detect\\live\\xxx.png
    venv\\Scripts\\python.exe scripts\\calibrate_top_bar.py <帧图> --slots 5 --write
    venv\\Scripts\\python.exe scripts\\calibrate_top_bar.py <帧图> --frame-w 1280 --frame-h 720
"""
import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision.top_status_bars import (  # noqa: E402
    EMPTY_GRAY, _bar_masks, find_bars_in_strip, load_cfg, save_cfg)

FRAME_W, FRAME_H = 1280, 720      # 生产帧空间（screencap_capture 统一 resize 到这个尺寸）


def _ext(gray, b):
    """血条右侧的"空槽延伸"长度（px）。真血条坐在一个空槽里；静态 UI 条通常没有延伸。"""
    row = gray[b["y"] + b["h"] // 2]
    end = b["x"] + b["w"]
    while end < row.shape[0] and row[end] > EMPTY_GRAY:
        end += 1
    return int(end - (b["x"] + b["w"]))


def scan(frame, slots=5, min_bars=3, y_step=4, band_h=26,
         x_frac=(0.12, 0.88), slots_pitch_tol=0.55,
         min_ext=8, min_ext_frac=0.6, y_max_frac=0.12,
         colors=("red", "blue", "green"), allow_no_ext=False):
    """在整帧上扫描候选 y 条带，返回候选 [(score, cfg, bars, ok, info)]（ok 的排前面）。

    ⚠️ 只用"同色 + 等距"判定会把静态 UI 条（击杀栏/经济条）误当成头像条 ——
    实测在非对局帧上它就排到了第一（runs/detect/live/now_check.png）。
    ⚠️ 再加一条语义约束：本脚本标的是**顶部头像条**，所以只扫画面顶部
      y < y_max_frac*H（默认 12%）。**不扫**中部——否则会把场景里英雄头顶血条
      （实测真对局帧在 y=140/224 有成排红/蓝条）当成头像条写进标定。
    ok 判定（两项都过才置 True，main() 只对 ok=True 允许 --write）：
      **同色** 且 **多数条右侧有空槽延伸**（真血条坐在血槽里，静态 UI 条没有）。

    ⚠️ 真机校正（2026-10-15）：顶部 HUD 血槽背景**很暗**（灰度 < EMPTY_GRAY），
    所以"空槽延伸"判据在真机顶部条上恒为 0 —— 5 条等距同色蓝条也会被判成静态 UI 条
    而拒绝写盘（实测现象就是"未找到候选条带"）。故加 `allow_no_ext`：
      False（默认）-> 保留原硬门槛（合成帧回归 T4f 依赖它）；
      True          -> 延伸判据降级为**加分项**，主判据变成"同色 + 等距 + 条数"。
    """
    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    masks = _bar_masks(frame)
    x0, x1 = int(w * x_frac[0]), int(w * x_frac[1])
    cands = []
    for y0 in range(0, max(6, int(h * y_max_frac)), y_step):
        y1 = min(h, y0 + band_h)
        if y1 - y0 < 6:
            break
        bars = []
        for color in colors:
            for b in find_bars_in_strip(masks[color], y0, y1, x0, x1):
                b["color"] = color
                b["ext"] = _ext(gray, b)
                bars.append(b)
        if not bars:
            continue
        # 去重：同一 x 邻域只留最宽的
        bars.sort(key=lambda b: -b["w"])
        kept = []
        for b in bars:
            if all(abs(b["x"] - k["x"]) > 12 for k in kept):
                kept.append(b)
        if len(kept) < min_bars:
            continue
        kept.sort(key=lambda b: b["x"])
        # 局部聚类：头像血条是**等距成排的一小段**，同一条带里右侧 KDA/金币区的色块
        # 会把"间距均匀"判据整体污染（真机实测就是这样被刷掉的）-> 先按间距切成组。
        # 阈值用**全局最小间距**估（避免只有 1 条时把默认 40px 当 pitch，导致每条自成一簇）
        allg = [kept[i + 1]["x"] - kept[i]["x"] for i in range(len(kept) - 1)]
        mg = min(allg) if allg else 40.0
        thr = max(mg * 2.0, 18.0)
        groups, cur = [], [kept[0]]
        for b in kept[1:]:
            if b["x"] - cur[-1]["x"] <= thr:
                cur.append(b)
            else:
                if len(cur) >= min_bars:
                    groups.append(cur)
                cur = [b]
        if len(cur) >= min_bars:
            groups.append(cur)
        for grp in groups:
            gaps = [grp[i + 1]["x"] - grp[i]["x"] for i in range(len(grp) - 1)]
            if gaps:
                mu = sum(gaps) / len(gaps)
                var = max(abs(g - mu) for g in gaps) / max(mu, 1e-6)
            else:
                mu, var = 40.0, 1.0
            if var > slots_pitch_tol:
                continue
            wmean = sum(b["w"] for b in grp) / len(grp)
            # 真机顶部头像行不是严格单色：实测 5 条里会有 1~2 条偏红（残血/头像美术色），
            # 要求"全同色"会把真正那一行刷掉 -> 改成"主色占比 >= 0.6"。
            cnt = {}
            for b in grp:
                cnt[b["color"]] = cnt.get(b["color"], 0) + 1
            dom, dom_n = max(cnt.items(), key=lambda kv: kv[1])
            dom_frac = dom_n / len(grp)
            same_color = dom_frac == 1.0
            ext_frac = sum(1 for b in grp if b["ext"] >= min_ext) / len(grp)
            wid_ratio = max(b["w"] for b in grp) / max(1, min(b["w"] for b in grp))
            ok = bool(dom_frac >= 0.6 and (ext_frac >= min_ext_frac or allow_no_ext))
            score = (len(grp) * 2 + (2 if same_color else 0) + (6 if ok else 0)
                     + dom_frac * 2 - var * 2 + min(wmean, 120) / 120.0)
            # 条带框按**这一组**的实际范围推（含缺检的槽位）：不能用整条带的 x0/x1，
            # 否则 pitch=(x1-x0)/slots 会被拉成几百 px，read() 的槽位划分全错。
            pitch = mu
            gx0 = max(0, int(grp[0]["x"] - pitch * 0.35))
            gx1 = min(w, int(gx0 + max(slots, len(grp)) * pitch))
            info = {"same_color": same_color, "ext_frac": round(ext_frac, 2),
                    "wid_ratio": round(wid_ratio, 2),
                    "color": dom, "dom_frac": round(dom_frac, 2),
                    "widths": [b["w"] for b in grp],
                    "exts": [b["ext"] for b in grp],
                    "pitch": round(pitch, 1)}
            cands.append((round(score, 3),
                          {"x0": gx0, "x1": gx1, "y0": y0, "y1": y1, "slots": slots,
                           "color": dom},
                          grp, ok, info))
    cands.sort(key=lambda t: -t[0])
    return cands


def main():
    ap = argparse.ArgumentParser(description="顶部血条条带自标定")
    ap.add_argument("frame", help="一张真机对局内帧图")
    ap.add_argument("--slots", type=int, default=5, help="条带内头像数(5v5=5)")
    ap.add_argument("--min-bars", type=int, default=3)
    ap.add_argument("--frame-w", type=int, default=FRAME_W,
                    help="生产帧空间宽（缩放基准），默认 1280")
    ap.add_argument("--frame-h", type=int, default=FRAME_H)
    ap.add_argument("--top", type=int, default=5, help="打印前 N 个候选")
    ap.add_argument("--max-y-frac", type=float, default=0.12,
                    help="只扫画面顶部这个比例内的 y（默认 12%%；放宽会扫到场景血条）")
    ap.add_argument("--write", action="store_true",
                    help="把最优候选写回 calibration_absolute.json（仅当有候选通过校验）")
    ap.add_argument("--allow-no-ext", action="store_true",
                    help="放宽'空槽延伸'硬门槛（真机顶部 HUD 血槽很暗，延伸恒为 0，必须开）")
    ap.add_argument("--color", default="",
                    help="只扫指定颜色（red/blue/green）；默认三色都扫")
    args = ap.parse_args()

    img = cv2.imdecode(np.fromfile(args.frame, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        print("读不到图片:", args.frame)
        return 1
    h, w = img.shape[:2]
    if (w, h) != (args.frame_w, args.frame_h):
        img = cv2.resize(img, (args.frame_w, args.frame_h), interpolation=cv2.INTER_AREA)
        print(f"帧已缩放到 {args.frame_w}x{args.frame_h}（原 {w}x{h}）以对齐生产帧空间")

    print("当前标定:", load_cfg())
    cands = scan(img, slots=args.slots, min_bars=args.min_bars,
                 y_max_frac=args.max_y_frac,
                 colors=(args.color,) if args.color else ("red", "blue", "green"),
                 allow_no_ext=args.allow_no_ext)
    if not cands:
        print("未找到候选条带：这张图顶部没有成排的头像血条"
              "（可能不是对局内画面，或顶部条在更靠下的位置——"
              "改用 --max-y-frac 0.2 再试。注意放宽后会扫到场景里的英雄血条）。")
        return 2
    for score, cfg, bars, ok, info in cands[:args.top]:
        colors = {b["color"] for b in bars}
        print(f"{'OK ' if ok else '-- '}score={score}  y=[{cfg['y0']},{cfg['y1']})  "
              f"x=[{cfg['x0']},{cfg['x1']}]  条数={len(bars)}  色={colors}  "
              f"空槽延伸占比={info['ext_frac']}  宽比={info['wid_ratio']}")
        print("    位置(x,宽,y,色,延伸):", [(b["x"], b["w"], b["y"], b["color"], b["ext"])
                                            for b in bars])
        print("    宽度序列:", info["widths"], " 延伸序列:", info["exts"])
    passed = [c for c in cands if c[3]]
    if args.write:
        if not passed:
            print("没有通过校验的候选（需同色 + 多数条有空槽延伸）-> **拒绝写盘**，"
                  "避免把静态 UI 条误标成头像血条。请换一张明确的对局内截图重跑。")
            return 3
        cfg = passed[0][1]
        p = save_cfg(cfg)
        print("已写入", p, "->", cfg)
    elif not passed:
        print("（无候选通过校验：这些多半是静态 UI 条；请换一张对局内截图再来）")
    else:
        print(f"（有 {len(passed)} 个候选通过校验；确认最上面那条是头像血条后加 --write）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
