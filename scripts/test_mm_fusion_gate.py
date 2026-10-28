# -*- coding: utf-8 -*-
"""小地图 `detect_minimap` 融合块门控守卫。

`docs/小地图实现对比-2026-10-26.md` §2 的结论：默认 ONNX 后端（8 通用类）没有 mm_* 类，
`YoloDetector.detect_minimap` 在该后端下**无条件 return []**，m2 的融合块永不生效。
2026-10-28 已按后端门控（跳过 ONNX、保留 `.pt` 11 类回退），离线 A/B 见
`scripts/bench_mm_fusion_ab.py`（1344 帧：非空 0、dots 不一致 0）。

三条断言：
  T1 ONNX 后端下 detect_minimap 恒空（用 _hok 占位，不加载模型）
  T2 m2 源码存在后端门控，且门控出现在 detect_minimap 调用之前
  T3 门控只对 ONNX 生效（`.pt` 路径仍会调用）——防止有人把整块删掉

运行：venv/Scripts/python.exe -X utf8 scripts/test_mm_fusion_gate.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_PASS, _FAIL = [], []


def check(name, cond, extra=""):
    if cond:
        _PASS.append(name)
        print(f"PASS {name}")
    else:
        _FAIL.append(name)
        print(f"FAIL {name} {extra}")


def main() -> int:
    from wzry.vision.detector import YoloDetector

    # T1：ONNX 后端（_hok 非空）必须恒空
    d = object.__new__(YoloDetector)          # 跳过 __init__，不加载模型
    d._hok = object()                          # 只要有值就走 ONNX 分支
    d.backend = "hok_yolov5_onnx"
    d.last_mm_ms = 0.0
    got = d.detect_minimap(object())           # 参数不会被使用
    check("T1_onnx_backend_returns_empty", got == [], f"got={got!r}")

    src = (ROOT / "scripts" / "m2_agent_v2.py").read_text(encoding="utf-8")
    i_gate = src.find('_mm_backend == "hok_yolov5_onnx"')
    i_call = src.find("det.detect_minimap(_mm_crop)")
    check("T2_m2_has_backend_gate", i_gate > 0, "未找到 _mm_backend 门控")
    check("T2b_gate_precedes_call", 0 < i_gate < i_call,
          f"gate@{i_gate} call@{i_call} —— 门控必须在调用之前")

    # T3：.pt 回退仍保留（调用点还在，且不依赖 ONNX 分支）
    check("T3_pt_fallback_preserved", i_call > 0,
          "det.detect_minimap(_mm_crop) 调用点消失 —— .pt 11 类后端回退能力被破坏")

    print(f"\n结果: {len(_PASS)} PASS / {len(_FAIL)} FAIL")
    if _FAIL:
        print("失败: " + ", ".join(_FAIL))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
