#!/usr/bin/env python
"""一条命令验证"逻辑单元数组"能不能用（对局中执行）。

做的事：
  1. 连真机，读 10 名英雄（精确坐标，作为交叉验证基准）；
  2. 发现 0xD0 步长的逻辑单元数组（扫 libGameCore 可写段的指针对）；
  3. **用英雄坐标验证**：同 id 的记录若与英雄实际位置偏差 <= 500 且命中 >= 3 只，才认可；
  4. 认可后统计：兵 / 野 / 塔各补到了多少坐标，并抽样打印。

用法：
    python scripts/verify_logic_units.py
    python scripts/verify_logic_units.py --serial 127.0.0.1:16384

大厅/结算里数组为空，输出"发现失败"属正常；对局中才有意义。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision.mem_battle import MemBattle          # noqa: E402
from wzry.vision.logic_units import LogicUnits        # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="127.0.0.1:16384")
    ap.add_argument("--self-id", type=int, default=0)
    a = ap.parse_args()

    mb = MemBattle(serial=a.serial)
    print("pid =", mb.connect())

    heroes = mb.read_heroes()
    print(f"英雄 {len(heroes)} 名，带坐标 "
          f"{sum(1 for h in heroes if h.get('x') is not None)} 名")

    lu = LogicUnits(mb.src.m)
    if not lu.discover():
        print("发现失败:", lu.reason)
        print("→ 若确认在对局中，试 `python reverse/find_logic_units.py --scan-heap 256`")
        return 1
    print("发现:", lu.reason)

    ok = lu.verify(heroes)
    print("校验:", lu.reason, "->", "通过" if ok else "**不通过**")
    if not ok:
        print("原则：校验不通过就不启用（宁可继续缺坐标，也不灌错数据）")
        return 2

    recs = lu.read()
    from collections import Counter
    print("记录数:", len(recs), "类型分布:",
          dict(Counter(r["type"] for r in recs).most_common(8)))
    print("阵营分布:", dict(Counter(r["camp"] for r in recs)))

    # 与 actor 单位对照：actor 缺坐标、逻辑单元能补上多少
    units = mb.read_units()
    kinds = Counter(u["kind"] for u in units)
    print("actor 单位:", dict(kinds))
    for kind in ("minion", "monster", "turret", "building", "hero"):
        rows = [u for u in units if u["kind"] == kind]
        if not rows:
            continue
        got = sum(1 for u in rows if u.get("x") is not None)
        print(f"  {kind:<9} n={len(rows):<3} 有坐标={got}")

    print("\n抽样（id/camp/type -> xyz）：")
    for r in recs[:8]:
        print(f"  id={r['id']:<8} camp={r['camp']} type={r['type']:<3} "
              f"xyz=({r['x']},{r['y']},{r['z']})")
    print("\n结论: 可以开 `WZRY_LOGIC_COORD=1` 让兵/野/塔拿到坐标。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
