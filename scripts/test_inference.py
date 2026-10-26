# -*- coding: utf-8 -*-
"""推理部署清单自检单测（MODEL-02）。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.decision.inference import (  # noqa: E402
    ManifestError, inspect, load_manifest,
)

PASS = []


def check(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print(f"PASS {name}")
    else:
        raise AssertionError(f"FAIL {name} {extra}")


def main() -> int:
    m = load_manifest()
    check("真实清单可加载", m["primary"] == "laya")
    check("primary_backend 合法", m["primary_backend"] in m["backends"])
    check("预算字段存在", "loop_p95" in m["budgets_ms"])

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        bad = d / "bad.json"
        bad.write_text(json.dumps({"primary": "x"}), encoding="utf-8")
        try:
            load_manifest(bad)
            check("缺字段必须报错", False)
        except ManifestError:
            check("缺字段必须报错", True)

        model = d / "m.onnx"
        model.write_bytes(b"fake")
        good = d / "good.json"
        good.write_text(json.dumps({
            "primary": "laya", "primary_backend": "onnx", "fallback": "rule",
            "budgets_ms": {"loop_p95": 200.0},
            "backends": {
                "onnx": {"model": "m.onnx", "module": "onnxruntime"},
                "gone": {"model": "nope.bin", "module": "nothing"},
            },
        }), encoding="utf-8")
        rep = inspect(load_manifest(good), d, load=False)
        check("主后端 ready", rep["primary_ready"])
        check("文件存在", rep["backends"]["onnx"]["exists"])
        check("缺失文件标记", rep["backends"]["gone"]["status"] == "missing")
        check("sha256 已计算", len(rep["backends"]["onnx"]["sha256"]) == 64)

    if len(PASS) != 8:
        raise AssertionError(f"expected 8 checks, got {len(PASS)}")
    print(f"\nALL PASS ({len(PASS)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
