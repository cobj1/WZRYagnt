# -*- coding: utf-8 -*-
"""操作层命令 -> ADB/HID 映射测试（无需板子/模拟器）。

覆盖：MoveDir/MovePos/MoveStop、AttackCommon/AttackActor、PosSkill/DirSkill/
ObjSkill/ChargeSkill、None、未复刻命令报错、apply_action/update_cooldowns 接线。

运行: venv/Scripts/python.exe -X utf8 scripts/test_operate_commands.py
"""
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import m2_agent_v2 as m                                      # noqa: E402
from wzry.action.executor_v2 import ActionExecutor, PTS      # noqa: E402
from wzry.action.operate_commands import (                   # noqa: E402
    OperateType, UnsupportedOperateCommand, execute_operate,
    is_operate_action, normalize_command,
)
from wzry.control.executor import AdbExecutor               # noqa: E402
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
    """mode='touch' 模拟多点执行器；无 mode 模拟 ADB。"""

    def __init__(self, mode=None):
        self.calls = []
        if mode == "touch":
            self.mode = "touch"
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

    def type_text(self, text, source="policy"):
        self.calls.append(("type_text", text))
        return True


class FakeLink:
    def __init__(self, hello="hello poweron 1 3200x1440"):
        self.lines = []
        self.last_ms = 1.0
        self.cmd_count = 0
        self._hello = hello

    def connect(self):
        return self._hello

    def send(self, line, retry=1):
        self.lines.append(line)
        self.cmd_count += 1
        return True, "ok"

    def close(self):
        pass


def _touch():
    f = FakeExec(mode="touch")
    return ActionExecutor(f, hold_enabled=True), f


def _adb():
    f = FakeExec()
    return ActionExecutor(f, hold_enabled=False), f


def _cd(**kw):
    cd = {"camp": "blue", "self_est": [0.10, 0.90]}
    cd.update(kw)
    return cd


