# -*- coding: utf-8 -*-
"""apply_action 接线测试（v16.2）—— 直接驱动 m2_agent_v2.apply_action，不需要板子/模拟器。

覆盖两件要紧的事
----------------
A. **边走边打**：摇杆按住（触点 0）时点攻击/技能，必须
   - 落在**另一个触点**上（固件 `freeContact()` 自动挑空闲触点）
   - **不松摇杆**（命令流里不能出现 `t_up 0`）
   这是"左手摇杆 + 右手技能"能同时生效的前提。
B. **所有移动入口都走持久摇杆**：`map_move` / `move` / `stick_move` 三条路径都必须在
   hold 模式下不松手。曾经 `move`（追击/逃生那 5 个 reason）被漏掉，仍走 swipe —— 那会
   ①每次松手 ②占 worker 450ms 挤掉攻击机会。本测试钉死这个回归。
   非 hold 模式（adb / mouse）必须**保持原 swipe 行为**。

运行: venv/Scripts/python.exe -X utf8 scripts/test_apply_action.py
"""
import contextlib
import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import m2_agent_v2 as m                                     # noqa: E402
from wzry.action.executor_v2 import ActionExecutor, PTS     # noqa: E402
from wzry.control.esp32_hid import Esp32HidExecutor          # noqa: E402

_PASS = []


def check(name, cond, extra=""):
    if cond:
        _PASS.append(name)
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {extra}")
        raise SystemExit(1)


class FakeExec:
    """可配置假执行器。mode=None 模拟 adb（无多点能力）。"""

    def __init__(self, mode=None):
        self.calls = []
        if mode is not None:
            self.mode = mode
        if mode == "touch":
            self.touch_down = self._down
            self.touch_move = self._move
            self.touch_up = self._up

    def _down(self, cid, x, y):
        self.calls.append(("t_down", cid, x, y))
        return True

    def _move(self, cid, x, y):
        self.calls.append(("t_move", cid, x, y))
        return True

    def _up(self, cid):
        self.calls.append(("t_up", cid))
        return True

    def swipe(self, x1, y1, x2, y2, duration=100, source="policy"):
        self.calls.append(("swipe", x1, y1, x2, y2, duration))
        return True

    def tap(self, x, y, source="policy"):
        self.calls.append(("tap", x, y))
        return True


class FakeLink:
    def __init__(self, hello="hello poweron 1 3200x1440"):
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


def _new_cd(**kw):
    cd = {"camp": "blue", "self_est": [0.10, 0.90]}
    cd.update(kw)
    return cd


FAR = {"type": "map_move", "nx": 0.90, "ny": 0.10}        # 远离 self_est -> 会推杆
AX, AY = PTS["attack"]


