# -*- coding: utf-8 -*-
"""事件差分探针：定位"装备栏 / buff 标记"这类**没有直接可读标志**的字段。

思路与 `probe_ep_cast.py` 相同（那次定位到了法力 `+0x1C4/+0x1C8`）：
静态扫不出来，就制造一次**事件**，然后比对事件前后的内存。

用法（两步，中间由人做一次局内操作）：

    # 1) 事件前存基线（只读）
    venv/Scripts/python.exe -X utf8 reverse/probe_fields.py --event equip --save before

    # 2) 在局内买 1 件装备（或在训练营里买）

    # 3) 事件后比对（自动再读一次，不需要先 save）
    venv/Scripts/python.exe -X utf8 reverse/probe_fields.py --event equip --diff before

buff 同理：
    venv/Scripts/python.exe -X utf8 reverse/probe_fields.py --event buff  --save base
    # 去吃一个红 buff，等 3 秒
    venv/Scripts/python.exe -X utf8 reverse/probe_fields.py --event buff  --diff base

输出约定：
  - `equip` 模式按 **u16** 扫描 `0x204..0x28D4`（装备 ID 是 u16），只报"由 0 变非 0"或"新增稳定 ID"；
  - `buff`  模式按 u32/u16 双口径，只报 **0↔非0 翻转** 与"小幅整数变化"（层数/剩余时间）；
  - 都会顺带报 `i32` 的大幅变化，方便排除金币/血量干扰项。

判据（写进 docs 的验收标准）：
  - 装备：买第 1 件后出现一个新 ID；再买第 2 件时它在**固定偏移**再出现一次；
  - buff：吃红后某字段变非 0，buff 到期后**精确回到原值**。
"""
from __future__ import annotations

import argparse
import base64
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wzry.vision.mem_source import (ACTOR_ATTR_OFF, HERO_ARR_STRIDE,  # noqa: E402
                                    SELF_OBJ_END, MemSource)

CACHE = ROOT / "temp" / "probe_fields"

# 装备 ID 段（王者荣耀装备 config_id 数量级；u16 足够）
EQUIP_BAND = (0x204, 0x28D4)
EQUIP_ID_MIN, EQUIP_ID_MAX = 1000, 4000


def _regions(src, base, slot_idx, actor) -> dict:
    """读三类区域：英雄记录（含装备/buff 段）、actor 对象、attr 组件。"""
    out = {
        "record": src.m.read(base + slot_idx * HERO_ARR_STRIDE, HERO_ARR_STRIDE),
    }
    if actor:
        out["actor"] = src.m.read(actor, 0x400)
        try:
            attr = struct.unpack("<Q", src.m.read(actor + ACTOR_ATTR_OFF, 8))[0]
            if 0x1000000000 < attr < 0x7FFFFFFFFFFF:
                out["attr"] = src.m.read(attr, 0x400)
        except Exception:
            pass
    return out


def _pack(regions: dict) -> str:
    return base64.b64encode(json.dumps(
        {k: base64.b64encode(v).decode() for k, v in regions.items()}).encode()).decode()


def _unpack(blob: str) -> dict:
    raw = json.loads(base64.b64decode(blob).decode())
    return {k: base64.b64decode(v) for k, v in raw.items()}


def _u16_changes(a: dict, b: dict, lo: int, hi: int):
    """u16 口径的差异，限定在 [lo,hi) 偏移带内。"""
    out = []
    for k in a:
        if k not in b:
            continue
        x, y = a[k], b[k]
        n = min(len(x), len(y))
        for off in range(lo & ~1, min(hi, n) - 2, 2):
            v0 = struct.unpack_from("<H", x, off)[0]
            v1 = struct.unpack_from("<H", y, off)[0]
            if v0 != v1:
                out.append((k, off, v0, v1))
    return out


def _i32_changes(a: dict, b: dict, lo: int = 0, hi: int = 0x400):
    out = []
    for k in a:
        if k not in b:
            continue
        x, y = a[k], b[k]
        n = min(len(x), len(y), hi)
        for off in range(lo, n - 4, 4):
            v0 = struct.unpack_from("<i", x, off)[0]
            v1 = struct.unpack_from("<i", y, off)[0]
            if v0 != v1:
                out.append((k, off, v0, v1))
    return out


