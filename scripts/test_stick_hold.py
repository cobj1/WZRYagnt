# -*- coding: utf-8 -*-
"""摇杆"持久触点"离线测试（v16.2，不需要板子/模拟器）。

要守住的性质
------------
1. **不支持的执行器自动回退**：adb / mouse 固件没有多点能力时，一切摇杆调用仍走原
   `swipe`（松手式），**行为与改造前一模一样**。
2. **能力判定必须看 mode，不能只看有没有方法**：`Esp32HidExecutor` 在 mouse 模式下
   也有 `touch_down`，但调用会抛 RuntimeError。
3. **持久摇杆只按下一次**：连续 `move_hold` 不重复 `t_down`，只发 `t_move`。
4. **真松手**：`stop()` 在持久模式下发 `t_up`；未按住时不发多余命令。
5. **链路失败可恢复**：`t_move` 失败后标记为未按住，下次会重新 `t_down`。

运行: venv/Scripts/python.exe -X utf8 scripts/test_stick_hold.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.action.executor_v2 import ActionExecutor, PTS       # noqa: E402
from wzry.control.esp32_hid import Esp32HidExecutor            # noqa: E402

CX, CY = PTS["move_stick_center"]
_PASS = []


def check(name, cond, extra=""):
    if cond:
        _PASS.append(name)
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {extra}")
        raise SystemExit(1)


class FakeExec:
    """可配置的假执行器。mode=None 模拟 adb（无 mode / 无 touch_*）。"""

    def __init__(self, mode=None, move_ok=True, down_ok=True, up_ok=True):
        self.calls = []
        if mode is not None:
            self.mode = mode
        self._move_ok = move_ok
        self._down_ok = down_ok
        self._up_ok = up_ok
        if mode == "touch":
            self.touch_down = self._touch_down
            self.touch_move = self._touch_move
            self.touch_up = self._touch_up

    def _touch_down(self, cid, x, y):
        self.calls.append(("t_down", cid, x, y))
        return self._down_ok

    def _touch_move(self, cid, x, y):
        self.calls.append(("t_move", cid, x, y))
        return self._move_ok

    def _touch_up(self, cid):
        self.calls.append(("t_up", cid))
        return self._up_ok

    def swipe(self, x1, y1, x2, y2, duration=100, source="policy"):
        self.calls.append(("swipe", x1, y1, x2, y2, duration))
        return True

    def tap(self, x, y, source="policy"):
        self.calls.append(("tap", x, y))
        return True


class FakeLink:
    """假板子链路：记录收到的每一行命令，一律回 ok。"""

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
        self.cmd_count += 1
        return True, "ok"

    def close(self):
        pass


def main():
    R = 0.8
    THETA = 0.0                                  # 0 = 右 -> 目标点在中心右侧
    exp_x = int(CX + 0.12 * 720 * R)             # executor_v2._stick_point 的既定公式
    exp_y = CY

    # ---------- H1 adb(无多点能力) 自动回退 ----------
    f = FakeExec()
    ex = ActionExecutor(f)
    check("H1a adb->hold_enabled=False", ex.hold_enabled is False, f"{ex.hold_enabled}")
    check("H1b adb->move_hold 返回 False", ex.move_hold(THETA, R) is False)
    ex.stop()
    check("H1c adb->stop 仍走 swipe(原行为)",
          f.calls == [("swipe", CX, CY, CX, CY, 1)], f"{f.calls}")

    # ---------- H2 touch 能力 -> 持久摇杆 ----------
    f = FakeExec(mode="touch")
    ex = ActionExecutor(f)
    check("H2a touch->hold_enabled=True", ex.hold_enabled is True)
    check("H2b 首次 move_hold 返回 True", ex.move_hold(THETA, R) is True)
    check("H2c 首次=先 t_down 中心, 再 t_move 目标",
          f.calls == [("t_down", 0, CX, CY), ("t_move", 0, exp_x, exp_y)], f"{f.calls}")
    check("H2d 已按住", ex.stick_held is True)

    f.calls.clear()
    ex.move_hold(THETA, R)
    check("H2e 再次 move_hold 只发 t_move(不重复 t_down)",
          f.calls == [("t_move", 0, exp_x, exp_y)], f"{f.calls}")

    # ---------- H3 stop 真松手 ----------
    f.calls.clear()
    ex.stop()
    check("H3a stop->t_up", f.calls == [("t_up", 0)], f"{f.calls}")
    check("H3b 已松手", ex.stick_held is False)
    f.calls.clear()
    ex.stop()
    check("H3c 未按住时不发多余命令", f.calls == [], f"{f.calls}")

    # ---------- H4 强制开但硬件不支持 -> 静默降级 ----------
    f = FakeExec()
    ex = ActionExecutor(f, hold_enabled=True)
    check("H4 强制开+不支持->降级 False", ex.hold_enabled is False, f"{ex.hold_enabled}")

    # ---------- H5 t_move 失败可恢复 ----------
    f = FakeExec(mode="touch", move_ok=False)
    ex = ActionExecutor(f)
    check("H5a t_move 失败 -> 返回 False", ex.move_hold(THETA, R) is False)
    check("H5b 失败后标记未按住(下次会重新 t_down)", ex.stick_held is False)
    f.calls.clear()
    f._move_ok = True
    ex.move_hold(THETA, R)
    check("H5c 恢复后重新 t_down + t_move",
          [c[0] for c in f.calls] == ["t_down", "t_move"], f"{f.calls}")

    # ---------- H6 真 Esp32HidExecutor(window_coords) 命令串 ----------
    lk = FakeLink()
    b = Esp32HidExecutor(link=lk, device_size=(1080, 2340), window_coords=True)
    b.connect()
    ex = ActionExecutor(b)
    check("H6a 真执行器 hold_enabled=True", ex.hold_enabled is True)
    ex.move_hold(THETA, R)
    # 帧坐标(1280x720) -> 设备像素(1080x2340): int(v*dev/win)，只缩放一次
    _dx0, _dy0 = int(CX * 1080 / 1280), int(CY * 2340 / 720)
    _dx1, _dy1 = int(exp_x * 1080 / 1280), int(exp_y * 2340 / 720)
    check("H6b t_down/t_move 命令串与坐标缩放",
          lk.lines[-2:] == [f"t_down 0 {_dx0} {_dy0}", f"t_move 0 {_dx1} {_dy1}"],
          f"{lk.lines}")
    ex.stop()
    check("H6c stop->t_up 0", lk.lines[-1] == "t_up 0", f"{lk.lines[-3:]}")

    # ---------- H7 不缩放(直接设备像素) ----------
    lk2 = FakeLink()
    b2 = Esp32HidExecutor(link=lk2, device_size=(1080, 2340))
    b2.connect()
    ex2 = ActionExecutor(b2)
    ex2.move_hold(THETA, R)
    check("H7 window_coords=False -> 原样设备像素",
          lk2.lines[-2:] == [f"t_down 0 {CX} {CY}", f"t_move 0 {exp_x} {exp_y}"],
          f"{lk2.lines}")

    # ---------- H8 mouse 模式必须判 False(有方法但会抛) ----------
    f = FakeExec(mode="mouse")
    f.touch_down = lambda *a: (_ for _ in ()).throw(RuntimeError("mode is touch"))
    ex = ActionExecutor(f)
    check("H8 mouse 模式 -> hold_enabled=False(只看 hasattr 会误判)",
          ex.hold_enabled is False, f"{ex.hold_enabled}")

    # ---------- H9 真 Esp32 mouse 模式 ----------
    b3 = Esp32HidExecutor(link=FakeLink(), mode="mouse", device_size=(1080, 2340))
    ex3 = ActionExecutor(b3)
    check("H9 真执行器 mouse 模式 -> 回退", ex3.hold_enabled is False, f"{ex3.hold_enabled}")

    print(f"\n全部通过: {len(_PASS)} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
