# -*- coding: utf-8 -*-
"""影子 A/B：停用敌人可信过滤后，`decide()` 会怎么变（只读）。

做法
----
不改任何源码，只在进程内临时替换 `wzry.decision.context.parse_units`：
- **A（现状）**：`trusted=enemy_hero_trusted`，`camp_src="default"` / `conf<0.55` 的敌英被丢弃
- **B（候选）**：`trusted=None`，同样剔除 HUD 区域，但保留 `default` 框

两套各自维护独立的 `cooldowns`（互不污染），逐帧调同一个 `m2_agent_v2.decide()`，然后 diff。

能回答三个问题
--------------
1. 停用过滤后，动作会变多少帧？
2. 变成什么（`map_move` → `retreat`/`recall`/`skill`？）？
3. 变化是否集中在"应该变化"的局面（低血 + 敌人贴脸）？

用法
----
  cd WZRYagnt
  venv/Scripts/python.exe -X utf8 scripts/shadow_trusted_filter_ab.py \
      --md ../docs/数据专题/敌人过滤-shadowAB-2026-10-26.md \
      --json runs/eval/trusted_filter_ab.json
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

MATCHES = ROOT / "data" / "matches"


def main() -> int:
    ap = argparse.ArgumentParser(description="停用敌人可信过滤的影子 A/B（只读）")
    ap.add_argument("--md", default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--samples", type=int, default=15)
    args = ap.parse_args()

    import m2_agent_v2 as m2
    import wzry.decision.context as ctx
    from wzry.decision.optimizer import enemy_hero_trusted

    orig_parse = ctx.parse_units

    def parse_all(units, **kw):
        kw.pop("trusted", None)          # 不再丢弃 default / 低置信框
        return orig_parse(units, trusted=None, **kw)

    total = 0
    blind = 0                        # 原始有敌英但 trusted 全丢的帧
    flips = []
    type_cross = collections.Counter()
    reason_cross = collections.Counter()
    hp_bins = collections.Counter()

    for path in sorted(MATCHES.glob("*/states.jsonl")):
        name = path.parent.name
        cd_a, cd_b = {}, {}
        for line in path.open("r", encoding="utf-8", errors="replace"):
            if not line.strip():
                continue
            try:
                st = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not (isinstance(st.get("minimap"), dict) and isinstance(st.get("units"), list)):
                continue
            total += 1

            ctx.parse_units = orig_parse
            act_a = m2.decide(st, cd_a)
            raw = [u for u in (st.get("units") or []) if u.get("cls") == "enemy_hero"]
            trusted = [u for u in raw if enemy_hero_trusted(u)]
            if raw and not trusted:
                blind += 1

            ctx.parse_units = parse_all
            try:
                act_b = m2.decide(st, cd_b)
            finally:
                ctx.parse_units = orig_parse

            ta, tb = act_a.get("type"), act_b.get("type")
            if ta != tb:
                flips.append({
                    "s": name, "fid": st.get("frame_id"),
                    "hp": (st.get("ui") or {}).get("hp"),
                    "a": ta, "a_reason": act_a.get("reason"),
                    "b": tb, "b_reason": act_b.get("reason"),
                    "n_raw": len(raw), "n_trusted": len(trusted),
                })
                type_cross[(ta, tb)] += 1
                reason_cross[(str(act_a.get("reason")), str(act_b.get("reason")))] += 1
                hp = (st.get("ui") or {}).get("hp")
                hp = float(hp) if isinstance(hp, (int, float)) else None
                hp_bins["hp<0.35" if hp is not None and hp < 0.35
                        else ("0.35-0.6" if hp is not None and hp < 0.6 else ">=0.6/未知")] += 1

    out = []
    def emit(s=""):
        print(s)
        out.append(s)

    n_flip = len(flips)
    emit(f"- 语料：**{total}** 帧")
    emit(f"- 有敌英但 trusted 全丢的帧：**{blind}（{blind/total*100:.1f}%）**")
    emit(f"- 停用过滤后动作改变的帧：**{n_flip}（{n_flip/total*100:.2f}%）**")
    emit("")
    if n_flip:
        emit("### 动作迁移矩阵（A=现状 → B=停用过滤）")
        emit("")
        names = sorted({k[0] for k in type_cross} | {k[1] for k in type_cross})
        emit("| A \\ B | " + " | ".join(names) + " | 合计 |")
        emit("|---" * (len(names) + 2) + "|")
        for a in names:
            row = [type_cross.get((a, b), 0) for b in names]
            if sum(row) == 0:
                continue
            emit(f"| **{a}** | " + " | ".join(str(v) for v in row) + f" | {sum(row)} |")
        emit("")
        emit("### 改变的帧按血量")
        emit("")
        for k, v in hp_bins.most_common():
            emit(f"- {k}：{v} 帧")
        emit("")
        emit("### 前 5 个 reason 迁移")
        emit("")
        emit("| 现状 reason | 停用后 reason | 帧数 |")
        emit("|---|---|---:|")
        for (ra, rb), v in reason_cross.most_common(5):
            emit(f"| `{ra}` | `{rb}` | {v} |")
        emit("")
        emit("### 样例")
        emit("")
        emit("| session | frame | hp | 原始敌英 | 可信敌英 | 现状 | 停用后 |")
        emit("|---|---|---:|---:|---:|---|---|")
        for f in flips[: args.samples]:
            hp = f["hp"]
            hp_s = f"{float(hp):.2f}" if isinstance(hp, (int, float)) else "—"
            emit(f"| {f['s']} | {f['fid']} | {hp_s} | {f['n_raw']} | {f['n_trusted']} | "
                 f"`{f['a']}`/{f['a_reason']} | `{f['b']}`/{f['b_reason']} |")
        emit("")

    if args.md:
        p = Path(args.md)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(out) + "\n", encoding="utf-8")
        print(f"\n写入 {p}")
    if args.json:
        p = Path(args.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "frames": total, "blind_frames": blind, "flips": n_flip,
            "type_cross": {f"{k[0]}->{k[1]}": v for k, v in type_cross.items()},
            "hp_bins": dict(hp_bins),
            "flips": flips,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"写入 {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
