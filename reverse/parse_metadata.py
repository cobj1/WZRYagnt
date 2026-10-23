# -*- coding: utf-8 -*-
"""解析 IL2CPP `global-metadata.dat`（v29）：拿到 C# 类型名 + 字段名 + 声明顺序。

为什么这件事比继续差分重要
--------------------------
本项目过去一直在做「差分猜偏移」：hp/hp_max/equip/skill/respawn 都是靠事件前后比对
定出来的（`reverse/probe_fields.py` 一族）。而 `global-metadata.dat` 里存着
**全部类名、字段名和声明顺序** —— 只要知道声明顺序，配合已有锚点（如 hp=0x1A4），
相邻字段的偏移就是推出来的，不需要再猜。

为什么不用 Il2CppDumper
-----------------------
`hok/dump.log` 显示它失败了：

    ERROR: This file may be protected.
    CodeRegistration : 0 / MetadataRegistration : 0
    ERROR: No symbol is detected

即 `libil2cpp.so` 被保护，找不到 registration 表。但 **metadata 本身没加密**
（`HeroBaseInfo` / `atk_range` / `HeroLogic` 都能 grep 到），所以直接按
metadata v29 的表结构解析即可，绕开 .so。

用法
----
    python reverse/parse_metadata.py --selftest
    python reverse/parse_metadata.py --list                        # 类名总览
    python reverse/parse_metadata.py --find hp_max                 # 按字段名找类
    python reverse/parse_metadata.py --class HeroLogic             # 某类的字段表
    python reverse/parse_metadata.py --search 英雄                 # 模糊搜类名
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
from pathlib import Path

# 逆向二进制原料所在目录（原 `reverse/` 已改名 `reverse-bin/`，避免与
# WZRYagnt/reverse/ 同名混淆）。可用环境变量 AIKINGS_ROOT 覆盖工作区根。
WORKSPACE = Path(os.environ.get("AIKINGS_ROOT", Path(__file__).resolve().parents[2]))
MD = WORKSPACE / "reverse-bin" / "hok" / "global-metadata.dat"
TD_SIZE = 88
FIELD_SIZE = 12


class Metadata:
    def __init__(self, path: Path = MD):
        self.data = path.read_bytes()
        h = struct.unpack_from("<64i", self.data, 0)
        self.sanity, self.version = h[0], h[1]
        self.str_off, self.str_size = h[6], h[7]
        self.fld_off, self.fld_size = h[24], h[25]
        self.td_off, self.td_size = h[40], h[41]
        self.td_count = self.td_size // TD_SIZE
        self.fld_count = self.fld_size // FIELD_SIZE
        self._cache: dict[int, str] = {}

    def cstr(self, idx: int) -> str:
        if idx in self._cache:
            return self._cache[idx]
        if idx < 0 or idx >= self.str_size:
            return ""
        start = self.str_off + idx
        end = self.data.find(b"\0", start, start + 256)
        s = self.data[start:end if end > 0 else start + 256].decode("utf-8", "replace")
        self._cache[idx] = s
        return s

    def td(self, i: int):
        """返回 (name, namespace, parentIndex, fieldStart, fieldCount)。"""
        o = self.td_off + i * TD_SIZE
        name_i, ns_i = struct.unpack_from("<2i", self.data, o)
        parent = struct.unpack_from("<i", self.data, o + 20)[0]
        field_start = struct.unpack_from("<i", self.data, o + 36)[0]
        field_count = struct.unpack_from("<H", self.data, o + 72)[0]
        return self.cstr(name_i), self.cstr(ns_i), parent, field_start, field_count

    def fields_of(self, i: int):
        name, _ns, _p, start, count = self.td(i)
        out = []
        for k in range(count):
            o = self.fld_off + (start + k) * FIELD_SIZE
            if o + FIELD_SIZE > len(self.data):
                break
            name_i = struct.unpack_from("<i", self.data, o)[0]
            out.append(self.cstr(name_i))
        return out


def selftest() -> int:
    md = Metadata()
    ok = True
    print(f"version={md.version} types={md.td_count} fields={md.fld_count}")
    # 第 0 个 int32 是 sanity，但按有符号读会变成负数，必须按位比较
    if (md.sanity & 0xFFFFFFFF) != 0xFAB11BAF:
        print(f"FAIL sanity=0x{md.sanity & 0xFFFFFFFF:x}")
        ok = False
    first, _ns, _p, _fs, _fc = md.td(0)
    print(f"第 0 个类型名 = {first!r}")
    if first != "<Module>":
        print("FAIL 首个类型应为 <Module>，说明表偏移算错了")
        ok = False
    # 至少能列出 1000 个非空类名
    names = [md.td(i)[0] for i in range(min(md.td_count, 5000))]
    nz = [n for n in names if n]
    if len(nz) < 1000:
        print(f"FAIL 类名解析出得太少 n={len(nz)}")
        ok = False
    print(f"前 5 个类型：{[n for n in nz[:5]]}")
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def find_by_field(md: Metadata, needle: str, limit: int = 20):
    hits = []
    for i in range(md.td_count):
        flds = md.fields_of(i)
        if needle in flds:
            hits.append((md.td(i)[0], len(flds), flds))
            if len(hits) >= limit:
                break
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", default=str(MD))
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--list", action="store_true", help="列出类名（前 60 个）")
    ap.add_argument("--find", default="", help="按字段名反查类")
    ap.add_argument("--class", dest="cls", default="", help="打印某类的字段表")
    ap.add_argument("--search", default="", help="模糊搜类名")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    md = Metadata(Path(a.md))
    print(f"metadata v{md.version}: {md.td_count} 类型 / {md.fld_count} 字段定义")

    if a.find:
        for name, n, flds in find_by_field(md, a.find):
            print(f"\n■ {name}  ({n} 字段)")
            print("   " + ", ".join(flds[:40]))
        return 0

    if a.cls:
        for i in range(md.td_count):
            name, ns, _p, _fs, _fc = md.td(i)
            if name == a.cls:
                flds = md.fields_of(i)
                print(f"\n■ {ns}.{name}  ({len(flds)} 字段，按声明顺序)")
                for k, f in enumerate(flds):
                    print(f"  [{k:>3}] {f}")
                return 0
        print(f"没找到类 {a.cls}")
        return 2

    if a.search:
        n = 0
        for i in range(md.td_count):
            name, ns, *_ = md.td(i)
            if a.search.lower() in name.lower() or a.search.lower() in ns.lower():
                print(f"  {ns}.{name}")
                n += 1
                if n >= 60:
                    break
        print(f"共 {n} 条")
        return 0

    for i in range(min(md.td_count, 60)):
        name, ns, _p, _fs, _fc = md.td(i)
        print(f"  [{i:>5}] {ns}.{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