def _resolve(src: MemSource, hero_id: int):
    hs = src.read_hero_states()
    if not hs:
        return None
    me = next((h for h in hs if h["id"] == hero_id), None)
    if me is None:
        return None
    actor = next((u.actor for u in src.snapshot()
                  if u.is_hero and u.config_id == hero_id), None)
    return src._hero_arrays[0], me["slot"], actor


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", choices=["equip", "buff"], required=True)
    ap.add_argument("--save", metavar="NAME", default="")
    ap.add_argument("--diff", metavar="BASE", default="")
    ap.add_argument("--id", type=int, default=0, help="本地英雄 cid（0=数组 slot0）")
    ap.add_argument("--serial", default="127.0.0.1:16384")
    a = ap.parse_args()

    src = MemSource(serial=a.serial)
    src.connect()
    hs = src.read_hero_states()
    if not hs:
        print("未读到英雄数组：可能不在对局中")
        return 1
    hero_id = a.id or next(h for h in hs if h["slot"] == 0)["id"]
    got = _resolve(src, hero_id)
    if got is None:
        print(f"英雄 {hero_id} 不在本局")
        return 1
    base, slot_idx, actor = got
    regions = _regions(src, base, slot_idx, actor)
    print(f"id={hero_id} slot={slot_idx} actor=0x{(actor or 0):x} "
          f"record={len(regions.get('record', b''))} attr={len(regions.get('attr', b''))}")

    CACHE.mkdir(parents=True, exist_ok=True)
    if a.save:
        p = CACHE / f"{a.save}.json"
        p.write_text(_pack(regions), encoding="utf-8")
        nxt = "买 1 件装备后" if a.event == "equip" else "去吃红 buff、等 3 秒后"
        print(f"已存基线 -> {p}")
        print(f"下一步：{nxt}，再运行 --event {a.event} --diff {a.save}")
        return 0

    if not a.diff:
        print("需要 --save 或 --diff")
        return 2
    p = CACHE / f"{a.diff}.json"
    if not p.exists():
        print(f"基线不存在: {p}")
        return 2
    pre = _unpack(p.read_text(encoding="utf-8"))

    print("\n=== 装备段 u16 变化（0x204..0x28D4）===")
    eq = _u16_changes(pre, regions, *EQUIP_BAND)
    hits = [(k, o, v0, v1) for k, o, v0, v1 in eq
            if (v0 == 0) != (v1 == 0)
            or (EQUIP_ID_MIN <= v1 <= EQUIP_ID_MAX and not (EQUIP_ID_MIN <= v0 <= EQUIP_ID_MAX))]
    for k, off, v0, v1 in (hits or eq)[:30]:
        print(f"   {k} +0x{off:04x}  u16 {v0} -> {v1}"
              + ("   ← 新增装备 ID 候选" if EQUIP_ID_MIN <= v1 <= EQUIP_ID_MAX else ""))
    if not eq:
        print("   （无变化：装备可能没买成功，或装备栏不在该段）")

    print("\n=== buff 关注：0↔非0 翻转 / 小幅整数 ===")
    flips = []
    for k, off, v0, v1 in _i32_changes(pre, regions):
        if (v0 == 0) != (v1 == 0):
            flips.append((k, off, v0, v1, "flip"))
        elif 0 < abs(v1 - v0) <= 64 and 0 <= v1 <= 8:
            flips.append((k, off, v0, v1, "small"))
    for k, off, v0, v1, tag in flips[:30]:
        print(f"   {k} +0x{off:04x}  i32 {v0} -> {v1}  ({tag})")
    if not flips:
        print("   （无 0↔非0 翻转）")

    print("\n=== i32 大幅变化（排除干扰：金币/血量/CD）===")
    for k, off, v0, v1 in _i32_changes(pre, regions)[:20]:
        print(f"   {k} +0x{off:04x}  {v0} -> {v1}")
    print("\n提示：把上面命中的偏移记进 docs/AI_UPGRADE_ALIGN_OFFICIAL.md §10.2 / §10.3，"
          "再在 mem_source 里加常量并写校验阈值。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
