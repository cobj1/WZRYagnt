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
    # v14.2 默认 0.5 -> 0.05: 实测规则分 top1-top2 间距只有 0.004~0.25,
    #   beta=0.5 的 bonus 极差(0.469)足以翻转任意排序, 不是小扰动。
    ap.add_argument("--beta", type=float, default=0.05)
    ap.add_argument("--hz", type=float, default=10.0, help="决策频率( mimic m2 10Hz)")
    ap.add_argument("--topk", type=int, default=16, help="送进 Laya 的候选数")
    ap.add_argument("--throttle", type=float, default=0.0,
                    help="提交推理的最小间隔(秒), 0=不限; 模型慢时用它降频")
    # v14.3 后端选择(方案 §6.2): 本机无独显, 用 OpenVINO 跑 Arc 140T 核显
    ap.add_argument("--backend", default="auto", choices=["auto", "onnx", "torch", "openvino"])
    ap.add_argument("--device", default="GPU", help="OpenVINO 设备: GPU/NPU/CPU/AUTO")
    ap.add_argument("--ir", default=None, help="OpenVINO IR; 默认 runs/laya/laya_ov/laya_qfreeze.xml")
    ap.add_argument("--max-len", type=int, default=320, help="IR 编译时的固定序列长度")
    ap.add_argument("--offline", action="store_true",
                    help="HF 缓存离线模式(无外网时必开, 否则加载模型会 ProxyError)")
    args = ap.parse_args()

    if args.offline:                      # v14.3 无外网时强制用 HF 本地缓存
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"

    # v14.2 环境核查: P1 的 80ms 门槛是"T4 GPU 约 40ms"推出来的, 纯 CPU 上必然不过。
    #   先把真实后端报出来, 免得把环境问题误判成代码问题。
    _env = []
    try:
        import onnxruntime as _ort
        _env.append(f"onnxruntime={_ort.__version__} providers={_ort.get_available_providers()}")
        if "CUDAExecutionProvider" not in _ort.get_available_providers():
            _env.append("  -> 无 CUDA provider: onnxruntime 推理跑在 CPU 上")
    except Exception as _e:
        _env.append(f"onnxruntime 不可用({_e})")
    try:
        import torch as _t
        _env.append(f"torch={_t.__version__} cuda={_t.cuda.is_available()}")
        if not _t.cuda.is_available():
            _env.append("  -> torch 为 CPU 构建: Router 后端也在 CPU 上")
    except Exception as _e:
        _env.append(f"torch 不可用({_e})")
    try:
        import openvino as _ov
        _c = _ov.Core()
        _env.append(f"openvino={_ov.__version__} devices={_c.available_devices}")
    except Exception as _e:
        _env.append(f"openvino 不可用({_e})")
    print("[probe] 环境: " + " | ".join(_env))

    sc = LayaScorer(mode=args.mode, beta=args.beta, top_k=args.topk,
                    min_submit_interval=args.throttle,
                    backend=args.backend, ov_device=args.device, ov_ir=args.ir,
                    ov_max_len=args.max_len, ov_max_opts=args.topk,
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
        out = optimizer.solve(st, dict(fb), 0.9, "blue")
        solve_ms.append((time.time() - ta) * 1000)
        # v14.2: 探针无安全层, executed == chosen; 走一遍回调以便记录/覆盖该路径
        try:
            sc.log_executed(out)
        except Exception:
            pass
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
    print(f"[probe] shadow 落盘 {stats['shadow_logged']} 条, 其中带延迟 "
          f"{stats['latency_logged']} 条 (v14.2: 预测/缓存命中两条路径都写) "
          f"| 标签重复 fail-open {stats['label_dups']} 次 | executed 记录 "
          f"{stats['exec_logged']} 次")
    print(f"[probe] Laya 推理延迟: p50={p50}ms p95={p95}ms (样本 {len(lat)}) "
          f"-> {'PASS' if p95 is not None and p95 < 80 else 'FAIL'} (门槛 <80ms)")
    print(f"[probe] 主线程开销: solve p95={p95_solve:.2f}ms, 其中 reorder p95={p95_re:.2f}ms "
          f"(scorer 占比 {'-' if not p95_solve else round(100*p95_re/p95_solve,1)}%)")
    print(f"[probe] 异常数: {stats['errors']} -> "
          f"{'PASS' if stats['errors'] == 0 else 'FAIL'} (门槛 =0)"
          f" | 标签重复 {stats['label_dups']} | 熔断 {stats.get('tripped')}"
          f" | 节流跳过 {stats.get('throttled', 0)}")
    ok = (p95 is not None and p95 < 80 and stats["errors"] == 0)
    print(f"\n[probe] P1 离线部分: {'PASS' if ok else 'FAIL'} "
          f"(帧率对比需真机: m2 --laya shadow 前后各跑一场)")
    if not ok and p95 is not None and p95 >= 80:
        print("[probe] 未过门槛的原因排查顺序:")
        print("        1) 后端是否在 GPU 上(见上面'环境'行); 纯 CPU 时模型卡给的 ~40ms 不适用,")
        print("           需装 onnxruntime-gpu(配 CUDA)或 torch 的 CUDA 版")
        print("        2) 减候选: --topk 8 (推理耗时随选项数增长)")
        print("        3) 降频: --throttle 0.5 (模型吞吐跟不上 10Hz 决策环时)")
        print("        4) 主循环开销: 用 --topk/--throttle 后重测 reorder p95,")
        print("           它应接近 worker 空闲时的基线(~1.5ms); 明显更高说明与推理抢 CPU")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
