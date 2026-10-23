"""WZRY GameState -> hok-decision-ai SearchState 适配器（shadow 专用）。

只读：不改变 WZRYagnt 动作流，不向执行器写任何东西。
主循环接线前，先用 ``scripts/hok_shadow_probe.py`` 在历史 states 上回放。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

# wzyr/decision/hok_bridge.py -> parents[2] = WZRYagnt, parent = aiKings
_AIKINGS_ROOT = Path(__file__).resolve().parents[2].parent
_HOK_SRC = _AIKINGS_ROOT / "hok-decision-ai" / "src"
if str(_HOK_SRC) not in sys.path:
    sys.path.insert(0, str(_HOK_SRC))

MINIMAP_AFFINE = {"x": (0.98787, 21.840), "y": (0.96455, 1.800)}


def _ref_pos(norm: list[float] | tuple[float, float]) -> tuple[float, float]:
    ax, bx = MINIMAP_AFFINE["x"]
    ay, by = MINIMAP_AFFINE["y"]
    return ax * float(norm[0]) * 416.0 + bx, ay * float(norm[1]) * 424.0 + by


def _hp_ratio(unit: dict[str, Any]) -> float:
    hp = unit.get("hp")
    if isinstance(hp, (int, float)) and 0.0 <= float(hp) <= 1.0:
        return float(hp)
    hp_abs = unit.get("hp_abs")
    hp_max = unit.get("hp_max")
    if isinstance(hp_abs, (int, float)) and isinstance(hp_max, (int, float)) and hp_max:
        return max(0.0, min(1.0, float(hp_abs) / float(hp_max)))
    return 1.0


def _camp_of(state: dict[str, Any]) -> str:
    self_state = state.get("self_state") or {}
    if self_state.get("camp") in {"blue", "red"}:
        return self_state["camp"]
    extra = state.get("extra") or {}
    if extra.get("camp") in {"blue", "red"}:
        return extra["camp"]
    return "blue"


def _hero_rows(state: dict[str, Any], self_camp: str) -> list[dict[str, Any]]:
    """优先使用带 pos_minimap 的 units；缺失时回退小地图色点。"""
    rows = []
    for index, unit in enumerate(state.get("units") or []):
        if not (unit.get("kind") == "hero" or str(unit.get("cls", "")).endswith("hero")):
            continue
        pos = unit.get("pos_minimap")
        if not pos:
            continue
        camp = unit.get("camp") or self_camp
        rows.append(
            {
                "id": unit.get("uid") or unit.get("hero_cid") or f"unit_{index}",
                "camp": camp,
                "position": _ref_pos(pos),
                "hp": _hp_ratio(unit),
                "alive": not bool(unit.get("dead")),
            }
        )

    if rows:
        return rows

    dots = (state.get("minimap") or {}).get("dots") or {}
    team_color = {"blue": "ally", "red": "enemy"} if self_camp == "blue" else {"blue": "enemy", "red": "ally"}
    if dots.get("green"):
        rows.append(
            {
                "id": "self",
                "camp": self_camp,
                "position": _ref_pos(dots["green"][0]),
                "hp": _hp_ratio(state.get("self_state") or {}),
                "alive": True,
            }
        )
    for color, limit in (("blue", 4), ("red", 5)):
        camp = self_camp if team_color.get(color) == "ally" else ("red" if self_camp == "blue" else "blue")
        for index, point in enumerate((dots.get(color) or [])[:limit]):
            rows.append(
                {
                    "id": f"{color}_{index}",
                    "camp": camp,
                    "position": _ref_pos(point),
                    "hp": 1.0,
                    "alive": True,
                }
            )
    return rows


def _gold_by_side(state: dict[str, Any], self_camp: str) -> dict[str, float]:
    totals = {"blue": 0.0, "red": 0.0}
    for row in state.get("scoreboard") or []:
        camp = row.get("camp")
        gold = row.get("gold_total")
        if isinstance(gold, (int, float)) and camp in {"blue", "red"}:
            totals[camp] += float(gold)
    if totals["blue"] == 0 and totals["red"] == 0:
        self_state = state.get("self_state") or {}
        gold = self_state.get("gold_total") or self_state.get("gold")
        if isinstance(gold, (int, float)):
            totals[self_camp] = float(gold)
    return totals


def _game_time(state: dict[str, Any]) -> float:
    for key in ("clock_s", "game_time_s"):
        value = state.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    clock = (state.get("ui") or {}).get("match_clock")
    return float(clock) if isinstance(clock, (int, float)) else 0.0


def load_hok_map(
    map_path: Path | None = None,
    anchors_path: Path | None = None,
):
    """加载 MapData 与 anchors；延迟导入，避免 WZRY 纯运行时依赖 pydantic。"""
    from hok_decision.schema.models import MapData

    map_path = map_path or (_AIKINGS_ROOT / "hok-decision-ai" / "data" / "S45" / "normalized" / "map.json")
    anchors_path = anchors_path or (_AIKINGS_ROOT / "hok-decision-ai" / "data" / "S45" / "normalized" / "terrain_anchors.json")
    map_data = MapData.model_validate(json.loads(map_path.read_text(encoding="utf-8")))
    anchors_payload = json.loads(anchors_path.read_text(encoding="utf-8"))["anchors"]
    anchors = {str(anchor["id"]): anchor for anchor in anchors_payload}
    return map_data, anchors


def build_search_state(
    state: dict[str, Any],
    *,
    map_data,
    self_camp: str | None = None,
):
    """WZRY GameState(dict) -> hok SearchState（self 侧映射为 blue）。"""
    from hok_decision.search.planner import SearchState
    from hok_decision.simulator.units import Hero

    self_camp = self_camp or _camp_of(state)
    heroes = {}
    for index, row in enumerate(_hero_rows(state, self_camp)):
        side = "blue" if row["camp"] == self_camp else "red"
        unit_id = f"{side}_{index}"
        hp = max(1e-6, float(row["hp"]))
        heroes[unit_id] = Hero(
            unit_id=unit_id,
            name=str(row["id"]),
            side=side,
            position=row["position"],
            hp=hp,
            max_hp=1.0,
            alive=bool(row["alive"]),
            attack_range=300.0,
        )

    tower_hp = {
        point.id: 1.0
        for point in map_data.resource_points
        if point.kind == "tower"
    }
    side_gold = _gold_by_side(state, self_camp)
    blue_ids = [unit_id for unit_id, hero in heroes.items() if hero.side == "blue"]
    red_ids = [unit_id for unit_id, hero in heroes.items() if hero.side == "red"]
    gold = {
        unit_id: side_gold["blue"] / max(1, len(blue_ids))
        for unit_id in blue_ids
    }
    gold.update({
        unit_id: side_gold["red"] / max(1, len(red_ids))
        for unit_id in red_ids
    })
    return SearchState(
        heroes=heroes,
        game_time=_game_time(state),
        captured_resources=set(),
        gold=gold,
        tower_hp=tower_hp,
    )


class HokShadow:
    """只读 shadow：调用 beam search 并落盘决策，不影响动作流。"""

    def __init__(
        self,
        *,
        out_path: Path,
        map_path: Path | None = None,
        anchors_path: Path | None = None,
        horizon_s: float = 12.0,
        beam_width: int = 2,
        max_candidates: int = 8,
        enabled: bool = True,
    ) -> None:
        self.enabled = enabled
        self.out_path = Path(out_path)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.map_data, self.anchors = load_hok_map(map_path, anchors_path)
        self.horizon_s = horizon_s
        self.beam_width = beam_width
        self.max_candidates = max_candidates

    def decide(self, state: dict[str, Any], *, self_camp: str | None = None) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        started = time.perf_counter()
        payload: dict[str, Any]
        try:
            from hok_decision.search.planner import beam_search

            search_state = build_search_state(state, map_data=self.map_data, self_camp=self_camp)
            best = beam_search(
                search_state,
                map_data=self.map_data,
                anchors=self.anchors,
                horizon_s=self.horizon_s,
                beam_width=self.beam_width,
                action_duration_s=4.0,
                max_candidates=self.max_candidates,
                with_decision_output=True,
            )
            first = best.actions[0] if best and best.actions else None
            output = best.decision_output if best else None
            payload = {
                "t": state.get("t"),
                "frame_id": state.get("frame_id"),
                "self_camp": self_camp or _camp_of(state),
                "value": round(float(best.value), 4) if best else None,
                "first_action": first.label if first else None,
                "first_kind": first.kind if first else None,
                "target_id": first.target_id if first else None,
                "actions": [action.label for action in (best.actions if best else ())],
                "goal_vector": output.goal_vector if output else None,
                "difficulty": output.difficulty if output else None,
                "target_position": output.target_position if output else None,
                "n_blue": sum(1 for hero in search_state.heroes.values() if hero.side == "blue"),
                "n_red": sum(1 for hero in search_state.heroes.values() if hero.side == "red"),
                "decision_ms": round((time.perf_counter() - started) * 1000.0, 2),
                "source": "shadow",
            }
        except Exception as exc:
            payload = {
                "t": state.get("t"),
                "frame_id": state.get("frame_id"),
                "error": f"{type(exc).__name__}: {exc}",
                "decision_ms": round((time.perf_counter() - started) * 1000.0, 2),
                "source": "shadow",
            }
        with self.out_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return payload
