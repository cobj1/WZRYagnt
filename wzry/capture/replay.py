# -*- coding: utf-8 -*-
"""回放采集源：把已录制的关键帧当作实时画面喂给主循环。

用途：无设备时验证 capture→detect→decide→trace 的端到端链路与延迟；
不是感知精度基准（关键帧 5s 一张，且没有 adb 采集开销）。
"""
from __future__ import annotations

import time
from pathlib import Path

import cv2


class ReplayCapture:
    def __init__(self, frames_dir: str | Path, fps: float = 10.0, loop: bool = True):
        self.frames_dir = Path(frames_dir)
        self.fps = max(0.1, float(fps))
        self.loop = bool(loop)
        self.frames: list[Path] = []
        self.index = 0
        self.last_latency_ms = 0.0
        self._next_t = 0.0

    def start(self):
        self.frames = sorted(self.frames_dir.glob("*.png"))
        self.index = 0
        self._next_t = time.perf_counter()
        return self

    def wait_frame(self, timeout: float = 5.0):
        if not self.frames:
            time.sleep(min(timeout, 0.1))
            self.last_latency_ms = 0.0
            return None, 0.0
        now = time.perf_counter()
        due = self._next_t
        if due > now:
            time.sleep(min(due - now, max(0.0, float(timeout))))
        t0 = time.perf_counter()
        lag_ms = max(0.0, (t0 - due) * 1000.0)
        frame = cv2.imread(str(self.frames[self.index]))
        self.last_latency_ms = (time.perf_counter() - t0) * 1000.0
        self.index += 1
        if self.index >= len(self.frames):
            if not self.loop:
                self.index = len(self.frames) - 1
            else:
                self.index = 0
        self._next_t = max(due + 1.0 / self.fps, time.perf_counter())
        if frame is None:
            return self.wait_frame(timeout)
        return frame, lag_ms

    def stop(self):
        self.frames = []
