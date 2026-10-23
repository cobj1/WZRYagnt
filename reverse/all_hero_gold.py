# -*- coding: utf-8 -*-
"""不打开战绩面板，读取 10 名英雄的实时金币/等级/KDA/HP。

数据源：C# 英雄状态数组（2026-10-20 实测）
  - 连续 10 个对象，stride 0x2AD8；每槽 +0x16c=英雄 ID、+0x200=当前金币、
    +0x19c=等级、+0x1f4/+0x1f8/+0x1fc=击杀/死亡/助攻、
    +0x1a4/+0x1a8=当前/最大 HP、+0x184/+0x18c=世界坐标 x/z。
  - 首次用 memscan idbag 按 actor 表的 10 个英雄 ID 定位（scudo-only，约 5s）；
    之后每帧只读一块 ~110KB 的连续内存（实测 ~60ms）。
  - 金币语义 = 当前可用金币，与 HUD 右上角、战绩面板“经济”列一致；
    total = gold + 已花费（差分推导，见 ACTOR_FINDINGS.md §10.5）。

用法：
  venv/Scripts/python.exe -X utf8 reverse/all_hero_gold.py
  venv/Scripts/python.exe -X utf8 reverse/all_hero_gold.py --loop 5 --interval 1
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision.mem_source import MemSource  # noqa: E402


def print_frame(states, tag=""):
    print(f"--- {tag} heroes={len(states)}")
    for r in sorted(states, key=lambda r: (r.get("camp") or "", -r["gold"])):
        print(f"  {r.get('camp') or '?':<4} id={r['id']:<4} "
              f"gold={r['gold']:<6} total={r.get('gold_total', '-')!s:<6} "
              f"lvl={r['level']:<2} kda={r['kills']}/{r['deaths']}/{r['assists']} "
              f"hp={r['hp']}/{r['hp_max']:<5} exp={r['exp']:<5} "
              f"pos=({r['x']},{r['z']})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="127.0.0.1:16384")
    ap.add_argument("--loop", type=int, default=1)
    ap.add_argument("--interval", type=float, default=1.0)
    a = ap.parse_args()

    src = MemSource(serial=a.serial)
    print("pid", src.connect(), flush=True)
    for k in range(a.loop):
        t0 = time.time()
        states = src.read_hero_states()
        dt = (time.time() - t0) * 1000
        if not states:
            print(f"[{k}] no hero array (lobby or still loading?) {dt:.0f}ms")
        else:
            print(f"[{k}] {dt:.0f}ms arrays={[hex(b) for b in src._hero_arrays]}")
            print_frame(states)
        if k + 1 < a.loop:
            time.sleep(max(0.0, a.interval - (time.time() - t0)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
