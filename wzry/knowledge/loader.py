# -*- coding: utf-8 -*-
"""知识库加载：S45 版本的装备(126) / 技能(538) / 英雄(132)。

数据来源是隔壁仓库 hok-decision-ai/data/S45/normalized/（官方游戏内接口抓取的
equip_details.json / items_vector.json / skills_vector.json / heroes.json）。

路径解析优先级：
  1) 环境变量 WZRY_KNOWLEDGE_DIR
  2) configs/knowledge.json 里的 {"dir": "..."}
  3) 默认 ../hok-decision-ai/data/S45/normalized（相对本仓库）

找不到文件 -> 返回空库并打印一次告警，调用方降级为纯规则（不崩）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = ROOT.parent / "hok-decision-ai" / "data" / "S45" / "normalized"

_FILES = {
    "items": "items_vector.json",
    "equip": "equip_details.json",
    "skills": "skills_vector.json",
    "heroes": "heroes.json",
}
_cache = {}
_warned = set()


def resolve_dir():
    env = os.environ.get("WZRY_KNOWLEDGE_DIR")
    if env:
        return Path(env)
    cfg = ROOT / "configs" / "knowledge.json"
    if cfg.exists():
        try:
            d = json.loads(cfg.read_text(encoding="utf-8")).get("dir")
            if d:
                return Path(d)
        except Exception:
            pass
    return DEFAULT_DIR


def _load(kind):
    if kind in _cache:
        return _cache[kind]
    p = resolve_dir() / _FILES[kind]
    rows = []
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            rows = d.get("data") or []
        except Exception as e:
            if kind not in _warned:
                _warned.add(kind)
                print(f"[knowledge] 解析失败 {p}: {type(e).__name__}: {e}")
    elif kind not in _warned:
        _warned.add(kind)
        print(f"[knowledge] 缺文件 {p} -> 该库为空，调用方按无知识降级")
    _cache[kind] = rows
    return rows


def items():
    """装备列表（items_vector：price/total_price/compound_ids/attributes/passive_vector/...）"""
    return _load("items")


def equip_details():
    """装备详情（equip_details：attributes_text/effects_text/fit_heroes/tips/...）"""
    return _load("equip")


def skills():
    """技能列表（cooldown / cooldown_min / cooldown_max / damage / slot / effect_vector）"""
    return _load("skills")


def heroes():
    """英雄列表（cname / ename / hero_type / roles / title）"""
    return _load("heroes")


def item_by_name(name):
    for it in items():
        if it.get("item_name") == name or it.get("equipment_id") == name:
            return it
    return None


def skills_by_hero(hero_cname):
    """按英雄名取技能（skills_vector 的 cname/ename 常为技能名，英雄归属需 names 前缀近似匹配）。"""
    out = []
    for s in skills():
        names = (str(s.get("cname", "")) + str(s.get("ename", "")) + str(s.get("name", "")))
        if hero_cname and hero_cname in names:
            out.append(s)
    return out


def skill_cd_prior(skill_name, slot=None):
    """技能 CD 先验（秒）：给记忆层做"剩余冷却"估计用。找不到返回 None。"""
    for s in skills():
        nm = str(s.get("name") or s.get("cname") or "")
        if skill_name and skill_name == nm:
            if slot is not None and s.get("slot") not in (None, slot):
                continue
            for k in ("cooldown", "cooldown_max"):
                v = s.get(k)
                if isinstance(v, (int, float)) and v > 0:
                    return float(v)
    return None


def stats():
    return {"dir": str(resolve_dir()), "items": len(items()),
            "equip": len(equip_details()), "skills": len(skills()),
            "heroes": len(heroes())}


if __name__ == "__main__":
    print(json.dumps(stats(), ensure_ascii=False, indent=1))
    it = item_by_name("痛苦面具")
    print("痛苦面具:", {k: it.get(k) for k in ("price", "total_price", "category",
                                              "compound_ids")} if it else None)
