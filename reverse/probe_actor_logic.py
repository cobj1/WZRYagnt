#!/usr/bin/env python
"""从 actor 反向找"逻辑单元记录"（0xD0）与坐标。

思路（对局中执行，目标是最少的 adb 往返）：
  1. 取当前单位；挑非英雄（kind == unit）里血量/阵营正常的几个；
  2. **先看 attr 组件对象体内**有没有坐标三元组（1 次读/单位）——有些引擎把
     transform 和 attr 塞在同一个组件里；
  3. 再看 actor 自身 0x600 体内有没有坐标三元组（1 次读/单位）；
  4. 都不行就扫 actor 里的指针：把候选指针按**页去重**批量读（read_pages 一次往返），
     再在本地判断 `p` 处是不是一条记录（id/camp/type/坐标都合法，且 id == 该 unit 的 cid）；
  5. 命中的记录地址再按 ±0xD0 向两侧扩展，验证是否能连成数组，从而给出 begin/end。

用法：
    python reverse/probe_actor_logic.py
    python reverse/probe_actor_logic.py --kind unit --limit 3
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wzry.vision.mem_source import MemSource  # noqa: E402

STRIDE = 0xD0
CAMP_ID = {"blue": 1, "red": 2}


def triples(buf: bytes, lo: int, hi: int):
    """在 buf[lo:hi] 里找"像世界坐标"的 i32 三元组（x, y, z）。"""
    out = []
    for off in range(lo, min(hi, len(buf) - 12) - 12, 4):
        x, y, z = struct.unpack_from("<3i", buf, off)
        if not (-90000 <= x <= 90000 and -90000 <= z <= 90000):
            continue
        if not (-3000 <= y <= 3000):
            continue
        if abs(x) < 2000 or abs(z) < 2000:
            continue
        out.append((off, x, y, z))
    return out


def rec_at(buf: bytes, off: int, cid: int | None = None):
    """判断 buf[off:] 是否是 0xD0 记录；返回字段 dict 或 None。"""
    if off < 0 or len(buf) < off + 0x20:
        return None
    rid = struct.unpack_from("<i", buf, off)[0]
    camp = struct.unpack_from("<i", buf, off + 8)[0]
    typ = struct.unpack_from("<i", buf, off + 0x0C)[0]
    x, y, z = struct.unpack_from("<3i", buf, off + 0x14)
    if not (0 < rid < 10_000_000) or camp not in (1, 2) or not (0 < typ < 60):
        return None
    if not (-90000 <= x <= 90000 and -90000 <= z <= 90000):
        return None
    if not (-3000 <= y <= 3000):
        return None
    if x == 0 and z == 0:
        return None
    if cid is not None and rid != cid:
        return None
    return {"id": rid, "camp": camp, "type": typ, "x": x, "y": y, "z": z}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="127.0.0.1:16384")
    ap.add_argument("--kind", default="unit")
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--span", type=lambda s: int(s, 0), default=0x600)
    a = ap.parse_args()

    src = MemSource(serial=a.serial)
    print("pid =", src.connect())
    m = src.m
    units = src.snapshot()
    from collections import Counter
    print("单位:", len(units), Counter(u.kind for u in units))

    targets = [u for u in units if u.kind == a.kind][: a.limit]
    if not targets:
        print(f"没有 kind={a.kind} 的单位")
        return 1

    for u in targets:
        print(f"\n=== {u.kind} cid={u.config_id} camp={u.camp} actor=0x{u.actor:x} "
              f"hp={u.hp}/{u.hp_max} pos=({u.x},{u.z})")
        raw = m.read(u.actor, a.span)

        # 2) attr 组件体内
        attr = struct.unpack_from("<Q", raw, 0x1D0)[0]
        if attr:
            ab = m.read(attr, 0x200)
            hits = triples(ab, 0, len(ab))
            print(f"  attr=0x{attr:x} 体内坐标三元组:", [(hex(o), x, y, z) for o, x, y, z in hits[:4]] or "无")

        # 3) actor 自身
        hits = triples(raw, 0, len(raw))
        print("  actor 体内坐标三元组:", [(hex(o), x, y, z) for o, x, y, z in hits[:4]] or "无")

        # 4) 指针 -> 批量读页 -> 找记录
        pages, ptrs = set(), []
        for off in range(0, len(raw) - 8, 8):
            p = struct.unpack_from("<Q", raw, off)[0]
            if not (0x100000 < p < 0x8000_0000_0000):
                continue
            ptrs.append((off, p))
            pages.add(p & ~0xFFF)
        print(f"  候选指针 {len(ptrs)} 个，去重页 {len(pages)} 页")
        if len(pages) <= 400:
            blobs = m.read_pages(sorted(pages))
            cid = int(u.config_id or 0)
            camp_id = CAMP_ID.get(u.camp, 0)
            found = []
            for off, p in ptrs:
                page = p & ~0xFFF
                buf = blobs.get(page) or b""
                if not buf:
                    continue
                r = rec_at(buf, p - page, cid)
                if r and r["camp"] == camp_id:
                    found.append((off, p, r))
            if found:
                print("  ★ 命中记录（actor 内槽位, 记录地址, 字段）：")
                for off, p, r in found[:5]:
                    print(f"    actor+0x{off:03x} -> 0x{p:x}  {r}")
            else:
                print("  指针所指对象里没有 id 匹配的记录")
        else:
            print("  页数太多，跳过指针扫描")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
