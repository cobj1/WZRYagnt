# -*- coding: utf-8 -*-
"""复刻版决策管线：特征 → 打分 → top-k → 规则覆盖 → 回退。

与官方的对应关系（逐段可对账）：

| 官方 | 本模块 |
| --- | --- |
| `GameTacticsManager::FeatureProcess()` 0x2751af4 | `FeatureBuilder`（features.build） |
| 模型前向 → `tactics_prob[]` | `Scorer` 协议（默认 `LinearScorer`，可替换成任何模型） |
| `SetTacticsResult(frame, prob, cmd_idx)` 0x2558f0c | `Decision.prob / rank / label` |
| `[SelectTactics] max prob / rank` | `Decision.prob / rank` |
| `RuleTactics->Process` 0x27d1614（141 条） | `rules.apply_rules()` |
| `[UnpredictProcess]` 回退 | `OfficialAI._unpredict()` |

**唯一的替代**：官方的神经网络权重不在包内（见 `docs/wzry-ai-data-inputs.md` §9），
所以 `Scorer` 位置用线性打分器顶替。接口一致，将来有模型可直接塞进来。
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import config as cfg
from . import features as feat
from . import field as vfield
from . import motion as vpost
from . import rules as rulemod
from .actions import Action, enumerate_actions, enumerate_moves, T_HERO, T_MONSTER
from .snapshot import Snapshot

MODEL = "model"
RULE = "rule"
FALLBACK = "fallback"
CACHED = "cached"

STUCK_MOVE_EPS = 60.0        # 相邻决策点位移小于此值（世界单位）视为"没走动"


@dataclass
class Decision:
    label: str
    action: Action
    prob: float = 0.0
    rank: int = 0
    source: str = MODEL
    hits: List[rulemod.RuleHit] = field(default_factory=list)
    pack_summary: str = ""
    frame: int = 0
    candidates: int = 0
    scene: str = "default"
    stuck: int = 0

    def as_dict(self) -> dict:
        return {"label": self.label, "prob": round(self.prob, 4), "rank": self.rank,
                "source": self.source,
                "hits": [{"rule": h.name, "verdict": h.verdict, "reason": h.reason,
                          "evidence": h.evidence}
                         for h in self.hits],
                "frame": self.frame, "candidates": self.candidates,
                "scene": self.scene, "stuck": self.stuck,
                "features": self.pack_summary}


# ---------------------------------------------------------------- 打分器
class LinearScorer:
    """线性打分器（替代官方模型）。

    打分维度刻意贴官方特征口径：
      血量差 / 是否在射程 / 技能就绪 / 目标价值 / 距离成本 / 塔与危险惩罚。
    输出是**原始分数**，由 `softmax` 转成概率，对应官方 `prob`。
    """

    W = {
        "hp_deficit": 60.0,     # 目标越残越值得打
        "in_range": 12.0,
        "killable": 25.0,
        "skill_bonus": 8.0,     # 用技能比普攻略优先（清线/换血）
        "dist_cost": 40.0,
        "tower_penalty": 30.0,
        "danger_penalty": 35.0,
        "monster_bonus": 4.0,
        "none_penalty": -6.0,
        "move_bonus": 2.0,
        "retreat_bonus": 10.0,
        "grid_delta": 3.0,      # 走位价值场增量权重（field.move_delta）
        "repeat_penalty": 3.0,  # 连续两帧同一走位方向 -> 罚（官方 last_n_yaction 防抖）
        "stuck_bonus": 6.0,     # 卡墙时"换向"候选加权
    }

    def score(self, snap: Snapshot, act: Action, pack: feat.FeaturePack) -> float:
        w = self.W
        me = snap.self_unit
        if act.kind == "none":
            return w["none_penalty"]
        if act.kind == "move":
            if act.meta.get("retreat"):
                # 血越少越该撤；撤退点本身也按价值场校验
                base = w["retreat_bonus"] * (1.0 - (me.hp_rate if me else 1.0)) + 4.0
                if act.point:
                    base += w["grid_delta"] * vfield.move_delta(snap, *act.point)
                return base
            if act.point is None:
                return w["none_penalty"]
            # 走位打分 = 价值场增量（对齐官方 grid action 由模型打分的位置）
            s = w["move_bonus"] + w["grid_delta"] * vfield.move_delta(snap, *act.point)
            recent = list(snap.last_actions or [])[-2:]
            if act.label.startswith("Move") and act.label in recent:
                s -= w["repeat_penalty"]          # 上一帧刚这么走过 -> 别原地抖
            if snap.stuck_frames >= 8 and act.label.startswith(("Move", "StuckBreak",
                                                               "GridBest")):
                s += w["stuck_bonus"] * 0.5       # 卡墙时更愿意换方向
            return s
        u = act.target
        if u is None:
            return w["none_penalty"]
        d = act.meta.get("dist", snap.dist(u))
        rng = max(1.0, float(me.atk_range if me else 300.0))
        # ⚠️ 血量未知 ≠ 残血。`Unit.hp_rate` 在 `hp_max` 缺失时返回 0.0，
        #   直接套公式会得到 **hp_deficit 满分（+60）且 killable 必中（+25）** ——
        #   "读不到血量的目标"反而成了最该打的目标。真机上建筑没有 pos/hp 时就会踩到。
        #   所以未知一律按"中性"处理：不给残血奖励，也不给可击杀奖励。
        hp_known = bool(u.hp_max) and u.hp is not None
        hp_rate = u.hp_rate if hp_known else 0.5
        s = 0.0
        if hp_known:
            s += w["hp_deficit"] * (1.0 - hp_rate)
        s += w["in_range"] if d <= rng * 1.15 else 0.0
        s += w["killable"] if (hp_known and u.hp <= 400) else 0.0
        if act.skill_slot > 0:
            # 技能优先级：大招 > 2 > 1，且**只在能打出价值时**加分
            # （官方模型学到的也是这个形态：残血才交大，满血只用小技能消耗）
            sv = {1: 2.0, 2: 3.5, 3: 5.0}.get(act.skill_slot, 2.0)
            s += w["skill_bonus"] * 0.4
            if hp_known:
                s += sv * (1.0 - hp_rate) * 2.0
                if hp_rate > 0.7:
                    s -= sv * 0.8
        s -= w["dist_cost"] * min(1.0, d / 3000.0)
        if act.target_kind in ("Tower", "Crystal"):
            s -= w["tower_penalty"]
            if snap.enemy_hero_min_dist() < 301:
                s -= w["tower_penalty"]
        if act.target_kind == T_HERO and snap.enemy_hero_min_dist() < 301:
            s -= w["danger_penalty"] * 0.5
        if act.target_kind == T_MONSTER:
            s += w["monster_bonus"]
        return s


def softmax(scores: Sequence[float], temperature: float = 1.0) -> List[float]:
    if not scores:
        return []
    t = max(1e-6, float(temperature))
    m = max(scores)
    ex = [math.exp((s - m) / t) for s in scores]
    z = sum(ex) or 1.0
    return [e / z for e in ex]


# ---------------------------------------------------------------- 主管线
class OfficialAI:
    """官方做法的复刻版：同一套输入、同一套动作空间、同一套决策顺序。

    用法：
        ai = OfficialAI(level="hard")
        dec = ai.decide(snapshot)
    """

    def __init__(self, level: str = "normal", scorer: Optional[LinearScorer] = None,
                 params: Optional[cfg.AIParams] = None,
                 clock: Callable[[], float] = time.monotonic,
                 rule_mode: str = "all",
                 predict_interval: Optional[float] = None,
                 delay_frames: Optional[int] = None):
        self.params = params or cfg.get(level)
        if predict_interval is not None or delay_frames is not None:
            kw = {}
            if predict_interval and predict_interval > 0:
                kw["predict_freq"] = 1.0 / float(predict_interval)
            if delay_frames is not None and delay_frames >= 0:
                kw["delay_frames"] = int(delay_frames)
            if kw:
                self.params = replace(self.params, **kw)
        self.scorer = scorer or LinearScorer()
        self.clock = clock
        self.rule_mode = rule_mode if rule_mode in rulemod.MODES else "all"
        self._last_ts = 0.0
        self._last_dec: Optional[Decision] = None
        self._history: deque = deque(maxlen=max(1, self.params.delay_frames + 1))
        self._acts: deque = deque(maxlen=8)          # 最近动作标签（官方 last_n_yaction）
        self._last_pos: Optional[tuple] = None
        self._last_kind: str = ""
        self.stuck_frames = 0
        self._frame = 0
        self.n_model = 0
        self.n_rule = 0
        self.n_fallback = 0
        self.n_cached = 0
        self.n_scene: Dict[str, int] = {}

    # ---- 节拍（官方 predict_freq / predict_interval_* 场景化）----
    def scene_of(self, snap: Snapshot, act: Optional[Action] = None) -> str:
        """当前场景（决定预测间隔）。判据顺序即优先级。"""
        me = snap.self_unit
        if self.stuck_frames >= rulemod.STUCK_WALL_FRAMES:
            return "stuck_wall"
        if me is not None and me.hp_rate < 0.30:
            return "return_city"
        if snap.enemy_hero_min_dist() < 900.0:
            return "dy"
        if act is not None and act.target_kind == "Monster":
            return "atk_monster"
        if snap.monsters:
            d = min(snap.dist(m) for m in snap.monsters)
            if d < 900.0:
                return "atk_monster"
        if me is not None and snap.minions:
            d = min(snap.dist(m) for m in snap.minions)
            if d <= float(me.atk_range or 300.0) * 1.15:
                return "atk_soldier"
        return "default"

    def _gap(self, scene: str) -> float:
        iv = self.params.scene_interval.get(scene, 0.0) or 0.0
        return iv if iv > 0 else 1.0 / max(0.1, self.params.predict_freq)

    def _interval_ok(self, scene: str = "default") -> bool:
        now = self.clock()
        gap = self._gap(scene)
        if now - self._last_ts < gap:
            return False
        self._last_ts = now
        return True

    def _delayed(self, snap: Snapshot) -> Snapshot:
        """观测延迟：用 N 帧之前的快照（拟人 + 抗抖动）。"""
        self._history.append(snap)
        if self.params.delay_frames <= 0 or len(self._history) <= self.params.delay_frames:
            return self._history[0] if self.params.delay_frames > 0 else snap
        return self._history[0]

    def _prepared(self, snap: Snapshot) -> Snapshot:
        """延迟快照 + 本帧的"非观测类"状态（可见半径/卡墙计数/动作历史）。

        ⚠️ `_delayed` 返回的是**历史快照对象**（hard=1 / normal=3 / easy=6 帧前），
        它带着当时的 `stuck_frames` / `last_actions`。这三项不属于"观测"，必须用**当前**值覆盖，
        否则有延迟的档位下 IsStuckWall / LongTimeOnlyAtk 读到的是 N 帧前的旧值而永不触发。
        """
        s = self._delayed(snap)
        s.view_distance = float(self.params.view_distance)
        s.stuck_frames = self.stuck_frames
        s.last_actions = list(self._acts)
        return s

    # ---- 卡墙/动作历史（官方 IsStuckWall / add_last_n_yaction）----
    def _observe(self, snap: Snapshot) -> Snapshot:
        pos = None if snap.self_x is None else (snap.self_x, snap.self_z)
        if (self._last_pos is not None and pos is not None
                and self._last_kind == "move"):
            moved = math.hypot(pos[0] - self._last_pos[0], pos[1] - self._last_pos[1])
            self.stuck_frames = self.stuck_frames + 1 if moved < STUCK_MOVE_EPS else 0
        else:
            self.stuck_frames = 0
        self._last_pos = pos
        snap.stuck_frames = self.stuck_frames
        snap.last_actions = list(self._acts)
        return snap

    # ---- 主入口 ----
    def decide(self, snap: Snapshot) -> Decision:
        self._frame += 1
        s = self._observe(snap)
        scene = self.scene_of(s)
        self.n_scene[scene] = self.n_scene.get(scene, 0) + 1
        if not self._interval_ok(scene) and self._last_dec is not None:
            d = self._last_dec
            self.n_cached += 1
            return Decision(d.label, d.action, d.prob, d.rank, CACHED, d.hits,
                            d.pack_summary, self._frame, d.candidates, scene,
                            self.stuck_frames)

        s = self._prepared(snap)
        pack = feat.build(s, interval_ok=(self._frame % max(1, self.params.feature_interval) == 0))
        if not pack.vec:
            pack = feat.build(s)          # 非抽样帧也要能出决策
        # 官方 `[UnpredictProcess] main hero dead` / 无单位时的同类回退
        if s.self_unit is None or not s.heroes:
            return self._unpredict(s, pack, "no_heroes", scene)
        # 复活倒计时内不下决策：官方 CheckReturnCity 的 REVIVE_DIS_NOT_RETURN_CITY 分支同理
        if (s.self_unit.respawn_s or 0) > 0:
            return self._unpredict(s, pack, f"respawn:{int(s.self_unit.respawn_s)}s", scene)
        actions = enumerate_actions(s)
        actions.extend(enumerate_moves(s, retreat=self._should_retreat(s)))
        # 官方 grid action 的强候选：价值场全局最优点（粗网格搜索）
        bp = vfield.best_point(s)
        if bp is not None:
            actions.append(Action("GridBest", "move", point=bp,
                                  meta={"grid_best": True}))
        if not actions:
            return self._unpredict(s, pack, "no_candidates", scene)

        scores = [self.scorer.score(s, a, pack) for a in actions]
        probs = softmax(scores, self.params.temperature)
        order = sorted(range(len(actions)), key=lambda i: probs[i], reverse=True)
        best = order[0]
        act, prob, rank = actions[best], probs[best], 0

        final, hits = rulemod.apply_rules(s, act, mode=self.rule_mode)
        if final is not act:
            self.n_rule += 1
            src = RULE
            label = final.label
        else:
            self.n_model += 1
            src = MODEL
            label = act.label
        # 官方 SetPostOptimze：方向量化 + 单步限幅 + 场景偏差（"人类可执行性"层）
        final = vpost.apply_post(s, final, scene=scene)
        label = final.label
        dec = Decision(label, final, prob, rank, src, hits, feat.summary(pack),
                       self._frame, len(actions), scene, self.stuck_frames)
        self._last_dec = dec
        # 官方 add_last_n_yaction：只记"自己真正下发的动作"，供下一帧特征/规则用
        self._acts.append(label)
        self._last_kind = final.kind
        return dec

    def _should_retreat(self, snap: Snapshot) -> bool:
        me = snap.self_unit
        return bool(me and me.hp_rate < 0.35)

    # ---- 回退：官方 `[UnpredictProcess]` ----
    def _unpredict(self, snap: Snapshot, pack: feat.FeaturePack, why: str,
                   scene: str = "default") -> Decision:
        self.n_fallback += 1
        me = snap.self_unit
        if me is not None and me.hp_rate < 0.3:
            act = Action("Retreat", "move", point=snap.retreat_point(),
                         meta={"retreat": True})
            label = "Retreat"
        else:
            foes = sorted([h for h in snap.enemies() if h.alive], key=snap.dist)
            if foes and snap.dist(foes[0]) <= float(me.atk_range if me else 300) * 1.15:
                act = Action("AtkHero", "attack", T_HERO, foes[0], 0,
                             meta={"dist": snap.dist(foes[0])})
                label = "AtkHero"
            else:
                act = Action("NoneAction", "none")
                label = "NoneAction"
        dec = Decision(label, act, 0.0, 0, FALLBACK,
                       [rulemod.RuleHit("UnpredictProcess", "PASS", why)],
                       feat.summary(pack), self._frame, 0, scene, self.stuck_frames)
        self._acts.append(label)
        self._last_kind = act.kind
        return dec

    def stats(self) -> dict:
        return {"model": self.n_model, "rule": self.n_rule,
                "fallback": self.n_fallback, "cached": self.n_cached,
                "stuck": self.stuck_frames, "rule_mode": self.rule_mode,
                "rules": rulemod.chain_stats(),
                "scenes": dict(self.n_scene),
                "params": self.params.name}
