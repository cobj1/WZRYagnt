# -*- coding: utf-8 -*-
"""同局内 A/B 基准: 依次用不同 --yolo-threads 背靠背跑 m2 --timing, 汇总对比。

教训(2026-10-15): 同样代码跨轮跑 5 次, 帧数 126/334/218/284/111 —— 游戏状态不同
(在局/阵亡/结算) 带来的噪声 > 信号。为此: (a) 多组配置在同一局内连续跑;
(b) 默认组序 0,4,2,0 —— 首尾同配置, 若末组明显低于首组说明局内状态已漂移, 整批作废。

用法(须用 venv python 跑; 变量: threads=0 表示 ORT 默认占满物理核):
    venv/Scripts/python.exe -X utf8 scripts/bench_yolo_threads.py --seconds 60
    venv/Scripts/python.exe -X utf8 scripts/bench_yolo_threads.py --threads 0,2 --extra "--action"
"""
import argparse
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
M2 = ROOT / "scripts" / "m2_agent_v2.py"


def parse_log(text):
    d = {}
    m = re.search(r"完成: (\d+)帧 感知(\d+) 次 平均检测 (\d+)ms 动作 (\d+) 次 模式: (\S+)", text)
    if m:
        d["frames"], d["ticks"], d["acts"], d["mode"] = \
            int(m.group(1)), int(m.group(2)), int(m.group(4)), m.group(5)
    for mm in re.finditer(r"^\s{2}(\S+)\s+([\d.]+)ms\s+([\d.]+)%\s+\((\d+)次, 均 ([\d.]+)ms/次\)",
                          text, re.M):
        d[mm.group(1)] = (float(mm.group(5)), float(mm.group(3)), int(mm.group(4)))
    return d


def _stage(d, key):
    return d.get(key, (float("nan"), float("nan"), 0))


def main():
    ap = argparse.ArgumentParser(description="同局内 A/B: 多组 --yolo-threads 背靠背对比")
    ap.add_argument("--threads", default="0,4,2,0", help="组序(逗号分隔, 0=ORT 默认)")
    ap.add_argument("--seconds", type=float, default=60.0, help="每组时长秒(默认 60)")
    ap.add_argument("--extra", default="--capture screencap",
                    help="透传 m2 的附加参数(默认 '--capture screencap')")
    ap.add_argument("--log-dir", default=None, help="日志目录(默认 temp/bench)")
    args = ap.parse_args()

    log_dir = Path(args.log_dir) if args.log_dir else ROOT / "temp" / "bench"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%m%d_%H%M")
    groups = [int(v) for v in args.threads.split(",") if v.strip()]
    if not groups:
        print("组序为空"); return 2

    results = []
    for i, n in enumerate(groups):
        log = log_dir / f"ab_{stamp}_g{i}_t{n}.log"
        cmd = [sys.executable, "-X", "utf8", str(M2), "--timing",
               "--seconds", str(args.seconds), "--yolo-threads", str(n),
               *shlex.split(args.extra)]
        print(f"[{i + 1}/{len(groups)}] yolo_threads={n} -> {log.name}", flush=True)
        t0 = time.time()
        with open(log, "w", encoding="utf-8", errors="replace") as f:
            p = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT,
                               cwd=str(ROOT), env={**os.environ, "PYTHONUTF8": "1"})
        d = parse_log(log.read_text(encoding="utf-8", errors="replace"))
        results.append((n, d, log.name, p.returncode))
        print(f"    rc={p.returncode} 帧={d.get('frames')} 感知={d.get('ticks')} "
              f"wait均={_stage(d, 'wait_frame阻塞取帧')[0]:.0f}ms 用时{time.time() - t0:.0f}s",
              flush=True)

    print("\n== 汇总(同局内 A/B) ==")
    print(f"{'threads':>8} {'帧数':>6} {'wait均':>9} {'wait占比':>8} {'感知拍':>6} "
          f"{'YOLO均':>8} {'渲染均':>8}  日志")
    for n, d, name, rc in results:
        w, y, r = _stage(d, "wait_frame阻塞取帧"), _stage(d, "YOLO检测"), _stage(d, "渲染标注render_live")
        print(f"{n:>8} {d.get('frames', -1):>6} {w[0]:>8.0f}ms {w[1]:>7.1f}% "
              f"{d.get('ticks', -1):>6} {y[0]:>7.1f}ms {r[0]:>7.1f}ms  {name}")
    if len(results) >= 2 and results[0][0] == results[-1][0]:
        f0, f1 = results[0][1].get("frames", 0), results[-1][1].get("frames", 0)
        drift = abs(f1 - f0) / max(1, f0)
        print(f"首尾同配置漂移: {f0} -> {f1} 帧 ({drift * 100:.0f}%)"
              + ("  ⚠️ 漂移过大, 局中状态已变, 结果不可比" if drift > 0.25 else "  (可比)"))
    for n, d, name, rc in results:
        if rc != 0 or d.get("frames", 0) <= 0 or d.get("ticks", 0) < 5:
            print(f"⚠️ 组 threads={n} 异常: rc={rc} 帧={d.get('frames')} 感知={d.get('ticks')}"
                  f" —— 检查是否在局(阵亡/结算/大厅会毁掉这一组)")
    print("日志目录:", log_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
