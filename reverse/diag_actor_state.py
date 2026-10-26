# -*- coding: utf-8 -*-
"""对局内 actor 状态诊断：身份稳定性 + HP 死亡去抖 + 位置抖动。

来源：TrueVision 的 actor 稳定性/HP 校验结论（11.3.1.1），在 12.1.1.1 上重验；
只读，不含 KPM/perf/注入/ESP。

用法：
  python reverse/diag_actor_state.py --duration 60 --hz 2
  python reverse/diag_actor_state.py --mode identity --duration 120
  python reverse/diag_actor_state.py --selftest          # 无设备离线自检

输出：
  reverse/out/diag/actor_state_<pid>_<ts>.jsonl          # 每帧每英雄
  reverse/out/diag/actor_state_<pid>_<ts>.csv
  reverse/out/diag/actor_state_<pid>_<ts>.summary.json   # 退出时汇总
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision.mem_source import MemSource, MemUnit, apply_hp  # noqa: E402

PAC_MASK = 0xFFFFFFFFFFFF


def strip_pac(ptr: int) -> int:
    return ptr & PAC_MASK if ptr else 0


@dataclass
class ActorSample:
    ts: float
    slot: int
    actor: int
    cid: int
    camp: str
    hp: Optional[int]
    hp_max: Optional[int]
    x: Optional[int]
    z: Optional[int]
    dead: bool
    hp_max_missing: bool

    @property
    def ident(self) -> Tuple[int, str]:
        return self.cid, self.camp


def read_samples(src: MemSource, heroes_only: bool = True) -> List[ActorSample]:
    _, _, cnt, actors = src._actor_table()
    if not cnt or not actors:
        return []
    units = src.snapshot()
    by_actor = {u.actor: u for u in units}
    ts = time.time()
    out: List[ActorSample] = []
    for slot, actor in enumerate(actors):
        u = by_actor.get(actor)
        if u is None or (heroes_only and not u.is_hero):
            continue
        out.append(ActorSample(
            ts=ts, slot=slot, actor=strip_pac(actor),
            cid=u.config_id, camp=u.camp,
            hp=u.hp, hp_max=u.hp_max, x=u.x, z=u.z,
            dead=u.dead, hp_max_missing=bool(u.extra.get("hp_max_missing")),
        ))
    return out


class IdentityTracker:
    """记录 actor 指针 / (cid,camp) / slot 三者的变化。"""

    def __init__(self):
        self.actors: Dict[int, dict] = {}
        self.identities: Dict[Tuple[int, str], dict] = {}
        self.events: List[dict] = []
        self.frames = 0

    def _event(self, kind: str, ts: float, **kw):
        ev = {"ts": round(ts, 3), "kind": kind}
        ev.update(kw)
        self.events.append(ev)

    def update(self, samples: List[ActorSample], ts: float):
        self.frames += 1
        counts = Counter(s.ident for s in samples)
        for ident, n in counts.items():
            if n > 1:
                self._event("duplicate_identity", ts, cid=ident[0],
                            camp=ident[1], slots=[s.slot for s in samples
                                                  if s.ident == ident])
        for s in samples:
            a = self.actors.setdefault(s.actor, {
                "cids": set(), "camps": set(), "slots": set(),
                "first": ts, "last": ts, "frames": 0,
            })
            if a["cids"] and s.cid not in a["cids"]:
                self._event("actor_reused_new_cid", ts, actor=s.actor,
                            old=sorted(a["cids"]), new=s.cid, slot=s.slot)
            a["cids"].add(s.cid)
            a["camps"].add(s.camp)
            a["slots"].add(s.slot)
            a["last"] = ts
            a["frames"] += 1

            it = self.identities.setdefault(s.ident, {
                "actors": set(), "slots": set(), "first": ts, "last": ts,
            })
            if it["actors"] and s.actor not in it["actors"]:
                self._event("identity_pointer_changed", ts, cid=s.cid,
                            camp=s.camp, old=sorted(it["actors"]), new=s.actor)
            if it["slots"] and s.slot not in it["slots"]:
                self._event("identity_slot_changed", ts, cid=s.cid,
                            camp=s.camp, old=sorted(it["slots"]), new=s.slot)
            it["actors"].add(s.actor)
            it["slots"].add(s.slot)
            it["last"] = ts

    def summary(self) -> dict:
        kinds = Counter(e["kind"] for e in self.events)
        return {
            "frames": self.frames,
            "actors": len(self.actors),
            "identities": len(self.identities),
            "event_counts": dict(kinds),
            "event_examples": self.events[:50],
            "identity_detail": [
                {"cid": cid, "camp": camp,
                 "actors": sorted(v["actors"]), "slots": sorted(v["slots"])}
                for (cid, camp), v in sorted(self.identities.items())
            ],
        }


class HpTracker:
    """current HP 连续零帧判死；max 缺失/非法时不据此反推死亡。"""

    def __init__(self, k: int = 3):
        self.k = k
        self.state: Dict[int, dict] = {}
        self.events: List[dict] = []
        self.anomalies: List[dict] = []
        self.frames = 0
        self.max_zero_run = 0

    def _event(self, kind: str, ts: float, s: ActorSample):
        self.events.append({
            "ts": round(ts, 3), "kind": kind, "actor": s.actor,
            "slot": s.slot, "cid": s.cid, "camp": s.camp,
            "hp": s.hp, "hp_max": s.hp_max, "hp_max_missing": s.hp_max_missing,
        })

    def _anomaly(self, kind: str, ts: float, s: ActorSample):
        self.anomalies.append({
            "ts": round(ts, 3), "kind": kind, "actor": s.actor,
            "slot": s.slot, "cid": s.cid, "camp": s.camp,
            "hp": s.hp, "hp_max": s.hp_max,
        })

    def update(self, samples: List[ActorSample], ts: float):
        self.frames += 1
        for s in samples:
            st = self.state.setdefault(s.actor, {
                "ident": None, "zero": 0, "dead": False,
                "last_hp": None, "last_max": None,
            })
            if st["ident"] not in (None, s.ident):
                st.update(ident=s.ident, zero=0, dead=False)
            st["ident"] = s.ident

            if s.hp is None:
                self._anomaly("missing_hp", ts, s)
                continue
            if s.hp < 0 or (s.hp_max is not None
                            and (s.hp_max <= 0 or s.hp > s.hp_max)):
                self._anomaly("implausible_hp", ts, s)
                continue
            if s.hp == 0 and s.hp_max_missing:
                # max 读不到时只记录，不据此确认死亡
                self._anomaly("zero_hp_max_missing", ts, s)
                st["zero"] = 0
                continue

            if s.hp == 0:
                st["zero"] += 1
                self.max_zero_run = max(self.max_zero_run, st["zero"])
                if st["zero"] >= self.k and not st["dead"]:
                    st["dead"] = True
                    self._event("confirmed_dead", ts, s)
            else:
                if st["dead"]:
                    self._event("revived", ts, s)
                st["dead"] = False
                st["zero"] = 0
            st["last_hp"], st["last_max"] = s.hp, s.hp_max

    def summary(self) -> dict:
        kinds = Counter(e["kind"] for e in self.events)
        return {
            "frames": self.frames,
            "k": self.k,
            "tracked_actors": len(self.state),
            "max_zero_run": self.max_zero_run,
            "event_counts": dict(kinds),
            "events": self.events[:100],
            "anomaly_counts": dict(Counter(a["kind"] for a in self.anomalies)),
            "anomaly_examples": self.anomalies[:50],
        }


class PositionTracker:
    """记录每个 actor 的位置抖动，供“是否冻结/是否真值”判断。"""

    def __init__(self):
        self.state: Dict[int, dict] = {}

    def update(self, samples: List[ActorSample]):
        for s in samples:
            if s.x is None or s.z is None:
                continue
            st = self.state.setdefault(s.actor, {
                "ident": s.ident, "n": 0, "xs": [], "zs": [],
                "last": (s.x, s.z), "max_delta": 0.0,
            })
            if st["ident"] != s.ident:
                st.update(ident=s.ident, n=0, xs=[], zs=[], max_delta=0.0)
            st["n"] += 1
            st["xs"].append(s.x)
            st["zs"].append(s.z)
            if st["last"] is not None:
                dx = s.x - st["last"][0]
                dz = s.z - st["last"][1]
                st["max_delta"] = max(st["max_delta"], (dx * dx + dz * dz) ** 0.5)
            st["last"] = (s.x, s.z)

    def summary(self) -> List[dict]:
        out = []
        for actor, v in sorted(self.state.items()):
            out.append({
                "actor": actor, "cid": v["ident"][0], "camp": v["ident"][1],
                "samples": v["n"],
                "x_std": round(statistics.pstdev(v["xs"]), 2) if len(v["xs"]) > 1 else 0.0,
                "z_std": round(statistics.pstdev(v["zs"]), 2) if len(v["zs"]) > 1 else 0.0,
                "max_delta": round(v["max_delta"], 2),
            })
        return out


def _selftest() -> int:
    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond

    def s(ts, slot, actor, cid, camp, hp, mx, x=0, z=0, dead=False, missing=False):
        return ActorSample(ts, slot, actor, cid, camp, hp, mx, x, z, dead, missing)

    idt = IdentityTracker()
    idt.update([s(1, 0, 0x1000, 106, "blue", 100, 200),
                s(1, 1, 0x2000, 107, "red", 100, 200)], 1)
    idt.update([s(2, 2, 0x1000, 106, "blue", 90, 200),
                s(2, 1, 0x2000, 107, "red", 80, 200)], 2)
    kinds = Counter(e["kind"] for e in idt.events)
    chk("identity slot change detected", kinds["identity_slot_changed"] == 1)

    idt2 = IdentityTracker()
    idt2.update([s(1, 0, 0x1000, 106, "blue", 100, 200),
                 s(1, 1, 0x2000, 106, "blue", 100, 200)], 1)
    chk("duplicate identity detected",
        Counter(e["kind"] for e in idt2.events)["duplicate_identity"] == 1)

    hp = HpTracker(k=3)
    for i in range(1, 5):
        hp.update([s(i, 0, 0x1000, 106, "blue", 0, 200)], i)
    chk("dead after K zero frames",
        Counter(e["kind"] for e in hp.events)["confirmed_dead"] == 1)
    hp.update([s(5, 0, 0x1000, 106, "blue", 50, 200)], 5)
    chk("revived",
        Counter(e["kind"] for e in hp.events)["revived"] == 1)

    hp2 = HpTracker(k=3)
    for i in range(1, 5):
        hp2.update([s(i, 0, 0x2000, 107, "red", 0, None, missing=True)], i)
    chk("max missing does not confirm death", not hp2.events)
    chk("max missing anomaly recorded",
        Counter(a["kind"] for a in hp2.anomalies)["zero_hp_max_missing"] == 4)

    pt = PositionTracker()
    pt.update([s(1, 0, 0x1000, 106, "blue", 100, 200, 0, 0)])
    pt.update([s(2, 0, 0x1000, 106, "blue", 100, 200, 3, 4)])
    chk("position max delta", abs(pt.summary()[0]["max_delta"] - 5.0) < 1e-6)

    u = MemUnit(actor=0x1000, config_id=106, actor_type=0,
                camp="blue", kind="hero")
    chk("max=0 trusts current", apply_hp(u, 0, 3815)
        and u.hp == 3815 and u.hp_max is None
        and not u.dead and u.extra.get("hp_max_missing") == 1)
    u2 = MemUnit(actor=0x2000, config_id=107, actor_type=0,
                 camp="red", kind="hero")
    chk("max=0/current=0 not dead", apply_hp(u2, 0, 0)
        and u2.hp == 0 and not u2.dead)
    u3 = MemUnit(actor=0x3000, config_id=108, actor_type=0,
                 camp="red", kind="hero")
    chk("valid max/current=0 is dead", apply_hp(u3, 5000, 0)
        and u3.hp == 0 and u3.hp_max == 5000 and u3.dead)
    chk("missing max returns False", not apply_hp(
        MemUnit(actor=0x4000, config_id=109, actor_type=0,
                camp="blue", kind="hero"), None, 100))
    u4 = MemUnit(actor=0x5000, config_id=110, actor_type=0,
                 camp="blue", kind="hero")
    apply_hp(u4, 0, 100)
    apply_hp(u4, 200, 100)
    chk("valid max clears missing flag",
        u4.hp == 100 and u4.hp_max == 200
        and "hp_max_missing" not in u4.extra)

    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="127.0.0.1:16384")
    ap.add_argument("--mode", choices=["identity", "hp", "both"], default="both")
    ap.add_argument("--hz", type=float, default=2.0)
    ap.add_argument("--duration", type=float, default=30.0)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--all-units", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return _selftest()
    if a.hz <= 0 or a.duration <= 0:
        print("--hz/--duration 必须 > 0")
        return 2

    src = MemSource(serial=a.serial)
    pid = src.connect()
    print(f"pid={pid} maps={len(src.m.maps)}")
    for seg in src.m.segment_info("libGameCore.so"):
        print(f"  seg start=0x{seg[0]:x} off=0x{seg[2]:x} perms={seg[4]} {seg[3]}")
    exec_start = src.m.active_exec_start("libGameCore.so")
    print(f"libGameCore exec_start=0x{exec_start:x}" if exec_start else "no libGameCore")

    out = Path(a.out) if a.out else (
        ROOT / "reverse" / "out" / "diag"
        / f"actor_state_{pid}_{datetime.now():%Y%m%d_%H%M%S}.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    csv_path = out.with_suffix(".csv")
    summary_path = out.with_suffix(".summary.json")

    identity = IdentityTracker() if a.mode in ("identity", "both") else None
    hp = HpTracker(k=a.k) if a.mode in ("hp", "both") else None
    pos = PositionTracker()
    frames = 0
    t_end = time.time() + a.duration
    print(f"out={out}")

    with out.open("w", encoding="utf-8", newline="\n") as fj, \
            csv_path.open("w", encoding="utf-8", newline="") as fc:
        writer = csv.writer(fc)
        writer.writerow(["ts", "slot", "actor", "cid", "camp", "hp", "hp_max",
                         "x", "z", "dead", "hp_max_missing"])
        try:
            while time.time() < t_end:
                t0 = time.time()
                try:
                    samples = read_samples(src, heroes_only=not a.all_units)
                except Exception as exc:
                    print(f"[diag] read failed: {exc}")
                    break
                ts = time.time()
                for s in samples:
                    fj.write(json.dumps(asdict(s), ensure_ascii=False) + "\n")
                    writer.writerow([round(s.ts, 3), s.slot, s.actor, s.cid,
                                     s.camp, s.hp, s.hp_max, s.x, s.z,
                                     s.dead, s.hp_max_missing])
                if identity:
                    identity.update(samples, ts)
                if hp:
                    hp.update(samples, ts)
                pos.update(samples)
                frames += 1
                if frames % max(1, int(a.hz)) == 0:
                    print(f"frame={frames} units={len(samples)} "
                          f"elapsed={time.time() - t0:.2f}s")
                time.sleep(max(0.0, 1.0 / a.hz - (time.time() - t0)))
        except KeyboardInterrupt:
            print("interrupted")

    summary = {
        "pid": pid, "frames": frames, "duration_s": a.duration,
        "mode": a.mode, "out": str(out),
        "identity": identity.summary() if identity else None,
        "hp": hp.summary() if hp else None,
        "position": pos.summary(),
        "read_stats": dict(src.m.read_stats),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    print(f"summary={summary_path}")
    print(json.dumps({
        "frames": frames,
        "identity_events": identity.summary()["event_counts"] if identity else {},
        "hp_events": hp.summary()["event_counts"] if hp else {},
        "hp_anomalies": hp.summary()["anomaly_counts"] if hp else {},
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
