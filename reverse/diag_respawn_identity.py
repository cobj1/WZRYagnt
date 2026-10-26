# -*- coding: utf-8 -*-
"""死亡→复活的身份稳定性实验（离线对局，只读内存；可选摇杆送塔）。

上游 TrueVision 在 11.3.1.1 发现 actor slot 顺序会随死亡/重生/换将 shift，
身份键必须用指针/objID。我们在 35 帧内验证过 actor 指针不变，但没覆盖死亡→复活。
本脚本补这一段：逐帧记录 self 的 actor 指针 / slot / cid / camp / HP /
C# 英雄状态对象地址 / 死亡计数，确认死亡后等复活，再对比死前与复活后的身份字段。

--auto-die 会用摇杆闭环走向敌方中塔（仅离线训练；死亡后自动松手）。

用法：
  python reverse/diag_respawn_identity.py --self-cid 106 --duration 120
  python reverse/diag_respawn_identity.py --self-cid 106 --auto-die --duration 180
  python reverse/diag_respawn_identity.py --selftest

输出：
  reverse/out/diag/respawn_<pid>_<ts>.jsonl
  reverse/out/diag/respawn_<pid>_<ts>.summary.json
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision.camp_identity import camp_id, resolve_self  # noqa: E402
from wzry.vision.mem_source import MemSource  # noqa: E402

PAC_MASK = 0xFFFFFFFFFFFF

# 送塔目标梯度：中塔 1 → 中塔 2 → 高地 → 水晶 → 泉水
TOWER_TARGETS = (
    "enemy_mid_norm_tower_1st", "enemy_mid_norm_tower_2nd",
    "enemy_mid_high_tower", "enemy_crystal", "enemy_spring_tower",
)
# 摇杆（归一化坐标，1920×1080 与 1280×720 通用；取自 configs/calibration.json）
STICK_CENTER = (0.153125, 0.797222)
STICK_RADIUS = 0.17          # × 屏幕高度
STICK_BURST_MS = 450
ARRIVE_RADIUS = 1200.0


def strip_pac(ptr: int) -> int:
    return ptr & PAC_MASK if ptr else 0


@dataclass
class Frame:
    ts: float
    actor: Optional[int]
    slot: Optional[int]
    cid: int
    camp: Optional[str]
    hp: Optional[int]
    hp_max: Optional[int]
    dead: bool
    x: Optional[int]
    z: Optional[int]
    state_obj: Optional[int]
    deaths: Optional[int]
    respawn_s: Optional[int]
    level: Optional[int]
    pos_src: str


def read_frame(src: MemSource, self_cid: int) -> Optional[Frame]:
    try:
        units = src.snapshot()
        ss = src.read_self_state(self_cid)
    except Exception as exc:
        print(f"[respawn] read failed: {exc}")
        return None
    self_pos = None
    if ss and ss.get("x") is not None and ss.get("z") is not None:
        self_pos = (ss["x"], ss["z"])
    res = resolve_self(units, self_cid, self_pos)
    u = res.unit
    slot = None
    if u is not None:
        try:
            _, _, _cnt, actors = src._actor_table()
            want = strip_pac(u.actor)
            for i, a in enumerate(actors or []):
                if strip_pac(a) == want:
                    slot = i
                    break
        except Exception:
            slot = None
    hp = u.hp if u is not None else (ss or {}).get("hp")
    hp_max = u.hp_max if u is not None else (ss or {}).get("hp_max")
    x = u.x if u is not None else (ss or {}).get("x")
    z = u.z if u is not None else (ss or {}).get("z")
    dead = bool(u.dead) if u is not None else bool((ss or {}).get("dead"))
    if ss and ss.get("respawn_s") is not None:
        dead = dead or ss["respawn_s"] > 0
    return Frame(
        ts=time.time(),
        actor=strip_pac(u.actor) if u is not None else None,
        slot=slot,
        cid=(u.config_id if u is not None else (ss or {}).get("id", self_cid)),
        camp=(u.camp if u is not None else res.camp),
        hp=hp, hp_max=hp_max, dead=dead, x=x, z=z,
        state_obj=(ss or {}).get("obj"),
        deaths=(ss or {}).get("deaths"),
        respawn_s=(ss or {}).get("respawn_s"),
        level=(ss or {}).get("level"),
        pos_src=("actor" if u is not None and u.x is not None else
                 "self_state" if self_pos else "none"),
    )


def _same(a, b) -> Optional[bool]:
    """三态比较：任一侧缺失返回 None（未知），不把"读不到"当成"变了"。"""
    if a is None or b is None:
        return None
    return a == b


class RespawnTracker:
    """alive → dead → respawned；输出死前/复活后身份对比。"""

    def __init__(self, k: int = 2):
        self.k = k
        self.phase = "alive"
        self.zero = 0
        self.alive_run = 0
        self.last_alive: Optional[Frame] = None
        self.death_frame: Optional[Frame] = None
        self.deaths_seen: Optional[int] = None
        self.frames = 0
        self.frames_during_death = 0
        self.events = []
        self.verdict: Optional[dict] = None
        self._pre: Optional[Frame] = None
        self._post: Optional[Frame] = None
        self.post_frames = 0
        self.post_last: Optional[Frame] = None
        self.post_changes = 0

    def _event(self, kind: str, f: Frame, **kw):
        ev = {"ts": round(f.ts, 3), "kind": kind}
        ev.update(kw)
        self.events.append(ev)

    @staticmethod
    def _is_dead(f: Frame) -> bool:
        if f.respawn_s is not None and f.respawn_s > 0:
            return True
        return f.hp is not None and f.hp <= 0

    @staticmethod
    def _is_alive(f: Frame) -> bool:
        return (f.hp is not None and f.hp > 0
                and (f.respawn_s is None or f.respawn_s <= 0))

    def update(self, f: Frame):
        self.frames += 1
        counter_death = (self.deaths_seen is not None and f.deaths is not None
                         and f.deaths > self.deaths_seen)
        if f.deaths is not None:
            self.deaths_seen = max(self.deaths_seen or 0, f.deaths)

        if self.phase == "alive":
            if self._is_dead(f):
                self.zero += 1
                if self.zero >= self.k or counter_death:
                    self._enter_dead(f)

            elif counter_death:
                # 采样错过零血窗口（deaths 已 +1）→ 仍认定发生过死亡
                self._enter_dead(f)
            elif self._is_alive(f):
                self.last_alive = f
                self.zero = 0
        elif self.phase == "dead":
            self.frames_during_death += 1
            if self._is_alive(f):
                self.alive_run += 1
                if self.alive_run >= self.k:
                    self._enter_respawned(f)
            else:
                self.alive_run = 0
        else:
            # 复活后继续观察：actor/slot 若再变（换将/重建）也记下来
            self.post_frames += 1
            self.post_last = f
            if (self._post is not None
                    and (f.actor, f.slot, f.camp) !=
                    (self._post.actor, self._post.slot, self._post.camp)):
                self.post_changes += 1
                self._event("post_respawn_change", f,
                            actor=[self._post.actor, f.actor],
                            slot=[self._post.slot, f.slot],
                            camp=[self._post.camp, f.camp])
                self._post = f

    def _enter_dead(self, f: Frame):
        self.phase = "dead"
        self.death_frame = f
        self.frames_during_death = 0
        self.alive_run = 0
        self.zero = 0
        self._pre = self.last_alive or f
        self._event("death", f, deaths=f.deaths, respawn_s=f.respawn_s,
                    hp=f.hp, actor=f.actor, slot=f.slot)

    def _enter_respawned(self, f: Frame):
        self.phase = "respawned"
        self._post = f
        pre, post = self._pre, f
        self.verdict = {
            "pre": asdict(pre) if pre else None,
            "post": asdict(post),
            # True/False/None(读不到)。复活瞬间 C# 状态对象可能短暂不可读，
            # 不能把 None 当成"地址变了"。
            "cid_stable": _same(pre.cid if pre else None, post.cid),
            "actor_stable": _same(pre.actor if pre else None, post.actor),
            "slot_stable": _same(pre.slot if pre else None, post.slot),
            "camp_stable": _same(pre.camp if pre else None, post.camp),
            "state_obj_stable": _same(pre.state_obj if pre else None,
                                      post.state_obj),
            "post_state_obj_readable": post.state_obj is not None,
            "deaths_before": pre.deaths if pre else None,
            "deaths_after": post.deaths,
            "downtime_s": round(post.ts - self.death_frame.ts, 2)
            if self.death_frame else None,
        }
        self._event("respawn", f, **{k: v for k, v in self.verdict.items()
                                     if k not in ("pre", "post")})

    def summary(self) -> dict:
        return {
            "frames": self.frames,
            "phase": self.phase,
            "events": self.events,
            "frames_during_death": self.frames_during_death,
            "verdict": self.verdict,
            # 复活后继续采样的结果：位移是否在几秒内落到泉水、身份是否再漂移
            "post_samples": self.post_frames,
            "post_identity_changes": self.post_changes,
            "post_last": asdict(self.post_last) if self.post_last else None,
        }


class StickDriver:
    """adb 摇杆闭环：原地 burst 推杆，不依赖视觉标定。"""

    def __init__(self, serial: str, screen=(1920, 1080)):
        from wzry.adb_util import resolve_adb
        self.adb = resolve_adb()
        self.serial = serial
        self.w, self.h = screen
        self.cx = int(STICK_CENTER[0] * self.w)
        self.cy = int(STICK_CENTER[1] * self.h)
        self.radius = STICK_RADIUS * self.h
        self.stopped = False

    def _run(self, *args, timeout=4):
        try:
            subprocess.run([self.adb, "-s", self.serial, "shell", *args],
                           capture_output=True, timeout=timeout)
        except Exception as exc:
            print(f"[drive] adb failed: {exc}")

    def screen_size(self) -> tuple:
        try:
            out = subprocess.run([self.adb, "-s", self.serial, "shell", "wm",
                                  "size"], capture_output=True, text=True,
                                 timeout=4).stdout
            for line in out.splitlines():
                if "size:" in line:
                    w, h = line.split("size:")[1].strip().split("x")
                    return int(w), int(h)
        except Exception:
            pass
        return self.w, self.h

    def push(self, theta: float, duration_ms: int = STICK_BURST_MS):
        x = int(self.cx + self.radius * math.cos(theta))
        y = int(self.cy - self.radius * math.sin(theta))
        self._run("input", "swipe", str(self.cx), str(self.cy),
                  str(x), str(y), str(duration_ms))
        self.stopped = False

    def stop(self):
        if not self.stopped:
            self._run("input", "swipe", str(self.cx), str(self.cy),
                      str(self.cx), str(self.cy), "1")
            self.stopped = True


def _selftest() -> int:
    ok = True

    def chk(name, cond, extra=""):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))
        ok = ok and cond

    def F(ts, hp, deaths=0, actor=0x1000, slot=3, camp="blue",
          obj=0x9000, respawn_s=0, cid=106):
        return Frame(ts, actor, slot, cid, camp, hp, 3000, hp <= 0,
                     0, 0, obj, deaths, respawn_s, 1, "actor")

    t = RespawnTracker(k=2)
    for i in range(3):
        t.update(F(i * 0.5, 3000))
    for i in range(2):
        t.update(F(1.5 + i * 0.5, 0, deaths=1, respawn_s=8))
    for i in range(2):
        t.update(F(3.0 + i * 0.5, 3000, deaths=1))
    v = t.verdict or {}
    chk("death + respawn detected", t.phase == "respawned"
        and v.get("deaths_before") == 0 and v.get("deaths_after") == 1)
    chk("stable identity", v.get("actor_stable") and v.get("slot_stable")
        and v.get("camp_stable") and v.get("state_obj_stable"), str(v))

    t = RespawnTracker(k=2)
    for i in range(2):
        t.update(F(i * 0.5, 3000))
    t.update(F(1.0, 0, deaths=1))
    t.update(F(1.5, 0, deaths=1))
    t.update(F(2.0, 3000, deaths=1, actor=0x2000, slot=5))
    t.update(F(2.5, 3000, deaths=1, actor=0x2000, slot=5))
    v = t.verdict or {}
    chk("actor/slot change detected",
        not v.get("actor_stable") and not v.get("slot_stable")
        and v.get("camp_stable"), str(v))

    t = RespawnTracker(k=2)
    for i in range(5):
        t.update(F(i * 0.5, 3000))
    chk("no death -> no verdict", t.verdict is None and t.phase == "alive")

    # 采样错过零血窗口：deaths 直接 +1，仍要识别出死亡与复活
    t = RespawnTracker(k=2)
    t.update(F(0.0, 3000, deaths=0))
    t.update(F(0.5, 2600, deaths=1))
    t.update(F(1.0, 3000, deaths=1))
    t.update(F(1.5, 3000, deaths=1))
    chk("missed zero-hp window still detected", t.phase == "respawned"
        and (t.verdict or {}).get("deaths_after") == 1)

    # camp 翻转
    t = RespawnTracker(k=2)
    for i in range(2):
        t.update(F(i * 0.5, 3000))
    t.update(F(1.0, 0, deaths=1))
    t.update(F(1.5, 0, deaths=1))
    t.update(F(2.0, 3000, deaths=1, camp="red"))
    t.update(F(2.5, 3000, deaths=1, camp="red"))
    chk("camp flip detected", not (t.verdict or {}).get("camp_stable"))

    # 复活瞬间 C# 状态对象短暂读不到：应报未知(None)，不能报"地址变了"
    t = RespawnTracker(k=2)
    for i in range(2):
        t.update(F(i * 0.5, 3000))
    t.update(F(1.0, 0, deaths=1))
    t.update(F(1.5, 0, deaths=1))
    t.update(F(2.0, 3000, deaths=1, obj=None))
    t.update(F(2.5, 3000, deaths=1, obj=None))
    v = t.verdict or {}
    chk("unreadable post state obj is unknown",
        v.get("state_obj_stable") is None
        and v.get("post_state_obj_readable") is False, str(v))
    chk("actor still compared when obj unreadable",
        v.get("actor_stable") is True)

    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="127.0.0.1:16384")
    ap.add_argument("--self-cid", type=int, default=None)
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument("--hz", type=float, default=2.0)
    ap.add_argument("--k", type=int, default=2)
    ap.add_argument("--auto-die", action="store_true")
    ap.add_argument("--stand-seconds", type=float, default=15.0)
    ap.add_argument("--post-seconds", type=float, default=6.0,
                    help="复活后继续采样时长（观察坐标落回泉水/身份漂移）")
    ap.add_argument("--out", default=None)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return _selftest()
    if not a.self_cid:
        print("--self-cid 必填（玩家所选英雄 id）")
        return 2
    if a.hz <= 0 or a.duration <= 0:
        print("--hz/--duration 必须 > 0")
        return 2

    src = MemSource(serial=a.serial)
    pid = src.connect()
    print(f"pid={pid} self_cid={a.self_cid} auto_die={a.auto_die}")

    out = Path(a.out) if a.out else (
        ROOT / "reverse" / "out" / "diag"
        / f"respawn_{pid}_{datetime.now():%Y%m%d_%H%M%S}.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    summary_path = out.with_suffix(".summary.json")

    tracker = RespawnTracker(k=a.k)
    driver = StickDriver(a.serial) if a.auto_die else None
    targets = []
    target_idx = 0
    stand_since = None
    driving = False
    last_drive_pos = None
    no_move_pushes = 0

    t_end = time.time() + a.duration
    last = None
    respawn_ts = None
    with out.open("w", encoding="utf-8", newline="\n") as fj:
        while time.time() < t_end:
            t0 = time.time()
            f = read_frame(src, a.self_cid)
            if f is None:
                time.sleep(max(0.0, 1.0 / a.hz))
                continue
            if last is None:
                camp = camp_id(f.camp)
                if driver and camp:
                    from wzry.decision.official_ai import organ_slots as _osl
                    tbl = _osl.geo_table(camp)
                    targets = [tbl[k] for k in TOWER_TARGETS if k in tbl]
                print(f"first frame: actor=0x{(f.actor or 0):x} slot={f.slot} "
                      f"camp={f.camp} hp={f.hp}/{f.hp_max} targets={len(targets)}")
            last = f

            tracker.update(f)
            fj.write(json.dumps(asdict(f), ensure_ascii=False) + "\n")

            if tracker.phase == "respawned":
                if respawn_ts is None:
                    respawn_ts = time.time()
                    if driver:
                        driver.stop()
                    print(f"[respawn] 检测到复活，继续采样 {a.post_seconds:.0f}s")
                elif time.time() - respawn_ts >= a.post_seconds:
                    break

            if driver and targets and not f.dead and tracker.phase == "alive":
                target = targets[min(target_idx, len(targets) - 1)]
                if f.x is None or f.z is None:
                    pass
                else:
                    dist = math.hypot(target[0] - f.x, target[1] - f.z)
                    if dist > ARRIVE_RADIUS:
                        stand_since = None
                        theta = math.atan2(target[1] - f.z, target[0] - f.x)
                        driver.push(theta)
                        driving = True
                        if last_drive_pos is not None:
                            moved = math.hypot(f.x - last_drive_pos[0],
                                               f.z - last_drive_pos[1])
                            no_move_pushes = (no_move_pushes + 1
                                              if moved < 50 else 0)
                        last_drive_pos = (f.x, f.z)
                        if no_move_pushes >= 5:
                            print("[drive] 摇杆连续 5 次未产生位移"
                                  "（设置面板打开/对局暂停？）→ 停止驱动，只采样")
                            driver.stop()
                            driver = None
                            driving = False
                    else:
                        if stand_since is None:
                            stand_since = time.time()
                        if driving:
                            driver.stop()      # 到位后松手，站着挨塔
                            driving = False
                        if time.time() - stand_since > a.stand_seconds:
                            target_idx = min(target_idx + 1, len(targets) - 1)
                            stand_since = None
                            print(f"[drive] 目标 {target_idx} 久未死亡，推进到 "
                                  f"{targets[target_idx]}")
            elif driver and (f.dead or tracker.phase != "alive"):
                driver.stop()

            time.sleep(max(0.0, 1.0 / a.hz - (time.time() - t0)))

    if driver:
        driver.stop()
    summary = {"pid": pid, "self_cid": a.self_cid, "auto_die": a.auto_die,
               "duration_s": a.duration, "out": str(out),
               **tracker.summary(), "last_frame": asdict(last) if last else None}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    print(f"summary={summary_path}")
    print(json.dumps({"phase": tracker.phase,
                      "events": tracker.events,
                      "verdict": tracker.verdict},
                     ensure_ascii=False, indent=2))
    return 0 if tracker.verdict else 1


if __name__ == "__main__":
    sys.exit(main())
