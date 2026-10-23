# -*- coding: utf-8 -*-
"""到达闸门（ArrivalGate）离线测试 —— v16.2，无需板子/模拟器/游戏。

要守住的性质
------------
1. 到达即停：`dist < near` 时保持 'hold'（调用方松手），这是消除过冲的正常路径。
2. 保险丝：同一位置连续停推超过 `idle_max` 秒 → 'fuse'，让调用方回退到"重锚回基地
   + 继续推进"。这是原实现防"永久僵住"（实测 180s 只发 19 次 swipe）的兜底。
3. 未到达 / 离开目标后重新接近 → 'push'，且计时**重新开始**（不能沿用旧 since）。
4. 边界：`dist == near` 视为未到达（与调用方的 `dist < MAPMOVE_NEAR` 判据一致）。

运行: venv/Scripts/python.exe -X utf8 scripts/test_arrival_gate.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.decision.nav_gate import ArrivalGate        # noqa: E402

NEAR = 0.04
IDLE = 5.0
_PASS = []


def check(name, cond, extra=""):
    if cond:
        _PASS.append(name)
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {extra}")
        raise SystemExit(1)


def main():
    # ---------- N1 未到达 -> push ----------
    g = ArrivalGate(NEAR, IDLE)
    check("N1a 远距离->push", g.decide(0.0, 0.50) == "push")
    check("N1b push 后不处于 holding", g.holding is False)
    check("N1c 边界 dist==near -> push(未到达)", g.decide(0.1, NEAR) == "push")

    # ---------- N2 首次到达 -> hold ----------
    g = ArrivalGate(NEAR, IDLE)
    check("N2a 首次到达->hold", g.decide(10.0, 0.01) == "hold")
    check("N2b holding=True", g.holding is True)
    check("N2c 计时起点=到达时刻", g.since == 10.0)

    # ---------- N3 idle_max 内持续 hold ----------
    check("N3a +1s -> hold", g.decide(11.0, 0.02) == "hold")
    check("N3b +4.9s -> hold", g.decide(14.9, 0.03) == "hold")
    check("N3c 尚未触发保险丝", g.fuse_count == 0)

    # ---------- N4 超时 -> fuse ----------
    check("N4a 超 idle_max -> fuse", g.decide(15.1, 0.02) == "fuse")
    check("N4b fuse_count=1", g.fuse_count == 1)
    check("N4c fuse 后 holding 清空", g.holding is False)

    # ---------- N5 fuse 后重新计时, 可再次触发 ----------
    check("N5a fuse 后立刻再判 -> hold(重新计时)",
          g.decide(15.2, 0.02) == "hold")
    check("N5b 新一轮起点", g.since == 15.2)
    check("N5c 新一轮未超时仍 hold", g.decide(19.0, 0.02) == "hold")
    check("N5d 再次超时 -> fuse", g.decide(20.3, 0.02) == "fuse")
    check("N5e fuse_count=2", g.fuse_count == 2)

    # ---------- N6 离开目标 -> push，重置状态 ----------
    g = ArrivalGate(NEAR, IDLE)
    g.decide(0.0, 0.01)                       # hold
    check("N6a 到达后 hold", g.holding is True)
    check("N6b 离开目标->push", g.decide(1.0, 0.30) == "push")
    check("N6c holding 清空", g.holding is False)
    check("N6d since 归零", g.since == 0.0)
    check("N6e 未触发保险丝", g.fuse_count == 0)

    # ---------- N7 再次接近必须重新计时(不能沿用旧 since) ----------
    check("N7a 4s 后重新到达->hold", g.decide(5.0, 0.02) == "hold")
    check("N7b 计时从 5.0 起", g.since == 5.0)
    check("N7c 距 5.0 仅 4.9s -> hold(不会误触保险丝)",
          g.decide(9.9, 0.02) == "hold")
    check("N7d fuse_count 仍为 0", g.fuse_count == 0)

    # ---------- N8 reset ----------
    g = ArrivalGate(NEAR, IDLE)
    g.decide(0.0, 0.01)
    g.reset()
    check("N8a reset 清 holding", g.holding is False)
    check("N8b reset 清 since", g.since == 0.0)
    check("N8c reset 后首次到达重新 hold", g.decide(100.0, 0.01) == "hold")

    # ---------- N9 保险丝时长的口径 ----------
    g = ArrivalGate(NEAR, 1.0)
    g.decide(0.0, 0.01)
    check("N9a 恰好 idle_max -> hold(不早触发)", g.decide(1.0, 0.01) == "hold")
    check("N9b 略超 -> fuse", g.decide(1.01, 0.01) == "fuse")

    print(f"\n全部通过: {len(_PASS)} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
