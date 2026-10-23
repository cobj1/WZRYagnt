# -*- coding: utf-8 -*-
"""运行时探针：CActorManager 单例 + actor 数组。

静态依据（libGameCore.so）：
  0x45aa11c  CActorManager 单例 getter
      ldr x0, [0x5273000 + 0x9e0]   <- 单例指针
  0x45aab64  CActorManager.UpdateLogic
      数组基址 = [mgr + 0x30]，元素步长 0x18，首 qword 为 actor*
      count = [mgr + 0x4c]（选择子 [mgr+0x70]==0）否则 [mgr+0x6c]
  0x45d69c4  battle 更新入口
      x20 = **(0x4ff4520)，[x20+0x1f0] = CAIActorMgr

用法：python probe_actors.py [--dump N]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from livemem import LiveMem  # noqa: E402

ACTOR_MGR_GLOBAL = 0x52739E0
BATTLE_GLOBAL = 0x4FF4520


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", type=int, default=3, help="dump 前 N 个 actor 的 0x200 字节")
    ap.add_argument("--serial", default="127.0.0.1:16384")
    a = ap.parse_args()

    m = LiveMem(serial=a.serial)
    pid = m.find_pid()
    if not pid:
        print("game not running")
        return 1
    n = m.refresh_maps()
    print(f"pid={pid} maps={n}")

    def u64(addr):
        b = m.read(addr, 8)
        return int.from_bytes(b, "little") if len(b) == 8 else None

    def u32(addr):
        b = m.read(addr, 4)
        return int.from_bytes(b, "little") if len(b) == 4 else None

    g = m.resolve("libGameCore.so", ACTOR_MGR_GLOBAL)
    print(f"actor_mgr_global  static=0x{ACTOR_MGR_GLOBAL:x} runtime=0x{g:x}")
    mgr = u64(g)
    print(f"CActorManager* = {hex(mgr) if mgr else None}")

    bg = m.resolve("libGameCore.so", BATTLE_GLOBAL)
    holder = u64(bg)
    battle = u64(holder) if holder else None
    print(f"battle_global runtime=0x{bg:x} holder=0x{holder:x} battle=0x{battle:x}"
          if holder and battle else f"battle_global runtime=0x{bg:x} holder={holder} battle={battle}")
    if battle:
        ai = u64(battle + 0x1F0)
        print(f"CAIActorMgr* = {hex(ai) if ai else None} (battle+0x1f0)")

    if not mgr:
        return 0

    base = u64(mgr + 0x30)
    cnt_a = u32(mgr + 0x4C)
    cnt_b = u32(mgr + 0x6C)
    sel = u32(mgr + 0x70)
    print(f"actor array base = {hex(base) if base else None}")
    print(f"counts: [+0x4c]={cnt_a} [+0x6c]={cnt_b} sel[+0x70]={sel}")
    cnt = cnt_b if sel else cnt_a
    if not base or not cnt:
        print("no actors (lobby?)")
        return 0
    print(f"-> {cnt} actors")
    for i in range(min(cnt, a.dump)):
        ent = base + i * 0x18
        raw = m.read(ent, 0x18)
        vals = [int.from_bytes(raw[j:j + 8], "little") for j in range(0, 24, 8)]
        actor = vals[0]
        print(f"[{i}] entry=0x{ent:x} q={[hex(v) for v in vals]}")
        if actor and 0x1000 < actor < 0x800000000000:
            head = m.read(actor, 0x200)
            print(f"    actor=0x{actor:x} head={head[:0x40].hex()}")
            # 打印 4 字节整数/浮点候选（前 0x200）
            import struct
            rows = []
            for off in range(0, len(head) - 4, 4):
                u = struct.unpack_from("<I", head, off)[0]
                f = struct.unpack_from("<f", head, off)[0]
                if 0.001 < abs(f) < 200000 and (f == f) and abs(f) not in (0.0,):
                    rows.append(f"+{off:03x} u32={u:<10} f32={f:.4f}")
            print("    " + "\n    ".join(rows[:80]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
