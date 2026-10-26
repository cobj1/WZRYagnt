# -*- coding: utf-8 -*-
"""m2 装配点回归：确认主循环的"装配"已从 main() 抽出且行为不变。

背景（C3 装配收敛）：`main()` 原先 1847 行，把参数解析、采集源、执行通道、
内存直读、诊断、决策引擎、存档收尾全塞在一个函数里。抽成 6 个装配点后，
本测试锁住三件事：
  1. 装配点存在且可直接调用（不需要设备）；
  2. `main()` 里不再出现构造语句（防止回填）；
  3. `main()` 行数不再增长（防反弹；当前 1512，阈值 1600）。

运行：
    python -X utf8 scripts/test_m2_assembly.py
"""
from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print(f"PASS {name}")
    else:
        FAIL.append(name)
        print(f"FAIL {name} {extra}")


def load_m2():
    spec = importlib.util.spec_from_file_location(
        "m2_agent_v2", ROOT / "scripts" / "m2_agent_v2.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    m2 = load_m2()
    src = inspect.getsource(m2.main)

    # ---- 1. 装配点存在 ----
    for name in ("build_arg_parser", "build_capture", "build_executor",
                 "start_mem_worker", "start_diagnostics",
                 "build_decision_engine", "finalize_recorder",
                 "start_auto_learn"):
        check(f"A_{name}_exists", callable(getattr(m2, name, None)),
              "缺少装配点")

    # ---- 2. main 里不再构造（防回填） ----
    check("B_main_no_argparse", "argparse.ArgumentParser" not in src)
    check("B_main_no_capture_ctor",
          "DesktopCapturePrint(" not in src and "ScreencapCapture(" not in src)
    check("B_main_no_executor_ctor",
          "ActionExecutor(" not in src and "Esp32HidExecutor(" not in src)
    check("B_main_no_recorder_finalize_body",
          "coverage_gate()" not in src)
    check("B_main_calls_assembly_points",
          all(f"{n}(" in src for n in ("build_arg_parser", "build_capture",
                                       "build_executor", "finalize_recorder")))

    # ---- 3. 规模守卫 ----
    n_main = len(src.splitlines())
    check("C_main_under_1600", n_main <= 1600, f"main() {n_main} 行")

    # ---- 4. 参数装配：101 个选项，--result 可选值正确 ----
    ap = m2.build_arg_parser()
    check("D_parser_is_argparse", isinstance(ap, argparse.ArgumentParser))
    opts = [a for a in ap._actions if a.option_strings]
    check("D_option_count", len(opts) >= 60, f"{len(opts)} 个选项")
    args = ap.parse_args(["--result", "win"])
    check("D_result_arg", args.result == "win", f"{args.result}")
    check("D_result_rejects_bad",
          _parse_fails(ap, ["--result", "maybe"]), "--result 未校验非法值")

    # ---- 5. 存档装配：finalize 写胜负 + 闸门 + 关盘 ----
    from wzry.data.collector import MatchRecorder
    with tempfile.TemporaryDirectory() as td:
        rec = MatchRecorder(base_dir=td, input_source="test")
        rec.start(session_name="asm")
        rec.on_state({"clock_s": 1.0, "scoreboard": [{"cid": 1}],
                      "ui": {"hp": 1.0},
                      "units": [{"cls": "enemy_hero",
                                 "pos_minimap": [0.5, 0.5]}]})
        ns = argparse.Namespace(result="loss")
        m2.finalize_recorder(rec, ns, reason="test")
        man = json.loads((Path(td) / "asm" / "manifest.json")
                         .read_text(encoding="utf-8"))
        check("E_finalize_writes_result", man.get("result") == "loss",
              f"{man.get('result')}")
        check("E_finalize_closed", not rec.active)
        # 幂等：再调一次不应抛
        m2.finalize_recorder(rec, ns, reason="again")
        check("E_finalize_idempotent", True)

    # ---- 6. `--result auto`：结算页识别 -> 落盘（端到端接线） ----
    import numpy as np
    from wzry.vision.match_result import BANNER

    def _banner_frame(bgr, size=(720, 1280)):
        h, w = size
        img = np.full((h, w, 3), 18, dtype=np.uint8)
        x0, y0, x1, y1 = BANNER
        img[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)] = bgr
        return img

    with tempfile.TemporaryDirectory() as td:
        rec_a = MatchRecorder(base_dir=td, input_source="test")
        rec_a.start(session_name="auto_loss")
        rec_a.on_state({"clock_s": 9.0, "scoreboard": [{"cid": 1}],
                        "ui": {"hp": 1.0},
                        "units": [{"cls": "enemy_hero", "pos_minimap": [0.5, 0.5]}]})
        m2.finalize_recorder(rec_a, argparse.Namespace(result="auto"),
                             reason="auto", frame=_banner_frame((40, 30, 120)))
        man_a = json.loads((Path(td) / "auto_loss" / "manifest.json")
                           .read_text(encoding="utf-8"))
        check("F_auto_result_writes_loss", man_a.get("result") == "loss",
              f"{man_a.get('result')}")
        check("F_auto_result_source",
              (man_a.get("meta") or {}).get("result", {}).get("source") == "auto")

        # 非结算页帧 -> 不写胜负（不猜）
        rec_b = MatchRecorder(base_dir=td, input_source="test")
        rec_b.start(session_name="auto_none")
        rec_b.on_state({"clock_s": 9.0, "ui": {"hp": 1.0}})
        m2.finalize_recorder(rec_b, argparse.Namespace(result="auto"),
                             reason="auto", frame=_banner_frame((90, 90, 90)))
        man_b = json.loads((Path(td) / "auto_none" / "manifest.json")
                           .read_text(encoding="utf-8"))
        check("F_auto_result_absent_on_plain_frame", man_b.get("result") is None,
              f"{man_b.get('result')}")

    print(f"\n结果: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
    if FAIL:
        print("失败项:", ", ".join(FAIL))
    return 0 if not FAIL else 1


def _parse_fails(ap, argv):
    """非法参数应让 argparse 以 SystemExit 退出（stderr 静音，避免污染回归输出）。"""
    import contextlib
    import io
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            ap.parse_args(argv)
        return False
    except SystemExit:
        return True


if __name__ == "__main__":
    raise SystemExit(main())
