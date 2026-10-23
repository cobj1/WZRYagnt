# -*- coding: utf-8 -*-
"""外部/文件驱动（`--drive-stdin`）的输入解析测试 —— v16.2，无需板子/模拟器。

被测对象：`m2_agent_v2._drive_parse_line`（纯函数，负责"一行 stdin -> 动作 + 等待时长"）。

要守住的性质
------------
1. 脏输入一律跳过而不是崩：空行 / `#` 注释 / 非法 JSON / 非 dict / 缺 `type` / `type=="none"`。
2. 节奏优先级：`delay_ms`（毫秒）> `t`（绝对秒，用与前一条的差值）> 不等待。
3. `t` 模式能**直接喂** `data/matches/*/actions.jsonl`（schema 就是"动作 dict + t"）：
   第一条不等、后续按差值等、差值 clamp 到 [0, pace_max]、时间倒退不等。
4. `delay_ms` / `t` 必须从动作 dict 里**移除**，不能污染传给 apply_action 的动作。

运行: venv/Scripts/python.exe -X utf8 scripts/test_drive_stdin.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import m2_agent_v2 as m                                     # noqa: E402

_P = m._drive_parse_line
PACE = 5.0
_PASS = []


def check(name, cond, extra=""):
    if cond:
        _PASS.append(name)
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {extra}")
        raise SystemExit(1)


def main():
    # ---------- D1 脏输入一律跳过 ----------
    for _nm, _raw in (("空行", ""), ("纯空白", "   \n"), ("注释", "# 说明"),
                      ("非法JSON", "{not json"), ("非dict", "[1,2,3]"),
                      ("JSON标量", "42"), ("缺type", '{"nx":0.5}'),
                      ("type为none", '{"type":"none","reason":"dead_waiting"}')):
        a, w, pt = _P([_raw][0], 100.0, PACE)
        check(f"D1 {_nm} -> 跳过", a is None and w == 0.0 and pt == 100.0, f"{(a, w, pt)}")

    # ---------- D2 正常动作, 无任何时间字段 -> 不等待 ----------
    a, w, pt = _P('{"type":"map_move","nx":0.5,"ny":0.5}', None, PACE)
    check("D2a 动作解析正确", a == {"type": "map_move", "nx": 0.5, "ny": 0.5}, f"{a}")
    check("D2b 无时间字段 -> 不等待", w == 0.0)
    check("D2c prev_t 保持 None", pt is None)

    # ---------- D3 delay_ms ----------
    a, w, pt = _P('{"type":"attack","delay_ms":500}', None, PACE)
    check("D3a delay_ms=500 -> 等 0.5s", abs(w - 0.5) < 1e-9, f"{w}")
    check("D3b delay_ms 已从动作里移除", a == {"type": "attack"}, f"{a}")

    a, w, _ = _P('{"type":"attack","delay_ms":-100}', None, PACE)
    check("D3c 负 delay_ms -> 不等待", w == 0.0, f"{w}")

    # ---------- D4 t 差值(直接喂 actions.jsonl) ----------
    a, w, pt = _P('{"type":"map_move","nx":0.66,"ny":0.66,"t":1791905768.5}', None, PACE)
    check("D4a 第一条 t -> 不等待", w == 0.0, f"{w}")
    check("D4b prev_t 记录第一条时刻", pt == 1791905768.5, f"{pt}")
    check("D4c t 已从动作里移除", a == {"type": "map_move", "nx": 0.66, "ny": 0.66}, f"{a}")

    a, w, pt = _P('{"type":"attack","t":1791905769.7}', 1791905768.5, PACE)
    check("D4d 第二条按差值等 1.2s", abs(w - 1.2) < 1e-6, f"{w}")
    check("D4e prev_t 前移", pt == 1791905769.7, f"{pt}")

    # ---------- D5 clamp 与时间倒退 ----------
    _, w, _ = _P('{"type":"attack","t":100.0}', 0.0, PACE)
    check("D5a 差值超 pace_max -> clamp 到 pace_max", w == PACE, f"{w}")

    _, w, _ = _P('{"type":"attack","t":100.0}', 105.0, PACE)
    check("D5b 时间倒退(差值<0) -> 不等待", w == 0.0, f"{w}")

    _, w, _ = _P('{"type":"attack","t":100.0}', 99.0, 0.5)
    check("D5c pace_max 可调", abs(w - 0.5) < 1e-9, f"{w}")

    # ---------- D6 delay_ms 优先于 t, 但 t 仍要更新 prev_t ----------
    a, w, pt = _P('{"type":"attack","delay_ms":200,"t":500.0}', 100.0, PACE)
    check("D6a delay_ms 优先(0.2s 而不是 5s)", abs(w - 0.2) < 1e-9, f"{w}")
    check("D6b t 仍更新 prev_t", pt == 500.0, f"{pt}")
    check("D6c 两个字段都已移除", a == {"type": "attack"}, f"{a}")

    # ---------- D7 未知 type 也放行(交给 apply_action 自己决定; 不静默丢) ----------
    a, _, _ = _P('{"type":"skill","id":2,"mode":"drag","target":[900,400]}', None, PACE)
    check("D7 skill/drag 原样透传(含 target)",
          a == {"type": "skill", "id": 2, "mode": "drag", "target": [900, 400]}, f"{a}")

    # ================= _drive_run：驱动主循环本体 =================
    _R = m._drive_run

    def _mk():
        got, slept = [], []

        def _put(o):
            got.append(o)

        def _sleep(s):
            slept.append(s)
        return got, slept, _put, _sleep

    # ---------- E1 基本：顺序投递、无等待 ----------
    got, slept, put, sleep = _mk()
    n, sk, fs = _R(['{"type":"map_move","nx":0.1,"ny":0.2}',
                    '{"type":"attack"}',
                    '{"type":"recall"}'], put, sleep=sleep)
    check("E1a 三条全部下发", n == 3 and sk == 0 and fs is None, f"{(n, sk, fs)}")
    check("E1b 顺序与内容正确",
          [o["type"] for o in got] == ["map_move", "attack", "recall"], f"{got}")
    check("E1c 无等待", slept == [], f"{slept}")

    # ---------- E2 跳过统计（空行/注释不算, 坏行才算） ----------
    got, slept, put, sleep = _mk()
    n, sk, fs = _R(["", "   ", "# 注释", "{坏JSON", '{"nx":1}',
                    '{"type":"none"}', '{"type":"attack"}'], put, sleep=sleep)
    check("E2a 只下发 1 条", n == 1, f"{n}")
    check("E2b 跳过=3(坏JSON/缺type/type为none)", sk == 3, f"{sk}")
    check("E2c 首条被跳过的是坏 JSON 那行", fs == "{坏JSON", f"{fs!r}")

    # ---------- E3 delay_ms 生效 ----------
    got, slept, put, sleep = _mk()
    n, sk, _ = _R(['{"type":"attack","delay_ms":250}',
                   '{"type":"attack","delay_ms":0}'], put, sleep=sleep)
    check("E3a delay_ms 转成 sleep", slept == [0.25], f"{slept}")
    check("E3b 0 不产生 sleep", n == 2, f"{n}")

    # ---------- E4 t 跨行差值（验证 prev_t 在行间传递） ----------
    got, slept, put, sleep = _mk()
    n, _, _ = _R(['{"type":"attack","t":10.0}',
                  '{"type":"attack","t":10.5}',
                  '{"type":"attack","t":11.2}'], put, sleep=sleep)
    check("E4a 第一条不等, 后续按差值", [round(x, 3) for x in slept] == [0.5, 0.7], f"{slept}")

    # ---------- E5 背压：执行线程忙时等待 ----------
    got, slept, put, sleep = _mk()
    state = {"busy": 2}

    def _idle():
        return state["busy"] <= 0

    def _sleep2(s):
        slept.append(s)
        state["busy"] -= 1
    n, _, _ = _R(['{"type":"attack"}'], put, is_idle=_idle, sleep=_sleep2)
    check("E5a 忙时先等空闲再投递", n == 1 and len(slept) == 2, f"{(n, slept)}")

    # ---------- E6 is_done 提前收尾 ----------
    got, slept, put, sleep = _mk()
    flag = {"done": False}

    def _put2(o):
        got.append(o)
        flag["done"] = True          # 第一条投完就要求收尾

    n, _, _ = _R(['{"type":"attack"}', '{"type":"attack"}', '{"type":"attack"}'],
                 _put2, is_done=lambda: flag["done"], sleep=sleep)
    check("E6 收尾后不再下发后续行", n == 1 and len(got) == 1, f"{(n, got)}")

    # ---------- E7 投递出去的 dict 不含 t/delay_ms ----------
    got, slept, put, sleep = _mk()
    _R(['{"type":"attack","delay_ms":10,"t":99.0}'], put, sleep=sleep)
    check("E7 时间字段不污染动作", got == [{"type": "attack"}], f"{got}")

    print(f"\n全部通过: {len(_PASS)} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
