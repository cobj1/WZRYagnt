# -*- coding: utf-8 -*-
"""定位「行为树节点名 -> 创建函数」注册表。

思路：节点名是 .rodata 里的 C 串；注册表通常是 .data.rel.ro 里的一组
{name_ptr, creator_ptr} 或两张平行数组。.data.rel.ro 在文件里全 0，
真实值由 R_AARCH64_RELATIVE 的 r_addend 提供。

步骤：
  1) 收集 .rodata 中形如 XxxNode / XxxNodeRuntime 的串地址；
  2) 遍历 .rela.dyn，把 r_addend 落在上述集合的槽位记为「名字槽」；
  3) 按槽位连续性分组，打印每组的起始/长度/步长，并检查相邻槽位
     （+8 / -8 / 组内交错）是否指向 .text（= 创建函数）。

用法：python find_node_registry.py <elf>
"""
import struct
import sys
from pathlib import Path

R_AARCH64_RELATIVE = 1027


def load_sections(d: bytes):
    e_shoff, = struct.unpack_from("<Q", d, 0x28)
    es, = struct.unpack_from("<H", d, 0x3A)
    num, = struct.unpack_from("<H", d, 0x3C)
    shstr, = struct.unpack_from("<H", d, 0x3E)
    secs = []
    for i in range(num):
        o = e_shoff + i * es
        v = struct.unpack_from("<IIQQQQIIQQ", d, o)
        secs.append(dict(name=v[0], type=v[1], addr=v[3], off=v[4], size=v[5]))
    base = secs[shstr]["off"]
    for s in secs:
        e = d.find(b"\0", base + s["name"])
        s["sname"] = d[base + s["name"]:e].decode("ascii", "replace")
    return secs


def main() -> int:
    elf = Path(sys.argv[1])
    d = elf.read_bytes()
    secs = load_sections(d)
    text = next(s for s in secs if s["sname"] == ".text")
    t0, t1 = text["addr"], text["addr"] + text["size"]
    ro = next(s for s in secs if s["sname"] == ".rodata")

    # 1) 节点名串地址
    names = {}
    blob = d[ro["off"]:ro["off"] + ro["size"]]
    i, n = 0, len(blob)
    while i < n:
        j = blob.find(b"\0", i)
        if j < 0:
            break
        seg = blob[i:j]
        if 6 <= len(seg) <= 96 and seg.endswith(b"Node") and seg.isascii():
            try:
                s = seg.decode()
            except UnicodeDecodeError:
                s = ""
            if s and s[0].isupper() and s.replace("_", "").isalnum():
                names[ro["addr"] + i] = s
        i = j + 1
    print(f"node-name strings in .rodata: {len(names)}")

    # 2) 全量重定位
    rela = next(s for s in secs if s["sname"] == ".rela.dyn")
    rel = {}
    cnt = rela["size"] // 24
    for k in range(cnt):
        o = rela["off"] + k * 24
        r_off, r_info, r_add = struct.unpack_from("<QQq", d, o)
        if (r_info & 0xFFFFFFFF) == R_AARCH64_RELATIVE:
            rel[r_off] = r_add
    print(f".rela.dyn entries: {cnt}, RELATIVE: {len(rel)}")

    # 3) 名字槽
    slots = {k: v for k, v in rel.items() if v in names}
    print(f"name slots: {len(slots)}")

    # 4) 连续性分组（步长 8）
    keys = sorted(slots)
    groups, cur = [], [keys[0]] if keys else []
    for a, b in zip(keys, keys[1:]):
        if b - a == 8:
            cur.append(b)
        else:
            groups.append(cur)
            cur = [b]
    if cur:
        groups.append(cur)
    groups.sort(key=len, reverse=True)
    print(f"contiguous groups: {len(groups)}; top sizes: {[len(g) for g in groups[:8]]}")

    for g in groups[:6]:
        s, e = g[0], g[-1]
        step_in_text = sum(1 for k in range(s, e + 1, 8) if rel.get(k) and t0 <= rel[k] < t1)
        print(f"\n== group 0x{s:x}..0x{e:x}  slots={len(g)}  "
              f"其中指向 .text 的槽={step_in_text}")
        for k in g[:6]:
            tgt = rel.get(k)
            tag = "TEXT" if tgt and t0 <= tgt < t1 else ("name" if tgt in names else "-")
            print(f"   slot 0x{k:x} -> 0x{(tgt or 0):x} {tag}"
                  + (f'  "{names[tgt]}"' if tgt in names else ""))

    # 5) 检查名字槽的「相邻槽」是否是创建函数（stride 16 结构体）
    pairs = 0
    for k in keys:
        nb = rel.get(k + 8)
        if nb and t0 <= nb < t1:
            pairs += 1
    print(f"\nname slot 的 +8 邻居指向 .text 的数量: {pairs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
