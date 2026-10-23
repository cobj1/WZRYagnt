# -*- coding: utf-8 -*-
"""定位 runtime_id 的宿主偏移。

判据：某个 4 字节偏移上，**全部在场 actor 的值两两不同且非零**。
runtime_id 是引擎给每个实例发的唯一号（`[GetLastHitTarget] runtime_id:%d` 就是这么来的），
它必须满足「唯一 + 非零 + 同对象稳定」。

用途：给「目标字段」提供锚点。没有这个锚点，判据只能退化成"值像不像 id"，
实测会产生上百个假阳性（见 docs/wzry-ai-data-inputs.md §12.6）。

结论（2026-10-21 实测）：actor 头 0x800 内**没有**满足条件的列，
只有 cid(0x28)/type(0x30)/camp(0x34) 和 0x194 起的一片浮点变换数据
（`0x3871E218` 这类 float 位模式碰巧两两不同）。所以这个脚本目前的作用是
**证伪**，而不是给出偏移 —— 若要继续，应把 --depth 加到 0x2000 再试。

用法：
    venv/Scripts/python.exe -X utf8 reverse/find_runtime_id.py --depth 0x800
"""
import argparse
import importlib.util
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision.mem_source import MemSource  # noqa: E402


def _load_probe():
    spec = importlib.util.spec_from_file_location(
        "probe_target", ROOT / "reverse" / "probe_target.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--depth", type=lambda s: int(s, 0), default=0x800)
    ap.add_argument("--min-actors", type=int, default=20)
    a = ap.parse_args()

    pt = _load_probe()
    s = MemSource()
    s.connect()
    _mgr, _arr, _cnt, actors = s._actor_table()
    actors = [x for x in actors if x]
    plan = [(x, a.depth, i) for i, x in enumerate(actors)]
    got = pt._read_plan(s.m, plan, 0x8000)
    print(f"actor {len(actors)} 个，读到 {len(got)} 个，扫描深度 0x{a.depth:x}")

    rows = []
    for off in range(0, a.depth - 4, 4):
        vals = []
        for i, buf in got.items():
            if off + 4 > len(buf):
                continue
            vals.append(int.from_bytes(buf[off:off + 4], "little"))
        if len(vals) < a.min_actors:
            continue
        if sum(1 for v in vals if v) < len(vals) * 0.9:
            continue
        if len(set(vals)) != len(vals):
            continue
        rows.append((off, min(vals), max(vals), len(vals)))

    print(f"\n「全 actor 互不相同且非零」的偏移 {len(rows)} 个：\n")
    for off, lo, hi, n in rows:
        print(f" +0x{off:03x}  n={n}  范围 {lo}..{hi}")
    if not rows:
        print("（无：该深度内没有 runtime_id 量级的唯一列）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
