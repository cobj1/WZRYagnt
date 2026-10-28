# -*- coding: utf-8 -*-
"""C 组 16 条规则 · 用正确地址口径重判（可实施性审计）。

背景：2026-10-28 早些时候"C 组不可实现"的判断建立在 `disasm_one.py` 读偏 0x1000 的字节上，
已作废（见 `docs/反汇编地址口径勘误-2026-10-28.md`）。本工具对 C 组 16 条重新提取：

  · 动作码      —— 找 `mov w8, #imm` 后接 `str w8, [x?, #0x150]`（官方结果结构体的动作字段）
  · 关键阈值    —— `cmp/ccmp/fcmp` 的立即数（含 fmov 的浮点常量）
  · 目标函数    —— `adrp/ldr/blr` 的 GOT 槽位 → `.rela.dyn` addend → 目标函数
  · 引用串      —— 函数窗口内 `.rodata` 字符串（rule_* 名与判据线索）

输出一张 TSV，便于逐条判断"能否用当前 Snapshot 落地"。

用法：
  python reverse/audit_c_rules.py
  python reverse/audit_c_rules.py --out x.tsv --window 400
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

import capstone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from elf_va import sections, va_to_off                         # noqa: E402
from resolve_got import build_reloc_map, cstr, find_blr_sites   # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
SO = ROOT / "reverse" / "out" / "il2cpp" / "lib" / "arm64-v8a" / "libGameCore.so"
TRIAGE = ROOT / "docs" / "RULES_TRIAGE.md"
DEFAULT_OUT = ROOT / "reverse" / "out" / "diag" / "c_rules_audit.tsv"

C_GROUP = [2, 3, 7, 11, 22, 28, 32, 37, 38, 39, 41, 42, 44, 45, 46, 52]

ROW_RE = re.compile(
    r"^\|\s*(\d+)\s*\|\s*\[(\d+)\]\s*\|\s*(0x[0-9a-fA-F]+)\s*\|\s*(\d+)\s*\|\s*([^|]+)\|")
IMM_RE = re.compile(r"#(-?0x[0-9a-fA-F]+|-?\d+)")
LDR_RE = re.compile(r"^(\w+), \[(\w+), #(0x[0-9a-f]+)\]$")


def parse_triage() -> dict[int, dict]:
    out = {}
    for line in TRIAGE.read_text(encoding="utf-8").splitlines():
        m = ROW_RE.match(line.strip())
        if not m:
            continue
        out[int(m.group(1))] = {
            "slot": m.group(2), "addr": int(m.group(3), 16),
            "n_ins": int(m.group(4)), "name": m.group(5).strip(),
        }
    return out


def scan(md, data: bytes, func: int, window: int, rodata: dict, rel: dict) -> dict:
    off = va_to_off(data, func)
    if off < 0:
        return {}
    insns = list(md.disasm(data[off:off + window * 4], func))

    pages: dict[str, int] = {}
    action_code = None
    action_site = None
    consts: list[str] = []
    strings: list[str] = []
    last_mov_w8 = None
    last_mov_i = -99
    for i, ins in enumerate(insns):
        m, ops = ins.mnemonic, ins.op_str
        if m == "adrp":
            reg, imm = ops.split(", ")
            pages[reg] = int(imm.lstrip("#"), 16)
        elif m == "add":
            parts = [p.strip() for p in ops.split(",")]
            if len(parts) == 3 and parts[1] in pages and parts[2].startswith("#"):
                s = cstr(data, pages[parts[1]] + int(parts[2][1:], 0), rodata)
                if s and s not in strings:
                    strings.append(s)
        elif m == "mov" and ops.startswith("w8, #"):
            last_mov_w8 = int(ops.split("#")[1], 0)
            last_mov_i = i
        elif m == "str" and "#0x150]" in ops and action_code is None:
            # 官方结果结构体的动作码字段。要求 `mov w8,#imm` 紧邻（≤4 条），
            # 否则会把上文无关的常量误当动作码（#46 首版就踩过）。
            if last_mov_w8 is not None and i - last_mov_i <= 4:
                action_code = last_mov_w8
                action_site = ins.address
        elif m in ("cmp", "ccmp", "fcmp"):
            for v in IMM_RE.findall(ops):
                tok = v.lstrip("#")
                if tok not in consts:
                    consts.append(tok)
        elif m == "fmov" and ops.startswith("s"):
            for v in IMM_RE.findall(ops):
                consts.append("f" + v.lstrip("#"))

    got = []
    for site in find_blr_sites(md, data, func, window):
        slot = site.get("slot")
        if slot is None:
            continue
        tgt = rel.get(slot)
        got.append(f"0x{slot:x}->0x{tgt:x}" if tgt else f"0x{slot:x}->?")

    # 内部状态字段依赖：`ldr w?, [x0|self, #off]` / `[x2|result, #off]`
    fields: list[str] = []
    for ins in insns:
        if ins.mnemonic not in ("ldr", "ldrb", "ldrh", "ldrsw"):
            continue
        mm = LDR_RE.match(ins.op_str)
        if not mm:
            continue
        base, disp = mm.group(2), mm.group(3)
        if base in ("x0", "x1", "x2", "x8", "x9", "x11", "x21", "x23"):
            tok = f"{base}+{disp}"
            if tok not in fields:
                fields.append(tok)

    return {
        "action_code": action_code, "action_site": action_site,
        "consts": consts, "strings": strings, "got": got,
        "fields": fields,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    ap.add_argument("--window", type=int, default=400)
    args = ap.parse_args()

    data = SO.read_bytes()
    rodata = sections(data)[".rodata"]
    rel = build_reloc_map(data)
    md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
    md.skipdata = True

    triage = parse_triage()
    rows = []
    for no in C_GROUP:
        info = triage.get(no)
        if not info:
            print(f"[warn] #{no} 不在表内")
            continue
        r = scan(md, data, info["addr"], args.window, rodata, rel)
        rule_names = [s for s in r.get("strings", []) if s.startswith("rule_")]
        rows.append({
            "no": no, "addr": info["addr"], "name": info["name"],
            "n_ins": info["n_ins"],
            "action_code": r.get("action_code"),
            "rule_name": rule_names[0] if rule_names else "",
            "consts": ",".join(r.get("consts", [])[:10]),
            "got": " ".join(r.get("got", [])[:6]),
            "n_fields": len(r.get("fields", [])),
            "fields": " ".join(r.get("fields", [])[:12]),
            "strings": " | ".join(r.get("strings", [])[:5]),
        })

    hdr = ("no", "addr", "name", "n_ins", "action_code", "rule_name",
           "consts", "got", "n_fields", "fields", "strings")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="\n") as f:
        f.write("\t".join(hdr) + "\n")
        for r in rows:
            f.write("\t".join(
                (f"0x{r[k]:x}" if k == "addr" else str(r[k])) for k in hdr) + "\n")

    print(f"{'#':>3} {'addr':>10} {'act':>4} {'n':>4} {'fld':>4}  name / rule_name")
    for r in rows:
        ac = r["action_code"]
        print(f"{r['no']:>3} 0x{r['addr']:08x} {('—' if ac is None else ac):>4} "
              f"{r['n_ins']:>4} {r['n_fields']:>4}  {r['name'][:30]}"
              f"{(' / ' + r['rule_name']) if r['rule_name'] else ''}")
    n_ac = sum(1 for r in rows if r["action_code"] is not None)
    print(f"\n[汇总] {len(rows)} 条中 {n_ac} 条写出动作码（result+0x150）")
    print(f"[out] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
