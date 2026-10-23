# -*- coding: utf-8 -*-
"""在历史 WZRY states 上回放 hok shadow 决策（不连设备、不写动作）。

用法：
    python scripts/hok_shadow_probe.py --session data/matches/20261017_162901_384146 --limit 60
    python scripts/hok_shadow_probe.py --states path/to/states.jsonl --hz 1
"""
import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.decision.hok_bridge import HokShadow  # noqa: E402


def _load_states(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    rows.sort(key=lambda row: float(row.get("t") or 0.0))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", default="data/matches/20261017_162901_384146",
                        help="data/matches/<session> 目录或 states.jsonl")
    parser.add_argument("--states", default=None, help="直接指定 states.jsonl")
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--hz", type=float, default=1.0)
    parser.add_argument("--self-camp", choices=["blue", "red"], default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--horizon", type=float, default=12.0)
    parser.add_argument("--beam", type=int, default=2)
    parser.add_argument("--model", choices=["base", "linear"], default="base",
                        help="base=规则权重；linear=Tencent 线性价值基线（未做 WZRY 域校准）")
    args = parser.parse_args()

    if args.states:
        states_path = Path(args.states)
        session_name = states_path.parent.name
    else:
        session = Path(args.session)
        states_path = session / "states.jsonl" if session.is_dir() else session
        session_name = session.name
    if not states_path.exists():
        print(f"找不到 states: {states_path}")
        return 1

    out = Path(args.out) if args.out else ROOT / "temp" / "hok_shadow" / f"{session_name}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    linear_model = (
        ROOT.parent / "hokdata" / "value_linear_model.json"
        if args.model == "linear" else None
    )
    shadow = HokShadow(
        out_path=out,
        horizon_s=args.horizon,
        beam_width=args.beam,
        linear_model_path=linear_model,
    )
    rows = _load_states(states_path)
    interval = 1.0 / max(0.1, args.hz)
    selected = []
    last_t = None
    for row in rows:
        t = float(row.get("t") or 0.0)
        if last_t is None or t - last_t >= interval:
            selected.append(row)
            last_t = t
        if len(selected) >= args.limit:
            break

    print(f"session={session_name} states={len(rows)} selected={len(selected)} out={out}")
    decisions = []
    for index, state in enumerate(selected, 1):
        payload = shadow.decide(state, self_camp=args.self_camp)
        decisions.append(payload)
        if index % 10 == 0 or index == len(selected):
            action = (payload or {}).get("first_action")
            print(f"  {index}/{len(selected)} t={state.get('t')} -> {action}")

    errors = [row for row in decisions if row and row.get("error")]
    kinds = Counter((row or {}).get("first_kind") for row in decisions if row and not row.get("error"))
    latencies = [float(row["decision_ms"]) for row in decisions if row and row.get("decision_ms") is not None]
    summary = {
        "session": session_name,
        "states": len(rows),
        "decisions": len(decisions),
        "errors": len(errors),
        "action_kinds": dict(kinds),
        "decision_ms_median": round(statistics.median(latencies), 2) if latencies else None,
        "decision_ms_p95": round(sorted(latencies)[int(len(latencies) * 0.95)], 2) if latencies else None,
        "out": str(out),
    }
    (out.with_suffix(".summary.json")).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if errors:
        for row in errors[:5]:
            print("ERROR", row)
    return 0 if decisions and not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
