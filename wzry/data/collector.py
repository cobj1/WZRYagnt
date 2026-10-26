# -*- coding: utf-8 -*-
"""对局采集器：按"对局会话"组织存档，供模仿学习数据工厂直接消费。

会话结构（data/matches/<session_id>/）：
  states.jsonl   逐条 GameState JSON（含时间戳、检测、小地图、UI）
  frames/        关键帧 PNG（默认每 5 秒一帧，可配）
  actions.jsonl  动作事件流（由执行器写入，按时间戳与 states 对齐）
  manifest.json  会话元信息（起止时间、时长、帧数、状态条数、视频源等）

用法（集成到感知管线）：
    rec = MatchRecorder()
    rec.on_state(st)            # 对局中每帧/每周期调用
    rec.on_frame(frame)         # 可选：保存关键帧
    rec.on_action(act_dict)     # 可选：动作事件
    rec.close()                 # 对局结束（或进程退出）
"""
import json
import time
from datetime import datetime
from pathlib import Path

_KIND_TO_CLS = {
    "hero": "hero", "minion": "minion", "turret": "turret",
    "monster": "neutral_monster", "building": "crystal",
}


def _canonical_mem_units(mem_units, self_camp: str) -> list[dict]:
    """内存单位 -> canonical `units` 条目，仅用于录制/审计。

    决策层仍消费视觉 `units`（screen 坐标）；把内存单位混进去会让
    `screen` 缺失的条目被当成屏幕中心，故**只在 recorder 写盘时合并**。
    """
    from wzry.decision.official_ai.geo import world_to_norm

    out: list[dict] = []
    now = time.time()
    for u in mem_units or []:
        x, z = u.get("x"), u.get("z")
        kind = str(u.get("kind") or "")
        base = _KIND_TO_CLS.get(kind)
        if base is None or x is None or z is None:
            continue
        camp = str(u.get("camp") or "")
        if base == "hero":
            cls = "ally_hero" if camp == self_camp else "enemy_hero"
        elif base == "minion":
            cls = "ally_minion" if camp == self_camp else "enemy_minion"
        elif base == "turret":
            cls = "ally_turret" if camp == self_camp else "enemy_turret"
        elif base == "crystal":
            cls = "ally_crystal" if camp == self_camp else "enemy_crystal"
        else:
            cls = "neutral_monster"
        nx, ny = world_to_norm(float(x), float(z))
        hp_ratio = u.get("hp_ratio")
        if hp_ratio is None and u.get("hp") is not None and u.get("hp_max"):
            hp_ratio = float(u["hp"]) / float(u["hp_max"])
        out.append({
            "cls": cls, "kind": base, "camp": camp or None,
            "uid": f"{camp}:{kind}:{u.get('cid')}:{u.get('actor')}",
            "hero_cid": u.get("cid") if base == "hero" else None,
            "cid": u.get("cid"),
            "screen": None,
            "pos_world": [float(x), float(z)],
            "pos_minimap": [float(nx), float(ny)],
            "coord_src": "memory", "visible": None,
            "hp": hp_ratio, "hp_abs": u.get("hp"), "hp_max": u.get("hp_max"),
            "dead": bool(u.get("dead")), "conf": 1.0, "camp_src": "memory",
            "src": "memory", "last_seen_t": now,
        })
    return out


