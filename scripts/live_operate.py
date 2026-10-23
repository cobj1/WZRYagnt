# -*- coding: utf-8 -*-
"""实时逐条执行操作层命令（真机验证用）。

用法:
  venv/Scripts/python.exe -X utf8 scripts/live_operate.py actions.jsonl
每行一个 JSON 动作（{"type":"operate","cmd":"..."} 或 {"type":"SetSkillPosition",...}），
共享同一个 state（验证 SetSkillDirection/Position 的暂存瞄准），逐条打印结果。
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.action.executor_v2 import ActionExecutor          # noqa: E402
from wzry.action.operate_commands import command_of, execute_operate  # noqa: E402
from wzry.control.executor import AdbExecutor               # noqa: E402


def main():
    path = Path(sys.argv[1])
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    ex = ActionExecutor(AdbExecutor())
    state = {}
    for i, raw in enumerate(lines, 1):
        action = json.loads(raw)
        cmd = command_of(action)
        t0 = time.perf_counter()
        try:
            ok = execute_operate(ex, action, state)
            ms = (time.perf_counter() - t0) * 1000
            print(f"[{i}] {cmd.value:26s} -> {'OK ' if ok else 'FAIL'} ({ms:.0f} ms)")
        except Exception as e:
            ms = (time.perf_counter() - t0) * 1000
            print(f"[{i}] {cmd.value:26s} -> EXC {type(e).__name__}: {e} ({ms:.0f} ms)")
        time.sleep(0.6)


if __name__ == "__main__":
    main()
