# -*- coding: utf-8 -*-
"""HokShadowWorker 离线回归：异步、可丢弃、不改动作。"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.decision.hok_bridge import HokShadowWorker  # noqa: E402

out = ROOT / "temp" / "hok_shadow_worker_test" / "decisions.jsonl"
out.parent.mkdir(parents=True, exist_ok=True)
if out.exists():
    out.unlink()

state = {
    "t": 100.0,
    "frame_id": 1,
    "phase": "in_match",
    "units": [],
    "minimap": {
        "found": True,
        "dots": {
            "green": [[0.08, 0.9]],
            "blue": [[0.2, 0.8], [0.3, 0.7]],
            "red": [[0.8, 0.2], [0.7, 0.3]],
        },
    },
    "ui": {"hp": 0.8, "mp": 0.7},
    "self_state": {"camp": "blue", "hp": 0.8, "mp": 0.7},
    "extra": {"camp": "blue"},
}

worker = HokShadowWorker(out_path=out, hz=100.0).start()
passes = {
    "enabled": worker.enabled,
    "error_empty": not worker.error,
}
for index in range(3):
    passes[f"submit_{index}"] = worker.submit({**state, "frame_id": index + 1})
    time.sleep(0.1)
worker.close()

rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
passes["decisions_written"] = len(rows) >= 1
passes["no_errors"] = all("error" not in row for row in rows)
passes["shadow_source"] = all(row.get("source") == "shadow" for row in rows)
passes["status"] = worker.status()["decisions"] >= 1
passes["errors_zero"] = worker.status()["errors"] == 0
passes["dropped_zero"] = worker.status()["dropped"] == 0

# 错误必须计入 errors，而不是伪装成一次成功决策
out2 = out.parent / "decisions_error.jsonl"
if out2.exists():
    out2.unlink()
worker2 = HokShadowWorker(out_path=out2, hz=100.0).start()
worker2.shadow.decide = lambda state: {"error": "simulated", "source": "shadow"}
worker2.submit(state)
time.sleep(0.15)
worker2.close()
passes["error_counted"] = worker2.status()["errors"] >= 1
passes["error_not_decision"] = worker2.status()["decisions"] == 0

for name, ok in passes.items():
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
if not all(passes.values()):
    print("worker:", worker.status())
    print("rows:", rows[:2])
    raise SystemExit(1)
print("HOK_SHADOW_WORKER_OK")
