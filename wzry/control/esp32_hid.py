# -*- coding: utf-8 -*-
"""ESP32-S3 HID 桥控制通道（真机路线）：`AdbExecutor` 的等价替代。

设计约束
--------
1. **接口对齐** `wzry.control.executor.AdbExecutor`：同样提供
   `tap / swipe / key / tap_win / swipe_win / get_device_size / set_window_size /
   window_to_device / close`，同样吃【设备像素绝对坐标】，同样把动作写入
   JSONL 事件流（字段与 AdbExecutor 一致：t/type/x/y/source/latency_ms）。
   因此 `ActionExecutor(executor=Esp32HidExecutor(...))` 可整体替换 adb，
   上层感知、决策、状态机、录制一行不用改。

2. **板子无逻辑**：ESP32-S3 只做 USB HID 外设（插手机 USB 口，对手机表现为
   鼠标/键盘或触摸屏），通过 Wi-Fi 的 TCP 行协议受控：电脑连上板子后，每行
   发一条命令，板子回 `ok` 或 `err <reason>`；连接建立时板子先主动上报
   `hello <reset_reason> <uptime_ms>`。

3. **两种位置模式**（由烧的固件决定，构造参数 `mode`）：

   `mode="touch"`（推荐，绝对坐标 + 多点）
     固件实现 HID 触摸屏 Digitizer，直接收绝对坐标，无需指针追踪，
     且可用 `touch_down/move/up` 做**多点同时按**（左手摇杆 + 右手技能）。
     命令：`t_tap <x> <y> <ms>` / `t_swipe <x1> <y1> <x2> <y2> <ms>` /
           `t_down <id> <x> <y>` / `t_move <id> <x> <y>` / `t_up <id>`

   `mode="mouse"`（退路，单点 + 相对位移）
     固件只实现普通 USB 鼠标：`home`（指针归零到左上角已知点）/ `move <dx> <dy>` /
     `press` / `release` / `click` / `scroll <v> <h>`。
     绝对坐标靠 `home` + 相对移动 + 本类内部自跟踪实现；**无法多点同时按**，
     只能串行（走 -> 松手 -> 放技能）。指针若漂移，用 `mark_pointer(x, y)`
     由外部视觉闭环回填纠正。

公共命令：`ping` / `key <combo>` / `type <text>`（仅 ASCII）。

用法
----
    python -m wzry.control.esp32_hid --host 192.168.1.50 ping
    python -m wzry.control.esp32_hid --host 192.168.1.50 tap 600 320
    python -m wzry.control.esp32_hid --host 192.168.1.50 --mode mouse swipe 200 600 200 400 300
"""
import argparse
import json
import re
import socket
import threading
import time
from pathlib import Path

DEFAULT_BOARD_PORT = 7300
HELLO_PREFIX = "hello"
# 与固件 TOUCH_MAX_CONTACTS 必须一致：cid 超出这个范围固件会回
# "err contact id out of range"，两端不一致会让上层以为下发成功、实际被执行层拒绝。
MAX_CONTACTS = 5

# 这些命令板子要"真的动完"才回包（t_swipe 要按住 duration；mouse 的 home 要连发几十个
# HID 报告），读应答的超时必须放宽，否则会被误判超时 → 断连 → 动作被打断。
ACTION_HEADS = frozenset({
    "t_tap", "t_swipe", "t_down", "t_move", "t_up",
    "home", "move", "press", "release", "click", "scroll", "key", "type",
})

# Android keycode -> 板子 `key <combo>` 的键名（未被登记的 keycode 直接透传数字）
# 注：0 = KEYCODE_UNKNOWN，板子把它当空操作（探活/测延迟用）。
#     3/4 = Android 的 HOME/BACK：普通 USB 键盘 HID 产生不了，板子会明确回
#     "err unsupported key"，这两个键只能靠点屏幕上的按钮。
ANDROID_KEY_NAMES = {
    0: "noop",
    3: "home",
    4: "back",
    19: "up",
    20: "down",
    21: "left",
    22: "right",
    66: "enter",
    82: "menu",
    111: "esc",
}


