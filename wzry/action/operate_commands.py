# -*- coding: utf-8 -*-
"""操作层命令 -> ADB/HID 触摸原语。

命令名对齐 global-metadata.dat 的 COMMAND_TYPE_*（v121 publish，25 种）。
只做语义映射，不做进程注入；执行通道仍是 AdbExecutor / Esp32HidExecutor。

两种写法等价：
    {"type": "operate", "cmd": "MoveDir", "theta": 0.5, "r": 0.8}
    {"type": "MoveDir", "theta": 0.5, "r": 0.8}

坐标为帧空间 1280x720（与校准表、ActionExecutor 一致）；MovePos 的 nx/ny
是小地图归一化坐标。

15 条“实验性”命令（UI/特殊模式）需要显式坐标或 steps；见 COMMAND_EXPERIMENTAL。
它们只在提供坐标时下发，不提供就明确报错，不会猜位置。
"""
from __future__ import annotations

import math
import time
from enum import Enum
from typing import Any, Callable, Optional

FRAME_W, FRAME_H = 1280, 720
DEFAULT_AIM_RADIUS = 180.0
DEFAULT_MOVE_MS = 450
DEFAULT_CHARGE_MS = 600
DEFAULT_SKILL_MS = 150
DEFAULT_UI_DELAY_MS = 120
DEFAULT_CAMERA_MS = 250


class OperateType(str, Enum):
    # 与 COMMAND_TYPE_* 逐项对应
    NONE = "None"
    MOVE_DIR = "MoveDir"
    MOVE_POS = "MovePos"
    MOVE_STOP = "MoveStop"
    ATTACK_ACTOR = "AttackActor"
    ATTACK_COMMON = "AttackCommon"
    ATTACK_TO_POS = "AttackToPos"
    POS_SKILL = "PosSkill"
    DIR_SKILL = "DirSkill"
    OBJ_SKILL = "ObjSkill"
    CHARGE_SKILL = "ChargeSkill"
    USE_JOINT_SKILL = "UseJointSkill"
    LEARN_SKILL = "LearnSkill"
    BUY_EQUIP = "BuyEquip"
    SELL_EQUIP = "SellEquip"
    IN_BATTLE_MSG = "InBattleMsg"
    CONTROL_CAMERA = "ControlCamera"
    ROTATE = "Rotate"
    STOP_ROTATE = "StopRotate"
    SET_SKILL_DIRECTION = "SetSkillDirection"
    SET_SKILL_POSITION = "SetSkillPosition"
    SIGNAL_BTN_POSITION = "SignalBtnPosition"
    SIGNAL_MINIMAP_POSITION = "SignalMiniMapPosition"
    SIGNAL_MINIMAP_TARGET = "SignalMiniMapTarget"
    DIMENSION_HERO_CHANGE_SKILL = "DimensionHeroChangeSkill"
    SUMMONER_SKILL = "SummonerSkill"      # 召唤师技能（治疗/闪现/惩击）：点 summoner 按钮


class UnsupportedOperateCommand(ValueError):
    """命令名未知，或实验性命令缺坐标。"""


# 核心战斗命令：纯触摸语义，已单元测试。
COMMAND_SUPPORT = {
    OperateType.NONE: "no-op",
    OperateType.MOVE_DIR: "摇杆方向 theta/r -> move_hold，单点回退 move",
    OperateType.MOVE_POS: "小地图 nx/ny -> 摇杆方向（需 origin 或 state.self_est）",
    OperateType.MOVE_STOP: "松开摇杆",
    OperateType.ATTACK_ACTOR: "target(帧坐标)点选目标；无 target 回退攻击键",
    OperateType.ATTACK_COMMON: "攻击键（priority=free/minion/tower）",
    OperateType.POS_SKILL: "技能按钮拖到 target(帧坐标)",
    OperateType.SUMMONER_SKILL: "召唤师技能：点 summoner 标定按钮（id=5）",
    OperateType.DIR_SKILL: "技能按钮拖向 theta 方向（或 target）",
    OperateType.OBJ_SKILL: "有 target 拖向目标，否则点按智能施法",
    OperateType.CHARGE_SKILL: "按住技能按钮 hold_ms 后松开",
}

