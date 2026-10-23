# -*- coding: utf-8 -*-
"""分析 collect_hero_fields.py 的产物：定位 KDA / 累计经济字段。

思路：
  1) 每个 dump 是 10 槽 × 0x2AD8 原始字节；对每个 4 字节偏移取 10 槽向量。
  2) ground truth 来自战绩面板截图（人工读数后写进本文件 GT 表）；
     槽位 → 面板行用 +0x200 金币就近匹配。
  3) 三类候选：
     - economy：所有 GT 时刻 10 槽都 ≈ 面板经济（±TOL）
     - K/D/A ：所有 GT 时刻 10 槽都 == 面板 K/D/A
     - 累计金 ：全序列单调不减，且 ≥ 同槽 +0x200 金币
"""
import argparse
import json
import struct
import sys
from pathlib import Path

STRIDE = 0x2AD8
SLOTS = 10
GOLD_OFF = 0x200

# 由 gt_XX.png 人工读出：按面板行序（蓝上→下，红上→下）
# econ=面板"经济"列，kda=(击杀,死亡,助攻)，lvl=面板等级（用于槽位匹配）
GT = {
    4: {"econ": [786, 1192, 664, 750, 686, 784, 1080, 801, 1110, 1113],
        "lvl": [3, 4, 2, 2, 3, 3, 4, 2, 3, 3],
        "kda": [(0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 1, 0), (0, 0, 0),
                (0, 0, 0), (0, 0, 0), (0, 0, 0), (1, 0, 0), (0, 0, 1)]},
    12: {"econ": [1817, 2463, 1201, 1402, 1365, 1865, 1900, 1647, 2017, 1913],
         "lvl": [5, 7, 3, 4, 4, 5, 6, 4, 5, 5],
         "kda": [(1, 1, 1), (1, 0, 0), (0, 0, 0), (0, 2, 0), (0, 1, 2),
                 (0, 0, 2), (0, 1, 0), (1, 1, 0), (1, 0, 1), (2, 0, 1)]},
    20: {"econ": [2811, 3285, 1793, 2627, 2002, 2660, 2991, 2996, 2728, 2758],
         "lvl": [7, 9, 5, 7, 6, 7, 8, 7, 7, 6],
         "kda": [(1, 1, 1), (1, 0, 0), (0, 0, 0), (1, 2, 0), (0, 1, 2),
                 (0, 0, 2), (0, 1, 0), (1, 1, 0), (1, 1, 1), (2, 0, 1)]},
}


def load(outdir: Path):
    meta = json.loads((outdir / "meta.json").read_text(encoding="utf-8"))
    dumps = {}
    for d in meta["dumps"]:
        raw = (outdir / d["file"]).read_bytes()
        if len(raw) >= STRIDE * SLOTS:
            dumps[d["k"]] = raw
    return meta, dumps


def slot_vec(raw, off):
    return [struct.unpack_from("<i", raw, i * STRIDE + off)[0]
            for i in range(SLOTS)]


def gold_vec(raw):
    return slot_vec(raw, GOLD_OFF)


def match_gt_slots(raw, gt, tol=80):
    """槽位 -> 面板行。用 (金币+等级) 联合代价就近唯一匹配。"""
    gv = gold_vec(raw)
    lv = slot_vec(raw, 0x19C)
    econ, lvl = gt["econ"], gt["lvl"]
    used_r, pairs = set(), {}
    cand = sorted(((abs(g - e) + 3000 * abs(l - lvv), s, r)
                   for s, (g, l) in enumerate(zip(gv, lv))
                   for r, (e, lvv) in enumerate(zip(econ, lvl))))
    for d, s, r in cand:
        if s in pairs or r in used_r or d > tol:
            continue
        pairs[s] = r
        used_r.add(r)
    if len(pairs) < 8:
        return None
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="reverse/out/mem/structs5")
    ap.add_argument("--tol", type=int, default=80)
    a = ap.parse_args()
    outdir = Path(a.out)
    meta, dumps = load(outdir)
    if not GT:
        print("先在 GT 表里填入各 gt_XX.png 的经济/KDA 读数")
        return 1
    ks = sorted(dumps)
    print(f"dumps {ks[0]}..{ks[-1]} n={len(ks)}")

    # --- economy 候选 ---
    econ_hits = []
    kda_hits = {0: [], 1: [], 2: []}
    gt_pairs = {}
    for k, gt in GT.items():
        if k not in dumps:
            continue
        pairs = match_gt_slots(dumps[k], gt, a.tol)
        if not pairs:
            print(f"GT {k}: 槽位匹配失败")
            continue
        gt_pairs[k] = pairs
    for off in range(0, STRIDE - 4, 4):
        ok_e, ok_k = True, {0: True, 1: True, 2: True}
        for k, pairs in gt_pairs.items():
            v = slot_vec(dumps[k], off)
            for s, r in pairs.items():
                e, (kk, dd, aa) = GT[k]["econ"][r], GT[k]["kda"][r]
                if abs(v[s] - e) > a.tol:
                    ok_e = False
                if v[s] != kk:
                    ok_k[0] = False
                if v[s] != dd:
                    ok_k[1] = False
                if v[s] != aa:
                    ok_k[2] = False
        if ok_e:
            econ_hits.append(off)
        for i in (0, 1, 2):
            if ok_k[i]:
                kda_hits[i].append(off)

    print("economy 候选:", [hex(o) for o in econ_hits])
    for name, i in (("kills", 0), ("deaths", 1), ("assists", 2)):
        print(f"{name} 候选:", [hex(o) for o in kda_hits[i]][:20],
              f"(共{len(kda_hits[i])})")

    # --- 累计金候选：单调不减 + >= 金币 ---
    cum_hits = []
    for off in range(0, STRIDE - 4, 4):
        if off == GOLD_OFF:
            continue
        mono = 0
        ge = 0
        for s in range(SLOTS):
            seq = [slot_vec(dumps[k], off)[s] for k in ks]
            gv = [gold_vec(dumps[k])[s] for k in ks]
            if all(b >= c for c, b in zip(seq, seq[1:])):
                mono += 1
            if all(v >= g for v, g in zip(seq, gv)):
                ge += 1
        if mono >= 9 and ge >= 9:
            cum_hits.append(off)
    print("累计金候选(单调且>=金币):", [hex(o) for o in cum_hits][:40],
          f"(共{len(cum_hits)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
