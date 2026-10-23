# -*- coding: utf-8 -*-
"""scrcpy 流式采集：设备画面实时 H.264 -> mkv 文件 -> cv2 并发读取。

特性：
  - 帧率由 scrcpy --max-fps 控制（默认 30），有效采集延迟 ~33ms；
  - 独立读取线程 + 最新帧缓冲，get_frame() 非阻塞；
  - 断流自动重启；按 rotate_s 轮转录制文件防磁盘膨胀；
  - 帧为设备像素（1280x720 横屏），与 adb 输入同坐标系。
"""
import subprocess
import threading
import time
from pathlib import Path

import cv2


def bit_rate_int(bit_rate):
    """'6M' -> 6_000_000；'800k' -> 800_000。"""
    s = str(bit_rate).strip().lower()
    mult = {"k": 1000, "m": 1000_000}.get(s[-1:], 1)
    if mult != 1:
        s = s[:-1]
    return int(float(s) * mult)


class ScrcpyStreamCapture:
    def __init__(self, scrcpy_path, serial=None, max_fps=30, bit_rate="6M",
                 temp_dir=None, rotate_s=300, max_size=1280):
        self.scrcpy_exe = str(Path(scrcpy_path) / "scrcpy.exe")
        self.serial = serial
        self.max_fps = max_fps
        self.bit_rate = bit_rate
        self.max_size = max_size
        self.rotate_s = rotate_s
        self.temp_dir = (Path(temp_dir) if temp_dir else Path("temp")).resolve()

        self._proc = None
        self._cap = None
        self._file = None
        self._thread = None
        self._latest = None
        self._latest_t = 0.0
        self._latest_latency_ms = 0.0
        self._stop = False
        self._cond = threading.Condition()
        self._lock = threading.Lock()
        self._session_start = 0.0
        self.last_size = None
        self._seq = 0

    # ---------- 生命周期 ----------
    def start(self):
        if self.serial is None:
            from wzry.control.executor import discover_mumu_adb_devices
            serials = discover_mumu_adb_devices()
            if not serials:
                raise RuntimeError("未发现 MuMu ADB 设备")
            self.serial = serials[0]
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self._stop = False
        self._start_pipeline()
        self._thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._thread.start()
        return self

    def _start_pipeline(self):
        self._seq += 1
        self._file = self.temp_dir / f"stream_{self._seq:04d}.mkv"
        # v16.3: 先删同名遗留文件。否则 `_seq` 从小值起时很可能命中上一次的 mkv，
        #   于是"文件已有足够字节"的判据立刻通过 -> cv2 打开一个马上被 scrcpy 截断的文件
        #   （表现为 File ended prematurely / 读到旧帧 / 节奏错乱）。
        try:
            if self._file.exists():
                self._file.unlink()
        except OSError:
            pass
        # v16.4: unlink 失败(Windows 上文件仍被上一次的孤儿 scrcpy 占用)会静默放过，
        #   随后新 scrcpy 写不进这个路径 / cv2 读到旧流 -> 连续启动失败。这里显式查证。
        if self._file.exists():
            raise RuntimeError(
                f"无法清理残留录制文件（可能仍有 scrcpy 进程占用）: {self._file}")
        cmd = [
            self.scrcpy_exe, "-s", self.serial,
            "--no-window", "--no-control", "--no-audio",
            "--max-fps", str(self.max_fps),
            "--max-size", str(self.max_size),
            "--video-bit-rate", self.bit_rate,
            "--record-format=mkv",
            "--record", str(self._file),
        ]
        self._proc = subprocess.Popen(cmd)
        self._session_start = time.time()
        # 等文件里有 mkv 头 + 首个 cluster 才能被 ffmpeg 解析（太小会报 EBML header parsing failed）。
        # v16.3: 原来按 0.3s 码率量等（修完单位后 225KB），但**画面静止时 scrcpy 出帧很少**，
        #   文件长得极慢（实测 6.8s 才够），启动被无谓拖长。
        #   改为: 等 64KB 或 5s；打开失败则重试而不是直接抛（ffmpeg 解析头有竞态）。
        min_bytes = 65536
        _deadline = time.time() + 5.0
        while time.time() <= _deadline:
            if self._file.exists() and self._file.stat().st_size > min_bytes:
                break
            time.sleep(0.1)
        _open_deadline = _deadline + 5.0
        while True:
            self._cap = cv2.VideoCapture(str(self._file))
            if self._cap.isOpened():
                break
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
            if time.time() > _open_deadline:
                break
            time.sleep(0.2)
        if self._cap is None or not self._cap.isOpened():
            # v16.4: 失败必须先"收尸"，否则 scrcpy 变孤儿进程继续写 mkv，
            #   文件被锁 -> 下次 unlink 静默失败 -> 新 scrcpy 写不进去 -> 连续启动失败
            #   （实测 12:44/12:45 连挂两次，且第二次把污染扩散到第三次）。
            self._teardown_pipeline()
            raise RuntimeError(f"无法打开 scrcpy 录制流: {self._file}")

    def _teardown_pipeline(self):
        if self._cap:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except Exception:
                self._proc.kill()
        self._proc = None

    # ---------- 读取线程 ----------
    def _drain(self):
        """排空重开后的历史帧，直到追上实时边缘。"""
        for _ in range(600):   # 最多排空 600 帧
            ok = self._cap.read()
            if not ok or not ok[0]:
                break

    def _reader_loop(self):
        while not self._stop:
            try:
                if self._cap is None:
                    time.sleep(0.2)
                    continue
                ok = self._cap.read()
                if ok and ok[0]:
                    frame = ok[1]
                    with self._lock:
                        self._latest = frame
                        self._latest_t = time.time()
                        self.last_size = (frame.shape[1], frame.shape[0])
                    with self._cond:
                        self._cond.notify_all()
                    # 轮转：录制文件过大时重启流水线
                    if time.time() - self._session_start > self.rotate_s:
                        self._teardown_pipeline()
                        self._start_pipeline()
                        self._drain()
                else:
                    # v2.23 文件尾(写入中): 等待追加重试, 贴实时边缘读数(不再重开轮回旧片)
                    if self._proc and self._proc.poll() is not None:
                        self._teardown_pipeline()
                        self._start_pipeline()
                        continue
                    time.sleep(0.05)
                    ok2 = self._cap.read()
                    if ok2 and ok2[0]:
                        frame = ok2[1]
                        with self._lock:
                            self._latest = frame
                            self._latest_t = time.time()
                            self.last_size = (frame.shape[1], frame.shape[0])
                        with self._cond:
                            self._cond.notify_all()
                    else:
                        # 连续重试(最多 6s)后仍无增长 -> 重开并排空已读进度
                        stalled = True
                        for _ in range(120):
                            time.sleep(0.05)
                            ok3 = self._cap.read()
                            if ok3 and ok3[0]:
                                frame = ok3[1]
                                with self._lock:
                                    self._latest = frame
                                    self._latest_t = time.time()
                                    self.last_size = (frame.shape[1], frame.shape[0])
                                with self._cond:
                                    self._cond.notify_all()
                                stalled = False
                                break
                        if stalled:
                            old_count = int(self._cap.get(cv2.CAP_PROP_POS_FRAMES))
                            self._cap.release()
                            self._cap = cv2.VideoCapture(str(self._file))
                            if old_count > 0:
                                self._cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, old_count - 5))
            except Exception:
                time.sleep(0.2)
                try:
                    self._teardown_pipeline()
                    self._start_pipeline()
                except Exception:
                    time.sleep(1.0)

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
        if self._thread:
            self._thread.join(timeout=2)
        self._teardown_pipeline()
