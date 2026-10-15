# -*- coding: utf-8 -*-
"""ESP32 HID 桥离线测试: 用假链路断言"电脑端发的命令串"与"固件解析器"逐字对齐。

固件是 C++(firmware/esp32_hid_touch), 这里跑不了; 但两端能对上的地方就是**命令串**,
所以本测试把每条原语发出的整行命令钉死, 固件那边照同一张表解析 (见 .ino 的 handleLine)。

运行: venv/Scripts/python.exe -X utf8 scripts/test_esp32_bridge.py
覆盖:
  E1  接口对齐 AdbExecutor(可整体替换, 上层不用改)
  E2  touch 模式: t_tap/t_swipe/t_down/t_move/t_up 命令串
  E3  touch 模式: 触点编号越界 / mouse 模式下调 touch 原语 -> 报错
  E4  mouse 模式: home->move->click / press...release 的相对指针序列
  E5  固件 hello 里的屏幕尺寸解析 + 与 device_size 不一致时告警
  E6  事件流字段与 AdbExecutor 一致
  E7  常量契约: 动作命令分类表 + MAX_CONTACTS 与固件 TOUCH_MAX_CONTACTS 一致
  E8  **已发出去的命令绝不再发**(真 TCP 假板子验; 这是"技能放两次"的根治点)
  E9  touch_up 与 touch_down/touch_move 校验行为一致(越界 / mouse 模式)
  E10 window_coords=True: 帧坐标(1280x720)->设备像素 缩放正确, tap_win 不二次缩放
"""
import contextlib
import io
import json
import socket
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.control.esp32_hid import (                       # noqa: E402
    ACTION_HEADS, MAX_CONTACTS, BoardLink, Esp32HidExecutor,
)

_PASS = []


def check(name, cond, extra=""):
    if cond:
        _PASS.append(name)
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {extra}")
        raise SystemExit(1)


class FakeLink:
    """假板子链路: 记录收到的每一行命令, 一律回 ok。"""

    def __init__(self, hello="hello poweron 1234 1080x2340"):
        self.lines = []
        self.last_ms = 1.0
        self.cmd_count = 0
        self.hello = None
        self._hello = hello

    def connect(self):
        self.hello = self._hello
        return self.hello

    def send(self, line, retry=1):
        self.lines.append(line)
        self.last_ms = 1.0
        self.cmd_count += 1
        return True, "ok"

    def close(self):
        pass


