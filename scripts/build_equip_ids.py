# -*- coding: utf-8 -*-
"""从官方对局数据生成 `configs/equip_ids.json`（装备 config_id -> 名称/被动）。

为什么要有这张表：内存侧 `equip_ids` 已能读出**已购装备的 config_id 序列**
（`+0x1928` count / `+0x192C` 数组），但 `equip_config_id_file` 不随包，
ID 到名称的映射只能自建。没有它，`CheckReturnCity` 的"有复活甲不回城"
分支与 `WaitResurrectionArmor` 永远走不到。

数据来源（可核实、可重跑）：
    hokdata/external/kpl/raw/*/battle_*.json
    -> data.battle_player_list[*].BriefEquipList[*].{equip_id, equip_name, equip_desc_function}

这是腾讯官方赛事数据接口的原始返回，`equip_id` 与客户端 config_id 同源；
不掺任何手写 ID，避免"凭记忆编装备号"。

用法：
    python -X utf8 scripts/build_equip_ids.py            # 写 configs/equip_ids.json
    python -X utf8 scripts/build_equip_ids.py --check    # 只校验现有表是否过期
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "configs" / "equip_ids.json"

# 复活类被动的判定词：官方装备描述里写"死亡后…原地复活"或"复生"
REVIVE_WORDS = ("复活", "复生")


def find_sources() -> list[str]:
    """定位 KPL 官方对局数据（允许用环境变量换数据根）。"""
    roots = []
    env = os.environ.get("HOKDATA_ROOT")
    if env:
        roots.append(Path(env))
    roots.append(ROOT.parent / "hokdata")
    pats = []
    for r in roots:
        pats.append(str(r / "external" / "kpl" / "raw" / "*" / "battle_*.json"))
    out: list[str] = []
    for p in pats:
        out.extend(glob.glob(p))
    return sorted(set(out))


def collect(files: list[str]) -> dict[int, dict]:
    items: dict[int, dict] = defaultdict(lambda: {"name": None, "seen": 0,
                                                  "revive": False, "desc": ""})
    for p in files:
        try:
            data = json.load(open(p, encoding="utf-8"))["data"]
        except Exception:
            continue
        for pl in data.get("battle_player_list") or []:
            for e in pl.get("BriefEquipList") or []:
                eid = e.get("equip_id")
                if not isinstance(eid, int) or eid <= 0:
                    continue
                rec = items[eid]
                rec["seen"] += 1
                rec["name"] = e.get("equip_name") or rec["name"]
                fn = e.get("equip_desc_function") or ""
                if any(w in fn for w in REVIVE_WORDS):
                    rec["revive"] = True
                    if not rec["desc"]:
                        rec["desc"] = fn[:160]
    return dict(items)


def build(files: list[str]) -> dict:
    items = collect(files)
    if not items:
        raise SystemExit("没有找到任何 KPL 对局数据；先确认 hokdata/external/kpl/raw 存在")
    named = {i: r for i, r in items.items() if r["name"]}
    revive = sorted(i for i, r in named.items() if r["revive"])
    return {
        "schema": 1,
        "version": datetime.now().strftime("%Y-%m-%d.1"),
        "generated_by": "scripts/build_equip_ids.py",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": {
            "dataset": "hokdata/external/kpl/raw/*/battle_*.json",
            "field": "data.battle_player_list[*].BriefEquipList[*]",
            "n_battles": len(files),
            "note": "腾讯官方赛事数据；equip_id 与客户端 config_id 同源，未手工编造",
        },
        "n_items": len(named),
        "revive_armor_ids": revive,
        "items": {
            str(i): {"name": r["name"], "revive": bool(r["revive"]),
                     "seen": int(r["seen"]),
                     **({"desc": r["desc"]} if r["desc"] else {})}
            for i, r in sorted(named.items(), key=lambda kv: -kv[1]["seen"])
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="不写文件，只比较现有表与重新生成的结果")
    args = ap.parse_args()

    files = find_sources()
    payload = build(files)
    if args.check:
        if not OUT.exists():
            print("FAIL 配置不存在:", OUT)
            return 1
        old = json.loads(OUT.read_text(encoding="utf-8"))
        same = (old.get("revive_armor_ids") == payload["revive_armor_ids"]
                and set(old.get("items") or {}) == set(payload["items"]))
        print(f"{'PASS' if same else 'FAIL'} 装备表一致："
              f"{len(payload['items'])} 件 / 复活甲 {payload['revive_armor_ids']}")
        return 0 if same else 1

    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"写入 {OUT.relative_to(ROOT)}：{payload['n_items']} 件装备，"
          f"复活甲 ID={payload['revive_armor_ids']}（来源 {len(files)} 场官方对局）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
