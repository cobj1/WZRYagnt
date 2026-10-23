# -*- coding: utf-8 -*-
"""验证「逻辑单元记录 +0x04 = runtime_id」这个假设。

背景
----
`logic_units.py` 解析 0xD0 步长的战斗逻辑单元数组，已确认的字段是：

    +0x00 id(config)   +0x08 camp   +0x0C type   +0x14/0x18/0x1C x/y/z

`+0x04` 一直没被使用。静态证据 `0x0246c42c ldr w3,[x28]` 说明 +0x00 是
**config id**（日志 `solder configid:%d`），那么紧邻的 +0x04 最可能是
**runtime id**（引擎日志 `[GetLastHitTarget] runtime_id:%d` 用的就是这种唯一号）。

如果成立，收益很大：它给"目标字段"提供了锚点（见 docs §12.6 的失败原因），
也能让"谁在打我"归因到具体单位（不只英雄）。

判据（三条同时满足才算成立）
--------------------------
1. **唯一**：所有记录的 +0x04 两两不同（config id 会重复，runtime id 不会）；
2. **非零且值域合理**：0 < v < 10^7；
3. **与 id 不同源**：+0x04 的重复度必须显著低于 +0x00（否则只是 id 的副本）。

用法
----
    venv/Scripts/python.exe -X utf8 reverse/verify_runtime_id.py --selftest
    venv/Scripts/python.exe -X utf8 reverse/verify_runtime_id.py          # 需对局
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def judge(id_vals, cand_vals):
    """对候选列给出 (是否成立, 说明列表)。纯函数，便于自检。"""
    notes = []
    n = len(cand_vals)
    if n < 8:
        return False, [f"样本太少 n={n}"]

    nz = [v for v in cand_vals if v]
    if len(nz) < n * 0.95:
        notes.append(f"非零率仅 {len(nz) / n:.0%}")
    uniq_c = len(set(cand_vals))
    uniq_i = len(set(id_vals))
    notes.append(f"唯一值：候选 {uniq_c}/{n}，config id {uniq_i}/{n}")

    cond_unique = uniq_c == n
    cond_range = all(0 < v < 10_000_000 for v in cand_vals)
    cond_distinct = uniq_c > uniq_i + max(2, n * 0.1)

    ok = cond_unique and cond_range and cond_distinct
    notes.append(f"唯一={cond_unique} 值域={cond_range} 与id不同源={cond_distinct}")
    return ok, notes


def selftest() -> int:
    ok = True
    # 正例：id 有重复、候选唯一
    ids = [1001, 1001, 1001, 1002, 1002, 1003, 1003, 1003, 1004, 1004]
    cand = [20001 + i for i in range(10)]
    good, _ = judge(ids, cand)
    if not good:
        print("FAIL 正例未通过")
        ok = False
    # 反例1：候选有重复（其实是 id 副本）
    bad1, _ = judge(ids, ids)
    if bad1:
        print("FAIL 反例（重复列）误判为 runtime_id")
        ok = False
    # 反例2：候选全 0
    bad2, _ = judge(ids, [0] * 10)
    if bad2:
        print("FAIL 反例（全 0）误判")
        ok = False
    # 反例3：值域过大（浮点位模式）
    bad3, _ = judge(ids, [900_000_000 + i for i in range(10)])
    if bad3:
        print("FAIL 反例（值域过大）误判")
        ok = False
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def live() -> int:
    """直接读原始字节，不依赖 logic_units.read() 的字段集（它没解析 +0x04）。"""
    import struct

    from wzry.vision.logic_units import STRIDE, LogicUnits, _valid_record
    from wzry.vision.mem_source import MemSource

    src = MemSource()
    src.connect()
    hs = src.read_hero_states()
    if not hs:
        print("读不到英雄（不在对局中）")
        return 1

    u = LogicUnits(src.m)
    if not u.discover():
        print(f"没找到 0xD0 逻辑单元数组：{u.reason}")
        return 2
    raw = src.m.read(u.begin, u.count * STRIDE)
    recs = []
    for i in range(u.count):
        b = i * STRIDE
        if len(raw) < b + 0x20:
            break
        if not _valid_record(raw, b):
            continue
        recs.append(struct.unpack_from("<4i", raw, b))     # id, +0x04, camp, type

    print(f"数组 @0x{u.begin:x}  {len(recs)} 条有效记录")
    if not recs:
        return 3
    ids = [r[0] for r in recs]
    cand = [r[1] for r in recs]
    ok, notes = judge(ids, cand)
    print(f"\n+0x04 候选 runtime_id：{'✅ 成立' if ok else '❌ 不成立'}")
    for t in notes:
        print(f"   {t}")
    print("\n前 10 条（id, +0x04, camp, type）：")
    for r in recs[:10]:
        print(f"   {r[0]:>7} {r[1]:>9} {r[2]:>3} {r[3]:>3}")
    if ok:
        print("\n结论：+0x04 可作为 runtime_id 锚点 —— 下一步用它给「目标字段」定性"
              "（docs §12.6 缺的就是这个锚点）。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    return selftest() if a.selftest else live()


if __name__ == "__main__":
    raise SystemExit(main())
