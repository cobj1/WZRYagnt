#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""wait_frame 归因基准：把"取帧阻塞"拆成三类原因。

## 为什么需要它

`m2_agent_v2.py --timing` 已经能测出 `wait_frame` 占墙钟 88–97%，但**测不出为什么**。
三条候选原因，处理方式完全不同：

| 原因 | 现象 | 对策 |
|---|---|---|
| **A 采集源本身慢** | 空载也只有 5–10 fps | 换采集方式 / 降分辨率 / 提码率 |
| **B 主循环抢占 CPU（GIL/核）** | 空载 30 fps，加推理后掉到 3 fps | 线程隔离 / 进程化解码 / 降推理频率 |
| **C 下游处理慢导致帧堆积** | 处理耗时 > 帧间隔，队列持续满 | 降 detect 频率 / ROI 裁剪 / 跳帧 |

不区分这三者，就会在错误的方向上优化（例如明明是 B，却去调采集参数）。

## 三个相位（同一会话、背靠背）

    P1 capture-only        只取帧，不处理          -> 得到 A 的上限
    P2 capture + CPU 负载   纯 Python 忙等线程 1/2/4 -> 隔离 B（不含推理）
    P3 capture + 真实推理   可选，需要模型          -> 得到 C 的影响

归因判据（脚本自动给结论）：
    P1 就低            -> A
    P1 高、P2 随线程数陡降 -> B
    P2 稳、P3 陡降      -> C

## 用法

    # 无设备自检（验证本脚本逻辑，不需要 MuMu）
    python scripts/bench_capture_load.py --dry

    # 真机：先看 A，再看 B，最后看 C
    python scripts/bench_capture_load.py --source screenrecord --seconds 15
    python scripts/bench_capture_load.py --source screenrecord --burners 1,2,4
    python scripts/bench_capture_load.py --source screenrecord --with-yolo

    # 只跑一个相位
    python scripts/bench_capture_load.py --phase p2 --burners 2

注意：本脚本**只读**，不发送任何输入，不需要进入对局（有画面即可）。
"""
from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------- 统计
def pct(values: list[float], q: float) -> float:
    """简单百分位（不引入 numpy 依赖）。"""
    if not values:
        return float("nan")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


class Phase:
    """一个相位的采样结果。"""

    def __init__(self, name: str, seconds: float) -> None:
        self.name = name
        self.seconds = seconds
        self.lags: list[float] = []
        self.frames = 0
        self.t0 = 0.0
        self.t1 = 0.0

    @property
    def wall(self) -> float:
        return max(self.t1 - self.t0, 1e-6)

    @property
    def fps(self) -> float:
        return self.frames / self.wall

    def row(self) -> str:
        return (f"{self.name:<22}{self.fps:>7.1f}"
                f"{pct(self.lags, 0.50):>9.1f}{pct(self.lags, 0.95):>9.1f}"
                f"{pct(self.lags, 0.99):>9.1f}{self.frames:>8}")


# ---------------------------------------------------------------- 负载
class Burner:
    """模拟推理的 CPU 负载：Python 忙等 + 少量数值运算。

    用纯 Python 而不是 numpy，是为了**同时占用 GIL**——真实 YOLO 走 onnxruntime
    会在 C 层释放 GIL，但前后处理（letterbox、NMS、BGR 转换）在 Python 层，
    所以"GIL 竞争"这个因素是真实存在的，用纯 Python 负载是更保守的模拟。
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._acc = 0.0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        x = 0.0
        while not self._stop.is_set():
            for i in range(2000):
                x = (x + i * 1.000001) % 1_000_003
            self._acc += x


class SyntheticSource:
    """合成帧源：按固定帧率产帧，用于无设备验证脚本逻辑。

    `jitter_ms` 模拟真实采集的抖动；`stall_every` 模拟周期性卡顿。
    """

    def __init__(self, fps: float = 30.0, jitter_ms: float = 2.0, stall_every: int = 0) -> None:
        self.interval = 1.0 / fps
        self.jitter_ms = jitter_ms
        self.stall_every = stall_every
        self._next = time.monotonic()
        self._n = 0

    def wait_frame(self, timeout: float = 2.0):
        self._next += self.interval
        now = time.monotonic()
        if self._next > now:
            time.sleep(self._next - now)
        self._n += 1
        if self.stall_every and self._n % self.stall_every == 0:
            time.sleep(0.25)          # 模拟一次卡顿
        lag_ms = max(0.0, (time.monotonic() - self._next) * 1000.0)
        lag_ms += self.jitter_ms * ((self._n % 7) / 7.0)
        return None, lag_ms

    def close(self) -> None:
        pass


def make_source(kind: str):
    """构造采集源。真机源沿用产线实现，保证口径一致。"""
    if kind == "synthetic":
        return SyntheticSource(fps=30.0, jitter_ms=2.0, stall_every=37)
    if kind == "screenrecord":
        from wzry.capture.screenrecord_capture import ScreenrecordCapture
        cap = ScreenrecordCapture(ROOT / "tools" / "scrcpy")
        cap.start()
        return cap
    if kind == "scrcpy":
        from wzry.capture.scrcpy_stream import ScrcpyStreamCapture
        cap = ScrcpyStreamCapture(ROOT / "tools" / "scrcpy")
        cap.start()
        return cap
    if kind == "screencap":
        from wzry.capture.screencap_capture import ScreencapCapture
        cap = ScreencapCapture()
        cap.start()
        return cap
    raise SystemExit(f"未知采集源: {kind}")


