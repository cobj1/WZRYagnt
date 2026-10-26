# -*- coding: utf-8 -*-
"""内存偏移注册表单测（MEM-01）。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision import mem_offsets  # noqa: E402
from wzry.vision.mem_source import CONSTANT_OFFSETS  # noqa: E402

PASS = []


def check(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print(f"PASS {name}")
    else:
        raise AssertionError(f"FAIL {name} {extra}")


def main() -> int:
    reg = mem_offsets.load()
    check("注册表可加载", reg["schema"] == 1)
    check("版本非空", bool(reg["version"]))
    check("有证据链", len(reg["evidence"]) >= 1)
    check("代码与注册表一致", mem_offsets.drift(CONSTANT_OFFSETS, reg) == {})

    bad = dict(reg)
    bad["offsets"] = dict(reg["offsets"], attr_hp=reg["offsets"]["attr_hp"] + 4)
    d = mem_offsets.drift(CONSTANT_OFFSETS, bad)
    check("漂移可检出", d.get("attr_hp") is not None, f"{d}")

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "bad.json"
        p.write_text(json.dumps({"version": "x", "offsets": {}}), encoding="utf-8")
        try:
            mem_offsets.load(p)
            check("缺字段必须报错", False)
        except mem_offsets.OffsetRegistryError:
            check("缺字段必须报错", True)

        p.write_text(json.dumps({
            "version": "x", "game": "g", "module": "m", "validated_at": "d",
            "evidence": ["e"],
            "offsets": {k: "0xZZ" for k in mem_offsets.REQUIRED},
        }), encoding="utf-8")
        try:
            mem_offsets.load(p)
            check("非法十六进制必须报错", False)
        except ValueError:
            check("非法十六进制必须报错", True)

    if len(PASS) != 7:
        raise AssertionError(f"expected 7 checks, got {len(PASS)}")
    print(f"\nALL PASS ({len(PASS)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