class BoardLink:
    """与 ESP32-S3 HID 桥的 TCP 行协议连接（自动重连 + 往返延迟统计）。

    只负责收发，不关心命令语义。若固件改成"板子主动拨号到电脑"（离线式连接，
    不依赖板子固定 IP），只需替换本类为监听端实现，`Esp32HidExecutor` 无需改动。
    """

    def __init__(self, host, port=DEFAULT_BOARD_PORT, timeout=0.5, action_timeout=3.0):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)                 # 只读命令（ping）的读应答超时
        self.action_timeout = float(action_timeout)   # 动作命令的读应答超时，见 ACTION_HEADS
        self.last_ms = 0.0
        self.cmd_count = 0
        self.hello = None
        self.last_error = None
        self._sock = None
        self._rxbuf = b""
        self._lock = threading.Lock()

    # ---------- 连接 ----------
    def _connect_locked(self):
        """建立连接并尝试读入板子的 hello 行（调用方须已持有 _lock）。"""
        self._close_locked()
        self._rxbuf = b""
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._sock.settimeout(self.timeout)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            first = self._recv_line_locked()
        except Exception:
            first = ""
        if first.startswith(HELLO_PREFIX):
            self.hello = first
        else:
            # 板子未按约定上报 hello：把非 hello 行塞回缓冲，避免吞掉首条应答
            if first:
                self._rxbuf = (first + "\n").encode("utf-8")
        return self.hello

    def connect(self):
        with self._lock:
            return self._connect_locked()

    def _close_locked(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def close(self):
        with self._lock:
            self._close_locked()

    # ---------- 收发 ----------
    def _recv_line_locked(self):
        while b"\n" not in self._rxbuf:
            chunk = self._sock.recv(256)
            if not chunk:
                raise ConnectionError("板子连接已断开")
            self._rxbuf += chunk
        line, _, self._rxbuf = self._rxbuf.partition(b"\n")
        return line.decode("utf-8", errors="replace").strip()

    def send(self, line, retry=1):
        """发一条命令，返回 (ok, reply)。

        重发只在「命令很可能没送达」时发生：链路不存在，或 sendall 本身失败。
        **一旦 sendall 成功，之后无论读应答超时还是出错都绝不重发** —— 命令可能已经
        在板子上执行了，而 t_tap / t_swipe / t_down 这类动作不是幂等的，重发会变成
        "技能放两次 / 摇杆推两遍"。

        读应答超时按命令种类区分：动作命令用 action_timeout（板子要动完才回包），
        其余用 timeout。
        """
        payload = (line + "\n").encode("utf-8")
        head = line.split(" ", 1)[0]
        read_timeout = self.action_timeout if head in ACTION_HEADS else self.timeout

        for attempt in range(retry + 1):
            with self._lock:
                if self._sock is None:
                    try:
                        self._connect_locked()
                    except Exception as exc:
                        self.last_error = f"{type(exc).__name__}: {exc}"
                        return False, "err no link"
                t0 = time.perf_counter()
                try:
                    self._sock.settimeout(read_timeout)
                    self._sock.sendall(payload)
                except Exception as exc:  # 发送就失败：命令基本没送达，可以重发
                    self._close_locked()
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    if attempt < retry:
                        time.sleep(0.05)
                        continue
                    self.last_ms = (time.perf_counter() - t0) * 1000.0
                    return False, f"err {type(exc).__name__}"
                try:
                    reply = self._recv_line_locked()
                except Exception as exc:
                    # 已经发出去了：不重发。断链会让固件松开全部触点/按键，状态是干净的。
                    self._close_locked()
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    self.last_ms = (time.perf_counter() - t0) * 1000.0
                    return False, f"err {type(exc).__name__}"
                self.last_ms = (time.perf_counter() - t0) * 1000.0
                self.cmd_count += 1
                if not reply.startswith("ok"):
                    self.last_error = reply
                return reply.startswith("ok"), reply
        return False, "err no link"


class Esp32HidExecutor:
    """ESP32-S3 HID 桥执行器，接口对齐 `AdbExecutor`（设备像素绝对坐标）。"""

    MODE_TOUCH = "touch"
    MODE_MOUSE = "mouse"

    def __init__(self, host=None, port=DEFAULT_BOARD_PORT, mode=MODE_TOUCH,
                 device_size=None, event_log=None, timeout=0.5,
                 action_timeout=3.0, tap_ms=50, step_ms=16, link=None,
                 window_coords=False):
        """
        host           : 板子的 Wi-Fi 地址（板子作 TCP 服务端）
        mode           : "touch"（绝对坐标 + 多点）| "mouse"（相对指针 + 单点）
        device_size    : 手机物理分辨率 (w, h)；真机建议显式传入，否则 window_to_device 不可用
        event_log      : 动作事件流输出路径（JSONL，字段与 AdbExecutor 一致）
        timeout        : 只读命令（ping）的读应答超时
        action_timeout : 动作命令的读应答超时。必须明显大于板端最长的动作耗时 ——
                         t_swipe 要按住 duration 之后才回包，上层用的是 400ms
                         （m3_agent_v3 的 MOVE_DURATION_MS），所以默认留了 3s 余量
        window_coords  : 是否吃**帧坐标(1280x720)** 而非设备像素。上层 ActionExecutor 与
                         校准表(configs/calibration_absolute.json)都在 1280x720 帧空间，
                         接 ESP32 板子时必须置 True，否则 t_tap/t_swipe 会把帧坐标当设备
                         像素发出去(所有触点整体偏移)。False=调用方直接给设备像素(默认)。
        tap_ms         : touch 模式点击的按下时长
        step_ms        : mouse 模式拖动的插值步长（越小越平滑、命令数越多）
        link           : 复用已有 BoardLink（便于测试注入假链路）
        """
        self.mode = mode
        self.window_coords = bool(window_coords)
        self.event_log = Path(event_log) if event_log else None
        self._link = link or BoardLink(host, port=port, timeout=timeout,
                                       action_timeout=action_timeout)
        self._device_size = tuple(device_size) if device_size else None
        if self.window_coords:
            self.set_window_size(1280, 720)
        self.tap_ms = int(tap_ms)
        self.step_ms = int(step_ms)
        self.last_cmd_ms = 0.0
        self.cmd_count = 0
        self.surface_size = None        # 固件上报的屏幕尺寸 (w, h)（touch 版 hello 里带）
        self._pointer = None            # mouse 模式：自跟踪的指针位置
        self._contacts = {}             # touch 模式：各触点最近一次的绝对坐标
        self._lock = threading.Lock()
        self._event_fp = None

    # ---------- 连接 ----------
    @property
    def link(self):
        return self._link

    def connect(self):
        """连接板子并返回板子的 hello 行（None 表示未按约定上报）。

        touch 版固件的 hello 带屏幕尺寸（`hello <reset> <uptime> <W>x<H>`）；与构造参数
        device_size 不一致时给出警告 —— 不一致会让所有触点整体偏移，且**不会报错**
        （静默错位），所以必须提示。
        """
        hello = self._link.connect()
        self.surface_size = None
        if hello:
            m = re.search(r"(\d+)\s*x\s*(\d+)\s*$", hello)
            if m:
                self.surface_size = (int(m.group(1)), int(m.group(2)))
                if self._device_size and tuple(self._device_size) != self.surface_size:
                    print(f"[esp32_hid] 警告: 固件屏幕尺寸 {self.surface_size} 与 "
                          f"device_size {tuple(self._device_size)} 不一致，触点会整体偏移；"
                          f"请改固件 TOUCH_SURFACE_W/H 或改用 "
                          f"device_size={self.surface_size}")
        return hello

    def ping(self):
        return self._send("ping")

    def stat(self):
        """只读诊断：返回板子自报的 `cmds=/drops=/down=/up=` 原始文本。

        `drops` 是 HID 报告发送失败(TinyUSB FIFO 满等)的累计次数 —— 大于 0 说明
        触点实际位置可能落后于电脑端认知（摇杆/技能位置漂移的隐性来源）。
        未连板子/未知命令时返回 None，不抛。
        """
        ok, reply = self._link.send("stat")
        self.last_cmd_ms = self._link.last_ms
        self.cmd_count += 1
        return reply if ok else None

    # ---------- 内部：发命令 + 记事件 ----------
    def _send(self, line, event=None):
        ok, reply = self._link.send(line)
        self.last_cmd_ms = self._link.last_ms
        self.cmd_count += 1
        if event is not None:
            e = dict(event)
            e["ok"] = bool(ok)
            e["reply"] = reply
            e["latency_ms"] = round(self.last_cmd_ms, 2)
            self._log(e)
        return bool(ok)

    # ---------- 尺寸 / 坐标映射（与 AdbExecutor 同语义）----------
    def get_device_size(self):
        """返回手机物理分辨率 (w, h)。需构造时传入 device_size。"""
        if not self._device_size:
            raise RuntimeError("未提供 device_size，无法换算窗口<->设备坐标；"
                               "请用 Esp32HidExecutor(device_size=(1080, 2340))")
        return self._device_size

    def set_window_size(self, w, h):
        """记录采集窗口尺寸；坐标映射按 窗口像素/设备像素 比例缩放。"""
        self._win_w, self._win_h = int(w), int(h)

    def window_to_device(self, x_win, y_win):
        """窗口像素 -> 设备像素（线性缩放）。未 set_window_size 时按设备尺寸原样返回。"""
        x_win, y_win = int(round(x_win)), int(round(y_win))
        if getattr(self, "_win_w", None):
            dev_w, dev_h = self.get_device_size()
            return int(x_win * dev_w / self._win_w), int(y_win * dev_h / self._win_h)
        return x_win, y_win

    # ---------- 动作原语 ----------
    def _to_dev(self, x, y):
        """入参坐标 -> 设备像素。window_coords=True 时按 1280x720 帧空间线性换算。"""
        if self.window_coords:
            return self.window_to_device(x, y)
        return int(round(x)), int(round(y))

    def tap(self, x, y, source="policy"):
        """点击(x, y)：window_coords=True 时是帧坐标, 否则是设备像素绝对坐标。"""
        x, y = self._to_dev(x, y)
        if self.mode == self.MODE_MOUSE:
            return self._mouse_tap(x, y, source)
        return self._send(f"t_tap {x} {y} {self.tap_ms}",
                          {"type": "tap", "x": x, "y": y, "source": source, "mode": self.mode})

    def swipe(self, x1, y1, x2, y2, duration=100, source="policy"):
        """单指滑动：起点 -> 终点，按住 duration 毫秒（坐标空间同 tap）。"""
        x1, y1 = self._to_dev(x1, y1)
        x2, y2 = self._to_dev(x2, y2)
        duration = int(duration)
        if self.mode == self.MODE_MOUSE:
            return self._mouse_swipe(x1, y1, x2, y2, duration, source)
        return self._send(f"t_swipe {x1} {y1} {x2} {y2} {duration}",
                          {"type": "swipe", "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                           "duration": duration, "source": source, "mode": self.mode})

    def tap_win(self, x_win, y_win, source="policy"):
        """按帧像素点击。与 tap 同一坐标空间(由 window_coords 决定), 直接委托避免二次缩放。"""
        return self.tap(x_win, y_win, source=source)

    def swipe_win(self, x1, y1, x2, y2, duration=100, source="policy"):
        """按帧像素滑动。与 swipe 同一坐标空间, 直接委托避免二次缩放。"""
        return self.swipe(x1, y1, x2, y2, duration=duration, source=source)

    # ---------- 多点触摸（仅 touch 模式）----------
    # 王者荣耀需要"左手摇杆按住 + 右手技能同时按"，单点鼠标做不到，
    # 这三个原语就是上层做并发手势的积木。
    def touch_down(self, cid, x, y):
        """第 cid 个触点按下（cid: 0..MAX_CONTACTS-1）。"""
        return self._touch("t_down", cid, x, y)

    def touch_move(self, cid, x, y):
        """第 cid 个触点移动到绝对坐标。"""
        return self._touch("t_move", cid, x, y)

    def touch_up(self, cid):
        """第 cid 个触点抬起。"""
        cid = self._require_contact(cid)
        x, y = self._pointer_of(cid)
        return self._send(f"t_up {cid}",
                          {"type": "touch_up", "cid": cid, "x": x, "y": y})

    def _require_contact(self, cid):
        """共用的模式 + 触点编号校验（touch_down/move/up 三个原语行为保持一致）。"""
        if self.mode != self.MODE_TOUCH:
            raise RuntimeError("多点触摸需要 mode='touch'（固件须实现 HID 触摸屏）")
        cid = int(cid)
        if not 0 <= cid < MAX_CONTACTS:
            raise ValueError(f"触点编号越界: {cid}")
        return cid

    def _touch(self, verb, cid, x, y):
        cid = self._require_contact(cid)
        x, y = self._to_dev(x, y)          # 坐标空间同 tap（window_coords 决定）
        with self._lock:
            self._contacts[cid] = (x, y)
        return self._send(f"{verb} {cid} {x} {y}",
                          {"type": "touch", "cid": cid, "x": x, "y": y})

    def _pointer_of(self, cid):
        return self._contacts.get(int(cid), (-1, -1))

    # ---------- 键盘 / 文本 ----------
    def key(self, keycode, source="policy"):
        """按键。keycode 为 Android keycode（如 4=BACK），未知值直接透传数字。"""
        name = ANDROID_KEY_NAMES.get(int(keycode), str(int(keycode)))
        return self._send(f"key {name}",
                          {"type": "key", "keycode": str(keycode), "name": name,
                           "source": source})

    def type_text(self, text, source="policy"):
        """输入 ASCII 文本（板子当键盘；中文需走手机输入法，见项目限制）。"""
        if not text or not text.isascii():
            raise ValueError("板子只支持 ASCII 键盘输入")
        return self._send(f"type {text}",
                          {"type": "type", "text": text, "source": source})

    # ---------- mouse 模式实现（相对指针 + 单点）----------
    def home_pointer(self):
        """把相对指针推到左上角已知点，作为坐标基准。"""
        ok = self._send("home", {"type": "home", "source": "policy"})
        if ok:
            self._pointer = (0, 0)
        return ok

    def mark_pointer(self, x, y):
        """外部视觉闭环纠正后回填指针真实位置（mouse 模式防漂移）。"""
        self._pointer = (int(x), int(y))

    def move_rel(self, dx, dy):
        """相对移动指针（板子按恒定步速移动，保证 iOS/Android 指针加速可预期）。"""
        dx, dy = int(round(dx)), int(round(dy))
        if dx == 0 and dy == 0:
            return True
        ok = self._send(f"move {dx} {dy}", {"type": "move_rel", "dx": dx, "dy": dy})
        if ok and self._pointer is not None:
            self._pointer = (self._pointer[0] + dx, self._pointer[1] + dy)
        return ok

    def _move_to(self, x, y):
        """把指针移到绝对坐标（内部：home 兜底 + 相对移动）。"""
        if self._pointer is None and not self.home_pointer():
            return False
        return self.move_rel(x - self._pointer[0], y - self._pointer[1])

    def _mouse_tap(self, x, y, source):
        with self._lock:
            if not self._move_to(x, y):
                return False
            return self._send("click",
                              {"type": "tap", "x": x, "y": y, "source": source,
                               "mode": self.mode})

    def _mouse_swipe(self, x1, y1, x2, y2, duration, source):
        with self._lock:
            if not self._move_to(x1, y1):
                return False
            if not self._send("press"):
                return False
            steps = max(1, duration // self.step_ms)
            for i in range(1, steps + 1):
                tx = x1 + (x2 - x1) * i / steps
                ty = y1 + (y2 - y1) * i / steps
                if not self.move_rel(round(tx) - self._pointer[0],
                                     round(ty) - self._pointer[1]):
                    self._send("release")
                    return False
            return self._send("release",
                              {"type": "swipe", "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                               "duration": duration, "source": source, "mode": self.mode})

    # ---------- 事件流 ----------
    def _log(self, event):
        event = {"t": time.time(), **event}
        if self.event_log:
            if self._event_fp is None:
                self.event_log.parent.mkdir(parents=True, exist_ok=True)
                self._event_fp = open(self.event_log, "a", encoding="utf-8")
            self._event_fp.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._event_fp.flush()

    def close(self):
        self._link.close()
        if self._event_fp:
            self._event_fp.close()
            self._event_fp = None


def _main():
    ap = argparse.ArgumentParser(description="ESP32-S3 HID 桥连通性与原语自测")
    ap.add_argument("--host", required=True, help="板子 Wi-Fi 地址")
    ap.add_argument("--port", type=int, default=DEFAULT_BOARD_PORT)
    ap.add_argument("--mode", choices=[Esp32HidExecutor.MODE_TOUCH,
                                       Esp32HidExecutor.MODE_MOUSE],
                    default=Esp32HidExecutor.MODE_TOUCH)
    ap.add_argument("--event-log", default=None, help="事件流输出路径（可选）")
    ap.add_argument("cmd", choices=["ping", "stat", "home", "tap", "swipe", "key", "type"],
                    help="要执行的原语")
    ap.add_argument("args", nargs="*", help="tap: x y / swipe: x1 y1 x2 y2 [ms] / key: keycode / type: text")
    a = ap.parse_args()

    ex = Esp32HidExecutor(host=a.host, port=a.port, mode=a.mode, event_log=a.event_log)
    hello = ex.connect()
    print(f"连接 {a.host}:{a.port} 板子 hello={hello!r}")
    t0 = time.perf_counter()
    if a.cmd == "ping":
        ok = ex.ping()
    elif a.cmd == "stat":
        _rep = ex.stat()
        print(f"stat: {_rep}")
        ex.close()
        return 0 if _rep else 1
    elif a.cmd == "home":
        ok = ex.home_pointer()
    elif a.cmd == "tap":
        ok = ex.tap(int(a.args[0]), int(a.args[1]))
    elif a.cmd == "swipe":
        ok = ex.swipe(*(int(v) for v in a.args[:4]),
                      duration=int(a.args[4]) if len(a.args) > 4 else 100)
    elif a.cmd == "key":
        ok = ex.key(int(a.args[0]))
    else:
        ok = ex.type_text(" ".join(a.args))
    dt = (time.perf_counter() - t0) * 1000.0
    print(f"{a.cmd}: {'OK' if ok else 'FAIL'}  往返 {ex.last_cmd_ms:.1f}ms / 端到端 {dt:.1f}ms")
    ex.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
