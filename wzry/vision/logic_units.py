# -*- coding: utf-8 -*-
"""战斗逻辑单元数组（0xD0 步长）—— 兵 / 野 / 塔的世界坐标来源。

## 问题

`MemSource` 读世界坐标走的是 **actor 对象的 pos 组件**（`actor+0x230`）：
英雄 10/10 有效，但兵 / 野怪 / 建筑这一格是 `0`（没有位置组件）。
于是 `MemUnit.x` 恒为 None → official AI 永远枚举不出"打兵 / 打野 / 推塔"候选。

## 静态还原出来的结构（证据）

兵线可见性判定 `0x0246c1bc` 遍历一个 **0xD0 字节步长**的数组：

```
0x0246c1e0  ldp  x28, x29, [x1, #0xd0]   ; 容器 +0xD0/+0xD8 = begin/end
0x0246c278  add  x28, x28, #0xd0         ; 步长 0xD0
0x0246c284  ldr  w8, [x28, #0xc] ; cmp w8, #0xb     ; +0x0C == 11 → 小兵
0x0246c294  ldr  w3, [x28, #8]                      ; +0x08 = 阵营
0x0246c298  sub  w9, w22, w8                        ; 3 - 自己阵营 = 敌方
0x0246c2dc  ldr  x1, [x25, #0x14]!                  ; +0x14 起 8 字节（x, y）
0x0246c2e0  ldr  w2, [x25, #8]                      ; +0x1C（z）
0x0246c42c  ldr  w3, [x28]                          ; "solder configid:%d"
0x0246c430  ldr  w4, [x28, #0x14] / w5, #0x1c       ; "location:(%d,%d)"
```

| 偏移 | 含义 |
| --- | --- |
| +0x00 | id（config id） |
| +0x08 | 阵营 1/2 |
| +0x0C | 类型（**11 = 小兵**） |
| +0x14 | x（世界坐标 i32） |
| +0x18 | y（高度 i32） |
| +0x1C | z（世界坐标 i32） |
| +0x68 | 可见性位图指针 |
| +0x70 | 位图 bit 数 |

## 取数组的方式（不依赖指针链）

在 `libGameCore.so` 的可写段里找**相邻两个 u64** 组成的 `(begin, end)`：
差值必须被 0xD0 整除、条数在 2..800，且抽样记录全部通过字段校验。
找到后按 `(id, camp)` 索引，给缺坐标的单位补位置。

## 安全阀（关键）

不盲信结构：发现数组后**必须**用英雄坐标交叉验证 —— 英雄的精确坐标我们另有来源
（`mem_source.read_hero_states`），若数组里同 id 的记录与之偏差 > 500（世界单位）
的匹配数 < 3，就判定"这不是我们要的数组"，直接放弃，不往决策里灌任何数据。
"""
from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

STRIDE = 0xD0
OFF_ID = 0x00
OFF_CAMP = 0x08
OFF_TYPE = 0x0C
OFF_X = 0x14
OFF_Y = 0x18
OFF_Z = 0x1C
OFF_VIS_PTR = 0x68
OFF_VIS_N = 0x70

TYPE_SOLDIER = 11
MAX_UNITS = 800
VERIFY_TOL = 500.0        # 英雄坐标交叉验证容差（世界单位）
VERIFY_MIN_HITS = 3       # 至少几只英雄对得上才认可
MAX_XZ = 90000


def _valid_record(buf: bytes, base: int) -> bool:
    if len(buf) < base + 0x20:
        return False
    rid = struct.unpack_from("<i", buf, base + OFF_ID)[0]
    camp = struct.unpack_from("<i", buf, base + OFF_CAMP)[0]
    typ = struct.unpack_from("<i", buf, base + OFF_TYPE)[0]
    x, y, z = struct.unpack_from("<3i", buf, base + OFF_X)
    if not (0 < rid < 10_000_000):
        return False
    if camp not in (1, 2):
        return False
    if not (0 < typ < 60):
        return False
    if not (-MAX_XZ <= x <= MAX_XZ and -MAX_XZ <= z <= MAX_XZ):
        return False
    if not (-3000 <= y <= 3000):
        return False
    return not (x == 0 and z == 0)