class TinyBoard(threading.Thread):
    """极简假板子(真 TCP): 接受连接、按行登记收到的命令, 可配置是否回包。

    用来验证「命令一旦发出去就不再重发」—— 这事假链路(FakeLink)验不了,
    因为重发逻辑就发生在真实 socket 的收发异常路径里。
    """

    def __init__(self, reply=True):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.reply = reply
        self.lines = []
        self.conns = 0

    def run(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            self.conns += 1
            buf = b""
            conn.settimeout(2.0)
            try:
                while True:
                    chunk = conn.recv(256)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, _, buf = buf.partition(b"\n")
                        self.lines.append(line.decode("utf-8"))
                        if self.reply:
                            conn.sendall(b"ok\n")
            except Exception:
                pass
            finally:
                conn.close()

    def stop(self):
        self.srv.close()


def main():
    # E1 接口对齐 AdbExecutor
    ex = Esp32HidExecutor(link=FakeLink(), device_size=(1080, 2340))
    for m in ("tap", "swipe", "key", "tap_win", "swipe_win", "get_device_size",
              "set_window_size", "window_to_device", "close"):
        check(f"E1_has_{m}", callable(getattr(ex, m, None)), f"missing {m}")
    ex.set_window_size(1280, 720)
    check("E1_window_to_device_scales", ex.window_to_device(960, 360) == (810, 1170),
          f"{ex.window_to_device(960, 360)}")

    # E2 touch 模式命令串（固件 handleLine 按同一格式解析）
    lk = FakeLink()
    ex = Esp32HidExecutor(link=lk, device_size=(1080, 2340))
    ex.connect()
    lk.lines.clear()
    ex.tap(300, 400)
    ex.swipe(100, 200, 300, 400, 250)
    ex.touch_down(1, 500, 600)
    ex.touch_move(1, 520, 620)
    ex.touch_up(1)
    ex.ping()
    ex.key(66)
    ex.type_text("abc")
    check("E2_touch_command_lines",
          lk.lines == ["t_tap 300 400 50", "t_swipe 100 200 300 400 250",
                       "t_down 1 500 600", "t_move 1 520 620", "t_up 1",
                       "ping", "key enter", "type abc"],
          f"{lk.lines}")

    # E3 越界 / 模式不符
    ex_ok = True
    try:
        ex.touch_down(99, 1, 2)
        ex_ok = False
    except ValueError:
        pass
    check("E3_cid_out_of_range_raises", ex_ok)
    m = Esp32HidExecutor(link=FakeLink(), mode="mouse", device_size=(1080, 2340))
    m_ok = True
    try:
        m.touch_down(0, 1, 2)
        m_ok = False
    except RuntimeError:
        pass
    check("E3_touch_in_mouse_mode_raises", m_ok)

    # E4 mouse 模式相对指针序列
    lk2 = FakeLink()
    m = Esp32HidExecutor(link=lk2, mode="mouse", device_size=(1080, 2340))
    m.tap(300, 400)
    check("E4_mouse_tap_home_move_click", lk2.lines == ["home", "move 300 400", "click"],
          f"{lk2.lines}")
    lk2.lines.clear()
    m.swipe(100, 200, 300, 400, 250)
    check("E4_mouse_swipe_press_move_release",
          lk2.lines[0] == "move -200 -200" and lk2.lines[1] == "press"
          and lk2.lines[-1] == "release" and len(lk2.lines) >= 5,
          f"{lk2.lines}")

    # E5 hello 屏幕尺寸解析与不一致告警
    ex_t = Esp32HidExecutor(link=FakeLink(), device_size=(1080, 2340))
    ex_t.connect()
    check("E5_surface_parsed", ex_t.surface_size == (1080, 2340), f"{ex_t.surface_size}")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ex_bad = Esp32HidExecutor(link=FakeLink(), device_size=(1080, 1920))
        ex_bad.connect()
    check("E5_mismatch_warns",
          "不一致" in buf.getvalue() and ex_bad.surface_size == (1080, 2340),
          f"{buf.getvalue()!r}")
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        ex_ok2 = Esp32HidExecutor(link=FakeLink(), device_size=(1080, 2340))
        ex_ok2.connect()
    check("E5_match_no_warn", "不一致" not in buf2.getvalue(), f"{buf2.getvalue()!r}")
    ex_mouse = Esp32HidExecutor(link=FakeLink("hello poweron 1234"), mode="mouse",
                               device_size=(1080, 2340))
    ex_mouse.connect()
    check("E5_mouse_hello_has_no_size", ex_mouse.surface_size is None,
          f"{ex_mouse.surface_size}")

    # E6 事件流字段与 AdbExecutor 一致
    log = Path(tempfile.mkdtemp()) / "ev.jsonl"
    exl = Esp32HidExecutor(link=FakeLink(), device_size=(1080, 2340), event_log=log)
    exl.tap(300, 400)
    exl.swipe(10, 20, 30, 40, 120)
    recs = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x]
    need = {"t", "type", "source", "latency_ms"}
    check("E6_event_fields",
          len(recs) == 2 and all(need <= set(r) for r in recs)
          and recs[0]["type"] == "tap" and recs[0]["x"] == 300 and recs[0]["y"] == 400
          and recs[1]["type"] == "swipe" and recs[1]["duration"] == 120,
          f"{recs}")

    # E7 常量契约: 动作命令分类表 + 触点上限与固件一致
    check("E7_action_heads_covers_actions",
          {"t_tap", "t_swipe", "t_down", "t_move", "t_up", "home", "move",
           "press", "release", "click", "key", "type"} <= ACTION_HEADS,
          f"{sorted(ACTION_HEADS)}")
    check("E7_ping_uses_short_timeout", "ping" not in ACTION_HEADS)
    check("E7_max_contacts_matches_firmware", MAX_CONTACTS == 5, f"{MAX_CONTACTS}")

    # E8 已发出的命令绝不再发: 板子不回包(=应答丢失)时若重发, 非幂等动作会被执行两次
    board = TinyBoard(reply=False)
    board.start()
    link = BoardLink("127.0.0.1", board.port, timeout=0.3, action_timeout=0.3)
    ok, _ = link.send("t_tap 10 20 50")
    check("E8_no_resend_when_reply_lost",
          ok is False and board.lines == ["t_tap 10 20 50"] and board.conns == 1,
          f"ok={ok} lines={board.lines} conns={board.conns}")
    link.close()
    board.stop()

    # E9 cid / mode 校验: touch_down|move|up 三个原语行为一致
    ex5 = Esp32HidExecutor(link=FakeLink(), device_size=(1080, 2340))
    e9a = False
    try:
        ex5.touch_down(MAX_CONTACTS, 1, 2)
    except ValueError:
        e9a = True
    check("E9_cid_equal_to_max_rejected", e9a)
    mm = Esp32HidExecutor(link=FakeLink(), mode="mouse", device_size=(1080, 2340))
    e9b = False
    try:
        mm.touch_up(0)
    except RuntimeError:
        e9b = True
    check("E9_touch_up_mouse_mode_raises", e9b)

    # E10 window_coords=True: 帧坐标(1280x720) -> 设备像素 (接 m2 的必要条件,
    #     ActionExecutor 与校准表都在帧空间; 错则所有触点整体偏移)
    lk10 = FakeLink()
    ex10 = Esp32HidExecutor(link=lk10, device_size=(1080, 2340), window_coords=True)
    lk10.lines.clear()
    ex10.tap(960, 360)
    ex10.swipe(640, 360, 320, 180, 200)
    ex10.touch_down(1, 960, 360)
    check("E10_window_coords_scaled",
          lk10.lines == ["t_tap 810 1170 50", "t_swipe 540 1170 270 585 200",
                         "t_down 1 810 1170"], f"{lk10.lines}")
    lk10.lines.clear()
    ex10.tap_win(960, 360)
    check("E10_tap_win_no_double_scale", lk10.lines == ["t_tap 810 1170 50"],
          f"{lk10.lines}")
    lk10.lines.clear()
    ex10_dev = Esp32HidExecutor(link=lk10, device_size=(1080, 2340))
    ex10_dev.tap(960, 360)
    check("E10_default_device_px_passthrough", lk10.lines == ["t_tap 960 360 50"],
          f"{lk10.lines}")

    print(f"ALL PASS ({len(_PASS)})")


if __name__ == "__main__":
    main()
