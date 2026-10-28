# -*- coding: utf-8 -*-
"""阵营标签时序投票重标注（离线，只动 `states.jsonl`，不碰运行时/决策）。

问题（`scripts/eval_camp_jitter.py` 量化）：
  25,266 个英雄单位里 **26.1% 的阵营来自 `camp_src=default`**（血条探针失败后的兜底）。
  相邻帧同一目标（IoU≥0.5）共 23,356 对，阵营翻转 **418 对 = 1.79%**，
  其中 **88.5% 的翻转涉及 `default`/`None`**，`default↔default` 独占 267 对
  —— 即兜底路径自身不稳定，把检测器的逐帧类别噪声直接写进了训练标签。

为什么可以安全改：`default` 单位在**下游已被按不可信过滤**
（`wzry/vision/hok_yolov5.py` "保守判敌 + 下游按不可信过滤"、`hero_hp.py:174`），
所以它们不参与决策。修标签不会改变 `refactor_gate` 的决策 digest。

做法：
  1. 同会话内按屏幕框 IoU 把单位串成**轨迹**（跨帧同一物理目标）；
  2. 轨迹内若有**可信来源**（`bar` / `color` / `memory`）的帧，取这些帧的多数票，
     覆盖该轨迹里 `default` / `None` 的帧；
  3. 整条轨迹都不可信时，取**全轨迹多数票**（消掉 default↔default 的来回翻）；
  4. 被改写的单位记 `camp_src = "temporal_vote"`，原值留在 `cls_raw` / `camp_src_raw`。

默认 **dry-run**，只打印统计；`--apply` 才写盘且写前备份 `*.bak-camp2`。

用法：
  venv/Scripts/python.exe -X utf8 scripts/relabel_camp_temporal.py
  venv/Scripts/python.exe -X utf8 scripts/relabel_camp_temporal.py --apply
  venv/Scripts/python.exe -X utf8 scripts/relabel_camp_temporal.py --session 20261020_224657_708566
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import time
from collections import Counter

HERO_CLS = {"ally_hero", "enemy_hero"}
TRUSTED_SRC = {"bar", "color", "memory"}


def iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def center(u) -> tuple[float, float]:
    x, y, w, h = u["screen"]
    return x + w / 2.0, y + h / 2.0


def build_tracks(frames: list[list[dict]], min_iou: float,
                 max_shift: float = 0.15) -> list[list[tuple[int, int]]]:
    """把每帧的英雄单位串成轨迹；返回 [[(frame_idx, unit_idx), ...], ...]。

    `max_shift` 是**中心位移门限**（归一化屏幕坐标）。只靠 IoU 会把"两个英雄交错走过"
    串成同一条轨迹，进而让多数票把两个人的阵营互相覆盖 —— 加位移门限可挡住这类误连。
    """
    tracks: list[list[tuple[int, int]]] = []
    prev_map: dict[int, int] = {}          # prev unit_idx -> track_id
    for fi, units in enumerate(frames):
        cur_map: dict[int, int] = {}
        for ui, u in enumerate(units):
            cx, cy = center(u)
            best_t, best_iou = None, min_iou
            for pui, tid in prev_map.items():
                if tid in cur_map.values():
                    continue
                px, py = center(frames[fi - 1][pui])
                if ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5 > max_shift:
                    continue
                v = iou(frames[fi - 1][pui]["screen"], u["screen"])
                if v >= best_iou:
                    best_t, best_iou = tid, v
            if best_t is None:
                best_t = len(tracks)
                tracks.append([])
            tracks[best_t].append((fi, ui))
            cur_map[ui] = best_t
        prev_map = cur_map
    return tracks


def flip_stats(frames: list[list[dict]], min_iou: float) -> tuple[int, int]:
    """相邻帧配对里的阵营翻转数 / 配对数（与 eval_camp_jitter 同口径）。"""
    pairs = flips = 0
    prev = None
    for cur in frames:
        if prev is not None:
            for a in prev:
                best, best_iou = None, min_iou
                for b in cur:
                    v = iou(a["screen"], b["screen"])
                    if v >= best_iou:
                        best, best_iou = b, v
                if best is None:
                    continue
                pairs += 1
                if a["cls"] != best["cls"]:
                    flips += 1
        prev = cur
    return flips, pairs


def relabel_session(path: str, min_iou: float, apply: bool,
                    max_shift: float = 0.15) -> dict:
    frames: list[list[dict]] = []
    raw: list[dict] = []
    for line in open(path, encoding="utf-8"):
        if not line.strip():
            raw.append({})
            continue
        d = json.loads(line)
        raw.append(d)
        frames.append([u for u in (d.get("units") or [])
                       if u.get("cls") in HERO_CLS and u.get("screen")])

    flips0, pairs0 = flip_stats(frames, min_iou)

    tracks = build_tracks(frames, min_iou, max_shift)
    changed = 0
    stats = Counter()
    for tr in tracks:
        if len(tr) < 2:
            continue
        cells = [(fi, ui, frames[fi][ui]) for fi, ui in tr]
        trusted = [u["cls"] for _, _, u in cells
                   if u.get("camp_src") in TRUSTED_SRC]
        pool = trusted or [u["cls"] for _, _, u in cells]
        vote = Counter(pool).most_common(1)[0][0]
        for fi, ui, u in cells:
            if u["cls"] == vote:
                continue
            # 只改写不可信来源的帧；可信帧保持原样（避免覆盖实测证据）
            if u.get("camp_src") in TRUSTED_SRC:
                stats["skip_trusted"] += 1
                continue
            stats["rewritten"] += 1
            changed += 1
            if apply:
                # ⚠️ **不改 `camp_src`**：`hero_hp.py` / 决策层按 `camp_src == "default"`
                # 过滤不可信框，若改写成 `temporal_vote` 这些单位会变成"可信"，
                # 回放决策随之改变、`refactor_gate` 的 digest 会破。
                # 所以只平滑 `cls`（训练标签），来源标记另存 `camp_vote`。
                u["cls_raw"] = u["cls"]
                u["camp_vote"] = "temporal"
                u["cls"] = vote
            else:
                # dry-run 也要改内存里的副本，才能算出"改后翻转率"
                u["cls"] = vote
    flips1, pairs1 = flip_stats(frames, min_iou)

    stats["changed"] = changed
    stats["tracks"] = len(tracks)
    stats["flips_before"], stats["pairs"] = flips0, pairs0
    stats["flips_after"] = flips1
    if apply and changed:
        # 备份带时间戳：不用固定名，避免"上次中断留下的旧备份"让这次跳过备份
        bak = f"{path}.bak-camp2-{time.strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(path, bak)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for d in raw:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        stats["backup"] = bak
    return dict(stats)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", default="data/matches/*/states.jsonl")
    ap.add_argument("--session", default=None, help="只处理某个会话 id")
    ap.add_argument("--min-iou", type=float, default=0.5)
    ap.add_argument("--max-shift", type=float, default=0.15,
                    help="相邻帧中心位移上限（归一化），防跨单位误连")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    paths = sorted(glob.glob(args.glob))
    if args.session:
        paths = [p for p in paths if args.session in p]
    if not paths:
        print("[ERR] 没有匹配的 states.jsonl")
        return 2

    tot = Counter()
    rows = []
    for p in paths:
        st = relabel_session(p, args.min_iou, args.apply, args.max_shift)
        tot.update(st)
        if st.get("changed"):
            rows.append((os.path.basename(os.path.dirname(p)), st))

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"模式: {mode}   会话: {len(paths)}")
    print(f"轨迹数            : {tot['tracks']}")
    print(f"改写单位数        : {tot['changed']}")
    print(f"跳过（可信来源帧）: {tot['skip_trusted']}")
    b, a, p = tot["flips_before"], tot["flips_after"], tot["pairs"]
    if p:
        print(f"\n阵营翻转（相邻帧 IoU≥{args.min_iou}）：")
        print(f"  改前 {b}/{p} = {b / p:.2%}")
        print(f"  改后 {a}/{p} = {a / p:.2%}"
              f"   → {'−%.1f%%' % (100 * (1 - a / b)) if b else '—'}")
    if rows:
        print("\n改写最多的 8 个会话：")
        for name, st in sorted(rows, key=lambda r: -r[1]["changed"])[:8]:
            print(f"  {name}  {st['changed']}  (翻转 {st['flips_before']}→{st['flips_after']})")
    if not args.apply:
        print("\n提示：这是 dry-run。加 --apply 才会写盘（自动备份 *.bak-camp2）。")
        print("      写盘后请重跑 `scripts/eval_camp_jitter.py` 复核。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
