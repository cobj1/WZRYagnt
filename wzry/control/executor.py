# -*- coding: utf-8 -*-
"""动作执行器（M0 单实例）。

控制通道选择：
  - adb（默认）：常驻 adb server + `adb -s <serial> shell input ...`，
    比旧 touch.py 每次 subprocess.run('adb') 更快，且带序列号直连。
  - mumu（备用）：mumu-python-api（MuMuManager 封装，每次调用起子进程，仅作对照）。

所有动作都写入事件流（JSON Lines，带时间戳），供回放、延迟统计与训练对齐使用。
"""
import subprocess
import threading
import time
from pathlib import Path

from wzry.adb_util import resolve_adb

# 不再写死 "adb"：本机 PATH 无 adb，MuMu 自带的在 nx_main 下（见 wzry/adb_util.py）
ADB = resolve_adb()

# MuMu12 常见实例 ADB 端口（实例 0 通常是 16384，旧版为 7555）
MUMU_ADB_PORTS = (16384, 7555, 16416, 16448, 16480, 16512)


def _adb_devices():
    """执行 adb devices，返回在线序列号列表。"""
    try:
        out = subprocess.run([ADB, "devices"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=5).stdout
    except Exception:
        return []
    return [ln.split("\t")[0] for ln in out.splitlines()[1:] if "\tdevice" in ln]


def discover_mumu_adb_devices():
    """探测已连接的 MuMu 模拟器 adb 序列号。

    若当前无在线设备，尝试自动 `adb connect` 常见 MuMu 端口后重查。
    返回列表：优先 127.0.0.1:* 的直连端口，其次其他在线设备。
    """
    serials = _adb_devices()
    if not serials:
        for port in MUMU_ADB_PORTS:
            try:
                subprocess.run([ADB, "connect", f"127.0.0.1:{port}"],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=5)
            except Exception:
                continue
        serials = _adb_devices()
    loop = [s for s in serials if s.startswith("127.0.0.1:")]
    return loop or serials


class AdbExecutor:
    """基于 adb 的触摸执行器：tap / swipe / key，含延迟统计与动作事件流。"""

    def __init__(self, serial=None, event_log=None):
        self.serial = serial or self._auto_serial()
        self.event_log = Path(event_log) if event_log else None
        self._lock = threading.Lock()
        self._event_fp = None
        self.last_cmd_ms = 0.0
        self.cmd_count = 0
        # v14.0 坐标空间: 全项目(校准 configs/calibration_absolute.json、小地图 242 空间)
        # 都用 1280x720 帧坐标, 而设备实际可能是 1920x1080 -> tap/swipe 必须按比例缩放。
        # 旧实现在 1080p 设备上会把触点送到 ~67% 位置(技能键全按不中)。
        self._win_w, self._win_h = 1280, 720
        self._dev_w, self._dev_h = None, None      # 最近一次读到的设备显示尺寸

    def _auto_serial(self):
        # v2.58 自愈: 先尝试连固定 MuMu 端口, 再枚举
        try:
            subprocess.run([ADB, "connect", "127.0.0.1:16384"],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=5)
        except Exception:
            pass
        serials = discover_mumu_adb_devices()
        if not serials:
            time.sleep(1.5)
            try:
                subprocess.run([ADB, "connect", "127.0.0.1:16384"],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=5)
            except Exception:
                pass
            serials = discover_mumu_adb_devices()
        if not serials:
            raise RuntimeError("未发现 MuMu ADB 设备，请先启动模拟器并确认 adb 已连接")
        return serials[0]

    # ---------- 基础命令 ----------
    def _run(self, args, timeout=4):
        """adb 命令: 4s 超时快速失败(防主循环卡死), 失败自动重连一次, 永不抛出。"""
        t0 = time.perf_counter()
        try:
            with self._lock:
                r = subprocess.run(
                    [ADB, "-s", self.serial] + args,
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=timeout,
                )
        except subprocess.TimeoutExpired:
            self._reconnect()
            with self._lock:
                try:
                    r = subprocess.run(
                        [ADB, "-s", self.serial] + args,
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=timeout,
                    )
                except Exception:
                    return None
        except Exception:
            return None
        self.last_cmd_ms = (time.perf_counter() - t0) * 1000.0
        self.cmd_count += 1
        return r

    def _reconnect(self):
        try:
            subprocess.run([ADB, "connect", self.serial],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=5)
        except Exception:
            pass

    def get_device_size(self):
        """读取**当前逻辑显示尺寸**（= adb input 的坐标空间）。

        v14.0 修正：`wm size` 报的是物理原生尺寸（竖屏 1080x1920），而横屏对局
        的输入空间是 1920x1080 —— 用它做缩放会把触点算错（x 缩 0.84、y 放 2.67）。
        `dumpsys window displays` 的 `cur=WxH` 才是权威值（含旋转）。
        """
        import re
        r = self._run(["shell", "dumpsys", "window", "displays"])
        out = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"cur=(\d+)x(\d+)", out)
        if m:
            return int(m.group(1)), int(m.group(2))
        # 回退: wm size（可能为竖屏原生）-> 本项目全程横屏, 归一化为横屏
        r = self._run(["shell", "wm", "size"])
        out = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"(\d+)x(\d+)", out)
        if not m:
            raise RuntimeError(f"无法解析显示尺寸: {out.strip()[:120]}")
        w, h = int(m.group(1)), int(m.group(2))
        return max(w, h), min(w, h)

    def refresh_display_size(self):
        """v14.2 用户要求: 每次操作前重新读取当前显示尺寸(自适应旋转/分辨率变化)。

        原来只在首个动作时懒加载一次后就永久缓存 —— 若中途横竖屏切换(如大厅竖屏
        1080x1920 -> 对局横屏 1920x1080)或分辨率变化, 缓存的旧尺寸会让后续所有触点
        缩放错位。故每次 tap/swipe 前强制重读 `dumpsys window displays` 的 cur=WxH。
        读取失败时保留上一次已知值(不置 0, 避免退化成不做缩放)。
        """
        try:
            self._dev_w, self._dev_h = self.get_device_size()
        except Exception:
            pass

    # ---------- 窗口坐标 -> 设备坐标 ----------
    def set_window_size(self, w, h):
        """记录窗口尺寸；坐标映射按 窗口像素/设备像素 比例缩放。"""
        self._win_w, self._win_h = int(w), int(h)

    def window_to_device(self, x_win, y_win):
        """帧像素(1280x720 标定空间) -> 设备像素(线性缩放, 用最近一次读到的显示尺寸)。"""
        x_win, y_win = int(round(x_win)), int(round(y_win))
        if self._dev_w is None:                     # 兜底: 首次调用前未刷新
            self.refresh_display_size()
        if self._dev_w and self._win_w and \
                (self._dev_w, self._dev_h) != (self._win_w, self._win_h):
            return (int(x_win * self._dev_w / self._win_w),
                    int(y_win * self._dev_h / self._win_h))
        return x_win, y_win

    # ---------- 动作原语 ----------
    def tap(self, x, y, source="policy"):
        self.refresh_display_size()                 # v14.2 操作前自适应屏幕尺寸
        dx, dy = self.window_to_device(x, y)
        r = self._run(["shell", "input", "tap", str(dx), str(dy)])
        self._log({"type": "tap", "x": dx, "y": dy, "win": [int(round(x)), int(round(y))],
                   "display": [self._dev_w, self._dev_h], "source": source,
                   "latency_ms": round(self.last_cmd_ms, 2)})
        return r.returncode == 0

    def swipe(self, x1, y1, x2, y2, duration=100, source="policy"):
        self.refresh_display_size()                 # v14.2 操作前自适应屏幕尺寸
        d1 = self.window_to_device(x1, y1)
        d2 = self.window_to_device(x2, y2)
        d1 = (int(round(d1[0])), int(round(d1[1])))
        d2 = (int(round(d2[0])), int(round(d2[1])))
        r = self._run(["shell", "input", "swipe",
                       str(d1[0]), str(d1[1]), str(d2[0]), str(d2[1]), str(int(duration))])
        self._log({"type": "swipe", "x1": d1[0], "y1": d1[1], "x2": d2[0], "y2": d2[1],
                   "win": [[int(round(x1)), int(round(y1))],
                           [int(round(x2)), int(round(y2))]],
                   "duration": int(duration), "display": [self._dev_w, self._dev_h],
                   "source": source, "latency_ms": round(self.last_cmd_ms, 2)})
        return r.returncode == 0

    def tap_win(self, x_win, y_win, source="policy"):
        """按帧像素(1280x720 标定空间)点击。tap 已内置同一坐标空间换算, 此处直接委托。"""
        return self.tap(x_win, y_win, source=source)

    def swipe_win(self, x1, y1, x2, y2, duration=100, source="policy"):
        """按帧像素滑动。swipe 已内置同一坐标空间换算, 此处直接委托。"""
        return self.swipe(x1, y1, x2, y2, duration=duration, source=source)

    def key(self, keycode, source="policy"):
        r = self._run(["shell", "input", "keyevent", str(keycode)])
        self._log({"type": "key", "keycode": str(keycode), "source": source,
                   "latency_ms": round(self.last_cmd_ms, 2)})
        return r.returncode == 0

    def shell(self, cmd):
        """任意 shell 命令（如 getprop 测试往返延迟）。"""
        r = self._run(["shell"] + cmd.split())
        return r

    # ---------- 事件流 ----------
    def _log(self, event):
        event = {"t": time.time(), **event}
        if self.event_log:
            if self._event_fp is None:
                self.event_log.parent.mkdir(parents=True, exist_ok=True)
                self._event_fp = open(self.event_log, "a", encoding="utf-8")
            self._event_fp.write(__import__("json").dumps(event, ensure_ascii=False) + "\n")
            self._event_fp.flush()

    def close(self):
        if self._event_fp:
            self._event_fp.close()
            self._event_fp = None
