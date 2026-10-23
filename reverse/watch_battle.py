# -*- coding: utf-8 -*-
"""对局探测：等战斗单例初始化后，自动找 CActorManager + 单位列表。

判据（静态依据见 docs/wzry-ai-reverse.md / 本轮分析）：
  battle = **(0x4ff4520)
  [battle + 0x1f0] = CAIActorMgr（非空即进入战斗）
  CActorManager: +0x30 数组基址, +0x4c/+0x6c 计数, +0x70 选择子, 元素步长 0x18
  元素首 qword = actor*, actor->vtable[0x98] 指向 libGameCore .text

真实对局里 actor 的 vtable 应当**不止一种**（英雄/小兵/塔/野怪）。
英雄选择界面的伪候选所有 vtable 相同，用这一点排除。

用法：python watch_battle.py [--timeout 900]
输出：out/mem/battle_probe.txt（同时打印）
"""
import argparse
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from livemem import LiveMem  # noqa: E402

BATTLE_GLOBAL = 0x4FF4520
GAME_TEXT = (0x400036A00000, 0x40003BC6F000)
OUT = Path(__file__).resolve().parent / "out" / "mem" / "battle_probe.txt"


class Probe:
    def __init__(self, serial="127.0.0.1:16384"):
        self.m = LiveMem(serial=serial)
        self.logs = []

    def log(self, s):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {s}"
        print(line, flush=True)
        self.logs.append(line)

    def region(self, a):
        for st, en, _off, p in self.m.maps:
            if st <= a < en:
                return (st, en, p)
        return None

    def u64(self, a):
        if not self.region(a):
            return None
        b = self.m.read(a, 8)
        return int.from_bytes(b, "little") if len(b) == 8 else None

    def u32(self, a):
        if not self.region(a):
            return None
        b = self.m.read(a, 4)
        return int.from_bytes(b, "little") if len(b) == 4 else None

    def chase(self):
        bg = self.m.resolve("libGameCore.so", BATTLE_GLOBAL)
        a = self.u64(bg)
        b = self.u64(a) if a else None
        return b

    def actor_ok(self, actor):
        if not actor or not self.region(actor):
            return None
        vt = self.u64(actor)
        if not vt or not self.region(vt):
            return None
        fn = self.u64(vt + 0x98)
        if fn and GAME_TEXT[0] <= fn < GAME_TEXT[1]:
            return vt
        return None

    def probe_manager(self, p):
        """返回 (count, array, vtable_set, actors) 或 None。"""
        if not p or not self.region(p):
            return None
        h = self.m.read(p, 0x80)
        if len(h) < 0x80:
            return None
        arr = struct.unpack_from("<Q", h, 0x30)[0]
        c4c, c6c, _ = struct.unpack_from("<III", h, 0x4C)
        c70 = struct.unpack_from("<I", h, 0x70)[0]
        cnt = (c6c if c70 else c4c)
        if not arr or not self.region(arr) or not (0 < cnt < 3000):
            return None
        vts, actors = set(), []
        n = min(cnt, 12)
        raw = self.m.read(arr, n * 0x18)
        if len(raw) < n * 0x18:
            return None
        for i in range(n):
            actor = struct.unpack_from("<Q", raw, i * 0x18)[0]
            vt = self.actor_ok(actor)
            if not vt:
                return None
            vts.add(vt)
            actors.append(actor)
        if len(vts) < 2 and cnt > 3:
            return None   # 单一 vtable：多半是选人/展示场景的对象池
        return cnt, arr, vts, actors

    def dump_unit(self, actor, label=""):
        head = self.m.read(actor, 0x200)
        if len(head) < 0x200:
            return
        vt = int.from_bytes(head[:8], "little")
        ints = []
        for off in range(0, len(head) - 4, 4):
            u = struct.unpack_from("<I", head, off)[0]
            f = struct.unpack_from("<f", head, off)[0]
            if u and (0 < u < 200000):
                ints.append((off, u, f))
        self.log(f"  {label} actor=0x{actor:x} vt=0x{vt:x}")
        for off, u, f in ints[:60]:
            fs = f"{f:.3f}" if 0.001 < abs(f) < 200000 else ""
            self.log(f"      +0x{off:03x} u32={u:<8} f32={fs}")

    def run(self, timeout=900):
        self.m.find_pid()
        self.m.refresh_maps()
        self.log(f"pid={self.m.pid} maps={len(self.m.maps)}")
        found = False
        t0 = time.time()
        last_note = 0
        while time.time() - t0 < timeout:
            battle = self.chase()
            if battle:
                ai = self.u64(battle + 0x1F0)
                ai_ok = bool(ai and ai > 0x1000000000 and self.region(ai)
                             and not self.region(ai)[2].endswith(".so"))
                if not ai_ok:
                    if time.time() - last_note > 30:
                        self.log(f"waiting battle... ai={hex(ai) if ai else None}")
                        last_note = time.time()
                    time.sleep(2)
                    continue
                self.log(f"BATTLE ACTIVE battle=0x{battle:x} ai=0x{ai:x} "
                         f"region={self.region(ai)[0]:x}-{self.region(ai)[1]:x}")
                self.dump_head(ai, 0x200)
                # 扫描 battle 对象里的指针（战斗对象可能很大，扫 0x4000）
                for base_name, base, size in (("battle", battle, 0x4000),
                                              ("ai", ai, 0x400)):
                    if not self.region(base):
                        continue
                    raw = self.m.read(base, size)
                    if len(raw) < size:
                        continue
                    for off in range(0, len(raw) - 8, 8):
                        v = struct.unpack_from("<Q", raw, off)[0]
                        if not v or not self.region(v):
                            continue
                        r = self.probe_manager(v)
                        if not r:
                            continue
                        cnt, arr, vts, actors = r
                        self.log(f"*** MGR {base_name}+0x{off:x} -> 0x{v:x} "
                                 f"count={cnt} arr=0x{arr:x} vtables={[hex(x) for x in vts]}")
                        for i, a in enumerate(actors[:10]):
                            self.dump_unit(a, f"unit[{i}]")
                        found = True
                if not found:
                    # 兜底：扫 libGameCore 之后的 .bss 匿名区，找 actor 数组特征
                    segs = self.m.segments("libGameCore.so")
                    if segs:
                        st = max(s[1] for s in segs)
                        region = self.region(st + 0x1000)
                        if region:
                            self.log(f"fallback scan .bss {region[0]:x}-{region[1]:x}")
                            addr, CH = region[0], 0x200000
                            while addr < region[1] and not found:
                                raw = self.m.read(addr, min(CH, region[1] - addr))
                                for off in range(0, len(raw) - 8, 8):
                                    v = struct.unpack_from("<Q", raw, off)[0]
                                    if v <= 0x1000000000 or not self.region(v):
                                        continue
                                    r = self.probe_manager(v)
                                    if r and r[0] >= 5:
                                        cnt, arr, vts, actors = r
                                        self.log(f"*** MGR bss+0x{addr + off - region[0]:x} "
                                                 f"-> 0x{v:x} count={cnt} arr=0x{arr:x} "
                                                 f"vtables={[hex(x) for x in vts]}")
                                        for i, a in enumerate(actors[:10]):
                                            self.dump_unit(a, f"unit[{i}]")
                                        found = True
                                        break
                                addr += CH
                if found:
                    OUT.write_text("\n".join(self.logs), encoding="utf-8", newline="\n")
                    self.log("done (hit). saved.")
                    return 0
            time.sleep(2)
        OUT.write_text("\n".join(self.logs), encoding="utf-8", newline="\n")
        self.log("timeout, no match manager found")
        return 1

    def dump_head(self, p, n):
        b = self.m.read(p, n)
        for i in range(0, len(b) - 8, 8):
            v = struct.unpack_from("<Q", b, i)[0]
            if v and self.region(v):
                self.log(f"    ai+0x{i:03x} = 0x{v:x}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--serial", default="127.0.0.1:16384")
    a = ap.parse_args()
    raise SystemExit(Probe(a.serial).run(a.timeout))
