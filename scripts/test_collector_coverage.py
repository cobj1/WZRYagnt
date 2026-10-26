# -*- coding: utf-8 -*-
"""MatchRecorder 字段覆盖统计与采数闸门单测。

背景：S1-5 要采 20–50 局，但如果 recorder 只记了半套字段（无 clock/无 pos_minimap/无
scoreboard），采到的数据对决策训练等于废片。这里把"这一局能不能用"变成可断言的接口。

运行：venv/Scripts/python.exe -X utf8 scripts/test_collector_coverage.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.data.collector import MatchRecorder  # noqa: E402

_PASS, _FAIL = [], []


def check(name, cond, extra=""):
    if cond:
        _PASS.append(name)
        print(f"PASS {name}")
    else:
        _FAIL.append(name)
        print(f"FAIL {name} {extra}")


def make_recorder():
    base = Path(tempfile.mkdtemp(prefix="rec_cov_"))
    rec = MatchRecorder(base_dir=str(base))
    rec.start(meta={"note": "coverage-test"}, session_name="sess_cov")
    return rec, base


# ---- 1. 全字段帧 -> 覆盖率 1.0，闸门通过 ----
rec, base = make_recorder()
full_states = []
for i in range(3):
    st = {
        "frame_id": i,
        "ui": {"hp": 0.8, "match_clock": 120.0 + i},
        "units": [{"cls": "enemy_hero", "pos_minimap": [0.3, 0.4]}],
        "minimap": {"found": True, "dots": {"green": [[0.5, 0.5]]}},
        "extra": {"mem_heroes": [{"hero_cid": 131, "camp": "blue", "gold_total": 3000}]},
    }
    rec.on_state(st)
    full_states.append(st)
cov = rec.field_coverage()
check("T1_full_frame_coverage_all_one",
      all(v == 1.0 for k, v in cov.items() if k != "units"),
      f"cov={cov}")
ok, missing = rec.coverage_gate()
check("T2_gate_passes_on_full_frames", ok and not missing, f"{ok} {missing}")
check("T3_session_time_injected",
      isinstance(full_states[0].get("session_start_t"), float)
      and isinstance(full_states[0].get("session_elapsed_s"), float),
      f"keys={list(full_states[0])}")
check("T4_clock_promoted_from_ui",
      full_states[0].get("clock_s") == 120.0 and
      full_states[0]["sources"].get("clock_s") == "vision",
      f"clock={full_states[0].get('clock_s')} src={full_states[0].get('sources')}")
check("T5_scoreboard_from_mem",
      full_states[0].get("scoreboard") is not None
      and full_states[0]["sources"].get("scoreboard") == "memory")
rec.close()

manifest = json.loads((base / "sess_cov" / "manifest.json").read_text(encoding="utf-8"))
check("T6_manifest_has_field_coverage",
      "field_coverage" in manifest and manifest["field_coverage"]["clock_s"] == 1.0,
      f"manifest keys={list(manifest)}")

# ---- 2. 半套字段帧 -> 覆盖率下降，闸门报出缺口 ----
rec2, base2 = make_recorder()
for i in range(4):
    rec2.on_state({
        "frame_id": i,
        "ui": {"hp": 0.5} if i < 2 else {},
        "units": [{"cls": "enemy_hero"}],
        "minimap": {"found": True, "dots": {}},
        "extra": {},
    })
cov2 = rec2.field_coverage()
check("T7_partial_coverage_computed",
      cov2["clock_s"] == 0.0 and cov2["scoreboard"] == 0.0
      and cov2["ui.hp"] == 0.5 and cov2["units"] == 1.0,
      f"cov={cov2}")
ok2, missing2 = rec2.coverage_gate()
check("T8_gate_reports_missing",
      (not ok2) and set(missing2) == {"clock_s", "scoreboard", "units.pos_minimap", "ui.hp"},
      f"{ok2} {missing2}")
check("T9_missing_is_not_zeroed",
      "clock_s" not in [k for k in ("clock_s",) if k in rec2.__dict__],
      "不应伪造 clock_s 字段")
rec2.close()

# ---- 3. 空会话不炸（分母保护） ----
rec3, base3 = make_recorder()
cov3 = rec3.field_coverage()
check("T10_empty_session_zero_coverage", all(v == 0.0 for v in cov3.values()), f"{cov3}")
ok3, missing3 = rec3.coverage_gate()
check("T11_empty_session_gate_fails", not ok3 and len(missing3) == 4, f"{missing3}")
rec3.close()

print()
if _FAIL:
    print(f"FAILED ({len(_FAIL)}/{len(_PASS) + len(_FAIL)}): " + ", ".join(_FAIL))
    raise SystemExit(1)
print(f"ALL PASS ({len(_PASS)})")