# 实验性命令：有触摸实现，但依赖 UI 坐标/特殊模式，需实局校准。
COMMAND_EXPERIMENTAL = {
    OperateType.ATTACK_TO_POS:
        "mode=drag_attack(默认，攻击键拖到 target) / move_attack(nx/ny+origin，移动+攻击) / tap_attack",
    OperateType.LEARN_SKILL:
        "点 target/plus/button；或 slots[id]/ex.skill_up[id]；可 count 连点",
    OperateType.BUY_EQUIP:
        "快捷购买 slot[+slots/quick_buy]；或 open/item/buy/close；或 steps/points",
    OperateType.SELL_EQUIP:
        "必须给 steps/points（开商店→选装备→出售→确认→关闭）",
    OperateType.IN_BATTLE_MSG:
        "点 target/button/steps；有 type_text 时可发 text",
    OperateType.CONTROL_CAMERA:
        "从屏幕中心按 dx/dy 或 theta/radius 划动",
    OperateType.ROTATE:
        "同 ControlCamera（一次性划动）",
    OperateType.STOP_ROTATE:
        "no-op（镜头划动是一次性动作）",
    OperateType.SET_SKILL_DIRECTION:
        "存下一次技能方向；需同一 state/cooldowns，之后 DirSkill 消费",
    OperateType.SET_SKILL_POSITION:
        "存下一次技能位置；需同一 state/cooldowns，之后 PosSkill 消费",
    OperateType.SIGNAL_BTN_POSITION:
        "点 target/button/steps（信号轮盘后续点用 steps）",
    OperateType.SIGNAL_MINIMAP_POSITION:
        "点 target(帧坐标，小地图区域)/steps",
    OperateType.SIGNAL_MINIMAP_TARGET:
        "点 target(帧坐标，小地图区域)/steps",
    OperateType.USE_JOINT_SKILL:
        "点 target/button（特殊模式/合体按钮需显式坐标）",
    OperateType.DIMENSION_HERO_CHANGE_SKILL:
        "点 target/button（特殊模式按钮需显式坐标）",
}

_LEGACY_TYPES = {
    "map_move", "move", "skill", "attack", "summoner", "recall",
    "restore", "combo", "none", "stick_move", "stick_release",
}


def normalize_command(value: Any) -> OperateType:
    """接受 MoveDir / COMMAND_TYPE_MoveDir / OperateType.MoveDir。"""
    if isinstance(value, OperateType):
        return value
    if value is None:
        raise UnsupportedOperateCommand("缺少操作命令名")
    raw = str(value).strip()
    if raw.startswith("COMMAND_TYPE_"):
        raw = raw[len("COMMAND_TYPE_"):]
    _ALIAS = {"summoner": OperateType.SUMMONER_SKILL}   # legacy 风格小写别名
    if raw.lower() in _ALIAS:
        return _ALIAS[raw.lower()]
    for cmd in OperateType:
        if raw == cmd.value:
            return cmd
    raise UnsupportedOperateCommand(f"未知操作命令: {value!r}")


def command_of(action: dict) -> OperateType:
    """从动作 dict 取命令。wrapper 用 cmd/cmd_type，直写用 type。"""
    raw = action.get("cmd") or action.get("cmd_type")
    if raw is None:
        if action.get("type") == "operate":
            raise UnsupportedOperateCommand("operate 动作缺少 cmd/cmd_type")
        raw = action.get("type")
    return normalize_command(raw)


def is_operate_action(action: dict) -> bool:
    """判断动作是否属于操作层命令（避免和旧 type=move/skill/attack 混淆）。"""
    t = action.get("type")
    if t == "operate":
        return True
    if t in _LEGACY_TYPES:
        return False
    try:
        normalize_command(t)
        return True
    except UnsupportedOperateCommand:
        return False


def _skill_id(action: dict) -> int:
    sid = action.get("id", action.get("skill_id"))
    if sid is None:
        raise ValueError("技能命令需要 id/skill_id")
    sid = int(sid)
    if sid not in (1, 2, 3):
        raise ValueError(f"技能 id 必须是 1/2/3，收到 {sid}")
    return sid


def _duration(action: dict, default: int = DEFAULT_SKILL_MS) -> int:
    value = action.get("duration")
    if value is None:
        value = action.get("duration_ms")
    if value is None:
        value = default
    return max(1, int(value))


def _target(action: dict, *, required: bool) -> Optional[tuple[int, int]]:
    target = action.get("target")
    if target is None:
        if required:
            raise ValueError("该命令需要 target=(x,y)，坐标空间=帧坐标 1280x720")
        return None
    if len(target) != 2:
        raise ValueError(f"target 必须是 (x,y)，收到 {target!r}")
    x, y = int(target[0]), int(target[1])
    if not (0 <= x < FRAME_W and 0 <= y < FRAME_H):
        raise ValueError(f"target 超出帧空间: {(x, y)}")
    return x, y