def main():
    # ---------- A1 真执行器 + 假链路：按住摇杆后点攻击 ----------
    lk = FakeLink()
    board = Esp32HidExecutor(link=lk, device_size=(3200, 1440), window_coords=True)
    board.connect()
    ex = ActionExecutor(board, hold_enabled=True)
    check("A1a 真执行器 hold 模式已启用", ex.hold_enabled is True)
    cd = _new_cd()
    m.apply_action(ex, FAR, cd)
    lk.lines.clear()
    check("A1b 摇杆已按住", ex.stick_held is True and ex.stick_cid == 0)

    m.apply_action(ex, {"type": "attack", "priority": "free"}, cd)
    check("A1c 攻击落到点击(不是 swipe)",
          any(l.startswith("t_tap ") for l in lk.lines), f"{lk.lines}")
    check("A1d 攻击期间**没有**松摇杆(t_up 0 不出现)",
          not any(l.strip() == "t_up 0" for l in lk.lines), f"{lk.lines}")
    check("A1e 攻击后摇杆仍按住", ex.stick_held is True)

    # 摇杆方向仍可继续更新(即"边走边打")
    lk.lines.clear()
    cd2 = _new_cd(self_est=[0.10, 0.90], _map_t=time.time() - 1.0)
    m.apply_action(ex, {"type": "map_move", "nx": 0.80, "ny": 0.20}, cd2)
    check("A1f 打完还能继续改朝向(只 t_move, 不重新 t_down)",
          lk.lines and all(l.startswith("t_move 0 ") for l in lk.lines), f"{lk.lines}")
    check("A1g 全程未松手",
          not any(l.strip() == "t_up 0" for l in lk.lines), f"{lk.lines}")

    # ---------- A2 技能同理 ----------
    lk.lines.clear()
    m.apply_action(ex, {"type": "skill", "id": 1, "mode": "tap"}, _new_cd())
    check("A2a 技能点击与摇杆共存(无 t_up 0)",
          any(l.startswith("t_tap ") for l in lk.lines)
          and not any(l.strip() == "t_up 0" for l in lk.lines), f"{lk.lines}")

    # ---------- B1 map_move 走 hold(不 swipe) ----------
    f = FakeExec(mode="touch")
    ex2 = ActionExecutor(f, hold_enabled=True)
    m.apply_action(ex2, FAR, _new_cd())
    check("B1a map_move 首拍 = t_down + t_move",
          [c[0] for c in f.calls] == ["t_down", "t_move"], f"{f.calls}")
    check("B1b map_move 未使用 swipe",
          not any(c[0] == "swipe" for c in f.calls), f"{f.calls}")

    # ---------- B2 move(追击/逃生那条路径) 也必须走 hold ----------
    f.calls.clear()
    m.apply_action(ex2, {"type": "move", "theta": 0.5, "r": 0.8, "duration_ms": 450},
                   _new_cd(_map_t=time.time() - 1.0))
    check("B2a move 走 hold(不是 swipe) —— 曾经的漏改点",
          f.calls and all(c[0] == "t_move" for c in f.calls), f"{f.calls}")
    check("B2b move 未使用 swipe",
          not any(c[0] == "swipe" for c in f.calls), f"{f.calls}")

    # ---------- B3 非 hold 模式保持原 swipe 行为 ----------
    fa = FakeExec()                                        # 无 mode -> 模拟 adb
    exa = ActionExecutor(fa)
    check("B3a adb 下 hold_enabled=False", exa.hold_enabled is False)
    m.apply_action(exa, FAR, _new_cd())
    check("B3b adb 下 map_move 仍走 swipe(原行为)",
          [c[0] for c in fa.calls] == ["swipe"], f"{fa.calls}")
    fa.calls.clear()
    m.apply_action(exa, {"type": "move", "theta": 0.5, "r": 0.8, "duration_ms": 450},
                   _new_cd())
    check("B3c adb 下 move 仍走 swipe(原行为)",
          [c[0] for c in fa.calls] == ["swipe"], f"{fa.calls}")

    # ---------- B4 到达即停：松手且不推杆 ----------
    f.calls.clear()
    m.apply_action(ex2, {"type": "map_move", "nx": 0.52, "ny": 0.51},
                   _new_cd(self_est=[0.50, 0.50]))
    check("B4a 到达 -> t_up(真松手)",
          [c[0] for c in f.calls] == ["t_up"], f"{f.calls}")
    check("B4b 到达 -> 不再推杆", ex2.stick_held is False)

    # ---------- B5 回城/恢复/召唤师：先松手再点按钮 ----------
    for _t, _nm in (("recall", "回城"), ("restore", "恢复"), ("summoner", "召唤师")):
        f.calls.clear()
        ex2.move_hold(0.0, 0.8)                            # 先按住
        f.calls.clear()
        m.apply_action(ex2, {"type": _t}, _new_cd())
        check(f"B5 {_nm} 先 t_up 再点按钮",
              [c[0] for c in f.calls] == ["t_up", "tap"], f"{f.calls}")

    # ---------- C1 skill drag 的 target 必须透传(曾经的缺口: 只传 id/mode -> 必抛) ----------
    f.calls.clear()
    m.apply_action(ex2, {"type": "skill", "id": 1, "mode": "drag",
                         "target": [900, 400], "duration": 200}, _new_cd())
    check("C1a drag 透传 target/duration(不是被吞成异常)",
          f.calls == [("swipe", 963, 634, 900, 400, 200)], f"{f.calls}")

    f.calls.clear()
    m.apply_action(ex2, {"type": "skill", "id": 1, "mode": "cancel"}, _new_cd())
    check("C1b cancel 仍可用(不需要 target)",
          f.calls and f.calls[0][0] == "swipe", f"{f.calls}")

    try:
        m.apply_action(ex2, {"type": "skill", "id": 1, "mode": "drag"}, _new_cd())
        check("C1c drag 缺 target -> 明确报错", False, "未抛异常")
    except ValueError as _e:
        check("C1c drag 缺 target -> 明确报错", "target" in str(_e), str(_e))

    # ---------- C2 真执行器上验证 target 与起点同空间(帧坐标), 且只缩放一次 ----------
    lk2 = FakeLink()
    b2 = Esp32HidExecutor(link=lk2, device_size=(3200, 1440), window_coords=True)
    b2.connect()
    ex3 = ActionExecutor(b2, hold_enabled=True)
    lk2.lines.clear()
    m.apply_action(ex3, {"type": "skill", "id": 1, "mode": "drag",
                         "target": [900, 400], "duration": 200}, _new_cd())
    # 帧 (963,634) -> 设备 (2407,1268)  帧 (900,400) -> 设备 (2250,800)  两端都按 2.5/2.0 缩放
    check("C2 drag 两端同空间、只缩放一次",
          lk2.lines == ["t_swipe 2407 1268 2250 800 200"], f"{lk2.lines}")

    # ---------- F1 未知 type: 可见但行为不变(仍是 no-op) ----------
    f.calls.clear()
    cd_u = _new_cd()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.apply_action(ex2, {"type": "map_mov", "nx": 0.5, "ny": 0.5}, cd_u)   # 拼错
    check("F1a 未知 type 不触碰执行器(仍是 no-op)", f.calls == [], f"{f.calls}")
    check("F1b 计数 +1", cd_u.get("_act_unknown") == 1, f"{cd_u.get('_act_unknown')}")
    check("F1c 打印了告警", "[exec] 未知动作" in buf.getvalue(), repr(buf.getvalue()[:120]))

    # ---------- F2 节流: 5s 内只打印一次, 但每次都计数 ----------
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        m.apply_action(ex2, {"type": "map_mov"}, cd_u)
        m.apply_action(ex2, {"type": "totally_bogus"}, cd_u)
    check("F2a 仍只打印一次(节流)", buf2.getvalue().count("[exec] 未知动作") == 0,
          repr(buf2.getvalue()[:120]))
    check("F2b 计数照常累加", cd_u.get("_act_unknown") == 3, f"{cd_u.get('_act_unknown')}")

    # ---------- F3 none 是合法占位: 不计数不打印 ----------
    f.calls.clear()
    cd_n = _new_cd()
    buf3 = io.StringIO()
    with contextlib.redirect_stdout(buf3):
        m.apply_action(ex2, {"type": "none", "reason": "dead_waiting"}, cd_n)
    check("F3a none 不计数", cd_n.get("_act_unknown") is None, f"{cd_n.get('_act_unknown')}")
    check("F3b none 不打印", "[exec] 未知动作" not in buf3.getvalue(), repr(buf3.getvalue()))
    check("F3c none 不触碰执行器", f.calls == [], f"{f.calls}")

    # ---------- F4 已知类型不计入 ----------
    cd_k = _new_cd()
    with contextlib.redirect_stdout(io.StringIO()):
        m.apply_action(ex2, {"type": "attack", "priority": "free"}, cd_k)
        m.apply_action(ex2, {"type": "recall"}, cd_k)
    check("F4 已知类型不计数", cd_k.get("_act_unknown") is None, f"{cd_k.get('_act_unknown')}")

    print(f"\n全部通过: {len(_PASS)} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
