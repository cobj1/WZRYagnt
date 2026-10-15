# -*- coding: utf-8 -*-
"""事件记忆层：把"单帧感知"变成"最近几秒发生了什么" + 冷却估计。

它补的正是链路里缺的那层（原来只有 m2 进程内存里的 cooldowns，不落盘、换局即失）：
  - 技能上一次释放时间 / 剩余冷却秒数（自己：显式 note_cast 或 ready 跳变观测）
  - 露头 / 丢失（敌方英雄框出现与消失）
  - 自身位移事件（位置跳变 > 阈值 → 闪现/位移/复活回泉水，标注为 estimate）
  - 最近几秒轨迹 -> 朝向（世界系 0=右、逆时针，与执行器/编码同口径）
  - 敌方关键技能估计（**低可信**：敌方技能状态屏幕上没有像素来源，见模块说明）

可信度约定（防止把估计当真值）：
  - confidence=1.0 显式/直接观测（note_cast、框出现/消失）
  - confidence=0.5 由读数跳变推断（ready True->False）
  - confidence<=0.2 纯估计（敌方技能、位移归因）——**不要进动作约束当硬过滤**

落盘：data/memory/events.jsonl（每行一条事件，含 t/kind/...）。
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "data" / "memory" / "events.jsonl"

DISPLACE_FRAC = 0.25      # 归一化坐标跳变阈值（一帧内）→ 判位移类事件
TRAIL_LEN = 32            # 轨迹保留长度（帧）


class EventLog:
    def __init__(self, path=DEFAULT_PATH, persist=True, trail_len=TRAIL_LEN):
        self.path = Path(path)
        self.persist = persist
        self.trail_len = trail_len
        self.trail = []                  # [(t, x, y)] 自己（归一化）
        self.last_cast = {}              # skill_id -> t
        self.max_cd = {}                 # skill_id -> 秒（来自知识库/Hero Profile）
        self._ready_prev = {}            # skill_id -> bool（用于观测 ready 跳变）
        self._enemy_prev = 0
        self._red_mm_prev = 0
        self._last_pos = None
        self.n_events = 0
        if self.persist:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    # ---------- 写入 ----------
    def _emit(self, kind, now, **kw):
        rec = {"t": round(float(now), 3), "kind": kind}
        rec.update(kw)
        self.n_events += 1
        if self.persist:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception:
                pass
        return rec

    def set_max_cd(self, skill_id, seconds):
        if isinstance(seconds, (int, float)) and seconds > 0:
            self.max_cd[int(skill_id)] = float(seconds)

    def note_cast(self, skill_id, now=None, source="self"):
        """执行器显式告知"刚放了技能"——最可靠的一手来源（confidence=1.0）。"""
        now = float(now if now is not None else time.time())
        sid = int(skill_id)
        prev = self.last_cast.get(sid)
        self.last_cast[sid] = now
        return self._emit("cast", now, skill=sid, source=source, confidence=1.0,
                          since_prev_s=round(now - prev, 2) if prev else None)

    def note_panel(self, panel, fields, now=None, source="ui_nav"):
        """面板读到的内容回填到记忆层（事件流）—— 与"面板=辅助UI感知"的分工一致。

        面板类信息（阵亡回放/属性/商店/技能说明）不是实时战斗画面，所以它们不进主
        战斗感知，而是作为事件记下来，供状态层/后续训练用。
        """
        now = float(now if now is not None else time.time())
        return self._emit("panel_info", now, panel=str(panel),
                          fields=dict(fields or {}), source=source, confidence=1.0)

    # ---------- 每帧更新 ----------
    def update(self, state, now=None):
        """喂一帧 GameState dict -> 本拍新事件。

        返回 {"events":[...], "heading_deg":float|None, "n_enemy_scr":int,
              "n_red_mm":int, "trail_len":int, "cooldowns":{sid:剩余秒}}
        """
        now = float(now if now is not None else (state.get("t") or time.time()))
        events = []

        # 1) 自己位置 + 轨迹 + 朝向 + 位移事件
        pos = self._self_pos(state)
        if pos is not None:
            if self._last_pos is not None:
                d = math.hypot(pos[0] - self._last_pos[0], pos[1] - self._last_pos[1])
                if d >= DISPLACE_FRAC:
                    events.append(self._emit("displaced", now, dist=round(d, 3),
                                             confidence=0.2,
                                             note="位置跳变(闪现/位移/复活回城)估计"))
            self._last_pos = pos
            self.trail.append((now, pos[0], pos[1]))
            if len(self.trail) > self.trail_len:
                self.trail.pop(0)

        # 2) 技能释放观测：显式优先；否则用 ready True->False 的跳变（confidence=0.5）
        ui = state.get("ui") or {}
        for sid in (1, 2, 3):
            r = ui.get(f"skill{sid}_ready")
            if not isinstance(r, bool):
                continue
            prev = self._ready_prev.get(sid)
            if prev is True and r is False and \
                    (now - float(self.last_cast.get(sid, 0.0))) > 0.6:
                self.last_cast[sid] = now
                events.append(self._emit("cast", now, skill=sid, source="ready_edge",
                                         confidence=0.5))
            self._ready_prev[sid] = r

        # 3) 敌方露头/丢失（屏幕框数量 + 小地图红点）
        n_e = self._count(state, "enemy_hero")
        n_r = self._count_mm(state, "red")
        if n_e > 0 and self._enemy_prev == 0:
            events.append(self._emit("enemy_spotted", now, n=n_e, confidence=1.0))
        if n_e == 0 and self._enemy_prev > 0:
            events.append(self._emit("enemy_lost", now, confidence=1.0))
        if n_r > 0 and self._red_mm_prev == 0:
            events.append(self._emit("enemy_on_minimap", now, n=n_r, confidence=1.0))
        self._enemy_prev, self._red_mm_prev = n_e, n_r

        return {"events": events, "heading_deg": self.heading_deg(),
                "n_enemy_scr": n_e, "n_red_mm": n_r, "trail_len": len(self.trail),
                "cooldowns": self.cooldowns(now)}

    # ---------- 查询 ----------
    def _self_pos(self, state):
        extra = state.get("extra") or {}
        hp_pos = extra.get("hero_pos")
        ss = state.get("screen_size") or [1280, 720]
        if isinstance(hp_pos, (list, tuple)) and len(hp_pos) >= 2 \
                and isinstance(hp_pos[0], (int, float)):
            try:
                return (float(hp_pos[0]) / float(ss[0]), float(hp_pos[1]) / float(ss[1]))
            except Exception:
                return None
        return None

    def heading_deg(self):
        """最近轨迹位移方向（度）。屏幕系与执行器同向(0=右、逆时针)，仅移动时有效。"""
        if len(self.trail) < 3:
            return None
        (_, x0, y0), (_, x1, y1) = self.trail[-3], self.trail[-1]
        dx, dy = x1 - x0, y1 - y0
        if math.hypot(dx, dy) < 0.01:
            return None
        return round(math.degrees(math.atan2(-dy, dx)), 1)

    def cooldown_remaining(self, skill_id, now=None):
        sid = int(skill_id)
        cd = self.max_cd.get(sid)
        t0 = self.last_cast.get(sid)
        if cd is None or t0 is None:
            return None
        now = float(now if now is not None else time.time())
        return max(0.0, cd - (now - t0))

    def cooldowns(self, now=None):
        return {sid: self.cooldown_remaining(sid, now) for sid in (1, 2, 3)
                if self.cooldown_remaining(sid, now) is not None}

    def enemy_skill_estimate(self, hero_key):
        """敌方关键技能是否交过 / 还差多久 —— **只能估**。

        敌方技能状态在屏幕上没有任何像素来源（王者 HUD 只画自己的 CD），
        所以这里永远返回 confidence=0（除非外部把观测塞进来）。
        返回结构给上层当"低可信特征"用，**不要当动作硬过滤**。
        """
        return {"hero": hero_key, "known": False, "confidence": 0.0,
                "cd_remaining": None,
                "reason": "敌方技能状态不可直接观测（无像素来源）；需外部观测或段位先验"}

    def snapshot(self):
        return {"trail_len": len(self.trail), "last_cast": dict(self.last_cast),
                "max_cd": dict(self.max_cd), "n_events": self.n_events,
                "heading_deg": self.heading_deg()}

    @staticmethod
    def _count(state, cls):
        return sum(1 for u in (state.get("units") or []) if u.get("cls") == cls)

    @staticmethod
    def _count_mm(state, color):
        mm = state.get("minimap") or {}
        if not mm.get("found"):
            return 0
        return len((mm.get("dots") or {}).get(color) or [])