def _first_point(action: dict, keys: tuple[str, ...], *, required: bool) -> Optional[tuple[int, int]]:
    for key in keys:
        value = action.get(key)
        if value is not None:
            if len(value) != 2:
                raise ValueError(f"{key} 必须是 (x,y)，收到 {value!r}")
            x, y = int(value[0]), int(value[1])
            if not (0 <= x < FRAME_W and 0 <= y < FRAME_H):
                raise ValueError(f"{key} 超出帧空间: {(x, y)}")
            return x, y
    if required:
        raise ValueError(f"需要 {'/'.join(keys)} 之一（帧坐标 1280x720）")
    return None


def _sleep_ms(ms) -> None:
    ms = int(ms or 0)
    if ms > 0:
        time.sleep(ms / 1000.0)


def _tap_repeat(ex, point, count: int = 1, delay_ms: int = DEFAULT_UI_DELAY_MS) -> bool:
    count = max(1, int(count))
    ok = True
    for i in range(count):
        ok = bool(ex.tap(point[0], point[1], source="policy")) and ok
        if i < count - 1:
            _sleep_ms(delay_ms)
    return ok


def _tap_sequence(ex, points, delay_ms: int = DEFAULT_UI_DELAY_MS) -> bool:
    pts = []
    for p in points:
        if isinstance(p, dict):
            p = p.get("tap") or p.get("point")
        if p is None or len(p) != 2:
            raise ValueError(f"无效 UI 点: {p!r}")
        pts.append((int(p[0]), int(p[1])))
    ok = True
    for i, p in enumerate(pts):
        ok = bool(ex.tap(p[0], p[1], source="policy")) and ok
        if i < len(pts) - 1:
            _sleep_ms(delay_ms)
    return ok


def _run_steps(ex, steps, delay_ms: int = DEFAULT_UI_DELAY_MS) -> bool:
    """steps: [point] 或 [{"tap":[x,y]}, {"delay_ms":200}, {"swipe":[x1,y1,x2,y2,dur]}]。"""
    ok = True
    for step in steps:
        if isinstance(step, dict):
            if "tap" in step:
                ok = bool(ex.tap(int(step["tap"][0]), int(step["tap"][1]), source="policy")) and ok
            elif "swipe" in step:
                s = step["swipe"]
                ok = bool(ex.swipe(int(s[0]), int(s[1]), int(s[2]), int(s[3]),
                                   int(s[4]) if len(s) > 4 else 150, source="policy")) and ok
            elif "delay_ms" in step:
                _sleep_ms(step["delay_ms"])
                continue
            else:
                raise ValueError(f"未知 UI step: {step!r}")
        else:
            ok = bool(ex.tap(int(step[0]), int(step[1]), source="policy")) and ok
        _sleep_ms(delay_ms)
    return ok


def _ui_command(ex, action: dict, *, required_hint: str) -> bool:
    steps = action.get("steps")
    if steps:
        return _run_steps(ex, steps, int(action.get("delay_ms", DEFAULT_UI_DELAY_MS)))
    points = action.get("points")
    if points:
        return _tap_sequence(ex, points, int(action.get("delay_ms", DEFAULT_UI_DELAY_MS)))
    point = _first_point(action, ("target", "button", "item_point"), required=False)
    if point is None:
        raise ValueError(required_hint)
    return ex.tap(point[0], point[1], source="policy")


def _take_aim(state: Optional[dict], sid: int) -> Optional[dict]:
    if not state:
        return None
    aim = state.get("_operate_aim")
    if not isinstance(aim, dict):
        return None
    return aim.pop(int(sid), None)


def _move_dir(ex, action: dict, _state: Optional[dict]) -> bool:
    theta = action.get("theta")
    if theta is None:
        raise ValueError("MoveDir 需要 theta（0=右，逆时针）")
    r = float(action.get("r", 1.0))
    duration = action.get("duration_ms")
    duration = DEFAULT_MOVE_MS if duration is None else int(duration)
    if action.get("hold", True) and ex.move_hold(float(theta), r):
        return True
    return ex.move(float(theta), r, duration)


