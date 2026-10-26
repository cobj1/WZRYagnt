# -*- coding: utf-8 -*-
"""感知契约回归（PERC-02/03）。

不连设备，只验证三件事：
  1. 生产主循环只 import `mm_tracker_v7`，不再引 v1–v6 / minimap_tracker；
  2. m2 使用的四种流式采集器都具备 start/wait_frame/stop 同一接口；
  3. 内存源仍暴露 CID/HP/坐标/法力所需字段与 game_state 映射。

这是静态契约，不能替代真机采样；但它能防止"又接回旧实现"或"接口漂移"。
"""
from __future__ import annotations

import inspect
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from wzry.capture.desktop_capture import DesktopCapturePrint  # noqa: E402
from wzry.capture.scrcpy_stream import ScrcpyStreamCapture  # noqa: E402
from wzry.capture.screencap_capture import ScreencapCapture  # noqa: E402
from wzry.capture.screenrecord_capture import ScreenrecordCapture  # noqa: E402
from wzry.data.collector import MatchRecorder  # noqa: E402
from wzry.vision import mem_source as MS  # noqa: E402

PASS = []


def check(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print(f"PASS {name}")
    else:
        raise AssertionError(f"FAIL {name} {extra}")


def main() -> int:
    src = (ROOT / "scripts" / "m2_agent_v2.py").read_text(encoding="utf-8")
    check("m2 使用 v7 小地图", "from wzry.vision.mm_tracker_v7 import MMTrackerV7" in src)
    legacy = [name for name in ("minimap_tracker", "mm_rules_v1", "mm_rules_v2",
                                "mm_rules_v3", "mm_rules_v4", "mm_rules_v5",
                                "mm_rules_v6")
              if re.search(rf"from wzry\.vision\.{name}\b", src)]
    check("m2 不 import 旧小地图", legacy == [], f"{legacy}")

    streams = [ScrcpyStreamCapture, ScreencapCapture, ScreenrecordCapture,
               DesktopCapturePrint]
    missing = {c.__name__: [m for m in ("start", "wait_frame", "stop")
                            if not callable(getattr(c, m, None))]
               for c in streams}
    missing = {k: v for k, v in missing.items() if v}
    check("四种流式采集器接口一致", missing == {}, f"{missing}")
    check("采集器都提供 wait_frame 超时参数",
          all("timeout" in inspect.signature(c.wait_frame).parameters
              for c in streams))

    check("内存偏移含法力", "ep_cur" in MS.CONSTANT_OFFSETS
          and "ep_max" in MS.CONSTANT_OFFSETS)
    check("内存单位含 CID/HP/坐标",
          all(f in MS.MemUnit.__dataclass_fields__
              for f in ("config_id", "hp", "hp_max", "x", "z")))
    check("内存输出含世界/小地图坐标",
          "pos_world" in inspect.getsource(MS.MemSource.to_game_state)
          and "pos_minimap" in inspect.getsource(MS.MemSource.to_game_state))
    check("内存输出含 SelfState 法力", "mp=ss.get(\"ep\")" in
          inspect.getsource(MS.MemSource.to_game_state))

    with tempfile.TemporaryDirectory() as td:
        rec = MatchRecorder(base_dir=td, input_source="test")
        rec.start(session_name="s1")
        st = {
            "t": 1.0, "clock_s": 10.0, "scoreboard": [{"cid": 106}],
            "sources": {"units": "vision", "minimap": "vision"},
            "ui": {"hp": 0.8, "match_clock": 10.0},
            "minimap": {"dots": {"red": [[0.6, 0.4]]}},
            "units": [{"cls": "enemy_hero", "pos_minimap": [0.6, 0.4]}],
        }
        rec.on_state(st)
        rec.close()
        row = json.loads((Path(td) / "s1" / "states.jsonl")
                         .read_text(encoding="utf-8").splitlines()[0])
        check("录制保留 sources", row.get("sources", {}).get("units") == "vision")
        manifest = json.loads((Path(td) / "s1" / "manifest.json")
                              .read_text(encoding="utf-8"))
        cov = manifest.get("field_coverage") or {}
        check("录制覆盖四字段", all(cov.get(k, 0) >= 1.0 for k in
                                    ("clock_s", "scoreboard",
                                     "units.pos_minimap", "ui.hp")),
              f"{cov}")

        rec2 = MatchRecorder(base_dir=td, input_source="test")
        rec2.start(session_name="s2")
        rec2.on_state({
            "t": 2.0, "clock_s": 5.0, "ui": {"hp": 0.9, "hero_id": 106},
            "extra": {
                # 红方：self 由 ui.hero_id + mem_heroes 的 cid 定位（不假设 slot0）
                "mem_heroes": [{"id": 106, "camp": "red"}],
                "mem_units": [{
                    "cid": 1690, "kind": "minion", "camp": "red", "actor": 7,
                    "x": 1000, "z": 2000, "hp": 100, "hp_max": 200,
                    "hp_ratio": 0.5, "dead": False}]},
        })
        rec2.close()
        row2 = json.loads((Path(td) / "s2" / "states.jsonl")
                          .read_text(encoding="utf-8").splitlines()[0])
        mem = [u for u in row2.get("units") or [] if u.get("coord_src") == "memory"]
        check("红方内存单位按友方标注", len(mem) == 1
              and mem[0]["cls"] == "ally_minion" and len(mem[0]["pos_minimap"]) == 2,
              f"{mem}")

        rec3 = MatchRecorder(base_dir=td, input_source="test")
        rec3.start(session_name="s3")
        rec3.on_state({
            "t": 3.0, "clock_s": 6.0, "ui": {"hp": 0.9},   # 无 hero_id，self 不可解析
            "extra": {
                "mem_heroes": [{"id": 106, "camp": "red"}],
                "mem_units": [{
                    "cid": 1690, "kind": "minion", "camp": "red", "actor": 7,
                    "x": 1000, "z": 2000, "hp": 100, "hp_max": 200,
                    "hp_ratio": 0.5, "dead": False}]},
        })
        rec3.close()
        row3 = json.loads((Path(td) / "s3" / "states.jsonl")
                          .read_text(encoding="utf-8").splitlines()[0])
        mem3 = [u for u in row3.get("units") or [] if u.get("coord_src") == "memory"]
        check("自身阵营不可解析时不猜敌我", len(mem3) == 1
              and mem3[0]["cls"] == "unknown_minion" and mem3[0]["side"] == "unknown"
              and row3["sources"]["self_camp"] == "unknown",
              f"{mem3} / {row3.get('sources')}")
        manifest2 = json.loads((Path(td) / "s2" / "manifest.json")
                               .read_text(encoding="utf-8"))
        check("内存单位补足 pos_minimap",
              manifest2["field_coverage"]["units.pos_minimap"] >= 1.0)
        check("内存快照四字段全达标", all(
            manifest2["field_coverage"].get(k, 0) >= 1.0
            for k in ("clock_s", "scoreboard", "units.pos_minimap", "ui.hp")),
            f"{manifest2['field_coverage']}")

    if len(PASS) != 14:
        raise AssertionError(f"expected 14 checks, got {len(PASS)}")
    print(f"\nALL PASS ({len(PASS)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
