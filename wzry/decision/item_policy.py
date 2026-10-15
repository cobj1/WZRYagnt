# -*- coding: utf-8 -*-
"""装备购买决策（共享状态下的一个决策分支；三层结构里的"规划器 + 购买门控"）。

三层（与用户方案对齐）：
  1) 装备知识库  -> wzry.knowledge（126 件：price / total_price / compound_ids /
     passive_vector / effect_kind / category / fit_heroes）
  2) 出装规划器（本模块）：Hero Profile 的 route + 局势修正 -> 下一件 + 替代路线
  3) 购买执行器（不在本模块）：判金币、开商店、点装备、确认结果

**规划与购买分离**：本模块可以提前决定"下一件是什么"，但"能不能买"由 purchase_gate
决定。商店是**整屏遮挡**——开着商店时感知看到的是商店界面，对局判断会失效，
所以门控很严：只在泉水/安全区且不在战斗时才允许执行。

读数依赖（当前缺口）：金币 / 装备栏 需要感知层读数（digit_reader 的 gold / items ROI）。
缺读数 -> buyable=False（只出计划，不执行）。
"""
from __future__ import annotations

import json
from pathlib import Path

from wzry.decision import hero_profile as hp

ROOT = Path(__file__).resolve().parents[2]


def _gold(state):
    v = (state.get("ui") or {}).get("gold")
    return v if isinstance(v, (int, float)) and v >= 0 else None


def _price_of(name):
    try:
        from wzry.knowledge import item_by_name
        it = item_by_name(name)
        if not it:
            return None
        for k in ("total_price", "price"):
            p = it.get(k)
            if isinstance(p, (int, float)) and p > 0:
                return int(p)
    except Exception:
        pass
    return None


def plan(state, profile, owned=None, enemy_tags=None):
    """出装计划。

    返回 {"route", "next", "price", "stage", "alternates", "buyable", "gate", "reason"}
    enemy_tags: 可选，{"heal": True, "heavy_ad": True, "heavy_ap": True} 之类，
                用于把 profile.items.situational 里的替代件提到 alternates 前面。
    """
    route = hp.item_route(profile)
    if not route:
        return {"route": [], "next": None, "price": None, "stage": None,
                "alternates": [], "buyable": False, "gate": "no_route",
                "reason": "Hero Profile 无出装路线"}
    owned = set(owned or [])
    next_item = next((n for n in route if n not in owned), None)
    if next_item is None:
        return {"route": route, "next": None, "price": None, "stage": "done",
                "alternates": [], "buyable": False, "gate": "route_done",
                "reason": "路线已出完"}
    idx = route.index(next_item)
    stage = "early" if idx <= 1 else ("core" if idx <= 3 else "late")
    alternates = list(((profile or {}).get("items") or {}).get("situational") or [])
    price = _price_of(next_item)
    gold = _gold(state)
    reason = f"路线第 {idx + 1} 件: {next_item}" + (f"（{price} 金）" if price else "（价格未知）")
    if enemy_tags:
        reason += f" | 局势标签={sorted(enemy_tags.keys())}（替代件仅供人工/规划器参考）"
    return {"route": route, "next": next_item, "price": price, "stage": stage,
            "alternates": alternates,
            "buyable": bool(gold is not None and price is not None and gold >= price),
            "gate": "" if (gold is not None and price is not None and gold >= price)
                    else ("gold_unknown" if gold is None else "price_unknown"
                          if price is None else "gold_not_enough"),
            "reason": reason}


def purchase_gate(state, price, in_combat=False, in_fountain=False,
                  shop_available=True):
    """能不能"现在"执行购买（规划与购买分离的开关）。

    返回 (ok, reason)。任何一项不满足都拒绝——宁可晚买，也不要在战斗中开商店。
    """
    if not shop_available:
        return False, "商店不可用"
    if not in_fountain and in_combat:
        return False, "战斗中不开商店（整屏遮挡，会中断对局判断）"
    gold = _gold(state)
    if gold is None:
        return False, "金币读数不可用（感知缺口，digit_reader.gold 未标定）"
    if price is None:
        return False, "装备价格未知"
    if gold < price:
        return False, f"金币不足（{gold} < {price}）"
    return True, "泉水/安全区且金币足够"


def format_line(p, gate=None):
    if not p or not p.get("next"):
        return f"出装: 无计划（{p.get('reason') if p else 'n/a'}）"
    s = f"出装: 下一件 {p['next']}（{p.get('stage')}）"
    if gate and not gate[0]:
        s += f" [不购买: {gate[1]}]"
    return s
