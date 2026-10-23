# -*- coding: utf-8 -*-
"""screenrecord 流式采集 v16.4：adb 裸 H.264 流 -> 本地 TCP -> cv2 实时解码。

为什么不用 scrcpy --record 的 mkv/mp4（实测数据）：
  - mkv muxer 按簇落盘：首字节要等约 5.7s，之后每 ~5s 才写一批 256KB；
    mp4 约 2.2s 一批。cv2 尾读"生长中的容器文件"还会在 EOF 处永久锁死
    （read 恒 False，只能重开+seek 恢复）。
  - 裸 H.264 无索引，seek 后到下一个 IDR 前的帧全部无法解码（丢帧/花屏），
    所以"重开+seek"在这种流上不可用。
方案：adb screenrecord --output-format=h264 - 直接吐裸流（首字节 ~0.5s、连续写入），
      经本地 TCP（tcp://127.0.0.1:port）喂给 cv2 内置 ffmpeg。socket 是无 EOF 的流：
      cv2 顺序解码即可，无 muxer 缓冲、无 seek、无丢帧。
      实测：交付 32.6 fps、帧间隔中位 32ms、无 >200ms 空档
      （旧 mkv 路径：10.2 fps + 最长 6.1s 卡顿 + 启动白等 15s）。

限制与对策：
  - screenrecord 单次上限 180s：rotate_s 自动取 min(., 170)，到点整条管线重建
    （cv2 重连约 2s 空窗）；
  - --size 需具体宽高：由 `wm size` 物理分辨率按长边=max_size 等比缩放并对齐 16
    （1440x3200 -> 1280x576，与 scrcpy --max-size 1280 的输出一致）。
  - **设备息屏时 screenrecord 报 INVALID_LAYER_STACK 拒绝出流**（实测 Android 15）：
    起流前查 mScreenState，灭屏发 KEYCODE_POWER 点亮（该设备 KEYCODE_WAKEUP
    唤醒不可靠，而 POWER 是切换键不能盲发）；运行期按屏幕超时的一半周期保活
    （亮屏发 WAKEUP 重置超时计时）——否则一旦熄屏进入 Doze，WiFi adb 会整条
    掉线（adb connect 都连不上，只能人工点亮）。注意唤醒只到锁屏，运行期需
    保持游戏在前台且不锁屏（与旧 scrcpy 路径要求一致）。

接口与 ScrcpyStreamCapture 相同：start/get_frame/wait_frame/stop、.serial/.last_size。
"""
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

import cv2

from wzry.capture.scrcpy_stream import bit_rate_int

_ALIGN = 16  # screenrecord 部分设备要求宽高对齐 16