class MatchRecorder:
    def __init__(self, base_dir="data/matches", frame_every_s=5.0, max_frames=2000,
                 input_source="policy"):
        self.base_dir = Path(base_dir)
        self.frame_every_s = frame_every_s
        self.max_frames = max_frames
        self.input_source = input_source
        self.session_dir = None
        self._states_fp = None
        self._actions_fp = None
        self._frame_dir = None
        self._n_states = 0
        self._n_frames = 0
        self._start_t = None
        self._last_frame_t = 0.0
        self._meta = {}
        self._n_memory_states = 0
        self._n_clock_states = 0
        # 字段覆盖率计数（写进 manifest；采数后一眼判断这局能不能用）
        self._field_hits = {
            "clock_s": 0, "scoreboard": 0, "units.pos_minimap": 0,
            "ui.hp": 0, "minimap.dots": 0, "units": 0,
        }

    @property
    def active(self):
        return self._states_fp is not None

    def start(self, meta=None, session_name=None):
        """开启一个新对局会话。session_name 可指定会话目录名（默认时间戳）。"""
        if self.active:
            return
        ts = session_name or datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.session_dir = self.base_dir / ts
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._frame_dir = self.session_dir / "frames"
        self._frame_dir.mkdir(exist_ok=True)
        self._states_fp = open(self.session_dir / "states.jsonl", "a", encoding="utf-8")
        self._actions_fp = open(self.session_dir / "actions.jsonl", "a", encoding="utf-8")
        self._n_states = 0
        self._n_frames = 0
        self._start_t = time.time()
        self._last_frame_t = 0.0
        self._meta = dict(meta or {})
        self._n_memory_states = 0
        self._n_clock_states = 0
        self._write_manifest()

    @staticmethod
    def _scoreboard_from_mem(mem_heroes):
        rows = []
        for h in mem_heroes:
            hp = h.get("hp")
            hp_max = h.get("hp_max")
            rows.append({
                "cid": h.get("id"),
                "camp": h.get("camp"),
                "gold": h.get("gold"),
                "gold_total": h.get("gold_total", h.get("gold_earned")),
                "gold_spent": h.get("gold_spent"),
                "level": h.get("level"),
                "exp": h.get("exp"),
                "kills": h.get("kills", 0),
                "deaths": h.get("deaths", 0),
                "assists": h.get("assists", 0),
                "hp": hp,
                "hp_max": hp_max,
                "equip": h.get("equip") or [],
                "dead": bool(isinstance(hp, (int, float)) and hp <= 0),
                "x": h.get("x"),
                "z": h.get("z"),
                "src": "memory",
            })
        return rows

    def on_state(self, state_dict):
        """写入一条 GameState（dict），并把内存字段提升到统一契约。"""
        if not self.active:
            return
        ui = state_dict.setdefault("ui", {}) or {}
        extra = state_dict.setdefault("extra", {}) or {}
        sources = state_dict.get("sources") or {}
        state_dict["sources"] = sources

        clock = ui.get("match_clock")
        if clock is None:
            clock = extra.get("mem_clock_s")
            if clock is not None:
                ui["match_clock"] = clock
        if clock is not None:
            state_dict.setdefault("clock_s", clock)
            sources["clock_s"] = "memory" if extra.get("mem_clock_s") is not None else "vision"
            self._n_clock_states += 1

        mem_heroes = extra.get("mem_heroes") or []
        if mem_heroes:
            state_dict.setdefault("scoreboard", self._scoreboard_from_mem(mem_heroes))
            sources["scoreboard"] = "memory"
            self._n_memory_states += 1

        # 会话相对时间：纯视觉录制没有 clock_s，下游（hok_bridge）只能用墙钟差还原对局时间，
        # 这里把起点和已过时长直接写进每条状态，避免回放侧各自猜。
        if self._start_t is not None:
            now_wall = time.time()
            state_dict.setdefault("session_start_t", float(self._start_t))
            state_dict.setdefault("session_elapsed_s", float(now_wall - self._start_t))

        units = state_dict.get("units") or []
        mem_units = extra.get("mem_units") or []
        if mem_units:
            self_camp = (state_dict.get("self_state") or {}).get("camp")
            if not self_camp:
                # 不假设 slot0 是本地玩家：按 self 的 cid + 坐标定位，再由其阵营推导敌我
                from wzry.vision.camp_identity import resolve_self
                heroes = extra.get("mem_heroes") or []
                ss = extra.get("self_state") or {}
                self_id = ss.get("id") or ui.get("hero_id")
                self_pos = None
                if ss.get("x") is not None and ss.get("z") is not None:
                    self_pos = (ss["x"], ss["z"])
                if self_id and heroes:
                    res = resolve_self(heroes, int(self_id), self_pos)
                    self_camp = res.camp
                    sources["self_camp"] = res.camp_src
            self_camp = self_camp or "blue"
            units = list(units) + _canonical_mem_units(mem_units, self_camp)
            sources["units_memory"] = "memory"
        if units:
            self._field_hits["units"] += 1
            if any(isinstance(u, dict) and u.get("pos_minimap") for u in units):
                self._field_hits["units.pos_minimap"] += 1
        if isinstance(ui.get("hp"), (int, float)):
            self._field_hits["ui.hp"] += 1
        if isinstance(state_dict.get("clock_s"), (int, float)):
            self._field_hits["clock_s"] += 1
        if state_dict.get("scoreboard"):
            self._field_hits["scoreboard"] += 1
        if (state_dict.get("minimap") or {}).get("dots"):
            self._field_hits["minimap.dots"] += 1

        state_dict.setdefault("input_source", self.input_source)
        record = state_dict.get("record") or {}
        record.update({
            "input_source": self.input_source,
            "memory_active": bool(mem_heroes),
            "clock_known": clock is not None,
        })
        state_dict["record"] = record

        self._states_fp.write(json.dumps({**state_dict, "units": units},
                                         ensure_ascii=False,
                                         default=self._json_default) + "\n")
        self._n_states += 1

    @staticmethod
    def _json_default(o):
        """numpy 标量/数组 -> 原生类型（检测/血条等字段常带 numpy intc/float32）。"""
        import numpy as np
        if isinstance(o, np.generic):
            return o.item()
        if isinstance(o, np.ndarray):
            return o.tolist()
        raise TypeError(f'Object of type {o.__class__.__name__} is not JSON serializable')

    def on_frame(self, frame):
        """按 frame_every_s 节流保存关键帧 PNG。"""
        if not self.active or self._n_frames >= self.max_frames:
            return
        now = time.time()
        if now - self._last_frame_t < self.frame_every_s:
            return
        self._last_frame_t = now
        self.save_frame(frame)

    def save_frame(self, frame, tag=""):
        """立即保存一帧（不受 frame_every_s 节流）——用于"检测到敌人"这类瞬时事件取证。

        tag 非空时文件名带标签前缀（如 enemy_000123.png），便于事后按事件筛图。
        """
        if not self.active or self._n_frames >= self.max_frames:
            return None
        import cv2
        name = f"{tag}_{self._n_frames:06d}.png" if tag else f"frame_{self._n_frames:06d}.png"
        fn = self._frame_dir / name
        cv2.imwrite(str(fn), frame)
        self._last_frame_t = time.time()
        self._n_frames += 1
        return fn

    def on_action(self, act_dict):
        """写入一条动作事件（与执行器事件流同构），缺省标记来源。"""
        if not self.active:
            return
        payload = dict(act_dict or {})
        payload.setdefault("source", self.input_source)
        self._actions_fp.write(json.dumps(payload, ensure_ascii=False,
                                          default=self._json_default) + "\n")

    def on_human_input(self, event):
        """记录人类输入（swipe/tap/key），用于来源 A 的人类动作标签。"""
        payload = dict(event or {})
        payload["source"] = "human"
        self.on_action(payload)

    def field_coverage(self):
        """各关键字段的覆盖率（0..1）；分母是已写入的状态条数。"""
        n = max(1, self._n_states)
        return {k: round(v / n, 4) for k, v in self._field_hits.items()}

    def coverage_gate(self, thresholds=None):
        """采数闸门：返回 (ok, 未达标项)。

        默认阈值对应 S1-5/S2 下游需求——没有这些字段，决策桥接与价值模型只能走近似：
          clock_s >= 0.95 / scoreboard >= 0.90 / units.pos_minimap >= 0.90 / ui.hp >= 0.95
        """
        need = {"clock_s": 0.95, "scoreboard": 0.90,
                "units.pos_minimap": 0.90, "ui.hp": 0.95}
        if thresholds:
            need.update(thresholds)
        cov = self.field_coverage()
        missing = {k: {"got": cov.get(k, 0.0), "need": v}
                   for k, v in need.items() if cov.get(k, 0.0) < v}
        return (not missing), missing

    def _write_manifest(self, extra=None):
        if not self.session_dir:
            return
        manifest = {
            "session": self.session_dir.name,
            "started_at": datetime.fromtimestamp(self._start_t).isoformat()
            if self._start_t else None,
            "n_states": self._n_states,
            "n_frames": self._n_frames,
            "input_source": self.input_source,
            "memory_states": self._n_memory_states,
            "clock_states": self._n_clock_states,
            "field_coverage": self.field_coverage(),
            "meta": self._meta,
        }
        if extra:
            manifest.update(extra)
        (self.session_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def close(self):
        """结束会话并写清单。"""
        if not self.active:
            return
        self._write_manifest()
        self._states_fp.close()
        self._actions_fp.close()
        self._states_fp = None
        self._actions_fp = None
        self.session_dir = None