# ---------------------------------------------------------------- 相位执行
def run_phase(name: str, source, seconds: float, burners: int = 0,
              detector=None) -> Phase:
    ph = Phase(name, seconds)
    bs = [Burner() for _ in range(burners)]
    for b in bs:
        b.start()
    try:
        ph.t0 = time.monotonic()
        deadline = ph.t0 + seconds
        while time.monotonic() < deadline:
            frame, lag_ms = source.wait_frame(timeout=2.0)
            if frame is None and not hasattr(source, "interval"):
                # 真机源拿不到帧：记为一次超时，不中断
                ph.lags.append(2000.0)
                continue
            ph.lags.append(float(lag_ms))
            ph.frames += 1
            if detector is not None:
                detector.detect(frame)          # 真实下游处理
        ph.t1 = time.monotonic()
    finally:
        for b in bs:
            b.stop()
    return ph


# ---------------------------------------------------------------- 结论
def verdict(phases: dict[str, Phase]) -> list[str]:
    """按设计文档里的判据给出归因结论。"""
    out = []
    p1 = phases.get("P1 capture-only")
    if p1 is None:
        return ["未跑 P1，无法归因"]
    if p1.fps < 12:
        out.append(f"A 采集源本身慢：空载仅 {p1.fps:.1f} fps（阈值 <12）"
                   " -> 优先换采集方式/降分辨率，而不是调主循环")
        return out
    out.append(f"A 排除：空载 {p1.fps:.1f} fps 达标")

    p2 = [p for k, p in phases.items() if k.startswith("P2")]
    if p2:
        worst = min(p2, key=lambda p: p.fps)
        drop = 1 - worst.fps / p1.fps
        if drop > 0.5:
            out.append(f"B 主循环抢占是主因：加 {worst.name.split('+')[-1].strip()} 后 "
                       f"{p1.fps:.1f} -> {worst.fps:.1f} fps（掉 {drop*100:.0f}%）"
                       " -> 优先线程隔离/进程化解码，而非调采集参数")
        else:
            out.append(f"B 影响有限：最差相位仅掉 {drop*100:.0f}%")

    p3 = phases.get("P3 capture+yolo")
    if p3 and p2:
        base = min(p2, key=lambda p: p.fps).fps
        drop = 1 - p3.fps / base
        if drop > 0.5:
            out.append(f"C 下游处理是主因：加真实推理后 {base:.1f} -> {p3.fps:.1f} fps"
                       " -> 降 detect 频率 / ROI 裁剪 / 跳帧")
        else:
            out.append(f"C 影响有限：加推理后掉 {drop*100:.0f}%")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="wait_frame 归因基准（只读，不发输入）")
    ap.add_argument("--source", default="screenrecord",
                    choices=["screenrecord", "scrcpy", "screencap", "synthetic"])
    ap.add_argument("--seconds", type=float, default=15.0, help="每个相位时长")
    ap.add_argument("--burners", default="", help="P2 的线程数列表，如 1,2,4")
    ap.add_argument("--with-yolo", action="store_true", help="追加 P3（真实推理）")
    ap.add_argument("--model", default=str(ROOT / "runs" / "detect" / "hok_yolov5" / "hok_yolov5s.onnx"))
    ap.add_argument("--phase", default="", choices=["", "p1", "p2", "p3"], help="只跑某个相位")
    ap.add_argument("--dry", action="store_true", help="用合成源自检（不需要设备）")
    args = ap.parse_args(argv)

    source_kind = "synthetic" if args.dry else args.source
    print(f"采集源: {source_kind}  相位时长: {args.seconds:.0f}s")
    if args.dry:
        print("（--dry：合成源 30 fps + 周期卡顿，仅验证脚本与统计逻辑）\n")

    phases: dict[str, Phase] = {}
    src = make_source(source_kind)
    try:
        if args.phase in ("", "p1"):
            print("P1 只取帧 ...", end="", flush=True)
            phases["P1 capture-only"] = run_phase("P1 capture-only", src, args.seconds)
            print(f" {phases['P1 capture-only'].fps:.1f} fps")

        burn_list = [int(x) for x in args.burners.split(",") if x.strip()] or ([1, 2, 4] if args.phase == "" else [])
        if args.phase in ("", "p2") and burn_list:
            for n in burn_list:
                key = f"P2 capture+burn{n}"
                print(f"P2 +{n} 负载线程 ...", end="", flush=True)
                phases[key] = run_phase(key, src, args.seconds, burners=n)
                print(f" {phases[key].fps:.1f} fps")

        if (args.with_yolo or args.phase == "p3"):
            print("P3 加载检测器 ...", end="", flush=True)
            from wzry.vision.detector import YoloDetector
            det = YoloDetector(args.model, conf=0.35)
            print(" ok")
            print("P3 取帧 + 推理 ...", end="", flush=True)
            phases["P3 capture+yolo"] = run_phase("P3 capture+yolo", src, args.seconds, detector=det)
            print(f" {phases['P3 capture+yolo'].fps:.1f} fps")
    finally:
        try:
            src.close()
        except Exception:
            pass

    print("\n" + "=" * 62)
    print(f"{'相位':<22}{'fps':>7}{'p50ms':>9}{'p95ms':>9}{'p99ms':>9}{'帧数':>8}")
    print("-" * 62)
    for ph in phases.values():
        print(ph.row())
    print("=" * 62)
    print("\n归因结论：")
    for line in verdict(phases):
        print(f"  - {line}")
    print("\n说明：lag_ms 是'进入 wait_frame 到拿到帧'的阻塞时长；"
          "fps = 帧数 / 相位墙钟。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