def _move_pos(ex, action: dict, state: Optional[dict]) -> bool:
    nx, ny = float(action["nx"]), float(action["ny"])
    origin = action.get("origin")
    if origin is None and state is not None:
        origin = state.get("self_est")
    if origin is None:
        raise ValueError("MovePos 需要 origin=(sx,sy) 或 state['self_est']")
    sx, sy = float(origin[0]), float(origin[1])
    dx, dy = nx - sx, ny - sy
    if math.hypot(dx, dy) < 1e-9:
        return False
    moved = dict(action)
    moved["theta"] = math.atan2(-dy, dx)
    return _move_dir(ex, moved, state)


def _move_stop(ex, _action: dict, state: Optional[dict]) -> bool:
    if state is not None:
        state.pop("_map_t", None)
    return ex.stop()


def _attack_common(ex, action: dict, _state: Optional[dict]) -> bool:
    return ex.attack(str(action.get("priority", "free")))


def _attack_actor(ex, action: dict, _state: Optional[dict]) -> bool:
    target = _target(action, required=False)
    if target is None:
        return ex.attack(str(action.get("priority", "free")))
    # target 必须是已验证的屏幕敌人；点到空地会变成移动指令。
    return ex.tap(target[0], target[1], source="policy")


def _attack_to_pos(ex, action: dict, state: Optional[dict]) -> bool:
    mode = str(action.get("mode", "drag_attack"))
    if mode == "tap_attack":
        return ex.attack(str(action.get("priority", "free")))
    if mode == "move_attack":
        if "nx" not in action or "ny" not in action:
            raise ValueError("AttackToPos move_attack 需要 nx/ny + origin/state.self_est")
        moved = dict(action)
        moved.setdefault("hold", False)
        ok = _move_pos(ex, moved, state)
        if action.get("then_attack", True):
            ok = ex.attack(str(action.get("priority", "free"))) and ok
        return ok
    if mode != "drag_attack":
        raise ValueError(f"未知 AttackToPos mode: {mode}")
    target = _target(action, required=True)
    button = _first_point(action, ("button", "attack_button"), required=False)
    if button is None:
        button = getattr(ex, "attack_pts", {}).get("free")
    if button is None:
        raise ValueError("AttackToPos 需要 button 或 ActionExecutor.attack_pts")
    return ex.swipe(button[0], button[1], target[0], target[1],
                    _duration(action, 200), source="policy")


def _summoner_skill(ex, action: dict, _state: Optional[dict]) -> bool:
    """召唤师技能：点一下 `summoner` 标定按钮。

    标定缺失时 `skill_cast` 会抛 "未知技能: 5" —— 这是有意的：
    宁可报错，也不要在错误的位置乱点（召唤师技能一次错放代价很大）。
    """
    mode = str(action.get("mode") or "tap")
    target = action.get("target")
    return ex.skill_cast(5, mode, target, _duration(action))


def _pos_skill(ex, action: dict, state: Optional[dict]) -> bool:
    sid = _skill_id(action)
    target = _target(action, required=False)
    if target is None:
        aim = _take_aim(state, sid)
        if aim and aim.get("mode") == "pos":
            target = aim.get("target")
        elif aim and aim.get("mode") == "dir":
            raise ValueError("待用瞄准是方向；请用 DirSkill，或先 SetSkillPosition")
    if target is None:
        raise ValueError("PosSkill 需要 target 或先 SetSkillPosition")
    return ex.skill_cast(sid, "drag", target, _duration(action))


def _dir_skill(ex, action: dict, state: Optional[dict]) -> bool:
    sid = _skill_id(action)
    target = _target(action, required=False)
    theta = action.get("theta")
    if target is None and theta is None:
        aim = _take_aim(state, sid)
        if aim and aim.get("mode") == "dir":
            theta = aim.get("theta")
        elif aim and aim.get("mode") == "pos":
            target = aim.get("target")
    if target is None:
        if theta is None:
            raise ValueError("DirSkill 需要 target、theta，或先 SetSkillDirection")
        button = action.get("button")
        if button is None:
            button = getattr(ex, "skills", {}).get(sid)
        if button is None:
            raise ValueError("无法确定技能按钮位置（传 button 或使用 ActionExecutor）")
        sx, sy = float(button[0]), float(button[1])
        radius = float(action.get("aim_radius", DEFAULT_AIM_RADIUS))
        radius *= float(action.get("r", 1.0))
        x = max(0.0, min(FRAME_W - 1.0, sx + radius * math.cos(float(theta))))
        y = max(0.0, min(FRAME_H - 1.0, sy - radius * math.sin(float(theta))))
        target = (int(x), int(y))
    return ex.skill_cast(sid, "drag", target, _duration(action))