class LogicUnits:
    """一次性发现 + 每帧读取的 0xD0 记录数组。"""

    def __init__(self, mem, so: str = "libGameCore.so"):
        self.m = mem
        self.so = so
        self.begin: Optional[int] = None
        self.count = 0
        self.reason = "未发现"
        self.ok = False

    # ---------- 发现 ----------
    def _writable(self) -> List[Tuple[int, int]]:
        raw = self.m._run(["exec-out", f"cat /proc/{self.m.pid}/maps"]).decode(
            "utf-8", "replace")
        out = []
        for line in raw.splitlines():
            p = line.split(None, 5)
            if len(p) < 6 or not p[5].endswith("/" + self.so) or "w" not in p[1]:
                continue
            a, b = p[0].split("-")
            out.append((int(a, 16), int(b, 16)))
        return sorted(out)

    def _check(self, begin: int, end: int) -> Optional[int]:
        d = end - begin
        if begin <= 0x100000 or d <= 0 or d % STRIDE:
            return None
        n = d // STRIDE
        if n < 2 or n > MAX_UNITS:
            return None
        want = min(n, 5)
        raw = self.m.read(begin, want * STRIDE)
        if len(raw) < want * STRIDE:
            return None
        if not all(_valid_record(raw, i * STRIDE) for i in range(want)):
            return None
        return n

    def discover(self) -> bool:
        """扫 .so 可写段找 (begin,end)。纯算术预筛，只有少量候选会真读内存。"""
        try:
            spans = self._writable()
        except Exception as e:
            self.reason = f"读 maps 失败: {e}"
            return False
        for lo, hi in spans:
            addr = lo
            while addr < hi:
                size = min(8 * 1024 * 1024, hi - addr)
                raw = self.m.read(addr, size)
                if not raw:
                    addr += size
                    continue
                n = len(raw) // 8
                for i in range(n - 1):
                    a, b = struct.unpack_from("<2Q", raw, i * 8)
                    d = b - a
                    if a <= 0x100000 or d <= 0 or d % STRIDE:
                        continue
                    cnt = self._check(a, b)
                    if cnt:
                        self.begin, self.count = a, cnt
                        self.reason = f"found@0x{a:x} n={cnt}"
                        return True
                addr += size
        self.reason = "可写段内没有符合步长/字段的指针对"
        return False

    # ---------- 读取 ----------
    def read(self) -> List[dict]:
        if self.begin is None:
            return []
        raw = self.m.read(self.begin, self.count * STRIDE)
        out: List[dict] = []
        for i in range(self.count):
            b = i * STRIDE
            if len(raw) < b + 0x20:
                break
            if not _valid_record(raw, b):
                continue
            rid = struct.unpack_from("<i", raw, b + OFF_ID)[0]
            camp = struct.unpack_from("<i", raw, b + OFF_CAMP)[0]
            typ = struct.unpack_from("<i", raw, b + OFF_TYPE)[0]
            x, y, z = struct.unpack_from("<3i", raw, b + OFF_X)
            out.append({"id": rid, "camp": camp, "type": typ,
                        "x": x, "y": y, "z": z})
        return out

    # ---------- 交叉验证 ----------
    def verify(self, heroes: List[dict]) -> bool:
        """用英雄精确坐标验证数组身份。英雄来自 `read_hero_states()`（含 id/x/z）。"""
        recs = self.read()
        if not recs or not heroes:
            self.reason = "空数组或没有英雄可比对"
            return False
        by_id: Dict[int, List[dict]] = {}
        for r in recs:
            by_id.setdefault(r["id"], []).append(r)
        hits, best = 0, []
        for h in heroes:
            hid, hx, hz = h.get("id"), h.get("x"), h.get("z")
            if hid is None or hx is None or hz is None:
                continue
            cand = by_id.get(int(hid))
            if not cand:
                continue
            d = min(((c["x"] - hx) ** 2 + (c["z"] - hz) ** 2) ** 0.5 for c in cand)
            best.append((int(hid), round(d)))
            if d <= VERIFY_TOL:
                hits += 1
        self.ok = hits >= VERIFY_MIN_HITS
        self.reason = (f"英雄交叉验证 {hits} 命中 / 样本 {len(best)}"
                       f"（容差 {VERIFY_TOL:.0f}）样本={best[:6]}")
        return self.ok

    def index(self) -> Dict[Tuple[int, int], List[dict]]:
        """(id, camp) -> 记录列表（同 id 多个时按出现顺序取）。"""
        out: Dict[Tuple[int, int], List[dict]] = {}
        for r in self.read():
            out.setdefault((r["id"], r["camp"]), []).append(r)
        return out


def open_if_possible(mem, heroes: List[dict]) -> Optional[LogicUnits]:
    """发现 + 验证，成功返回可用的 LogicUnits；失败返回 None（调用方保持原行为）。"""
    lu = LogicUnits(mem)
    if not lu.discover():
        return None
    if not lu.verify(heroes):
        return None
    return lu