class ScreenrecordCapture:
    def __init__(self, adb_path, serial=None, bit_rate="8M", max_size=1280,
                 temp_dir=None, rotate_s=170):
        p = Path(adb_path)
        self.adb_exe = str(p if p.suffix.lower() == ".exe" else p / "adb.exe")
        self.serial = serial
        self.bit_rate = bit_rate
        self.max_size = max_size
        # screenrecord 单次上限 180s（自带 --time-limit 兜底），留 10s 余量
        self.rotate_s = min(int(rotate_s), 170)
        # 保留 temp_dir 形参兼容旧接口；流式路径不落盘
        self.temp_dir = Path(temp_dir) if temp_dir else None

        self._thread = None
        self._stop = False
        self._stop_evt = threading.Event()
        self._keepalive = None
        self._wake_interval = 30.0
        self._lock = threading.Lock()
        self._cond = threading.Condition()
        self._pl_lock = threading.Lock()

        self._srv = None
        self._conn = None
        self._relay = None
        self._proc = None
        self._cap = None
        self._session_start = 0.0
        self._size_wh = None

        self._latest = None
        self._latest_t = 0.0
        self._latest_latency_ms = 0.0
        self.last_size = None

    # ---------- 生命周期 ----------
    def start(self):
        if self.serial is None:
            from wzry.control.executor import discover_mumu_adb_devices
            serials = discover_mumu_adb_devices()
            if not serials:
                raise RuntimeError("未发现 ADB 设备")
            self.serial = serials[0]
        self._stop = False
        self._stop_evt.clear()
        self._size_wh = self._resolve_size()
        # 周期点亮保活：屏幕一旦超时熄灭，不仅 screenrecord 拒录（INVALID_LAYER_STACK），
        # WiFi adb 还会随 Doze 深睡整条掉线（实测 reconnect 都连不上），只能人工点亮救场。
        # 周期取屏幕超时的一半（clamp 10~50s），保证屏幕永不熄灭 = 不锁屏、不掉线。
        to_s = self._resolve_screen_timeout_s()
        self._wake_interval = max(10.0, min(50.0, (to_s or 60.0) / 2.0))
        print(f"[screenrecord] 屏幕保活周期 {self._wake_interval:.0f}s"
              f"（系统屏幕超时 {to_s if to_s else 60.0:.0f}s）", flush=True)
        self._keepalive = threading.Thread(target=self._keepalive_loop, daemon=True)
        self._keepalive.start()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        return self

    def _resolve_screen_timeout_s(self):
        """读系统屏幕超时（秒）；失败返回 None（用默认 60s 兜底算周期）。"""
        try:
            out = subprocess.run(
                [self.adb_exe, "-s", self.serial, "shell", "settings", "get",
                 "system", "screen_off_timeout"],
                capture_output=True, text=True, timeout=10).stdout.strip()
            ms = int(out)
            if ms > 0:
                return ms / 1000.0
        except Exception:
            pass
        return None

    def _keepalive_loop(self):
        while not self._stop:
            if self._stop_evt.wait(self._wake_interval):
                break
            self._wake_device()

    def _resolve_size(self):
        """wm size 物理分辨率 -> 长边 max_size、等比、对齐 16 的横屏尺寸。"""
        try:
            out = subprocess.run(
                [self.adb_exe, "-s", self.serial, "shell", "wm", "size"],
                capture_output=True, text=True, timeout=10).stdout
            hits = re.findall(r"(\d+)\s*x\s*(\d+)", out)
            if hits:
                w, h = (int(v) for v in hits[-1])
                long_e, short_e = max(w, h), min(w, h)
                nl = max(_ALIGN, self.max_size // _ALIGN * _ALIGN)
                ns = max(_ALIGN, int(short_e * self.max_size / long_e) // _ALIGN * _ALIGN)
                return nl, ns
        except Exception:
            pass
        lw = max(_ALIGN, self.max_size // _ALIGN * _ALIGN)
        lh = max(_ALIGN, lw * 9 // _ALIGN // _ALIGN * _ALIGN)
        return lw, lh

    def _run_loop(self):
        while not self._stop:
            try:
                self._start_pipeline()
                self._read_loop()
            except Exception:
                time.sleep(1.0)
            finally:
                self._teardown_pipeline()

    def _start_pipeline(self):
        with self._pl_lock:
            srv = socket.socket()
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            self._srv = srv
            port = srv.getsockname()[1]
            self._session_start = time.time()
            self._relay = threading.Thread(target=self._relay_pipe, args=(srv,),
                                           daemon=True)
            self._relay.start()
        # ffmpeg 以客户端身份连到我们的监听口；此处阻塞约 1~2s（探测裸流）
        cap = cv2.VideoCapture(f"tcp://127.0.0.1:{port}")
        with self._pl_lock:
            self._cap = cap
        if not cap.isOpened():
            raise RuntimeError("cv2 无法打开 screenrecord TCP 流")

    def _screen_on(self):
        """屏幕亮灭：True=亮 / False=灭 / None=查询失败。"""
        try:
            out = subprocess.run(
                [self.adb_exe, "-s", self.serial, "shell",
                 "dumpsys display | grep mScreenState="],
                capture_output=True, text=True, timeout=5).stdout
            if "mScreenState=ON" in out:
                return True
            if "OFF" in out or "DOZE" in out:
                return False
        except Exception:
            pass
        return None

    def _wake_device(self):
        """确保屏幕可录 + 重置熄屏计时。实测该设备（HyperOS/Android 15）：
        - 息屏时 screenrecord 报 INVALID_LAYER_STACK 拒录；
        - KEYCODE_WAKEUP 从息屏唤醒不可靠，POWER 才管用，但 POWER 是切换键
          （亮屏时盲发会把屏幕按灭）-> 先查 mScreenState：
          灭 -> KEYCODE_POWER 点亮（等真正 ON）；亮 -> KEYCODE_WAKEUP（无害，
          重置超时计时）；查询失败 -> 不动（避免误按灭）。"""
        state = self._screen_on()
        if state is False:
            try:
                subprocess.run(
                    [self.adb_exe, "-s", self.serial, "shell", "input", "keyevent",
                     "KEYCODE_POWER"], capture_output=True, timeout=5)
            except Exception:
                pass
            for _ in range(10):   # 等屏幕真正点亮（防起流竞态 INVALID_LAYER_STACK）
                time.sleep(0.3)
                if self._screen_on():
                    break
        elif state is True:
            try:
                subprocess.run(
                    [self.adb_exe, "-s", self.serial, "shell", "input", "keyevent",
                     "KEYCODE_WAKEUP"], capture_output=True, timeout=5)
            except Exception:
                pass

    def _relay_pipe(self, srv):
        """接受 cv2 连接，把 adb 裸流原样转投进 socket。"""
        conn = None
        proc = None
        try:
            conn, _ = srv.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._pl_lock:
                self._conn = conn
            self._wake_device()
            w, h = self._size_wh or (1280, 720)
            cmd = [
                self.adb_exe, "-s", self.serial, "exec-out", "screenrecord",
                "--output-format=h264",
                "--bit-rate", str(bit_rate_int(self.bit_rate)),
                "--size", f"{w}x{h}",
                "--time-limit", "180",
                "-",
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
            with self._pl_lock:
                self._proc = proc
            # read1: 有一块就返回（read 会攒满 64KB，导致 ~0.4s 一批的突发交付）
            while not self._stop:
                b = proc.stdout.read1(16384)
                if not b:
                    break
                conn.sendall(b)
        except Exception:
            pass
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            if conn is not None:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    conn.close()
                except OSError:
                    pass
            with self._pl_lock:
                if self._conn is conn:
                    self._conn = None
                if self._proc is proc:
                    self._proc = None

    def _read_loop(self):
        cap = self._cap
        try:
            while not self._stop:
                ok = cap.read()
                if ok and ok[0]:
                    frame = ok[1]
                    with self._lock:
                        self._latest = frame
                        self._latest_t = time.time()
                        self.last_size = (frame.shape[1], frame.shape[0])
                    with self._cond:
                        self._cond.notify_all()
                    if time.time() - self._session_start > self.rotate_s:
                        return  # 到点重建管线（screenrecord 也会在 180s 自停）
                else:
                    proc = self._proc
                    if self._stop:
                        return
                    if proc is not None and proc.poll() is not None:
                        return  # adb 退出（time-limit / 断流）-> 重建
                    time.sleep(0.02)
        finally:
            try:
                cap.release()
            except Exception:
                pass

    def _teardown_pipeline(self):
        with self._pl_lock:
            conn, srv, proc, relay = self._conn, self._srv, self._proc, self._relay
            self._conn = self._srv = self._proc = self._relay = self._cap = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        # 先断 socket：解除 cv2 阻塞读（cap.release 由读取线程自己在 finally 里做）
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass
        if relay is not None and relay.is_alive() and relay is not threading.current_thread():
            relay.join(timeout=2)

    # ---------- 对外接口 ----------
    def get_frame(self):
        """返回最新帧 (frame, latency_ms)；无帧返回 (None, 0)。非阻塞。"""
        with self._lock:
            f = self._latest
            t = self._latest_t
        if f is None:
            return None, 0.0
        self._latest_latency_ms = (time.time() - t) * 1000.0
        return f, self._latest_latency_ms

    def wait_frame(self, timeout=5.0):
        """阻塞直到新帧到达，返回 (frame, latency_ms)。"""
        with self._cond:
            before = self._latest_t
            self._cond.wait(timeout=timeout)
            with self._lock:
                f = self._latest
                t = self._latest_t
        if f is None or t <= before:
            return None, 0.0
        return f, (time.time() - t) * 1000.0

    def stop(self):
        self._stop = True
        self._stop_evt.set()
        self._teardown_pipeline()  # 主动断流，解除读取线程的阻塞
        if self._keepalive is not None:
            self._keepalive.join(timeout=2)
            self._keepalive = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