def main():
    # ---------- 命令名规范化 ----------
    check("命令名 COMMAND_TYPE_ 前缀",
          normalize_command("COMMAND_TYPE_MoveDir") is OperateType.MOVE_DIR)
    check("命令名直写", normalize_command("PosSkill") is OperateType.POS_SKILL)
    check("operate 包装识别", is_operate_action({"type": "operate", "cmd": "MoveDir"}))
    check("官方名直写识别", is_operate_action({"type": "MoveDir"}))
    check("旧 type=move 不误判", not is_operate_action({"type": "move"}))
    check("旧 type=none 不误判", not is_operate_action({"type": "none"}))
    check("拼错 type 不识别", not is_operate_action({"type": "movedir"}))

    # ---------- MoveDir ----------
    ex, f = _touch()
    execute_operate(ex, {"type": "MoveDir", "theta": 0.0, "r": 0.8}, {})
    check("MoveDir 走持久摇杆",
          [c[0] for c in f.calls] == ["t_down", "t_move"], f"{f.calls}")
    check("MoveDir 方向 theta=0 -> 向右",
          f.calls[1][2] == 265 and f.calls[1][3] == 574, f"{f.calls}")

    ex_a, fa = _adb()
    execute_operate(ex_a, {"type": "operate", "cmd": "MoveDir", "theta": 0.0,
                           "r": 1.0, "duration_ms": 300}, {})
    check("MoveDir 无多点回退 swipe",
          fa.calls == [("swipe", 196, 574, 282, 574, 300)], f"{fa.calls}")

    # ---------- MovePos / MoveStop ----------
    cd = _cd()
    f.calls.clear()
    execute_operate(ex, {"type": "MovePos", "nx": 0.90, "ny": 0.10}, cd)
    check("MovePos 用 self_est 算方向(右上)",
          [c[0] for c in f.calls] == ["t_move"]
          and f.calls[0][2] > 196 and f.calls[0][3] < 574, f"{f.calls}")

    cd["_map_t"] = 1.0
    f.calls.clear()
    execute_operate(ex, {"type": "MoveStop"}, cd)
    check("MoveStop 松手", f.calls == [("t_up", 0)], f"{f.calls}")
    check("MoveStop 清 _map_t", "_map_t" not in cd, f"{cd}")

    try:
        execute_operate(ex, {"type": "MovePos", "nx": 0.5, "ny": 0.5}, {})
        check("MovePos 缺 origin -> 报错", False, "未抛异常")
    except ValueError as e:
        check("MovePos 缺 origin -> 报错", "origin" in str(e), str(e))

    # ---------- Attack ----------
    f.calls.clear()
    execute_operate(ex, {"type": "AttackCommon", "priority": "minion"}, {})
    check("AttackCommon 点补兵键", f.calls == [("tap", *PTS["attack_minion"])], f"{f.calls}")

    f.calls.clear()
    execute_operate(ex, {"type": "AttackActor", "target": [640, 360]}, {})
    check("AttackActor 有目标 -> 点目标", f.calls == [("tap", 640, 360)], f"{f.calls}")

    f.calls.clear()
    execute_operate(ex, {"type": "AttackActor"}, {})
    check("AttackActor 无目标 -> 攻击键", f.calls == [("tap", *PTS["attack"])], f"{f.calls}")

    try:
        execute_operate(ex, {"type": "AttackActor", "target": [2000, 100]}, {})
        check("target 越界 -> 报错", False, "未抛异常")
    except ValueError as e:
        check("target 越界 -> 报错", "帧空间" in str(e), str(e))

    # ---------- 技能 ----------
    f.calls.clear()
    execute_operate(ex, {"type": "PosSkill", "id": 1,
                         "target": [900, 400], "duration": 200}, {})
    check("PosSkill 拖到位置",
          f.calls == [("swipe", 963, 634, 900, 400, 200)], f"{f.calls}")

    f.calls.clear()
    execute_operate(ex, {"type": "DirSkill", "id": 1, "theta": 0.0,
                         "aim_radius": 100}, {})
    check("DirSkill theta=0 -> 向右拖",
          f.calls == [("swipe", 963, 634, 1063, 634, 150)], f"{f.calls}")

    f.calls.clear()
    execute_operate(ex, {"type": "DirSkill", "id": 1,
                         "theta": math.pi / 2, "aim_radius": 100}, {})
    check("DirSkill theta=90 -> 向上拖",
          f.calls == [("swipe", 963, 634, 963, 534, 150)], f"{f.calls}")

    f.calls.clear()
    execute_operate(ex, {"type": "ObjSkill", "id": 2}, {})
    check("ObjSkill 无目标 -> 点按", f.calls == [("tap", *PTS["skill2"])], f"{f.calls}")

    f.calls.clear()
    execute_operate(ex, {"type": "ObjSkill", "id": 2, "target": [900, 400]}, {})
    check("ObjSkill 有目标 -> 拖动",
          f.calls == [("swipe", 1039, 497, 900, 400, 150)], f"{f.calls}")

    f.calls.clear()
    execute_operate(ex, {"type": "ChargeSkill", "id": 1, "hold_ms": 60}, {})
    check("ChargeSkill 多点 -> 按下/抬起(非摇杆触点)",
          f.calls == [("t_down", 1, *PTS["skill1"]), ("t_up", 1)], f"{f.calls}")

    fa.calls.clear()
    execute_operate(ex_a, {"type": "ChargeSkill", "id": 1, "hold_ms": 60}, {})
    check("ChargeSkill 单点 -> 同点 swipe",
          fa.calls == [("swipe", 963, 634, 963, 634, 60)], f"{fa.calls}")

    # 真执行器 + 假链路：确认帧->设备换算 + 非摇杆触点
    lk = FakeLink()
    board = Esp32HidExecutor(link=lk, device_size=(3200, 1440), window_coords=True)
    board.connect()
    ex_b = ActionExecutor(board, hold_enabled=True)
    lk.lines.clear()
    execute_operate(ex_b, {"type": "ChargeSkill", "id": 1, "hold_ms": 50}, {})
    check("ChargeSkill 真执行器 -> t_down/t_up 触点 1",
          lk.lines == ["t_down 1 2407 1268", "t_up 1"], f"{lk.lines}")

    # ---------- None / 缺坐标 / 未知命令 ----------
    f.calls.clear()
    check("None 是 no-op", execute_operate(ex, {"type": "None"}) is False)
    check("None 不触碰执行器", f.calls == [], f"{f.calls}")

    for cmd, needle in (("BuyEquip", "BuyEquip 需要"), ("AttackToPos", "target"),
                        ("SignalMiniMapTarget", "信号")):
        try:
            execute_operate(ex, {"type": cmd})
            check(f"{cmd} 缺坐标明确报错", False, "未抛异常")
        except ValueError as e:
            check(f"{cmd} 缺坐标明确报错", needle in str(e), str(e))

    try:
        execute_operate(ex, {"type": "NoSuchCommand"})
        check("未知命令仍抛 Unsupported", False, "未抛异常")
    except UnsupportedOperateCommand:
        check("未知命令仍抛 Unsupported", True)

    # ---------- 新增：AttackToPos ----------
    f.calls.clear()
    execute_operate(ex, {"type": "AttackToPos", "target": [640, 360]}, {})
    check("AttackToPos drag -> 攻击键拖到目标",
          f.calls == [("swipe", *PTS["attack"], 640, 360, 200)], f"{f.calls}")

    f.calls.clear()
    execute_operate(ex, {"type": "operate", "cmd": "AttackToPos", "mode": "move_attack",
                         "nx": 0.90, "ny": 0.10, "origin": [0.10, 0.90]}, {})
    check("AttackToPos move -> 先移动再普攻",
          any(c[0] == "swipe" for c in f.calls) and f.calls[-1] == ("tap", *PTS["attack"]),
          f"{f.calls}")

    # ---------- 新增：LearnSkill ----------
    f.calls.clear()
    execute_operate(ex, {"type": "LearnSkill", "target": [700, 500],
                         "count": 2, "delay_ms": 0}, {})
    check("LearnSkill 连点两次", f.calls == [("tap", 700, 500), ("tap", 700, 500)], f"{f.calls}")

    fa.calls.clear()
    execute_operate(ex_a, {"type": "LearnSkill", "id": 1,
                           "slots": {1: [700, 500]}, "delay_ms": 0}, {})
    check("LearnSkill slots[id] 取点", fa.calls == [("tap", 700, 500)], f"{fa.calls}")

    # ---------- 新增：BuyEquip / SellEquip ----------
    f.calls.clear()
    execute_operate(ex, {"type": "BuyEquip", "slot": 0,
                         "slots": [[1145, 105]], "delay_ms": 0}, {})
    check("BuyEquip 快捷购买 slot", f.calls == [("tap", 1145, 105)], f"{f.calls}")

    f.calls.clear()
    execute_operate(ex, {"type": "BuyEquip", "open": [1240, 100], "item": [667, 400],
                         "buy": [733, 600], "close": [1253, 80], "delay_ms": 0}, {})
    check("BuyEquip 显式商店流程",
          [c[:2] for c in f.calls] == [("tap", 1240), ("tap", 667), ("tap", 733), ("tap", 1253)],
          f"{f.calls}")

    f.calls.clear()
    execute_operate(ex, {"type": "SellEquip",
                         "steps": [{"tap": [667, 400]}, {"tap": [733, 600]},
                                   {"tap": [1253, 80]}], "delay_ms": 0}, {})
    check("SellEquip steps 顺序执行", len(f.calls) == 3, f"{f.calls}")

    # ---------- 新增：Signal / InBattleMsg ----------
    f.calls.clear()
    execute_operate(ex, {"type": "SignalMiniMapTarget", "target": [640, 360]}, {})
    check("Signal 点目标", f.calls == [("tap", 640, 360)], f"{f.calls}")

    f.calls.clear()
    execute_operate(ex, {"type": "InBattleMsg", "target": [1067, 40]}, {})
    check("InBattleMsg 打开聊天", f.calls == [("tap", 1067, 40)], f"{f.calls}")

    f.calls.clear()
    execute_operate(ex, {"type": "InBattleMsg", "target": [1067, 40],
                         "text": "go mid", "delay_ms": 0}, {})
    check("InBattleMsg 点开后输入文本",
          f.calls == [("tap", 1067, 40), ("type_text", "go mid")], f"{f.calls}")

    # ---------- ADB input text 转义（假 _run，不连设备） ----------
    class _FakeAdb(AdbExecutor):
        def __init__(self):
            self.sent, self.event_log, self.last_cmd_ms = [], None, 0.0

        def _run(self, args, timeout=4):
            self.sent.append(args)
            return type("R", (), {"returncode": 0})()

    aex = _FakeAdb()
    check("ADB type_text 空格转 %s", aex.type_text("go mid")
          and aex.sent == [["shell", "input text 'go%smid'"]], f"{aex.sent}")
    check("ADB type_text 单引号闭合转义", aex.type_text("it's")
          and aex.sent[-1] == ["shell", "input text 'it'\\''s'"], f"{aex.sent}")
    try:
        aex.type_text("你好")
        check("ADB type_text 拒绝中文", False, "未抛异常")
    except ValueError:
        check("ADB type_text 拒绝中文", True)

    # ---------- 新增：Camera ----------
    f.calls.clear()
    execute_operate(ex, {"type": "ControlCamera", "dx": 100, "dy": 0}, {})
    check("ControlCamera 从屏幕中心划动",
          f.calls == [("swipe", 640, 360, 740, 360, 250)], f"{f.calls}")

    f.calls.clear()
    check("StopRotate no-op", execute_operate(ex, {"type": "StopRotate"}) is False)
    check("StopRotate 不触碰执行器", f.calls == [], f"{f.calls}")

    # ---------- 新增：SetSkillDirection/Position + 技能消费 ----------
    st = {}
    execute_operate(ex, {"type": "SetSkillDirection", "id": 1, "theta": 0.0}, st)
    f.calls.clear()
    execute_operate(ex, {"type": "DirSkill", "id": 1}, st)
    check("SetSkillDirection -> DirSkill 消费方向",
          f.calls == [("swipe", 963, 634, 1143, 634, 150)], f"{f.calls}")
    check("方向 aim 已消费", "_operate_aim" in st and not st["_operate_aim"], f"{st}")

    st2 = {}
    execute_operate(ex, {"type": "SetSkillPosition", "id": 1,
                         "target": [900, 400]}, st2)
    f.calls.clear()
    execute_operate(ex, {"type": "PosSkill", "id": 1}, st2)
    check("SetSkillPosition -> PosSkill 消费位置",
          f.calls == [("swipe", 963, 634, 900, 400, 150)], f"{f.calls}")

    # ---------- 新增：特殊模式按钮 ----------
    f.calls.clear()
    execute_operate(ex, {"type": "UseJointSkill", "target": [800, 600]}, {})
    check("UseJointSkill 显式按钮", f.calls == [("tap", 800, 600)], f"{f.calls}")

    f.calls.clear()
    execute_operate(ex, {"type": "DimensionHeroChangeSkill", "target": [820, 600]}, {})
    check("DimensionHeroChangeSkill 显式按钮",
          f.calls == [("tap", 820, 600)], f"{f.calls}")

    # ---------- apply_action 接线 ----------
    cd2 = _cd()
    f.calls.clear()
    m.apply_action(ex, {"type": "operate", "cmd": "AttackCommon", "priority": "free"}, cd2)
    check("apply_action 接受 operate 包装",
          f.calls == [("tap", *PTS["attack"])], f"{f.calls}")

    f.calls.clear()
    m.apply_action(ex, {"type": "MoveDir", "theta": 0.0, "r": 0.8}, cd2)
    check("apply_action 接受官方命令名直写",
          [c[0] for c in f.calls] in (["t_move"], ["t_down", "t_move"]), f"{f.calls}")

    f.calls.clear()
    m.apply_action(ex, {"type": "MoveStop"}, cd2)
    check("apply_action MoveStop 松手", f.calls == [("t_up", 0)], f"{f.calls}")

    # ---------- update_cooldowns 接线 ----------
    cd3 = {}
    m.update_cooldowns({"type": "operate", "cmd": "PosSkill", "id": 2}, cd3, 123.0)
    check("operate 技能写 skill2_t",
          cd3.get("skill2_t") == 123.0 and cd3.get("skill") == 123.0, f"{cd3}")

    m.update_cooldowns({"type": "AttackCommon"}, cd3, 124.0)
    check("官方 AttackCommon 写 attack_t", cd3.get("attack_t") == 124.0, f"{cd3}")

    cd3["_map_t"] = 1.0
    m.update_cooldowns({"type": "MoveStop"}, cd3, 125.0)
    check("官方 MoveStop 清 _map_t", "_map_t" not in cd3, f"{cd3}")

    print(f"\n全部通过: {len(_PASS)} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
