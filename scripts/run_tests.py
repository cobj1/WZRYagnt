# -*- coding: utf-8 -*-
"""统一回归入口：不依赖 pytest，顺序运行现有 check() 脚本并汇总退出码。

用法：
    python -X utf8 scripts/run_tests.py
    python -X utf8 scripts/run_tests.py --list
    python -X utf8 scripts/run_tests.py --only behavior
    python -X utf8 scripts/run_tests.py --keep-going
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITES = [
    ("protocols", "scripts/test_protocols.py", False),
    ("m2_assembly", "scripts/test_m2_assembly.py", False),
    ("match_result", "scripts/test_match_result.py", False),
    ("decision_output", "scripts/test_decision_output.py", False),
    ("action_dispatch", "scripts/test_action_dispatch.py", False),
    ("context", "scripts/test_context.py", False),
    ("engage", "scripts/test_engage.py", False),
    ("combat", "scripts/test_combat.py", False),
    ("combo", "scripts/test_combo.py", False),
    ("safety", "scripts/test_safety.py", False),
    ("mem_offsets", "scripts/test_mem_offsets.py", False),
    ("perception_contract", "scripts/test_perception_contract.py", False),
    ("replay_capture", "scripts/test_replay_capture.py", False),
    ("channel_health", "scripts/test_channel_health.py", False),
    ("inference", "scripts/test_inference.py", False),
    ("trace_report", "scripts/test_trace_report.py", False),
    ("eval_replay", "scripts/test_eval_replay.py", False),
    ("official", "scripts/test_official_ai_replica.py", False),
    ("laya", "scripts/test_laya_fusion.py", False),
    ("laya_stale", "scripts/test_laya_stale_probs.py", False),
    ("new_modules", "scripts/test_new_modules.py", False),
    ("bridge_fields", "scripts/test_hok_bridge_fields.py", True),
    ("data_contract", "../hok-decision-ai/tests/test_data_contract.py", True),
    ("golden_readiness", "../hok-decision-ai/tests/test_golden_readiness.py", True),
    ("frame_features", "../hok-decision-ai/tests/test_build_frame_features.py", True),
    ("splits", "../hok-decision-ai/tests/test_splits.py", True),
    ("coverage", "scripts/test_collector_coverage.py", False),
    ("mem_worker", "scripts/test_mem_worker_self_state.py", False),
    ("disasm_addr", "scripts/test_disasm_addr.py", False),
    ("mm_fusion_gate", "scripts/test_mm_fusion_gate.py", False),
    ("security", "scripts/validate_safety.py", False),
    ("behavior", "scripts/m2_agent_v2_test.py", False),
]


def run_one(name: str, rel: str, needs_sibling: bool) -> tuple[bool, str]:
    if needs_sibling and not (ROOT.parent / "hok-decision-ai").exists():
        return True, "SKIP (sibling hok-decision-ai absent)"
    script = ROOT / rel
    if not script.exists():
        return False, "MISSING"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(script)],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    lines = [ln.strip() for ln in (proc.stdout + "\n" + proc.stderr).splitlines() if ln.strip()]
    tail = lines[-1] if lines else "(no output)"
    return proc.returncode == 0, tail


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--keep-going", action="store_true")
    args = ap.parse_args(argv)

    suites = [s for s in SUITES if not args.only or s[0] in args.only]
    if args.list:
        for name, rel, sibling in SUITES:
            print(f"{name:14s} {rel}" + (" (needs sibling)" if sibling else ""))
        return 0
    if not suites:
        print("no suites selected", file=sys.stderr)
        return 2

    failed = []
    slow = []
    for name, rel, sibling in suites:
        _t0 = time.perf_counter()
        ok, tail = run_one(name, rel, sibling)
        _dt = time.perf_counter() - _t0
        slow.append((_dt, name))
        print(f"[{'PASS' if ok else 'FAIL'}] {name:14s} {_dt:6.1f}s  {tail}")
        if not ok:
            failed.append(name)
            if not args.keep_going:
                break
    if failed:
        print(f"FAILED: {', '.join(failed)}", file=sys.stderr)
        return 1
    slow.sort(reverse=True)
    print("最慢套件: " + ", ".join(f"{n} {d:.1f}s" for d, n in slow[:3]))
    print(f"ALL PASS ({len(suites)} suites, {sum(d for d, _ in slow):.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
