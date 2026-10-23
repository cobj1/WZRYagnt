#!/usr/bin/env python
"""定位"非英雄单位"的世界坐标（兵 / 野怪 / 建筑）。

背景：`MemSource.snapshot()` 里，英雄的 pos 组件在 `actor+0x230`，校验通过后
能读到 `comp+0x128 / +0x130` 的 x/z。但兵、野怪、建筑的这一格是 **0**，所以
`MemUnit.x` 一直是 None —— 结果 official AI 永远枚举不出"打兵 / 打野 / 推塔"候选，
只能追英雄；特征里它们还会以 (0,0) 混进来（已在下游堵住，但根因在读数）。

这个脚本不改任何代码，只在**一局进行中**做三件事：
  1. 打印当前各类单位的读数状态（有坐标/无坐标、血量是否合理）；
  2. 对每个非英雄 actor，扫描它内部所有"候选组件指针"，找出 `comp+0x10 == actor`
     且 `comp+0xc == sid` 的那些（英雄的 pos 组件就是这条规则）；
  3. 若组件路径找不到，退一步在 actor 体内扫描 i32 三元组，挑出"像地图坐标"
     （与英雄坐标同量级、随时间缓慢变化）的偏移，并按稳定性排序。

用法（对局中执行）：
    python reverse/probe_unit_pos.py                    # 自动找第一个小兵
    python reverse/probe_unit_pos.py --kind monster     # 只看野怪
    python reverse/probe_unit_pos.py --watch 3          # 连续采样 3 次，看哪个偏移在动

拿到的偏移直接告诉我就行，我把它接进 `MemSource._refresh` 的校验分支。
"""
from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wzry.vision.mem_source import MemSource  # noqa: E402

SCAN_END = 0x400          # actor 对象头的扫描范围
COMP_READ = 0x148         # 组件对象读多长


def _ptr(v: int) -> bool:
    return 0x1000 < v < 0x8000_0000_0000


def scan_components(src: MemSource, actor: int) -> list:
    """扫描 actor 内的指针槽，找"回指 actor"的组件对象。"""
    raw = src.m.read(actor, SCAN_END)
    out = []
    for off in range(0, SCAN_END - 8, 8):
        p = struct.unpack_from("<Q", raw, off)[0]
        if not _ptr(p):
            continue
        try:
            b = src.m.read(p, COMP_READ)
        except Exception:
            continue
        if len(b) < COMP_READ:
            continue
        back = struct.unpack_from("<Q", b, 0x10)[0]
        if back != actor:
            continue
        sid = struct.unpack_from("<I", b, 0xC)[0]
        sig = struct.unpack_from("<4i", b, 0x100)
        xyz = struct.unpack_from("<3i", b, 0x128)
        out.append({"slot": off, "comp": p, "sid": sid, "sig": sig, "xyz": xyz})
    return out


def scan_inline(src: MemSource, actor: int) -> list:
    """在 actor 体内找"像世界坐标"的 i32 三元组。"""
    raw = src.m.read(actor, SCAN_END)
    n = len(raw) // 4
    vals = struct.unpack_from("<%di" % n, raw, 0)
    hits = []
    for i in range(n - 2):
        x, y, z = vals[i], vals[i + 1], vals[i + 2]
        if not (-90000 <= x <= 90000 and -90000 <= z <= 90000):
            continue
        if abs(x) < 2000 or abs(z) < 2000:      # 排除 0/小整数噪声
            continue
        if not (-3000 <= y <= 3000):
            continue
        hits.append((i * 4, x, y, z))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="127.0.0.1:16384")
    ap.add_argument("--kind", default="", help="minion / monster / turret / building")
    ap.add_argument("--watch", type=int, default=1, help="连续采样次数（看谁在动）")
    args = ap.parse_args()

    src = MemSource(serial=args.serial)
    print("pid =", src.connect())
    units = src.snapshot()
    if not units:
        print("没有读到单位 —— 需要**进入对局**（大厅/结算读不到 actor）")
        return 1

    from collections import Counter
    print("单位总数:", len(units), Counter(u.kind for u in units))
    for kind in ("hero", "minion", "monster", "turret", "building"):
        rows = [u for u in units if u.kind == kind]
        if not rows:
            continue
        n_pos = sum(1 for u in rows if u.x is not None)
        hp = sorted({(u.hp, u.hp_max) for u in rows})
        print(f"  {kind:<9} n={len(rows):<3} 有坐标={n_pos:<3} hp样本={hp[:3]}")

    targets = [u for u in units if u.kind == (args.kind or "minion")]
    if not targets:
        targets = [u for u in units if not u.is_hero]
    if not targets:
        print("没有非英雄单位（对局还没出兵？等 30 秒再试）")
        return 1

    for u in targets[:3]:
        print(f"\n=== {u.kind} cid={u.config_id} actor=0x{u.actor:x} "
              f"当前 x={u.x} z={u.z} hp={u.hp}/{u.hp_max}")
        comps = scan_components(src, u.actor)
        if comps:
            print("  回指 actor 的组件：")
            for c in comps:
                print(f"    slot=0x{c['slot']:03x} comp=0x{c['comp']:x} sid={c['sid']} "
                      f"sig={c['sig']} xyz={c['xyz']}")
        else:
            print("  没有任何回指 actor 的组件（pos 组件确实不存在）")
        inline = scan_inline(src, u.actor)
        if inline:
            print("  actor 体内疑似坐标三元组（off, x, y, z）:")
            for off, x, y, z in inline[:8]:
                print(f"    +0x{off:03x}  ({x}, {y}, {z})")

    if args.watch > 1:
        print(f"\n=== 连续采样 {args.watch} 次（看哪个偏移随时间变化）")
        u = targets[0]
        snaps = []
        for i in range(args.watch):
            hits = {off: (x, y, z) for off, x, y, z in scan_inline(src, u.actor)}
            snaps.append(hits)
            print(f"  #{i} 候选 {len(hits)} 个，时刻 {time.strftime('%H:%M:%S')}")
            if i + 1 < args.watch:
                time.sleep(1.0)
        common = set(snaps[0])
        for s in snaps[1:]:
            common &= set(s)
        moved = [off for off in sorted(common) if len({sn[off] for sn in snaps}) > 1]
        print("  稳定存在且在变化的偏移:", [hex(o) for o in moved] or "（无 —— 多走两步再试）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
