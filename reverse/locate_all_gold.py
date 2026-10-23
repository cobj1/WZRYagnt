# -*- coding: utf-8 -*-
"""读取当前局全部英雄的实时金币（兼容旧入口）。

旧实现逐英雄跑 `memscan herostate`，会因坐标/HP 过滤漏掉部分英雄；
2026-10-20 已确认全英雄在同一个连续数组里（stride 0x2AD8）。
现在直接复用 MemSource.read_hero_states()。

用法：
  venv/Scripts/python.exe -X utf8 reverse/locate_all_gold.py
  venv/Scripts/python.exe -X utf8 reverse/all_hero_gold.py --loop 5   # 连续观察
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision.mem_source import MemSource  # noqa: E402


def main() -> int:
    src = MemSource()
    print("pid", src.connect())
    states = src.read_hero_states()
    if not states:
        print("no hero array (lobby or still loading?)")
        return 1
    print(f"heroes={len(states)} arrays={[hex(b) for b in src._hero_arrays]}")
    for r in sorted(states, key=lambda r: (r.get("camp") or "", -r["gold"])):
        print(f"{r.get('camp') or '?':<4} id={r['id']:<4} gold={r['gold']:<6} "
              f"lvl={r['level']:<2} hp={r['hp']}/{r['hp_max']} "
              f"exp={r['exp']} pos=({r['x']},{r['z']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
