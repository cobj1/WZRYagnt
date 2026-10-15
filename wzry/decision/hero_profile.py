# -*- coding: utf-8 -*-
"""Hero Profile：英雄专属配置的加载与应用（加点计划 + 出装路线 + 射程/连招）。

设计原则：**英雄知识全在配置里，代码不写死**（configs/heroes/<name>.json）。
  改一个英雄的加点/出装 = 改 JSON，不动代码；训练别的英雄仍复用共享能力。

加点硬规则（王者规则，写在代码里，配置只给"优先级顺序"）：
  - 1/2 技能上限 5 级；大招上限 3 级，且只有英雄等级到 4/8/12 才能升大招
  - 15 级共 15 点：主技 5 + 副技 5 + 大招 3 = 13，剩 2 点无技能可升（正常）
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = ROOT / "configs" / "heroes"

SKILL_CAP = {"main": 5, "sub": 5, "ult": 3}
ULT_LEVEL_GATES = (4, 8, 12)
MAX_LEVEL = 15


def list_profiles():
    if not PROFILE_DIR.exists():
        return {}
    return {p.stem: p for p in sorted(PROFILE_DIR.glob("*.json"))}


def load(name_or_path):
    """加载 profile。传英雄名（'钟馗'/'zhongkui'）或直接给路径；找不到返回 None。"""
    if not name_or_path:
        return None
    p = Path(name_or_path)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    cand = PROFILE_DIR / f"{p.stem if p.suffix else p.name}.json"
    if cand.exists():
        return json.loads(cand.read_text(encoding="utf-8"))
    for stem, path in list_profiles().items():
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("hero") == name_or_path or stem == name_or_path:
            return d
    return None


def build_level_plan(profile):
    """profile -> {等级: 该升的技能id}（1..15）。缺 level_order 返回 {}。"""
    order = [str(x) for x in (profile or {}).get("level_order") or []]
    if len(order) < 2:
        return {}
    main = order[0]
    sub = order[1] if len(order) > 1 else None
    ult = order[2] if len(order) > 2 else None
    used = {main: 0} if main else {}
    if sub:
        used[sub] = 0
    if ult:
        used[ult] = 0
    plan = {}
    for lv in range(1, MAX_LEVEL + 1):
        pick = None
        if ult and lv in ULT_LEVEL_GATES and used.get(ult, 0) < SKILL_CAP["ult"]:
            pick = ult
        if pick is None and used.get(main, 99) < SKILL_CAP["main"]:
            pick = main
        if pick is None and sub and used.get(sub, 99) < SKILL_CAP["sub"]:
            pick = sub
        plan[lv] = pick
        if pick:
            used[pick] = used.get(pick, 0) + 1
    return plan


def legal_upgrades(profile, level, skill_levels=None, skill_points=None):
    """当前可升级的技能掩码（硬规则）。

    skill_levels: {技能id: 当前等级}；缺读数时按"全 0"保守处理。
    skill_points: 可用技能点数（None/-1 = 未知）。
    返回 {"mask": {id: bool}, "legal": bool, "reason": str}
    """
    order = [str(x) for x in (profile or {}).get("level_order") or ["1", "2", "3"]]
    ult = order[2] if len(order) > 2 else None
    lv = int(level) if isinstance(level, (int, float)) else 0
    sl = dict(skill_levels or {})
    mask = {}
    for sid in ("1", "2", "3"):
        cur = sl.get(sid) or sl.get(int(sid)) or 0
        if sid == ult:
            mask[sid] = bool(lv in ULT_LEVEL_GATES and cur < SKILL_CAP["ult"])
        else:
            mask[sid] = bool(cur < SKILL_CAP["main"])
    if not any(mask.values()):
        return {"mask": mask, "legal": False, "reason": "无任何技能可升级"}
    if skill_points is None or (isinstance(skill_points, (int, float)) and skill_points <= 0):
        return {"mask": mask, "legal": False,
                "reason": "技能点读数不可用（或为 0）-> 只出计划，不执行"}
    return {"mask": mask, "legal": True, "reason": "有技能点且目标未满级"}


def next_upgrade(profile, level, skill_levels=None, skill_points=None):
    """默认加点计划里"这一级该升谁" -> {"target", "reason", "legal", "mask"}。"""
    plan = build_level_plan(profile)
    target = plan.get(int(level)) if isinstance(level, (int, float)) else None
    chk = legal_upgrades(profile, level, skill_levels, skill_points)
    if target is None:
        return {"target": None, "reason": "该等级无加点计划", "confidence": "none",
                "mask": chk["mask"], "legal": False}
    if not chk["mask"].get(target, False):
        return {"target": target, "reason": f"计划升 skill{target} 但硬规则不允许",
                "confidence": "plan", "mask": chk["mask"], "legal": False}
    return {"target": target, "reason": f"按加点计划升 skill{target}", "confidence": "plan",
            "mask": chk["mask"], "legal": chk["legal"]}


def combo(profile):
    return [str(s) for s in (profile or {}).get("combo") or []]


def item_route(profile):
    it = (profile or {}).get("items") or {}
    return list(it.get("route") or [])


def skill_range_frac(profile, skill_id):
    r = (profile or {}).get("ranges") or {}
    v = r.get(f"skill{skill_id}_frac")
    return float(v) if isinstance(v, (int, float)) else None
