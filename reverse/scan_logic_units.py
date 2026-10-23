#!/usr/bin/env python
"""定向堆扫描：找 0xD0 步长的逻辑单元记录数组。

为什么不扫全进程：对局中可读内存约 9GB，30MB/s 要 5 分钟。
这里只扫**可写段**、按大小降序、带时间预算，命中即停；
签名用 numpy 向量化（camp + type==11 + 两条记录相隔 0xD0 + 坐标合法）。

命中后会：
  1. 用真值（建筑/野怪的出生点 == 实时位置）验证；
  2. 沿 ±0xD0 向两侧扩展，给出数组 begin/end 与条数；
  3. 打印前若干条记录。

用法：
    python reverse/scan_logic_units.py                # 预算 240s
    python reverse/scan_logic_units.py --budget 600
    python reverse/scan_logic_units.py --contains 44700  # 只扫含该坐标（快速定位）
"""
from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from wzry.vision.mem_source import MemSource  # noqa: E402

STRIDE = 0xD0
CHUNK = 8 * 1024 * 1024


def regions(m, pid, want_w: bool):
    raw = m._run(["exec-out", f"cat /proc/{pid}/maps"]).decode("utf-8", "replace")
    out = []
    for line in raw.splitlines():
        p = line.split(None, 5)
        if len(p) < 5:
            continue
        a, b = p[0].split("-")
        a, b = int(a, 16), int(b, 16)
        perm = p[1]
        if "r" not in perm:
            continue
        if want_w and "w" not in perm:
            continue
        out.append((b - a, a, b, perm))
    out.sort(reverse=True)
    return out


def scan_chunk(buf: bytes, base: int):
    """返回候选记录基址（绝对地址）列表。"""
    if len(buf) < STRIDE * 2 + 16:
        return []
    n4 = len(buf) // 4
    b = np.frombuffer(buf[: n4 * 4], dtype=np.uint32)
    bi = b.view(np.int32)
    k = b.size - 56
    if k <= 0:
        return []
    camp1 = b[2: 2 + k]
    type1 = b[3: 3 + k]
    camp2 = b[54: 54 + k]
    type2 = b[55: 55 + k]
    x = bi[5: 5 + k]
    z = bi[7: 7 + k]
    rid = bi[0:k]
    cond = (((camp1 == 1) | (camp1 == 2)) & (type1 == 11)
            & ((camp2 == 1) | (camp2 == 2)) & (type2 == 11)
            & (rid > 0) & (rid < 10_000_000)
            & (x > -90000) & (x < 90000) & (z > -90000) & (z < 90000)
            & ~((x == 0) & (z == 0)))
    idx = np.nonzero(cond)[0]
    return [base + int(i) * 4 for i in idx]


def read_rec(m, addr: int):
    buf = m.read(addr, STRIDE)
    if len(buf) < 0x20:
        return None
    rid = struct.unpack_from("<i", buf, 0)[0]
    camp = struct.unpack_from("<i", buf, 8)[0]
    typ = struct.unpack_from("<i", buf, 0x0C)[0]
    x, y, z = struct.unpack_from("<3i", buf, 0x14)
    return {"addr": addr, "id": rid, "camp": camp, "type": typ, "x": x, "y": y, "z": z}


def expand(m, addr: int):
    """从一条记录向两侧按 0xD0 扩展，返回 (begin, end, n)。"""
    def ok(a):
        r = read_rec(m, a)
        return r is not None and 0 < r["type"] < 60 and r["camp"] in (1, 2)

    lo = addr
    while ok(lo - STRIDE) and addr - lo < STRIDE * 400:
        lo -= STRIDE
    hi = addr + STRIDE
    while ok(hi) and hi - addr < STRIDE * 400:
        hi += STRIDE
    return lo, hi, (hi - lo) // STRIDE


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="127.0.0.1:16384")
    ap.add_argument("--budget", type=int, default=240, help="扫描时间预算（秒）")
    ap.add_argument("--contains", type=lambda s: int(s, 0), default=None,
                    help="只扫包含该整数的 16 字节对齐位置（快速定位用）")
    a = ap.parse_args()

    src = MemSource(serial=a.serial)
    pid = src.connect()
    print("pid =", pid)
    m = src.m
    regs = regions(m, pid, want_w=True)
    total = sum(r[0] for r in regs) / 1e9
    print(f"可写段 {len(regs)} 个，合计 {total:.2f} GB")

    t0 = time.time()
    cands = []
    scanned = 0
    for size, lo, hi, perm in regs:
        if time.time() - t0 > a.budget:
            print("预算到，停止扫描")
            break
        addr = lo
        while addr < hi:
            if time.time() - t0 > a.budget:
                break
            n = min(CHUNK, hi - addr)
            buf = m.read(addr, n)
            if not buf:
                addr += n
                continue
            scanned += len(buf)
            if a.contains is not None:
                pat = struct.pack("<i", a.contains)
                i = buf.find(pat)
                while i >= 0:
                    cands.append(addr + i)
                    i = buf.find(pat, i + 4)
            else:
                cands.extend(scan_chunk(buf, addr))
            addr += n
        if cands:
            break

    dt = time.time() - t0
    print(f"扫描 {scanned/1e9:.2f} GB，用时 {dt:.1f}s，候选 {len(cands)} 个")

    # 用真值核对：建筑/野怪的 +0x278 即实时位置
    units = src.snapshot()
    truth = {}
    for u in units:
        if u.config_id in (92, 95, 21, 35, 44, 46, 16100, 16101):
            raw = m.read(u.actor, 0x2A0)
            x, y, z = struct.unpack_from("<3i", raw, 0x278)
            truth[u.config_id] = (x, z)

    shown = 0
    for c in cands[:400]:
        rec = read_rec(m, c)
        if not rec:
            continue
        lo, hi, n = expand(m, c)
        if n < 3:
            continue
        hit = ""
        for cid, (tx, tz) in truth.items():
            if abs(rec["x"] - tx) <= 2 and abs(rec["z"] - tz) <= 2:
                hit = f"  ★ 与 cid={cid} 出生点一致"
        if truth and not hit and shown > 0:
            continue
        shown += 1
        print(f"\n候选 0x{c:x}  begin=0x{lo:x} n={n}{hit}")
        print(f"  {rec}")
        if shown >= 5:
            break

    if not cands:
        print("没找到。可试：--budget 600 或 --contains <某建筑出生点坐标>")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
