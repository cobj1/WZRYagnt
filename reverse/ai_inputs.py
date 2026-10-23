# -*- coding: utf-8 -*-
"""把 AI 代码区的间接调用解析成「调了哪个访问器」，并按数据类别汇总。

输入（由 ai_scan.py 产出）：
  gc.icalls.tsv   调用点 / 归属函数 / 槽位
  gc.strrefs.tsv  指令 / 归属函数 / 字符串
  gc.slots.tsv    槽位加载点

槽位 -> 实现地址：读 .rela.dyn 的 R_AARCH64_RELATIVE.r_addend。
实现地址 -> 名字：用该函数自身引用到的字符串（C++ 断言/日志里常带函数全名）。

用法：python ai_inputs.py <elf> <prefix> [region_spec]
region_spec 形如 "0x27d0000-0x27e6000,0x2550000-0x2570000"
"""
import re
import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path

R_AARCH64_RELATIVE = 1027
RE_IDENT = re.compile(r"^[A-Za-z_~][\w:<>~ .]{3,70}$")


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


def score_name(s: str) -> int:
    if not RE_IDENT.match(s):
        return -1
    sc = 0
    if "::" in s:
        sc += 6
    if re.match(r"^(Get|Set|Is|Has|Can|Calc|Update|Process|Init|Check|Find|Query)", s):
        sc += 4
    if re.search(r"[a-z][A-Z]", s):
        sc += 2
    if s.islower() or s.isupper():
        sc -= 2
    if " " in s and "::" not in s:
        sc -= 3
    return sc


def main() -> int:
    elf = Path(sys.argv[1])
    pref = Path(sys.argv[2])
    regions = []
    if len(sys.argv) > 3:
        for part in sys.argv[3].split(","):
            a, b = part.split("-")
            regions.append((int(a, 0), int(b, 0)))

    d = elf.read_bytes()
    secs = load_sections(d)
    text = next(s for s in secs if s["sname"] == ".text")
    t0, t1 = text["addr"], text["addr"] + text["size"]
    rela = next(s for s in secs if s["sname"] == ".rela.dyn")

    rel = {}
    for k in range(rela["size"] // 24):
        o = rela["off"] + k * 24
        r_off, r_info, r_add = struct.unpack_from("<QQq", d, o)
        if (r_info & 0xFFFFFFFF) == R_AARCH64_RELATIVE:
            rel[r_off] = r_add

    # func -> best string
    best = {}
    allstr = defaultdict(list)
    with open(f"{pref}.strrefs.tsv", encoding="utf-8") as f:
        for line in f:
            a, fn, s = line.rstrip("\n").split("\t", 2)
            fn = int(fn, 16)
            allstr[fn].append(s)
            sc = score_name(s)
            if sc > best.get(fn, (-1, ""))[0]:
                best[fn] = (sc, s)
    names = {fn: v[1] for fn, v in best.items() if v[0] > 0}

    def nm(addr):
        return names.get(addr) or (allstr.get(addr) or ["?"])[0]

    def in_reg(a):
        return any(lo <= a < hi for lo, hi in regions)

    agg = Counter()
    per_func = defaultdict(Counter)
    with open(f"{pref}.icalls.tsv", encoding="utf-8") as f:
        for line in f:
            a, fn, slot = line.rstrip("\n").split("\t")
            a, fn, slot = int(a, 16), int(fn, 16), int(slot, 16)
            if regions and not in_reg(fn):
                continue
            tgt = rel.get(slot)
            if not tgt or not (t0 <= tgt < t1):
                key = f"slot:0x{slot:x}"
            else:
                key = f"0x{tgt:x}\t{nm(tgt)}"
            agg[key] += 1
            per_func[fn][key] += 1

    print(f"AI 区函数数: {len(per_func)}  唯一被调 API: {len(agg)}  调用点: {sum(agg.values())}")
    print("\n=== 被调用最多的 60 个 API ===")
    for k, c in agg.most_common(60):
        print(f"{c:6d}  {k}")

    out = Path(f"{pref}.ai_inputs.tsv")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("calls\ttarget\ttarget_name\n")
        for k, c in agg.most_common():
            parts = k.split("\t")
            f.write(f"{c}\t{parts[0]}\t{parts[1] if len(parts) > 1 else ''}\n")
    print(f"\n-> {out}")

    out2 = Path(f"{pref}.ai_per_func.tsv")
    with open(out2, "w", encoding="utf-8", newline="\n") as f:
        for fn, c in sorted(per_func.items()):
            f.write(f"0x{fn:08x}\t{nm(fn)}\t" + "|".join(f"{v}x{k}" for k, v in c.most_common(12)) + "\n")
    print(f"-> {out2}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
