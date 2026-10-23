# -*- coding: utf-8 -*-
"""22 具名建筑槽位的校验工具。

两种用法：

  1) 静态自检（无需设备，CI 可跑）
     venv/Scripts/python.exe -X utf8 scripts/verify_organ_slots.py

  2) 真机校验（需要一局对局 + adb root + memscan，**只读**）
     venv/Scripts/python.exe -X utf8 scripts/verify_organ_slots.py --live --serial 192.168.0.103:5555

判定标准（真机）：
  - `slots=22/22`：22 个槽位全部有单位（塔未被推掉时应当如此）
  - 没有 `conflict:` 条目：同一槽位没出现两个单位
  - 没有 `far:` 条目：所有建筑都落在已知拓扑上（否则说明世界坐标标定漂了）
  - 打印每个槽位的单位 cid / 血量 / 到标定点的归一化误差

如果出现 `far:`，先跑 `scripts/calibrate_minimap_box.py` 复核世界坐标范围，
再调 `official_ai/geo.py` 的 MAP_X/Z 常量（唯一口径），不要在各处分别打补丁。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.decision.official_ai import geo, organ_slots as O   # noqa: E402
from wzry.decision.official_ai import schema as S             # noqa: E402
from wzry.decision.official_ai.snapshot import from_state_dict  # noqa: E402


def static() -> int:
    sc = O.self_check()
    print("静态自检:", sc)
    print(f"槽位表 {len(O.table(1))} 项；与 schema.ORGAN_SLOTS 一致: "
          f"{tuple(O.SLOT_ORDER) == tuple(S.ORGAN_SLOTS)}")
    for name, (nx, ny) in O.table(1).items():
        print(f"  {name:<28} norm=({nx:.3f},{ny:.3f})  world={geo.norm_to_world(nx, ny)}")
    return 0 if sc["ok"] else 1


def anchors(serial: str) -> int:
    """打印"建筑单位的真实世界坐标"，并与 22 槽位静态几何对照。

    为什么要单独做：`actor+0x278` 是**出生/编队点**，对建筑与野怪而言就是实时位置
    （它们不动）。有了真值，才能判断 `organ_geo` 那张静态表是否在同一坐标系里。
    """
    import struct
    from wzry.vision.mem_source import MemSource
    from wzry.decision.official_ai import geo, organ_slots as OS

    src = MemSource(serial=serial)
    print("pid =", src.connect())
    units = src.snapshot()
    if not units:
        print("没有单位 —— 需要在对局中")
        return 1

    tab = OS.table(1)
    print(f"{'cid':<7}{'camp':<6}{'world(+0x278)':<22}{'最近槽位':<30}{'偏差':<8}槽位世界坐标")
    for u in sorted(units, key=lambda z: (z.camp, z.config_id)):
        if u.kind != "unit":
            continue
        raw = src.m.read(u.actor, 0x2A0)
        x, y, z = struct.unpack_from("<3i", raw, 0x278)
        if x == 0 and z == 0:
            continue
        best, bd = None, 1e18
        for name, (nx, ny) in tab.items():
            wx, wz = geo.norm_to_world(nx, ny)
            d = ((wx - x) ** 2 + (wz - z) ** 2) ** 0.5
            if d < bd:
                best, bd = (name, wx, wz), d
        print(f"{u.config_id:<7}{str(u.camp):<6}{str((x, z)):<22}"
              f"{best[0]:<30}{bd:<8.0f}({best[1]:.0f},{best[2]:.0f})")
    print("\n判读：偏差普遍 >1500 说明静态槽位表与真机世界坐标不在同一坐标系"
          "（需要按真值重标定或直接用 +0x278）。")
    return 0


def live(serial: str, self_id: int) -> int:
    from wzry.vision.mem_battle import MemBattle
    mb = MemBattle(serial=serial)
    print("pid =", mb.connect())
    state = {"ui": {}, "extra": {}}
    summary = mb.patch_state(state, self_id=self_id)
    if not summary.get("n_units"):
        print("未读到单位：可能不在对局中")
        return 1
    snap = from_state_dict(
        state, self_id=self_id or None,
        atk_range=float((state.get("ui") or {}).get("atk_range") or 650.0))
    print(f"建筑总数={len(snap.organs)}  camp={snap.self_camp}  "
          f"几何槽位={len(snap.organ_geo)}/22  活体槽位={len(snap.organ_slots)}/22  "
          f"视觉存活={len(snap.organ_alive)}/22")
    # 建筑是否有坐标：决定"活体槽位"能不能用
    with_pos = [o for o in snap.organs if o.x is not None]
    print(f"带坐标的建筑={len(with_pos)}/{len(snap.organs)}"
          + ("   ← 建筑 pos 组件为空，活体槽位无法分配" if not with_pos and snap.organs else ""))
    for note in snap.organ_notes[:6]:
        print("  !", note)
    for name in O.SLOT_ORDER:
        u = snap.organ_slots.get(name)
        g = snap.organ_geo.get(name)
        al = snap.organ_alive.get(name)
        if u is not None and u.x is not None:
            nx, ny = geo.world_to_norm(u.x, u.z)
            ref = O.table(snap.self_camp or 1)[name]
            print(f"  {name:<28} live cid={u.cid:<4} hp={u.hp}/{u.hp_max} "
                  f"alive={u.alive} err={geo.dist_norm((nx, ny), ref):.3f}")
        elif g is not None:
            nx, ny = geo.world_to_norm(*g)
            ref = O.table(snap.self_camp or 1)[name]
            print(f"  {name:<28} geo  world=({g[0]:.0f},{g[1]:.0f}) "
                  f"alive={'-' if al is None else al} err={geo.dist_norm((nx, ny), ref):.3f}")
        else:
            print(f"  {name:<28} -")
    bad = [n for n in snap.organ_notes if n.startswith(("conflict:", "far:"))]
    geo_ok = len(snap.organ_geo) == 22 and not bad
    live_ok = len(snap.organ_slots) >= 20
    print(f"\n结论: 几何 {'PASS' if geo_ok else 'CHECK'} / "
          f"活体 {'PASS' if live_ok else 'BLOCKED(建筑 pos 组件为空)'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--anchors", action="store_true",
                    help="打印建筑真实坐标并与 22 槽位表对照")
    ap.add_argument("--serial", default="127.0.0.1:16384")
    ap.add_argument("--self-id", type=int, default=0)
    a = ap.parse_args()
    if a.anchors:
        return anchors(a.serial)
    return live(a.serial, a.self_id) if a.live else static()


if __name__ == "__main__":
    raise SystemExit(main())
