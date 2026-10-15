# -*- coding: utf-8 -*-
"""技能升级策略（共享状态下的一个决策分支，低频：只在"获得技能点"时触发）。

分工（与 HoK 层级动作思路一致）：
  - 默认加点顺序 = Hero Profile 里的 level_order（规则，不是模型）
  - 偏离默认 = 规则修正（对线压力/保命需求 → 优先控制/减速类技能）
  - 合法性 = hero_profile.legal_upgrades 的硬掩码（有技能点 + 未满级 + 大招等级门槛）
  - 模型（可选）= 后续用模仿学习训一个"加点头"；当前不启用
  - 执行 = 由 m2 的 UI 加点执行器负责；本模块**只出建议**，动作为空时不改动作流

读数依赖（当前缺口）：技能点 / 各技能当前等级 / 英雄等级 需要感知层读数；
  缺读数时 legal=False（门关着），仍然输出建议供日志与后续训练用。
"""
from __future__ import annotations

from wzry.decision import hero_profile as hp


def _ui(state, key):
    v = (state.get("ui") or {}).get(key)
    return v if isinstance(v, (int, float)) else None


def suggest(state, profile, skill_levels=None):
    """状态 -> 加点建议。

    返回 {"target", "reason", "confidence", "legal", "mask", "gate"}
      gate: 为空表示"可以执行"；非空是拒绝理由（执行器据此不开 UI）。
    """
    if not profile:
        return {"target": None, "reason": "无 Hero Profile", "confidence": "none",
                "legal": False, "mask": {}, "gate": "no_profile"}
    level = _ui(state, "level")
    pts = _ui(state, "skill_point")
    sl = skill_levels or (state.get("extra") or {}).get("skill_levels") or {}
    base = hp.next_upgrade(profile, level if level is not None else 0, sl, pts)
    tips = list((profile.get("skills") or {}).values())
    hp_now = _ui(state, "hp")

    # 规则偏离：残血/对线压力大 -> 优先"控制/减速/护盾"类技能（保命与反打）
    if base.get("target") and hp_now is not None and hp_now < 0.45:
        cands = [sid for sid, ok in (base.get("mask") or {}).items() if ok]
        if cands:
            proto = {str(t.get("slot")): t for t in tips}
            prefer = [s for s in cands
                      if (proto.get(s) or {}).get("kind") in ("control", "slow", "shield")]
            if prefer and prefer[0] != base["target"]:
                base = dict(base, target=prefer[0], confidence="rule:low_hp",
                            reason=f"残血(hp={hp_now:.2f}) -> 优先保命/控制类 skill{prefer[0]}")

    gate = "" if base.get("legal") else (base.get("reason") or "illegal")
    return {"target": base.get("target"), "reason": base.get("reason"),
            "confidence": base.get("confidence"), "legal": bool(base.get("legal")),
            "mask": base.get("mask") or {}, "gate": gate}


def format_line(s):
    if not s or s.get("target") is None:
        return f"加点: 无建议（{s.get('reason') if s else 'n/a'}）"
    t = f"加点建议: skill{s['target']}（{s.get('reason')}）"
    return t if s.get("legal") else t + f" [不执行: {s.get('gate')}]"
