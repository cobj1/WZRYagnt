# -*- coding: utf-8 -*-
"""动作执行器 v0（M2）：把策略输出（连续角度/幅度/技能意图）翻译为触摸原语。

基于 wzry.control.executor.AdbExecutor，在【设备像素】坐标系工作（1280x720 横屏）。
坐标来源：configs/calibration_absolute.json（move_stick_center / dir_* / skill1-3 / attack）。

原语：
  - move(theta, r, duration)    摇杆任意方向（theta 弧度，r 0-1）+ 按住时长
  - move_dir(name)              8 向快捷移动（校准点直用）
  - skill_cast(skill_id, mode, target)  点按 / 拖到目标点瞄准 / 取消
  - skill_hold(skill_id, hold_ms)       蓄力技能：按住按钮后松开
  - attack(priority)            攻击键（自由/补兵/推塔）
  - sequence([...])             连招编排（每步原语 + 间隔）
"""
import json
import math
import time
from pathlib import Path

from wzry.control.executor import AdbExecutor

ROOT = Path(__file__).resolve().parents[2]
CALIB = json.loads((ROOT / "configs" / "calibration_absolute.json").read_text(encoding="utf-8"))
PTS = CALIB["points"]


class ActionExecutor:
    def __init__(self, executor: AdbExecutor = None, stick_radius: float = 0.12,
                 hold_enabled=None):
        """stick_radius: 摇杆拖动半径（相对画面高度，默认 0.12 = 720p 下 ~86px）。
        hold_enabled: 是否用"持久触点"持续摇杆（t_down/t_move/t_up，不松手）。
                      None = 自动（执行器具备 touch 多点能力时才开）；
                      True/False = 强制。不支持时所有摇杆调用自动回退 swipe。
        """
        self.ex = executor or AdbExecutor()
        self.stick_radius = stick_radius
        self.cx, self.cy = PTS["move_stick_center"]
        # v16.2 持久摇杆：swipe 模式每条 move 都会 press->...->release，
        #   而王者"松手即停"-> 移动走走停停；且 worker 被占 200~1000ms/条，
        #   把动作带宽压到 ~1Hz。t_down/t_move/t_up 才能真的不松手（单条 ~20ms）。
        #   判定必须同时看 mode：Esp32HidExecutor 在 mouse 模式下也有 touch_down 方法，
        #   但那会抛 RuntimeError，所以只认 mode=="touch"。
        self._hold_capable = (getattr(self.ex, "mode", None) == "touch"
                              and callable(getattr(self.ex, "touch_down", None)))
        self.hold_enabled = self._hold_capable if hold_enabled is None else bool(hold_enabled)
        if self.hold_enabled and not self._hold_capable:
            self.hold_enabled = False        # 要求开但硬件不支持 -> 静默回退，不抛
        self.stick_cid = 0                   # 固定触点 0 当摇杆；板子的 t_tap/t_swipe 会自动用 1..4
        self._stick_held = False
        self.skills = {1: PTS["skill1"], 2: PTS["skill2"], 3: PTS["skill3"]}
        # 召唤师技能（治疗/闪现/惩击…）走**另一个按钮**，标定点名 `summoner`。
        # 用 id=5 表示（与内存技能槽 slot5/slot6 的"召唤师类"对齐，避免与 1..3 混淆）。
        # 标定缺失时不注册 —— 调用方会收到 "未知技能: 5" 而不是静默点错位置。
        if PTS.get("summoner"):
            self.skills[5] = PTS["summoner"]
        self.attack_pts = {"free": PTS["attack"], "minion": PTS["attack_minion"],
                           "tower": PTS["attack_tower"]}

    # ---------- 坐标换算 ----------
    def _stick_point(self, theta: float, r: float):
        """角度/幅度 -> 摇杆终点（设备像素）。theta 0=右，逆时针。"""
        r = max(0.0, min(1.0, r))
        h = CALIB["screen_height"]
        radius = self.stick_radius * h
        return (int(self.cx + radius * r * math.cos(theta)),
                int(self.cy - radius * r * math.sin(theta)))

    # ---------- 移动 ----------
    def move(self, theta: float, r: float = 1.0, duration: int = 300):
        """任意方向移动：按住摇杆 duration 毫秒。"""
        x, y = self._stick_point(theta, r)
        return self.ex.swipe(self.cx, self.cy, x, y, duration, source="policy")

    def move_dir(self, name: str, duration: int = 300):
        """8 向快捷移动（校准点）。name: up/left_up/left/..."""
        key = f"dir_{name}"
        if key not in PTS:
            raise ValueError(f"未知方向: {name}")
        x, y = PTS[key]
        return self.ex.swipe(self.cx, self.cy, x, y, duration, source="policy")

    def move_hold(self, theta: float, r: float = 1.0):
        """持续摇杆：首次按在摇杆中心并推到目标方向，之后只更新朝向，**不松手**。

        返回 True=已下发；False=当前执行器不支持（调用方应回退 move()）或链路失败。
        与 move() 的区别：move() 是"推一次就松手"（swipe），本方法让摇杆保持压下状态，
        英雄连续移动；只有 stop()/release_stick() 或回城类动作才会真正松手。
        """
        if not (self.hold_enabled and self._hold_capable):
            return False
        x, y = self._stick_point(theta, r)
        if not self._stick_held:
            if not self.ex.touch_down(self.stick_cid, self.cx, self.cy):
                return False
            self._stick_held = True
        if not self.ex.touch_move(self.stick_cid, x, y):
            # 链路失败：标记已松手，下一次调用会重新 t_down 建contact，避免"以为按住其实没按"
            self._stick_held = False
            return False
        return True

    def release_stick(self):
        """松开摇杆（英雄停止移动）。未按住时不下发，避免多余的 t_up。"""
        if not self._stick_held:
            return False
        self._stick_held = False
        return bool(self.ex.touch_up(self.stick_cid))

    @property
    def stick_held(self):
        return self._stick_held

    def stop(self):
        """停止移动。

        持久摇杆模式：只在真的按住时发 `t_up`；未按住则**不做任何下发**（真空操作，
        否则会白发一条 swipe，在 adb 上还要 ~100ms）。
        非持久模式(adb / mouse)：保持原实现（同点 1ms swipe），行为与改造前逐字节一致。
        """
        if self.hold_enabled:
            return self.release_stick()
        return self.ex.swipe(self.cx, self.cy, self.cx, self.cy, 1, source="policy")

    # ---------- 小地图点击移动（v2.27 王者辅助移动）----------
    def tap(self, x: int, y: int, source: str = "policy"):
        """点击设备像素坐标（小地图目标点→自动寻路）。"""
        return self.ex.tap(x, y, source=source)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int = 150,
              source: str = "policy"):
        """帧坐标滑动（操作层命令的通用拖拽出口）。"""
        return self.ex.swipe(x1, y1, x2, y2, duration, source=source)

    def type_text(self, text: str, source: str = "policy"):
        """文本输入（聊天/消息）；无此能力的执行器明确报错，不静默吞掉。"""
        fn = getattr(self.ex, "type_text", None)
        if not callable(fn):
            raise ValueError("当前执行器不支持 type_text，无法发送文本")
        return fn(text, source=source)

    # ---------- 技能 ----------
    def skill_cast(self, skill_id: int, mode: str = "tap", target=None, duration: int = 150):
        """技能释放。

        mode='tap'      点按（智能施法）
        mode='drag'     从技能按钮拖到 target（手动瞄准）。**target 必须与技能按钮同坐标空间,
                        即帧坐标(1280x720)** —— 不是设备像素。原因: 起点 `sx,sy` 取自校准表
                        (帧空间), 而底层 swipe 会对**两端一起**做 frame->device 缩放; 若 target
                        传设备像素会被二次缩放, 拖到完全错误的位置。
        mode='cancel'   按下后拖回按钮位置取消
        """
        if skill_id not in self.skills:
            raise ValueError(f"未知技能: {skill_id}")
        sx, sy = self.skills[skill_id]
        if mode == "tap":
            return self.ex.tap(sx, sy, source="policy")
        if mode == "drag":
            if target is None:
                raise ValueError("drag 模式需要 target=(x,y)，坐标空间=帧坐标(1280x720)")
            return self.ex.swipe(sx, sy, int(target[0]), int(target[1]), duration,
                                 source="policy")
        if mode == "cancel":
            return self.ex.swipe(sx, sy, sx, sy + 60, duration, source="policy")
        raise ValueError(f"未知模式: {mode}")

    def skill_hold(self, skill_id: int, hold_ms: int = 600):
        """蓄力技能：按住技能按钮 hold_ms 后松开。

        多点执行器（ESP32 touch）占用非摇杆触点，支持边走边蓄力；
        ADB/单点退化为同点 swipe（Android 按 duration 保持按下）。
        hold_ms 钳到 [50, 3000]，避免 worker 被异常值卡死。
        """
        if skill_id not in self.skills:
            raise ValueError(f"未知技能: {skill_id}")
        hold_ms = max(50, min(3000, int(hold_ms)))
        sx, sy = self.skills[skill_id]
        if self._hold_capable and callable(getattr(self.ex, "touch_up", None)):
            # 摇杆固定占触点 0；蓄力用另一个触点，才能边走边蓄力。
            cid = 1 if self.stick_cid != 1 else 2
            if not self.ex.touch_down(cid, sx, sy):
                return False
            try:
                time.sleep(hold_ms / 1000.0)
            finally:
                up_ok = self.ex.touch_up(cid)
            return bool(up_ok)
        return self.ex.swipe(sx, sy, sx, sy, hold_ms, source="policy")

    def attack(self, priority: str = "free"):
        """攻击键切换。priority: free/minion/tower（分别点击对应按键）。"""
        if priority not in self.attack_pts:
            raise ValueError(f"未知攻击优先级: {priority}")
        x, y = self.attack_pts[priority]
        return self.ex.tap(x, y, source="policy")

    def summoner(self):
        """召唤师技能（校准点 summoner，默认闪现/惩击位）。"""
        x, y = PTS["summoner"]
        return self.ex.tap(x, y, source="policy")

    def recall(self):
        """回城（校准点 recall；点按触发回城读条，持续 7 秒）。"""
        x, y = PTS["recall"]
        return self.ex.tap(x, y, source="policy")

    def restore(self):
        """恢复键（校准点 restore；回复药/恢复，有冷却）。"""
        x, y = PTS["restore"]
        return self.ex.tap(x, y, source="policy")


if __name__ == "__main__":
    pass

    # ---------- 连招 ----------
    def sequence(self, steps, gap_ms: int = 120):
        """连招编排。steps: [(callable, args, kwargs), ...] 或原语 dict 列表。
        dict 格式: {"move": {"theta": 0, "r": 1, "duration": 200}} /
                   {"skill": {"id": 2, "mode": "tap"}} /
                   {"attack": {"priority": "free"}} / {"stop": {}}
        """
        import time
        for step in steps:
            if isinstance(step, dict):
                self._exec_dict(step)
            else:
                fn, args, kwargs = step
                fn(*args, **kwargs)
            time.sleep(gap_ms / 1000)

    def _exec_dict(self, d: dict):
        for name, kw in d.items():
            if name == "move":
                self.move(**kw)
            elif name == "move_dir":
                self.move_dir(**kw)
            elif name == "stop":
                self.stop()
            elif name == "skill":
                kw = dict(kw)
                kw["skill_id"] = kw.pop("id")
                self.skill_cast(**kw)
            elif name == "attack":
                self.attack(**kw)
            else:
                raise ValueError(f"未知原语: {name}")
