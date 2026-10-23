# -*- coding: utf-8 -*-
"""感知融合：帧 + 检测结果 + 小地图 -> GameState。"""
import time
from typing import List, Optional

from wzry.state.schema import GameState, Unit
from wzry.vision.detector import Det

_KIND_BY_CLS = {
    "enemy_hero": "hero", "ally_hero": "hero",
    "enemy_minion": "minion", "ally_minion": "minion",
    "enemy_turret": "organ", "ally_turret": "organ",
    "enemy_crystal": "organ", "ally_crystal": "organ",
    "neutral_monster": "monster",
}


def build_state(frame, dets: List[Det], phase: str, minimap: Optional[dict] = None,
                frame_id: int = 0, source: str = "live") -> GameState:
    h, w = frame.shape[:2]
    now = time.time()
    st = GameState(t=now, t_capture=now, phase=phase, frame_id=frame_id,
                   source=source, screen_size=[w, h],
                   minimap=minimap or {},
                   sources={"units": "vision",
                            "minimap": "vision" if minimap else "none"})
    for d in dets:
        st.units.append(Unit(
            cls=d.cls,
            kind=_KIND_BY_CLS.get(d.cls, "unknown"),
            screen=[d.center[0] / w, d.center[1] / h,
                    (d.xyxy[2] - d.xyxy[0]) / w, (d.xyxy[3] - d.xyxy[1]) / h],
            conf=float(d.conf),
            camp_src=getattr(d, "camp_src", None),
            visible=True,
            coord_src="screen",
            src="vision",
            last_seen_t=now,
        ))
    return st
