# -*- coding: utf-8 -*-
"""laya-fusion-plan P1 门槛·离线可执行部分:
  1) Laya 真实推理 p95 < 80ms (模型卡: T4 ~40ms / CPU 193-464ms; 本机 4060 Laptop)
  2) 异常数 = 0 (LayaScorer.stats.errors)
  3) 主线程开销: reorder()/log_chosen() 单次耗时 (代理"帧率下降"中 scorer 占比;
     真机帧率对比仍需 P0 环境跑 m2 --laya shadow 前后各一场)

不依赖模拟器: 用合成 GameState 以 solve() 热路径驱动 LayaScorer(shadow/ensemble)。
模型: laya 包 Router 默认(HF Convaiinnovations/checkpoint, 首次运行自动下载)。

运行: venv/Scripts/python.exe -X utf8 scripts/probe_laya_p1.py [--seconds 30]
      [--mode ensemble] [--beta 0.5]
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")  # CN 网络下 HF 直连不稳
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.decision import optimizer                       # noqa: E402
from wzry.decision.laya_scorer import LayaScorer          # noqa: E402


def make_state(hp=0.9, n_red=2):
    """合成 GameState: 红点/蓝点/屏内敌英, 驱动 solve 的完整候选路径。"""
    units = [{"cls": "enemy_hero", "screen": [0.60, 0.52]},
             {"cls": "enemy_minion", "screen": [0.62, 0.58]}]
    return {
        "minimap": {"found": True, "dots": {
            "red": [[0.70, 0.75], [0.55, 0.60]][:n_red],
            "blue": [[0.55, 0.50], [0.45, 0.45]],
            "green": [[0.50, 0.50]]}},
        "units": units,
        "ui": {"hp": hp, "mp": 0.8},
    }


def main():
    ap = argparse.ArgumentParser(description="P1 offline gate probe")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--mode", default="ensemble", choices=["shadow", "ensemble"])
    ap.add_argument("--beta", type=float, default=0.5)
    ap.add_argument("--hz", type=float, default=10.0, help="决策频率( mimic m2 10Hz)")
    args = ap.parse_args()

    sc = LayaScorer(mode=args.mode, beta=args.beta,
                    shadow_path=ROOT / "runs" / "laya" / "shadow_p1probe.jsonl")
    optimizer.set_scorer(sc, beta=args.beta, mode=args.mode)
    # 计时探针: worker 侧真实推理耗时 + 主线程 reorder 耗时
    # (推理 456ms >> 决策周期 100ms 时 pending 回填常被顶掉, shadow 记录的 latency 不可靠)
    _real_predict, pred_ms = sc._predict, []
    def _timed_predict(state_text, labels):
        t0 = time.time()
        try:
            return _real_predict(state_text, labels)
        finally:
            pred_ms.append((time.time() - t0) * 1000)
    sc._predict = _timed_predict
    _real_reorder, re_ms = sc.reorder, []
    def _timed_reorder(*a, **k):
        t0 = time.time()
        try:
            return _real_reorder(*a, **k)
        finally:
            re_ms.append((time.time() - t0) * 1000)
    sc.reorder = _timed_reorder
    sc.start()
    print(f"[probe] 等待模型加载(laya Router 首次可能下载 checkpoint)...")
    t0 = time.time()
    while not sc.stats()["model_ok"] and sc.stats()["errors"] == 0 \
            and time.time() - t0 < 300:
        time.sleep(1.0)
    st0 = sc.stats()
    if not st0["model_ok"]:
        print(f"[probe] 模型加载失败: {st0['last_error']}")
        sc.stop()
        return 1
    print(f"[probe] 模型就绪 {time.time()-t0:.0f}s")

    fb = {"type": "map_move", "nx": 0.5, "ny": 0.5, "reason": "rule_follow"}
    n = 0
    reorder_ms = []
    solve_ms = []
    t_end = time.time() + args.seconds
    while time.time() < t_end:
        st = make_state(hp=0.9 if n % 7 else 0.4, n_red=1 + n % 3)
        ta = time.time()
        optimizer.solve(st, dict(fb), 0.9, "blue")
        solve_ms.append((time.time() - ta) * 1000)
        n += 1
        time.sleep(1.0 / args.hz)

    # 等一拍让 worker 收尾, 收集延迟样本(shadow 记录含 latency_ms)
    time.sleep(2.0)
    stats = sc.stats()
    sc.stop()
    lat = sorted(pred_ms)
    p50 = round(lat[len(lat) // 2], 1) if lat else None
    p95 = round(lat[int(0.95 * (len(lat) - 1))], 1) if lat else None
    p95_re = sorted(re_ms)[int(0.95 * (len(re_ms) - 1))] if re_ms else 0
    p95_solve = sorted(solve_ms)[int(0.95 * (len(solve_ms) - 1))] if solve_ms else 0

    print(f"\n[probe] 决策 {n} 次 | 推理 {stats['predicts']} | 缓存命中 {stats['cache_hits']}")
    print(f"[probe] Laya 推理延迟: p50={p50}ms p95={p95}ms (样本 {len(lat)}) "
          f"-> {'PASS' if p95 is not None and p95 < 80 else 'FAIL'} (门槛 <80ms)")
    print(f"[probe] 主线程开销: solve p95={p95_solve:.2f}ms, 其中 reorder p95={p95_re:.2f}ms "
          f"(scorer 占比 {'-' if not p95_solve else round(100*p95_re/p95_solve,1)}%)")
    print(f"[probe] 异常数: {stats['errors']} -> "
          f"{'PASS' if stats['errors'] == 0 else 'FAIL'} (门槛 =0)")
    ok = (p95 is not None and p95 < 80 and stats["errors"] == 0)
    print(f"\n[probe] P1 离线部分: {'PASS' if ok else 'FAIL'} "
          f"(帧率对比需真机: m2 --laya shadow 前后各跑一场)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
