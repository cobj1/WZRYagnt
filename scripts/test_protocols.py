# -*- coding: utf-8 -*-
"""Runtime 三协议离线回归（S1-1）。

只验证协议本身，不接主循环，确保零行为变更。
用法：
    venv\\Scripts\\python.exe scripts/test_protocols.py
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.runtime.action import Action, ACTION_TYPES  # noqa: E402
from wzry.state.record import StateRecord  # noqa: E402
from wzry.state.schema import GameState, Unit  # noqa: E402
from wzry.task.protocol import Claim, OnAbort, Task  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  {extra}" if extra else ""))


def raises(fn):
    try:
        fn()
    except Exception:
        return True
    return False


# ---------------------------------------------------------------- 状态协议
unavailable = StateRecord.unavailable("self.hp", t_obs=10.0)
check("S1 缺失字段 value=None/source=unavailable",
      unavailable.value is None and unavailable.source == "unavailable" and not unavailable.observed)
check("S2 不可观测不能进硬过滤",
      not unavailable.hard_filterable(10.1, max_age_s=0.5, min_conf=0.6))
fresh = StateRecord.observed_value(
    "self.hp", 0.8, t_obs=10.0, source="detector", conf=0.9
)
check("S3 新鲜+高置信+非estimate 可进硬过滤",
      fresh.hard_filterable(10.2, max_age_s=0.5, min_conf=0.6))
check("S4 过期记录不能进硬过滤",
      not fresh.hard_filterable(10.6, max_age_s=0.5, min_conf=0.6))
low = StateRecord.observed_value(
    "self.hp", 0.8, t_obs=10.0, source="detector", conf=0.3
)
check("S5 低置信不能进硬过滤", not low.hard_filterable(10.2, max_age_s=0.5, min_conf=0.6))
est = StateRecord.observed_value(
    "enemy.skill2.cd", 3.2, t_obs=10.0, source="estimate", conf=1.0
)
check("S6 estimate 只能软打分，不能进硬过滤",
      est.observed and not est.hard_filterable(10.2, max_age_s=0.5, min_conf=0.6))
check("S7 缺失值伪造为 detector 会被拒绝",
      raises(lambda: StateRecord("self.hp", None, t_obs=1.0, source="detector", conf=1.0)))
check("S8 unavailable 携带非空 value 会被拒绝",
      raises(lambda: StateRecord("self.hp", 1.0, t_obs=1.0, source="unavailable", conf=0.0)))

# ---------------------------------------------------------------- Unit 别名
unit = Unit(last_seen_t=3.0)
check("S9 Unit.t_last_seen 与 last_seen_t 双向兼容",
      unit.t_last_seen == 3.0)
unit.t_last_seen = 4.0
check("S10 Unit.t_last_seen 可写回 last_seen_t", unit.last_seen_t == 4.0)

# ---------------------------------------------------------------- 任务协议
task = Task(
    task_id="task_001",
    agent_id="hero_A",
    source="self",
    goal="support_ally",
    priority=80,
    preemptible=True,
    preconditions=["self_alive", "target_reachable"],
    expires_in_seconds=5,
    abort_conditions=["self_health_too_low"],
    on_abort=OnAbort(release_stick=True, rollback_cooldowns=False, cancel_pending_combo=True),
    claim=Claim(target_id="enemy_hero_2", lease_seconds=3),
    created_at=100.0,
)
check("T1 任务字段完整", task.goal == "support_ally" and task.claim.target_id == "enemy_hero_2")
check("T2 任务过期判定", not task.is_expired(104.9) and task.is_expired(105.1))
roundtrip = Task.from_dict(json.loads(json.dumps(task.to_dict(), ensure_ascii=False)))
check("T3 Task JSON 往返一致",
      roundtrip.to_dict() == task.to_dict())
check("T4 非法 source 被拒绝",
      raises(lambda: Task(task_id="x", source="external", goal="farm")))
check("T5 非法 priority 被拒绝",
      raises(lambda: Task(task_id="x", goal="farm", priority=101)))

# ---------------------------------------------------------------- 动作协议
action = Action.for_type("skill", {"id": 2}, deadline_ms=200)
check("A1 skill 默认非幂等、需屏幕、不占屏",
      action.type == "skill" and not action.idempotent
      and action.requires_screen and not action.occupies_screen)
buy = Action.for_type("buy", {"item_id": 1111})
check("A2 buy 默认占屏且不要求战斗屏幕", buy.occupies_screen and not buy.requires_screen)
move = Action.for_type("map_move", {"nx": 0.5, "ny": 0.5})
check("A3 move 默认幂等", move.idempotent)
check("A4 deadline 过期判定",
      not action.is_expired(action.t_decide + 0.1)
      and action.is_expired(action.t_decide + 0.3))
critical = Action.for_type("skill", {"id": 1}, cls_name="critical")
check("A5 critical 标记", critical.critical)
roundtrip_action = Action.from_dict(json.loads(json.dumps(action.to_dict())))
check("A6 Action JSON 往返一致",
      roundtrip_action.to_dict() == action.to_dict())
check("A7 非法动作类型被拒绝", raises(lambda: Action(type="teleport")))
check("A8 所有协议动作类型可构造",
      all(Action.for_type(t).type == t for t in ACTION_TYPES))
decision = {"type": "move", "theta": 0.5, "duration_ms": 380, "reason": "lane"}
proto = Action.from_decision(decision)
check("A9 决策 dict 转 Action 保留语义",
      proto is not None and proto.reason == "lane" and proto.duration_ms == 380)
check("A10 Action 还原执行 dict 逐键一致",
      proto.to_legacy_dict() == decision)
check("A11 none/stick 不转 Action",
      Action.from_decision({"type": "none", "reason": "x"}) is None)

# ---------------------------------------------------------------- 零行为变更
state = GameState(t=1.0, source="live")
payload = state.to_dict()
check("Z1 GameState 结构保持可序列化", payload["t"] == 1.0 and "units" in payload)
check("Z2 新增协议模块无副作用导入", len(ACTION_TYPES) == 10)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", ", ".join(FAIL))
    raise SystemExit(1)
print("PROTOCOLS_OK")
