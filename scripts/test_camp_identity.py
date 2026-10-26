# -*- coding: utf-8 -*-
"""camp_identity 无设备回归：自我定位、镜像消歧、阵营推导、敌我划分。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision.camp_identity import (  # noqa: E402
    BLUE, RED, camp_id, camp_name, classify, enemy_camp,
    infer_camp_from_base, pick_self, resolve_self, split_teams,
)
from wzry.vision.mem_source import MemUnit  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def U(cid, camp, x, z, actor=0x1000, kind="hero"):
    return MemUnit(actor=actor, config_id=cid, actor_type=0 if kind == "hero" else 1,
                   camp=camp, kind=kind, x=x, z=z)


def D(cid, camp, x, z, kind="hero"):
    return {"cid": cid, "camp": camp, "x": x, "z": z, "kind": kind}


def main() -> int:
    check("camp_name accepts str/int", camp_name("Red") == RED
          and camp_name(1) == BLUE and camp_name(3) is None)
    check("camp_id roundtrip", camp_id("blue") == 1 and camp_id(2) == 2)
    check("enemy_camp flips", enemy_camp("blue") == "red"
          and enemy_camp("red") == "blue" and enemy_camp(None) is None)

    # 1) 唯一 cid：不该假设自己是蓝方
    units = [U(106, RED, 30000, 30000, 0x1000), U(107, BLUE, -30000, -30000, 0x2000)]
    r = resolve_self(units, 106)
    check("unique cid on red side stays red",
          r.ok and r.camp == RED and r.camp_src == "unit", str(r))

    # 2) camp 缺失时只信"站在泉水"，不用 x 符号猜
    units = [U(106, "", -52000, -44000, 0x1000)]
    r = resolve_self(units, 106)
    check("missing camp inferred from own spring",
          r.ok and r.camp == BLUE and r.camp_src == "unit_base", str(r))
    units = [U(106, "", -20000, -20000, 0x1000)]
    check("camp stays unknown away from spring",
          resolve_self(units, 106).camp is None)
    check("invader near enemy spring judged by position only",
          infer_camp_from_base((49000, 53000)) == RED
          and infer_camp_from_base((100, 5000)) is None)

    # 3) 镜像：同 cid 双阵营 + 自身坐标 -> 选最近者
    mirror = [U(106, BLUE, -10000, -10000, 0x1000),
              U(106, RED, 40000, 40000, 0x2000)]
    r = resolve_self(mirror, 106, self_pos=(40100, 39900))
    check("mirror disambiguated by self pos",
          r.ok and r.unit.actor == 0x2000 and r.camp == RED
          and r.match == "cid+pos", str(r))

    # 4) 镜像：无坐标 -> 必须报 ambiguous，不能默认蓝
    r = resolve_self(mirror, 106)
    check("mirror without pos is ambiguous",
          not r.ok and r.match == "ambiguous" and r.camp is None, str(r))

    # 5) 同阵营重复 cid（如傀儡）：即使无法定位个人，阵营仍可判定
    same = [U(106, BLUE, -10000, -10000, 0x1000),
            U(106, BLUE, -12000, -9000, 0x2000)]
    r = resolve_self(same, 106)
    check("same-camp duplicates give camp consensus",
          not r.ok and r.camp == BLUE and r.camp_src == "cid_consensus", str(r))

    # 6) self 坐标与所有候选都太远 -> 不猜
    r = resolve_self(mirror, 106, self_pos=(0, -59000))
    check("out-of-tolerance stays ambiguous",
          not r.ok and r.match == "ambiguous" and r.camp is None, str(r))

    # 7) 两个候选几乎等距 -> 不猜
    near = [U(106, BLUE, 0, 5000, 0x1000), U(106, RED, 0, 6000, 0x2000)]
    r = resolve_self(near, 106, self_pos=(59000, 5500))
    check("equidistant candidates stay ambiguous",
          not r.ok and r.camp is None, str(r))

    # 8) 自己不在 actor 表，但 self 坐标在己方泉水 -> 只推阵营
    r = resolve_self([U(107, RED, 1000, 1000)], 106, self_pos=(-52000, -44000))
    check("missing self infers camp from spring",
          not r.ok and r.match == "missing" and r.camp == BLUE
          and r.camp_src == "self_base", str(r))

    # 9) dict 输入同样可用
    r = resolve_self([D(106, "red", 30000, 30000)], 106)
    check("dict units supported",
          r.ok and r.camp == RED and classify(D(106, "red", 0, 0), r.camp) == "ally")

    # 10) 敌我划分
    allies, enemies, unknown = split_teams(
        [U(106, RED, 1, 1), U(107, RED, 2, 2), U(108, BLUE, 3, 3),
         U(109, "", 4, 4)], RED, heroes_only=True)
    check("split teams", len(allies) == 2 and len(enemies) == 1
          and len(unknown) == 1, f"{len(allies)}/{len(enemies)}/{len(unknown)}")
    check("classify never guesses without camp",
          classify(U(106, BLUE, 0, 0), None) == "unknown")

    # 11) pick_self 兼容非英雄过滤
    mixed = [U(106, BLUE, 0, 0, 0x1000, kind="minion")]
    check("hero filter honors kind",
          pick_self(mixed, 106)[1] == "missing"
          and pick_self(mixed, 106, heroes_only=False)[1] == "cid")

    # 12) 快照层接线：红方自己不再被当成蓝方
    from wzry.decision.official_ai.snapshot import from_state_dict

    def hero(cid, camp, x, z):
        return {"id": cid, "camp": camp, "x": x, "z": z, "hp": 100,
                "hp_max": 100, "level": 1}

    red_side = [hero(106, "red", 30000, 30000)] + [
        hero(cid, "red" if i < 4 else "blue", i * 1000, i * 1000)
        for i, cid in enumerate([107, 108, 109, 110, 111, 112, 113, 114, 115])]
    st = {"extra": {"mem_heroes": red_side}}
    snap = from_state_dict(st, self_id=106)
    check("snapshot keeps red self camp",
          snap.self_camp == 2 and snap.self_match == "cid",
          f"camp={snap.self_camp} match={snap.self_match}")

    # 同 cid 双实例 + self 坐标 -> 选红方那个
    mirror_state = {"extra": {"mem_heroes": [
        hero(106, "blue", -10000, -10000), hero(106, "red", 40000, 40000),
    ] + [hero(cid, "blue", -20000 - i, -20000) for i, cid in
         enumerate([107, 108, 109, 110, 111, 112, 113, 114], start=1)]},
        "self_state": {"x": 40100, "z": 39900}}
    snap = from_state_dict(mirror_state, self_id=106)
    check("snapshot disambiguates mirror self by pos",
          snap.self_camp == 2 and snap.self_match == "cid+pos",
          f"camp={snap.self_camp} match={snap.self_match}")

    # 同 cid 双实例、无坐标 -> 不猜
    mirror_state.pop("self_state")
    snap = from_state_dict(mirror_state, self_id=106)
    check("snapshot leaves ambiguous self unset",
          snap.self_unit is None and snap.self_match == "ambiguous",
          f"unit={snap.self_unit} match={snap.self_match}")

    # camp 缺失 + 站在泉水 -> 由位置判定，且标注来源
    spring_state = {"extra": {"mem_heroes": [
        hero(106, None, -52000, -44000)]}}
    snap = from_state_dict(spring_state, self_id=106)
    check("snapshot infers camp only at spring",
          snap.self_camp == 1 and snap.self_camp_src == "inferred_base",
          f"camp={snap.self_camp} src={snap.self_camp_src}")

    print()
    if FAILS:
        print(f"{len(FAILS)} failed: {FAILS}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
