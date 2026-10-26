# -*- coding: utf-8 -*-
"""E2 迷雾表型探针：比较内存位置与小地图可见性。

目的：判定 12.1.1.1 中 actor 位置在敌方进雾后属于哪一类：
  - visible：小地图能看到，且内存位置与红点匹配
  - unseen_moving：小地图看不到，但内存位置仍在移动（候选真值）
  - unseen_static：小地图看不到，内存位置也不动（可能冻结/真静止）
  - no_fit：盟友点不足，无法拟合内存→小地图变换

方法：用己方英雄（永远可见）拟合 2D 仿射变换，再把敌方内存位置投到
小地图坐标系，与检测到的红点最近邻比较。只读内存 + 截屏；不含 KPM/perf/注入。

用法：
  python reverse/fog_probe.py --self-cid 106 --duration 60 --hz 2
  python reverse/fog_probe.py --self-camp blue --duration 30
  python reverse/fog_probe.py --selftest
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.capture.screencap_capture import ScreencapCapture  # noqa: E402
from wzry.vision.mem_source import MemSource  # noqa: E402
from wzry.vision.mm_tracker_v7 import MMTrackerV7  # noqa: E402


def fit_affine(mem_pts: List[Tuple[float, float]],
               dot_pts: List[Tuple[float, float]],
               max_points: int = 4):
    """枚举匹配，拟合 screen = [x,y,1] @ A；返回 (A, mean_residual, k) 或 None。"""
    if len(mem_pts) < 3 or len(dot_pts) < 3:
        return None
    best = None
    k_max = min(len(mem_pts), len(dot_pts), max_points)
    for k in range(k_max, 2, -1):
        for mi in itertools.combinations(range(len(mem_pts)), k):
            for di in itertools.permutations(range(len(dot_pts)), k):
                X = np.array([[mem_pts[i][0], mem_pts[i][1], 1.0] for i in mi])
                Y = np.array([dot_pts[j] for j in di])
                A, *_ = np.linalg.lstsq(X, Y, rcond=None)
                res = float(np.sqrt(((X @ A - Y) ** 2).sum(axis=1)).mean())
                if best is None or res < best[1]:
                    best = (A, res, k)
        if best is not None and best[1] < 0.03:
            break
    return best


def apply_affine(A: np.ndarray, p: Tuple[float, float]) -> Tuple[float, float]:
    v = np.array([p[0], p[1], 1.0]) @ A
    return float(v[0]), float(v[1])


def nearest_dist(p: Tuple[float, float],
                 pts: List[Tuple[float, float]]) -> float:
    if not pts:
        return 9.9
    return min(float(np.hypot(p[0] - q[0], p[1] - q[1])) for q in pts)


def mm_points(result: dict, key: str) -> List[Tuple[float, float]]:
    out = []
    for d in (result.get("v7") or {}).get(key) or []:
        n = d.get("n")
        if n and len(n) == 2:
            out.append((float(n[0]), float(n[1])))
    return out


def choose_camp(units, result: dict, max_residual: float = 0.06):
    """未显式给 camp 时，用两组记忆点分别拟合，取残差小的阵营。"""
    dots = mm_points(result, "self") + mm_points(result, "ally")
    if len(dots) < 3:
        return None
    best = None
    for camp in ("blue", "red"):
        pts = [(u.minimap_norm()[0], u.minimap_norm()[1])
               for u in units if u.is_hero and u.camp == camp
               and u.x is not None and u.z is not None]
        fit = fit_affine(pts, dots)
        if fit and fit[1] <= max_residual:
            if best is None or fit[1] < best[1]:
                best = (camp, fit[1])
    return best[0] if best else None


class FogTracker:
    def __init__(self, match_thresh: float = 0.06,
                 move_thresh: float = 0.01, jump_thresh: float = 0.20):
        self.match_thresh = match_thresh
        self.move_thresh = move_thresh
        self.jump_thresh = jump_thresh
        self.prev: Dict[int, Tuple[float, float, int, str]] = {}
        self.records: List[dict] = []
        self.fit_residuals: List[float] = []

    def update(self, ts: float, units, result: dict, self_camp: str):
        ally_mem = [(u.minimap_norm()[0], u.minimap_norm()[1])
                    for u in units if u.is_hero and u.camp == self_camp
                    and u.x is not None and u.z is not None]
        ally_dot = (mm_points(result, "self")
                    + mm_points(result, "ally"))
        fit = fit_affine(ally_mem, ally_dot)
        A, residual = (fit[0], fit[1]) if fit else (None, None)
        if residual is not None:
            self.fit_residuals.append(residual)
        enemy_dots = mm_points(result, "enemy")

        frame_recs = []
        for u in units:
            if not u.is_hero or u.camp == self_camp:
                continue
            if u.x is None or u.z is None:
                continue
            if u.dead:
                frame_recs.append(self._rec(ts, u, None, None, None, None,
                                            None, "dead", 0.0, residual))
                continue
            mm = u.minimap_norm()
            prev = self.prev.get(u.actor)
            delta = (float(np.hypot(mm[0] - prev[0], mm[1] - prev[1]))
                     if prev else 0.0)
            self.prev[u.actor] = (mm[0], mm[1], u.config_id, u.camp)
            if A is None:
                frame_recs.append(self._rec(ts, u, mm, None, None, None,
                                            None, "no_fit", delta, residual))
                continue
            pred = apply_affine(A, (mm[0], mm[1]))
            d = nearest_dist(pred, enemy_dots)
            visible = d <= self.match_thresh
            if visible:
                state = "visible"
            elif delta >= self.jump_thresh:
                state = "unseen_jump"
            elif delta >= self.move_thresh:
                state = "unseen_moving"
            else:
                state = "unseen_static"
            frame_recs.append(self._rec(ts, u, mm, pred, d, visible,
                                        len(enemy_dots), state, delta, residual))
        self.records.extend(frame_recs)
        return frame_recs

    @staticmethod
    def _rec(ts, u, mm, pred, d, visible, n_dots, state, delta, residual):
        return {
            "ts": round(ts, 3), "actor": u.actor, "cid": u.config_id,
            "camp": u.camp, "hp": u.hp, "dead": bool(u.dead),
            "mem_mm": [round(mm[0], 4), round(mm[1], 4)] if mm else None,
            "pred_mm": [round(pred[0], 4), round(pred[1], 4)] if pred else None,
            "nearest_dot_dist": round(d, 4) if d is not None else None,
            "visible": visible, "enemy_dots": n_dots,
            "state": state, "mem_delta": round(delta, 4),
            "fit_residual": round(residual, 4) if residual is not None else None,
        }

    def summary(self) -> dict:
        from collections import Counter
        return {
            "frames": len({r["ts"] for r in self.records}),
            "records": len(self.records),
            "state_counts": dict(Counter(r["state"] for r in self.records)),
            "by_enemy": {
                f"{r['camp']}:{r['cid']}": dict(Counter(
                    x["state"] for x in self.records
                    if x["cid"] == r["cid"] and x["camp"] == r["camp"]))
                for r in self.records
            },
            "fit_residual_mean": (round(float(np.mean(self.fit_residuals)), 4)
                                  if self.fit_residuals else None),
            "fit_residual_max": (round(float(np.max(self.fit_residuals)), 4)
                                 if self.fit_residuals else None),
            "max_mem_delta": (round(max(r["mem_delta"] for r in self.records), 4)
                              if self.records else None),
        }


def _selftest() -> int:
    ok = True

    def chk(name, cond, extra=""):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))
        ok = ok and cond

    # 己方 4 点 + 一个仿射（含翻转/缩放/平移）
    mem = [(-0.8, -0.8), (-0.8, -0.2), (-0.2, -0.8), (-0.2, -0.2)]
    A_true = np.array([[0.0, -0.6], [0.6, 0.0], [0.8, 0.8]])
    dots = [apply_affine(A_true, p) for p in mem]
    fit = fit_affine(mem, list(reversed(dots)))
    chk("affine fit survives shuffle", fit is not None
        and fit[1] < 1e-6, str(fit[1]) if fit else "none")
    if fit:
        p = apply_affine(fit[0], (0.2, 0.2))
        q = apply_affine(A_true, (0.2, 0.2))
        chk("affine maps point", abs(p[0] - q[0]) < 1e-6
            and abs(p[1] - q[1]) < 1e-6, f"{p} vs {q}")

    chk("nearest visible", nearest_dist((0.5, 0.5), [(0.51, 0.5)]) < 0.06)
    chk("nearest invisible", nearest_dist((0.5, 0.5), [(0.7, 0.5)]) > 0.06)
    chk("no dots", nearest_dist((0.5, 0.5), []) > 1)

    class U:
        def __init__(self, camp, x, z):
            self.is_hero, self.camp, self.x, self.z = True, camp, x, z

        def minimap_norm(self):
            return [(self.x + 60000) / 120000.0,
                    (60000 - self.z) / 120000.0]

    blue = [U("blue", x, z) for x, z in
            [(-50000, -50000), (-50000, -30000),
             (-30000, -50000), (-30000, -30000)]]
    blue_dots = [{"n": list(apply_affine(A_true, u.minimap_norm()))}
                 for u in blue]
    red = [U("red", x, z) for x, z in
           [(50000, 50000), (50000, 30000), (30000, 50000),
            (30000, 30000), (10000, 10000)]]
    result = {"v7": {"self": [], "ally": blue_dots, "enemy": []}}
    chk("auto camp chooses blue",
        choose_camp(blue + red, result) == "blue")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="127.0.0.1:16384")
    ap.add_argument("--self-cid", type=int, default=None)
    ap.add_argument("--self-camp", choices=["blue", "red"], default=None)
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--hz", type=float, default=2.0)
    ap.add_argument("--match-thresh", type=float, default=0.06)
    ap.add_argument("--move-thresh", type=float, default=0.01)
    ap.add_argument("--jump-thresh", type=float, default=0.20)
    ap.add_argument("--out", default=None)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return _selftest()
    if a.hz <= 0 or a.duration <= 0:
        print("--hz/--duration 必须 > 0")
        return 2

    src = MemSource(serial=a.serial)
    pid = src.connect()
    self_camp = a.self_camp
    camp_src = "arg" if self_camp else "unknown"
    if self_camp is None and a.self_cid is not None:
        from wzry.vision.camp_identity import resolve_self
        res = resolve_self(src.snapshot(), a.self_cid)
        if res.camp:
            self_camp, camp_src = res.camp, res.camp_src
        elif res.match == "ambiguous":
            print(f"self-cid={a.self_cid} 有多个同 cid 实例且阵营不一致，"
                  f"放弃 cid 判定，改用小地图拟合")
        else:
            print(f"self-cid={a.self_cid} 不在当前英雄列表")
    if self_camp is None:
        # 先用一帧小地图自动判定；失败则继续尝试，不直接退出。
        print("self_camp 未指定，将在运行时自动判定")

    cap = ScreencapCapture(serial=a.serial, interval=max(0.05, 0.5 / a.hz))
    tracker = MMTrackerV7()
    fog = FogTracker(a.match_thresh, a.move_thresh, a.jump_thresh)
    out = Path(a.out) if a.out else (
        ROOT / "reverse" / "out" / "diag"
        / f"fog_probe_{pid}_{datetime.now():%Y%m%d_%H%M%S}.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    csv_path = out.with_suffix(".csv")
    summary_path = out.with_suffix(".summary.json")
    print(f"pid={pid} self_camp={self_camp} out={out}")

    cap.start()
    frames = 0
    t_end = time.time() + a.duration
    try:
        with out.open("w", encoding="utf-8", newline="\n") as fj, \
                csv_path.open("w", encoding="utf-8", newline="") as fc:
            writer = None
            while time.time() < t_end:
                t0 = time.time()
                frame, _lat = cap.wait_frame(timeout=2.0)
                if frame is None:
                    print("[fog] no frame")
                    break
                result = tracker.update(frame)
                if not result.get("found"):
                    print("[fog] minimap not found")
                    time.sleep(max(0.0, 1.0 / a.hz - (time.time() - t0)))
                    continue
                try:
                    units = src.snapshot()
                except Exception as exc:
                    print(f"[fog] memory read failed: {exc}")
                    break
                if self_camp is None:
                    self_camp = choose_camp(units, result)
                    if self_camp is None:
                        print("[fog] cannot auto-detect self camp; skip frame")
                        time.sleep(max(0.0, 1.0 / a.hz - (time.time() - t0)))
                        continue
                    camp_src = "minimap_fit"
                    print(f"[fog] auto self_camp={self_camp} (minimap fit)")
                recs = fog.update(time.time(), units, result, self_camp)
                for r in recs:
                    fj.write(json.dumps(r, ensure_ascii=False) + "\n")
                    if writer is None:
                        writer = csv.DictWriter(fc, fieldnames=list(r.keys()))
                        writer.writeheader()
                    writer.writerow(r)
                frames += 1
                if frames % max(1, int(a.hz)) == 0:
                    vis = sum(1 for r in recs if r["state"] == "visible")
                    print(f"frame={frames} enemies={len(recs)} visible={vis} "
                          f"res={result.get('v7', {}).get('enemy') and len(result['v7']['enemy'])}")
                time.sleep(max(0.0, 1.0 / a.hz - (time.time() - t0)))
    except KeyboardInterrupt:
        print("interrupted")
    finally:
        cap.stop()

    summary = {"pid": pid, "self_camp": self_camp, "self_camp_src": camp_src,
               "frames": frames, "out": str(out), **fog.summary()}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    print(f"summary={summary_path}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