def _obj_skill(ex, action: dict, state: Optional[dict]) -> bool:
    sid = _skill_id(action)
    target = _target(action, required=False)
    if target is None:
        aim = _take_aim(state, sid)
        if aim and aim.get("mode") == "pos":
            target = aim.get("target")
    if target is None:
        return ex.skill_cast(sid, "tap")
    return ex.skill_cast(sid, "drag", target, _duration(action))


def _charge_skill(ex, action: dict, _state: Optional[dict]) -> bool:
    hold_ms = action.get("hold_ms")
    if hold_ms is None:
        hold_ms = action.get("duration")
    if hold_ms is None:
        hold_ms = DEFAULT_CHARGE_MS
    return ex.skill_hold(_skill_id(action), int(hold_ms))


def _set_skill_direction(_ex, action: dict, state: Optional[dict]) -> bool:
    sid = _skill_id(action)
    theta = action.get("theta")
    if theta is None:
        raise ValueError("SetSkillDirection 需要 theta")
    if state is None:
        raise ValueError("SetSkillDirection 需要 state/cooldowns 保存瞄准")
    state.setdefault("_operate_aim", {})[sid] = {"mode": "dir", "theta": float(theta)}
    return True


def _set_skill_position(_ex, action: dict, state: Optional[dict]) -> bool:
    sid = _skill_id(action)
    target = _target(action, required=True)
    if state is None:
        raise ValueError("SetSkillPosition 需要 state/cooldowns 保存瞄准")
    state.setdefault("_operate_aim", {})[sid] = {"mode": "pos", "target": target}
    return True


def _learn_skill(ex, action: dict, _state: Optional[dict]) -> bool:
    sid = action.get("id", action.get("skill_id"))
    point = _first_point(action, ("target", "plus", "button"), required=False)
    if point is None and sid is not None:
        points = action.get("slots") or getattr(ex, "skill_up", None)
        if isinstance(points, dict):
            point = points.get(int(sid)) or points.get(str(sid))
        elif isinstance(points, (list, tuple)) and 1 <= int(sid) <= len(points):
            point = points[int(sid) - 1]
    if point is None:
        raise ValueError("LearnSkill 需要 target/plus/button，或 slots[id]/ex.skill_up[id]")
    return _tap_repeat(ex, point, int(action.get("count", 1)),
                       int(action.get("delay_ms", DEFAULT_UI_DELAY_MS)))


def _buy_equip(ex, action: dict, _state: Optional[dict]) -> bool:
    slot = action.get("slot")
    if slot is not None:
        points = action.get("slots") or getattr(ex, "quick_buy", None)
        point = None
        if isinstance(points, dict):
            point = points.get(int(slot)) or points.get(str(slot))
        elif isinstance(points, (list, tuple)) and 0 <= int(slot) < len(points):
            point = points[int(slot)]
        if point is None:
            raise ValueError("BuyEquip slot 需要 slots/slot 或 ex.quick_buy 校准点")
        return _tap_repeat(ex, point, int(action.get("count", 1)),
                           int(action.get("delay_ms", DEFAULT_UI_DELAY_MS)))
    open_p = _first_point(action, ("open",), required=False)
    item_p = _first_point(action, ("item", "target", "item_point"), required=False)
    if open_p and item_p:
        seq = [open_p, item_p]
        buy_p = _first_point(action, ("buy",), required=False)
        close_p = _first_point(action, ("close",), required=False)
        if buy_p:
            seq.append(buy_p)
        if close_p:
            seq.append(close_p)
        return _tap_sequence(ex, seq, int(action.get("delay_ms", DEFAULT_UI_DELAY_MS)))
    return _ui_command(ex, action, required_hint=(
        "BuyEquip 需要 slot(+slots/quick_buy)、target、open+item[+buy][+close]，或 steps/points"))


def _sell_equip(ex, action: dict, _state: Optional[dict]) -> bool:
    return _ui_command(ex, action, required_hint=(
        "SellEquip 需要 steps/points/target（开商店→选装备→出售→确认→关闭）"))


def _in_battle_msg(ex, action: dict, _state: Optional[dict]) -> bool:
    ok = _ui_command(ex, action, required_hint="InBattleMsg 需要 target/button/steps/points")
    text = action.get("text")
    if text:
        if not callable(getattr(ex, "type_text", None)):
            raise ValueError("当前执行器不支持 type_text，无法发送文本")
        ok = bool(ex.type_text(str(text))) and ok
    return ok


