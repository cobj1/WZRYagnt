#!/usr/bin/env python
"""定位"战斗逻辑单元数组"（0xD0 步长），拿兵的坐标。

## 为什么需要它

`MemSource` 读坐标走的是 **actor 对象的 pos 组件**（`actor+0x230`）：英雄 10/10 有效，
兵/野/塔这一格是 0（没有位置组件）。于是 `MemUnit.x` 一直是 None：
official AI 永远枚举不出"打兵 / 打野 / 推塔"，只能追英雄。

## 静态侧已经还原出的结构（证据见下）

兵线可见性判定函数 `0x0246c1bc` 遍历一个 **0xD0 字节步长**的记录数组：

```
0x0246c1e0  ldp  x28, x29, [x1, #0xd0]      ; container+0xD0 = begin, +0xD8 = end
0x0246c278  add  x28, x28, #0xd0            ; 步长 0xD0
0x0246c284  ldr  w8, [x28, #0xc]
0x0246c288  cmp  w8, #0xb                   ; +0x0C == 11 -> 小兵
0x0246c290  ldr  w8, [x20, #0x24]           ; 自己阵营
0x0246c294  ldr  w3, [x28, #8]              ; +0x08 = 阵营
0x0246c298  sub  w9, w22, w8                ; 3 - 自己阵营 = 敌方
0x0246c29c  cmp  w3, w9
0x0246c2dc  ldr  x1, [x25, #0x14]!          ; +0x14 取 8 字节（x, y）
0x0246c2e0  ldr  w2, [x25, #8]              ; x25 = rec+0x14 -> +0x1C（z）
0x0246c2ac  ldr  x9, [x28, #0x70]           ; 可见性位图大小
0x0246c2c0  ldr  x10, [x28, #0x68]          ; 可见性位图指针
0x0246c430  ldr  w4, [x28, #0x14]           ; 日志 "location:(%d,%d)" 的 x
0x0246c42c  ldr  w3, [x28]                  ; 日志 "solder configid:%d" 的 id
0x0246c430  ldr  w5, [x28, #0x1c]           ; 日志里的 z
```

记录字段：

| 偏移 | 含义 |
| --- | --- |
| +0x00 | id（日志里的 `configid`） |
| +0x08 | 阵营（1 / 2；`3 - 自己阵营` 得到敌方） |
| +0x0C | 单位类型（**11 = 小兵**） |
| +0x14 | x（世界坐标，i32） |
| +0x18 | y（高度，i32） |
| +0x1C | z（世界坐标，i32） |
| +0x68 | 可见性位图指针（bit per 阵营） |
| +0x70 | 位图 bit 数 |

日志串互证：`solder configid:%d location:(%d,%d) not visible`（0x1165430）、
`solder configid:%d location:(%d,%d) not in enmey area`（0x11d8522）。

## 用法

不依赖指针链：在 `libGameCore` 的可写段里扫所有 8 字节值，当成候选 begin 指针，
用**记录内容**自校验（步长整除 + 类型/阵营/坐标都在合法范围），命中的就是数组。

    python reverse/find_logic_units.py                 # 对局中：扫 .data/.bss
    python reverse/find_logic_units.py --dump 0x...    # 已知 begin 时直接 dump
    python reverse/find_logic_units.py --scan-heap 256 # 再扫 256MB 堆（更慢）

对局外（大厅/结算）数组为空，扫不到属正常。
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "reverse"))

from livemem import LiveMem  # noqa: E402

SO = "libGameCore.so"
STRIDE = 0xD0
TYPE_SOLDIER = 11
MAX_UNITS = 800


def _rec_ok(buf: bytes, base: int = 0) -> bool:
    """单条记录是否像"战斗逻辑单元"。"""
    if len(buf) < base + 0x20:
        return False
    mid = struct.unpack_from("<i", buf, base + 0x00)[0]
    camp = struct.unpack_from("<i", buf, base + 0x08)[0]
    typ = struct.unpack_from("<i", buf, base + 0x0C)[0]
    x, y, z = struct.unpack_from("<3i", buf, base + 0x14)
    if typ < 1 or typ > 60:
        return False
    if camp not in (1, 2):
        return False
    if not (0 < mid < 10_000_000):
        return False
    if not (-90000 <= x <= 90000 and -90000 <= z <= 90000):
        return False
    if not (-3000 <= y <= 3000):
        return False
    if x == 0 and z == 0:
        return False
    return True


def probe_array(m: LiveMem, begin: int, end: int, sample: int = 3) -> dict:
    """校验 [begin, end) 是否是 0xD0 步长的记录数组。"""
    d = end - begin
    if begin <= 0x1000 or end <= begin:
        return {"ok": False, "why": "bad-range"}
    if d % STRIDE:
        return {"ok": False, "why": f"stride-rem-{d % STRIDE}"}
    n = d // STRIDE
    # 单条记录一律当噪声：真实数组里至少有一个英雄 + 若干兵
    if n < 2 or n > MAX_UNITS:
        return {"ok": False, "why": f"count-{n}"}
    want = min(n, sample + 2)
    raw = m.read(begin, want * STRIDE)
    if len(raw) < want * STRIDE:
        return {"ok": False, "why": "read-short"}
    # 抽样必须**全部**通过（上一版写成 `oks < want - 1`，n=1 时恒真，出了一堆假阳性）
    oks = sum(1 for i in range(want) if _rec_ok(raw, i * STRIDE))
    if oks < want:
        return {"ok": False, "why": f"rec-bad-{oks}/{want}"}
    return {"ok": True, "n": n, "raw": raw, "oks": oks}


def dump_array(m: LiveMem, begin: int, n: int):
    raw = m.read(begin, n * STRIDE)
    print(f"begin=0x{begin:x} n={n}")
    for i in range(n):
        b = i * STRIDE
        if len(raw) < b + 0x20:
            break
        mid, camp, typ = struct.unpack_from("<3i", raw, b + 0x00)
        camp = struct.unpack_from("<i", raw, b + 0x08)[0]
        typ = struct.unpack_from("<i", raw, b + 0x0C)[0]
        x, y, z = struct.unpack_from("<3i", raw, b + 0x14)
        bits_ptr = struct.unpack_from("<Q", raw, b + 0x68)[0]
        bits_n = struct.unpack_from("<i", raw, b + 0x70)[0]
        print(f"  [{i:>3}] id={mid:<8} camp={camp} type={typ:<3} "
              f"xyz=({x},{y},{z}) vis=0x{bits_ptr:x}/{bits_n}")


def writable_segments(m: LiveMem, so_name: str) -> list:
    """取 .so 的可写段（maps 里按权限过滤；LiveMem.maps 不存权限，这里重读一次）。"""
    raw = m._run(["exec-out", f"cat /proc/{m.pid}/maps"]).decode("utf-8", "replace")
    out = []
    for line in raw.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6 or not parts[5].endswith("/" + so_name):
            continue
        if "w" not in parts[1]:
            continue
        a, b = parts[0].split("-")
        out.append((int(a, 16), int(b, 16)))
    return sorted(out)


def find_in(m: LiveMem, lo: int, hi: int, tag: str, limit: int = 6) -> list:
    """在 [lo,hi) 里找"begin/end 指针对"。

    关键：**先用算术筛，再读内存**。相邻两个 u64 的差必须能被 0xD0 整除、
    且在合理条数内 —— 这一步纯算术，不产生任何 adb 往返；
    否则几十万个指针逐个读 16 字节，一次 adb 30ms 会跑到天亮（上一版就踩了这个坑）。
    """
    hits = []
    step = 8 * 1024 * 1024
    addr = lo
    while addr < hi:
        size = min(step, hi - addr)
        raw = m.read(addr, size)
        if not raw:
            addr += size
            continue
        n = len(raw) // 8
        cand = []
        for i in range(n - 1):
            a, b = struct.unpack_from("<2Q", raw, i * 8)
            d = b - a
            if a <= 0x100000 or d <= 0:
                continue
            if d % STRIDE or d // STRIDE > MAX_UNITS:
                continue
            cand.append((addr + i * 8, a, b))
        for holder, a, b in cand:
            try:
                r = probe_array(m, a, b)
            except Exception:
                continue
            if r["ok"]:
                hits.append({"holder": holder, "begin": a, "end": b,
                             "n": r["n"], "where": tag})
                if len(hits) >= limit:
                    return hits
        addr += size
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="127.0.0.1:16384")
    ap.add_argument("--dump", type=lambda s: int(s, 0), default=0,
                    help="已知数组 begin，直接 dump")
    ap.add_argument("--n", type=int, default=40, help="--dump 时打印的记录数")
    ap.add_argument("--scan-heap", type=int, default=0, metavar="MB",
                    help="额外扫描最大的 N MB 匿名堆（默认只扫 .so 可写段）")
    args = ap.parse_args()

    m = LiveMem(serial=args.serial)
    print("pid =", m.find_pid())
    m.refresh_maps()

    if args.dump:
        dump_array(m, args.dump, args.n)
        return 0

    spans = writable_segments(m, SO)
    print(f"libGameCore 段数 {len(spans)}，合计 "
          f"{sum(e - s for s, e in spans) / 1e6:.1f} MB")

    hits = []
    for s, e in spans:
        hits += find_in(m, s, e, "so")
        if hits:
            break
    if not hits and args.scan_heap:
        # 只扫最大的几个匿名段（堆），限制总量
        anon = sorted([(s, e) for (s, e, _o, p) in m.maps if not p],
                      key=lambda x: x[1] - x[0], reverse=True)
        budget = args.scan_heap * 1024 * 1024
        for s, e in anon:
            if budget <= 0:
                break
            take = min(e - s, budget)
            hits += find_in(m, s, s + take, "heap")
            budget -= take
            if hits:
                break

    if not hits:
        print("\n没找到 —— 通常是不在对局里（数组为空），或这段不在 .so 可写段。")
        print("对局中再跑；仍找不到就加 --scan-heap 256。")
        return 1

    print(f"\n命中 {len(hits)} 个候选：")
    for h in hits:
        print(f"  holder=0x{h['holder']:x} begin=0x{h['begin']:x} n={h['n']} ({h['where']})")
    best = max(hits, key=lambda h: h["n"])
    print()
    dump_array(m, best["begin"], min(best["n"], 12))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
