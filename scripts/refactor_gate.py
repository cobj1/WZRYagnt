# -*- coding: utf-8 -*-
"""重构闸门：给 m2 拆分提供"行为未变"的机械证据。

三项检查（对应 `docs/规划与方案/m2装配拆分方案-2026-10-26.md` §4 P0）：
  1. CLI 契约：`m2_agent_v2.py --help` 的文本哈希不变（参数集合/默认值/帮助文案都不许飘）
  2. 决策等价：`eval_decision_offline.py` 在固定子集上的 `digest` 不变（逐帧决策逐字节一致）
  3. 回归：`run_tests.py` 全绿

用法：
  # 第一次（或确认基线已过时）：记录基线
  venv/Scripts/python.exe -X utf8 scripts/refactor_gate.py --record
  # 重构后：比对
  venv/Scripts/python.exe -X utf8 scripts/refactor_gate.py
  # 只看决策等价（跳过较慢的测试套件）
  venv/Scripts/python.exe -X utf8 scripts/refactor_gate.py --skip-tests

退出码：0 全过 / 1 有差异 / 2 基线缺失
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "venv" / "Scripts" / "python.exe"
if not PY.exists():                                    # 非 Windows 或未建 venv
    PY = Path(sys.executable)
BASE_DIR = ROOT / "runs" / "refactor"
HELP_BASE = BASE_DIR / "help.sha256"
DIGEST_BASE = BASE_DIR / "digest.json"
EVAL_LIMIT = 400
EVAL_SEED = 20261024
RECORD_MODE = False


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run(cmd: list[str], timeout: int = 900) -> tuple[int, str]:
    """跑子进程。

    固定 PYTHONHASHSEED=0 是**防御性**措施，不是修 bug：实测 digest 对哈希种子不敏感
    （seed 0/1/2/3/12345 五次运行 digest 全同）。曾一度把“digest 抖动”误判为哈希种子问题，
    实际根因是抽样漂移——设备在线录制让语料增长，`--limit+seed` 抽到的帧集合每次都不同。
    真正解决靠 `--dump-keys/--keys-file`（按 frame_id 冻结帧集合），见 §8 勘误。
    """
    env = {**os.environ, "PYTHONHASHSEED": "0"}
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def check_help() -> dict:
    target = ROOT / "scripts" / "m2_agent_v2.py"
    code, out = run([str(PY), "-X", "utf8", str(target), "--help"], timeout=180)
    # 帮助文本里可能带本机绝对路径，归一化后再哈希，避免换机误报
    normalized = out.replace(str(ROOT), "<ROOT>").replace("\\", "/")
    return {"exit": code, "sha256": sha256_text(normalized), "chars": len(normalized),
            "head": normalized.strip().splitlines()[:1]}


def check_digest() -> dict:
    out_json = Path(tempfile.mkdtemp(prefix="gate_")) / "eval.json"
    trace = out_json.with_name("trace.jsonl")
    keys_file = BASE_DIR / "keys.json"
    cmd = [str(PY), "-X", "utf8", str(ROOT / "scripts" / "eval_decision_offline.py"),
           "--limit", str(EVAL_LIMIT), "--seed", str(EVAL_SEED),
           "--json", str(out_json), "--trace", str(trace), "--quiet"]
    if keys_file.exists():
        cmd += ["--keys-file", str(keys_file)]
    elif RECORD_MODE:
        cmd += ["--dump-keys", str(keys_file)]
    code, out = run(cmd)
    if code != 0 or not out_json.exists():
        return {"exit": code, "error": out.strip().splitlines()[-3:]}
    data = json.loads(out_json.read_text(encoding="utf-8"))
    return {"exit": code, "digest": data.get("digest"),
            "frames": data.get("sampling", {}).get("evaluated"),
            "config": data.get("config"), "limit": EVAL_LIMIT, "seed": EVAL_SEED}


def check_tests() -> dict:
    target = ROOT / "scripts" / "run_tests.py"
    if not target.exists():
        return {"exit": -1, "skipped": "run_tests.py 不存在"}
    code, out = run([str(PY), "-X", "utf8", str(target)], timeout=1800)
    tail = [ln for ln in out.strip().splitlines() if ln.strip()][-3:]
    return {"exit": code, "tail": tail}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true", help="记录当前状态为基线")
    ap.add_argument("--skip-tests", action="store_true", help="跳过 run_tests.py")
    ap.add_argument("--json", default="", help="把结果写到该路径")
    args = ap.parse_args()

    global RECORD_MODE
    RECORD_MODE = args.record
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    help_res = check_help()
    dig_res = check_digest()
    tests_res = {"skipped": "--skip-tests"} if args.skip_tests else check_tests()

    print("=== 1. CLI 契约 ===")
    print(f"  exit={help_res['exit']} sha256={help_res['sha256'][:16]} chars={help_res['chars']}")
    print("=== 2. 决策等价 ===")
    print(f"  digest={str(dig_res.get('digest'))[:16]} frames={dig_res.get('frames')}")
    print("=== 3. 回归 ===")
    print(f"  {tests_res}")

    if args.record:
        HELP_BASE.write_text(help_res["sha256"], encoding="utf-8")
        DIGEST_BASE.write_text(json.dumps(dig_res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已记录基线：{HELP_BASE} / {DIGEST_BASE}")
        return 0

    if not HELP_BASE.exists() or not DIGEST_BASE.exists():
        print("\n基线缺失：先跑一次 --record", file=sys.stderr)
        return 2

    diffs = []
    if help_res["sha256"] != HELP_BASE.read_text(encoding="utf-8").strip():
        diffs.append("CLI 帮助文本变化（参数/默认值/文案）")
    base_dig = json.loads(DIGEST_BASE.read_text(encoding="utf-8"))
    if dig_res.get("digest") != base_dig.get("digest"):
        diffs.append(f"决策 digest 变化：{base_dig.get('digest')} -> {dig_res.get('digest')}")
    if not args.skip_tests and tests_res.get("exit") != 0:
        diffs.append("run_tests 未全绿")

    payload = {"checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "help": help_res, "digest": dig_res, "tests": tests_res,
               "baseline": {"help": str(HELP_BASE), "digest": base_dig.get("digest")},
               "diffs": diffs, "verdict": "fail" if diffs else "pass"}
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    if diffs:
        print("FAIL:")
        for d in diffs:
            print(f"  - {d}")
        return 1
    print("PASS：行为与基线一致（CLI + 决策 + 回归）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