def _signal(ex, action: dict, _state: Optional[dict]) -> bool:
    return _ui_command(ex, action, required_hint=(
        "信号命令需要 target(帧坐标)/button/steps（轮盘后续点放 steps）"))


def _camera_drag(ex, action: dict, _state: Optional[dict]) -> bool:
    dx, dy = action.get("dx"), action.get("dy")
    if dx is None and dy is None:
        theta = action.get("theta")
        if theta is None:
            raise ValueError("ControlCamera/Rotate 需要 dx/dy 或 theta/radius")
        radius = float(action.get("radius", 200.0))
        dx = radius * math.cos(float(theta))
        dy = -radius * math.sin(float(theta))
    origin = action.get("origin", (FRAME_W / 2.0, FRAME_H / 2.0))
    return ex.swipe(int(origin[0]), int(origin[1]),
                    int(float(origin[0]) + float(dx)), int(float(origin[1]) + float(dy)),
                    _duration(action, DEFAULT_CAMERA_MS), source="policy")


def _stop_rotate(_ex, _action: dict, _state: Optional[dict]) -> bool:
    # 镜头划动是一次性 swipe，没有持续的旋转状态可停。
    return False


def _joint_skill(ex, action: dict, _state: Optional[dict]) -> bool:
    return _ui_command(ex, action, required_hint=(
        "UseJointSkill 需要 target/button（特殊模式合体按钮，当前未内置坐标）"))


def _dimension_skill(ex, action: dict, _state: Optional[dict]) -> bool:
    return _ui_command(ex, action, required_hint=(
        "DimensionHeroChangeSkill 需要 target/button（特殊模式按钮，当前未内置坐标）"))


def _none(_ex, _action: dict, _state: Optional[dict]) -> bool:
    return False


_HANDLERS: dict[OperateType, Callable[[Any, dict, Optional[dict]], bool]] = {
    OperateType.NONE: _none,
    OperateType.MOVE_DIR: _move_dir,
    OperateType.MOVE_POS: _move_pos,
    OperateType.MOVE_STOP: _move_stop,
    OperateType.ATTACK_ACTOR: _attack_actor,
    OperateType.ATTACK_COMMON: _attack_common,
    OperateType.ATTACK_TO_POS: _attack_to_pos,
    OperateType.POS_SKILL: _pos_skill,
    OperateType.DIR_SKILL: _dir_skill,
    OperateType.OBJ_SKILL: _obj_skill,
    OperateType.CHARGE_SKILL: _charge_skill,
    OperateType.USE_JOINT_SKILL: _joint_skill,
    OperateType.LEARN_SKILL: _learn_skill,
    OperateType.BUY_EQUIP: _buy_equip,
    OperateType.SELL_EQUIP: _sell_equip,
    OperateType.IN_BATTLE_MSG: _in_battle_msg,
    OperateType.CONTROL_CAMERA: _camera_drag,
    OperateType.ROTATE: _camera_drag,
    OperateType.STOP_ROTATE: _stop_rotate,
    OperateType.SET_SKILL_DIRECTION: _set_skill_direction,
    OperateType.SUMMONER_SKILL: _summoner_skill,
    OperateType.SET_SKILL_POSITION: _set_skill_position,
    OperateType.SIGNAL_BTN_POSITION: _signal,
    OperateType.SIGNAL_MINIMAP_POSITION: _signal,
    OperateType.SIGNAL_MINIMAP_TARGET: _signal,
    OperateType.DIMENSION_HERO_CHANGE_SKILL: _dimension_skill,
}


def execute_operate(ex, action: dict, state: Optional[dict] = None) -> bool:
    """执行一条操作层命令。未知命令抛 UnsupportedOperateCommand，缺坐标抛 ValueError。"""
    cmd = command_of(action)
    handler = _HANDLERS.get(cmd)
    if handler is None:
        raise UnsupportedOperateCommand(f"{cmd.value} 没有触摸映射")
    return handler(ex, action, state if state is not None else {})


if __name__ == "__main__":
    for _cmd in OperateType:
        _desc = COMMAND_SUPPORT.get(_cmd) or COMMAND_EXPERIMENTAL.get(_cmd) or "UNSUPPORTED"
        _tag = "core" if _cmd in COMMAND_SUPPORT else "exp "
        print(f"{_tag} {_cmd.value:28s} {_desc}")
