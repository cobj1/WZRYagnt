# -*- coding: utf-8 -*-
"""hok_bridge 状态保真度单测（回退链 + 不回归）。

背景：`data/matches/*/states.jsonl` 里 `clock_s` / `game_time_s` / `self_state` /
`units.pos_minimap` 全部缺席（0/11043），改造前 `_game_time()` 恒 0、自身血量恒 1.0，
等于把每帧都当成"第 0 分钟满血"。这会直接废掉 `time_norm` 这个符号最稳定的特征。

运行：venv/Scripts/python.exe -X utf8 scripts/test_hok_bridge_fields.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.decision.hok_bridge import (_camp_of, _game_time,  # noqa: E402
                                      _self_state, build_search_state,
                                      load_hok_map)

_PASS, _FAIL = [], []


def check(name, cond, extra=""):
    if cond:
        _PASS.append(name)
        print(f"PASS {name}")
    else:
        _FAIL.append(name)
        print(f"FAIL {name} {extra}")


def visual_frame(**over):
    """模拟纯视觉录制的一帧：只有 units/minimap/ui/t。"""
    frame = {
        "t": 1000.0,
        "session_start_t": 940.0,
        "units": [],
        "minimap": {"found": True,
                    "dots": {"green": [[0.5, 0.5]], "blue": [[0.4, 0.45]], "red": [[0.6, 0.55]]}},
        "ui": {"hp": 0.42, "mp": 1.0},
        "extra": {},
    }
    frame.update(over)
    return frame


# ---- 1. 时钟回退链优先级 ----
check("T1_clock_s_wins", _game_time({"clock_s": 321.0, "game_time_s": 999.0}) == 321.0)
check("T2_game_time_s_second", _game_time({"game_time_s": 654.0}) == 654.0)
check("T3_ui_match_clock", _game_time({"ui": {"match_clock": 77.0}}) == 77.0)
check("T4_extra_game_time", _game_time({"extra": {"game_time_s": 88.0}}) == 88.0)
check("T5_session_elapsed", _game_time({"session_elapsed_s": 45.0}) == 45.0)
check("T6_wall_clock_delta", _game_time({"t": 1000.0, "session_start_t": 940.0}) == 60.0,
      f"got {_game_time({'t': 1000.0, 'session_start_t': 940.0})}")
check("T7_no_source_keeps_zero", _game_time({"ui": {}}) == 0.0)
check("T8_negative_delta_guarded", _game_time({"t": 10.0, "session_start_t": 940.0}) == 0.0)

# ---- 2. 自身状态合成 ----
synth = _self_state(visual_frame(), "blue")
check("T9_self_state_synthesized_from_ui_hp",
      synth.get("hp") == 0.42 and synth.get("camp") == "blue" and synth.get("synthesized") is True,
      f"got {synth}")
check("T10_self_state_no_fake_gold", "gold" not in synth and "gold_total" not in synth)
real = {"camp": "red", "hp": 0.9, "gold_total": 5000.0}
check("T11_real_self_state_untouched", _self_state({"self_state": real}, "blue") is real)
check("T12_missing_hp_not_invented",
      "hp" not in _self_state({"ui": {}}, "blue"))

# ---- 3. 集成：build_search_state 在纯视觉帧上 ----
map_data, anchors = load_hok_map()
st = build_search_state(visual_frame(), map_data=map_data, self_camp="blue")
check("T13_search_state_game_time_from_wall_clock", st.game_time == 60.0, f"got {st.game_time}")
blue = [h for h in st.heroes.values() if h.side == "blue"]
self_hero = next((h for h in blue if h.name == "self"), None)
check("T14_self_hp_comes_from_ui", self_hero is not None and abs(self_hero.hp - 0.42) < 1e-6,
      f"got {None if self_hero is None else self_hero.hp}")

# ---- 4. 不回归：字段齐全时走原路径 ----
full = {
    "clock_s": 512.0,
    "self_state": {"camp": "red", "hp": 0.75, "gold_total": 4321.0},
    "units": [{"cls": "enemy_hero", "pos_minimap": [0.3, 0.6], "hp": 0.5}],
    "minimap": {"found": True, "dots": {"green": [[0.5, 0.5]]}},
    "ui": {"hp": 0.1},
}
st_full = build_search_state(full, map_data=map_data, self_camp=None)
check("T15_full_fields_game_time", st_full.game_time == 512.0, f"got {st_full.game_time}")
check("T16_full_fields_camp_from_self_state", _camp_of(full) == "red")
red_hp = [round(h.hp, 4) for h in st_full.heroes.values() if h.side == "blue"]
check("T17_unit_pos_path_used", red_hp == [0.5], f"got {red_hp}")
check("T18_full_fields_self_hp_ignores_ui",
      _self_state(full, "red").get("hp") == 0.75)

print()
if _FAIL:
    print(f"FAILED ({len(_FAIL)}/{len(_PASS) + len(_FAIL)}): " + ", ".join(_FAIL))
    raise SystemExit(1)
print(f"ALL PASS ({len(_PASS)})")
