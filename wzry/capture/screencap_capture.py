# -*- coding: utf-8 -*-
"""实时采集 v14.0：adb screencap 轮询（同 ScrcpyStreamCapture 接口）。

用户要求：低延迟实时抽帧、不读历史切片。
screencap 每 0.25s 拉一张，永远读当前屏幕。

v14.0 修正（真机 1080p）：
  1) adb 不再依赖 PATH —— 本机 PATH 里没有 adb，按候选路径自动解析；
  2) 分辨率自适应 —— 从 RAW 头部读 w/h，再缩放到 1280x720。
     下游（小地图 frame[0:232,0:232] 裁剪、hero_dedup_ui 死区阈值、
     minimap_center 分数）全部按 1280x720 标定，故必须在此统一。
"""
import struct
import subprocess
import threading
import time

import cv2
import numpy as np

from wzry.adb_util import resolve_adb

PROD_SIZE = (1280, 720)          # 下游统一标定的帧尺寸


class ScreencapCapture:
    def __init__(self, interval=0.02, serial=None, rotate_s=1800):
        self.interval = interval
        self.serial = serial or "127.0.0.1:16384"
        self.rotate_s = rotate_s
        self.adb = resolve_adb()
        self._latest = None
        self._latest_t = 0.0
        self.last_size = None
        self._stop = False
        self._thread = None
        self._session_start = 0.0
        self.last_ms = 0.0
        self._fail_streak = 0
        self.native_size = None      # 设备原生分辨率(诊断用)

    def start(self):
        import subprocess as sp
        sp.run([self.adb, "connect", self.serial], capture_output=True, timeout=15)
        self._stop = False
        self._session_start = time.time()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _adb(self, args, timeout=3.0):
        """执行 adb 命令, 带断线自愈: 失败自动 reconnect 重试。"""
        for attempt in range(3):
            try:
                p = subprocess.run(
                    [self.adb, "-s", self.serial] + args,
                    capture_output=True, timeout=timeout)
                if p.returncode == 0 and p.stdout:
                    return p.stdout
            except Exception:
                pass
            # 自愈: 重连
            try:
                subprocess.run([self.adb, "connect", self.serial],
                               capture_output=True, timeout=10)
            except Exception:
                pass
            time.sleep(0.4 * (attempt + 1))
        return None

    def _grab(self):
        """RAW screencap(免PNG编码) -> BGR 1280x720 帧。

        RAW 头 16 字节 = width(4) height(4) format(4) colorspace(4), 之后是
        w*h*4 的 RGBA 像素 —— 从头部读尺寸即可适配任意设备分辨率。
        """
        t0 = time.perf_counter()
        raw = self._adb(["exec-out", "screencap"])
        if raw is None or len(raw) < 16:
            return None
        w, h = struct.unpack_from("<II", raw, 0)
        need = 16 + w * h * 4
        if w <= 0 or h <= 0 or w > 4096 or h > 4096 or len(raw) < need:
            return None
        self.native_size = (w, h)
        buf = np.frombuffer(raw[16:need], dtype=np.uint8)
        img_rgba = buf.reshape(h, w, 4)
        img = np.ascontiguousarray(img_rgba[:, :, :3][:, :, ::-1])  # RGBA->BGR
        if (w, h) != PROD_SIZE:
            # 缩放统一到生产尺寸(下游标定按 1280x720)
            img = cv2.resize(img, PROD_SIZE, interpolation=cv2.INTER_AREA)
        # v2.38 RAW 帧偏暗 ~10% -> 增益对齐 PNG 标定色域
        img = np.clip(img.astype(np.float32) * 1.15 + 4.0, 0, 255).astype(np.uint8)
        self.last_ms = (time.perf_counter() - t0) * 1000.0
        return img

    def _loop(self):
        while not self._stop:
            img = self._grab()
            if img is not None:
                self._fail_streak = 0
                self._latest = img
                self._latest_t = time.time()
                self.last_size = (img.shape[1], img.shape[0])
            else:
                self._fail_streak += 1
            time.sleep(self.interval)

    def wait_frame(self, timeout=5.0):
        t_end = time.time() + timeout
        while time.time() < t_end:
            if self._latest is not None and time.time() - self._latest_t < 1.0:
                return self._latest, (time.time() - self._latest_t) * 1000.0
            time.sleep(0.03)
        return None, 0.0

    def get_frame(self):
        if self._latest is None:
            return None, 0.0
        return self._latest, (time.time() - self._latest_t) * 1000.0

    def stop(self):
        self._stop = True
        if self._thread:
            self._thread.join(timeout=3.0)
