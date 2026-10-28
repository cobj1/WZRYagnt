# -*- coding: utf-8 -*-
"""官方动作码对照表守卫（`wzry/decision/official_ai/action_codes.py`）。

码表来自 `libGameCore.so @0x4a4a6b0` 的标签→码转换函数
（`reverse/dump_action_codes.py` 抽取，19 个码，见 `docs/动作码对照表-2026-10-28.md`）。

断言：
  T1 码表恰 19 项且码连续 1..19
  T2 规律式 `code = 6 + 4*(slot-1) + target_idx` 与码表逐项一致
  T3 LABEL_TO_CODE 是 CODE_TO_LABEL 的严格逆
  T4 split_code 对全部码可反解且 (target, slot) 与标签自洽
  T5 官方枚举**不含** AtkCrystalSkill* / AtkMonsterSkill*（实测无对应 strcmp 分支）
  T6 collapse() 把上述标签折叠为通用 AtkSkillN
  T7 我们的 `_label()` 能产出的所有标签，要么是官方标签、要么可折叠为官方标签
     —— 这条把"动作空间比官方多 6 项"这个已知缺口钉住，防止被误当成 bug 或误删

运行：venv/Scripts/python.exe -X utf8 scripts/test_action_codes.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.decision.official_ai import action_codes as AC          # noqa: E402
from wzry.decision.official_ai.actions import (                   # noqa: E402
    _label, T_HERO, T_SOLDIER, T_TOWER, T_CRYSTAL, T_MONSTER, T_NONE,
)

_PASS, _FAIL = [], []


def check(name, cond, extra=""):
    if cond:
        _PASS.append(name)
        print(f"PASS {name}")
    else:
        _FAIL.append(name)
        print(f"FAIL {name} {extra}")


def main() -> int:
    m = AC.CODE_TO_LABEL

    check("T1_code_table_is_1_to_19",
          len(m) == 19 and sorted(m) == list(range(1, 20)),
          f"n={len(m)} codes={sorted(m)}")

    # T2 规律式
    ok = True
    detail = []
    for slot in (1, 2, 3):
        for idx in (0, 1, 2, 3):
            code = 6 + 4 * (slot - 1) + idx
            want = f"Atk{AC.SKILL_TARGETS[idx]}Skill{slot}"
            if m.get(code) != want:
                ok = False
                detail.append(f"{code}:{m.get(code)}!={want}")
    check("T2_formula_matches_table", ok, "; ".join(detail[:4]))

    # T3 逆映射
    check("T3_label_to_code_inverse",
          len(AC.LABEL_TO_CODE) == 19
          and all(AC.LABEL_TO_CODE[v] == k for k, v in m.items()),
          f"n={len(AC.LABEL_TO_CODE)}")

    # T4 反解自洽
    bad = []
    for code, lab in m.items():
        sp = AC.split_code(code)
        if sp is None:
            bad.append(f"{code}:None")
            continue
        tk, slot = sp
        # 由 (tk, slot) 重新拼标签，必须回到原标签
        if slot == 0 and code in AC.TARGET_ONLY:
            expect = lab
        elif code == 19:
            expect = "AtkMonster"
        elif code == 18:
            expect = "AtkSkill4"
        else:
            expect = f"Atk{tk if tk != 'None' else ''}Skill{slot}"
        if expect != lab:
            bad.append(f"{code}:{lab}->{expect}")
    check("T4_split_code_roundtrip", not bad, "; ".join(bad[:4]))

    # T5 官方枚举不含按目标细分的 Crystal / Monster 技能
    absent = [f"Atk{t}Skill{s}" for t in ("Crystal", "Monster", "Norm") for s in (1, 2, 3)]
    check("T5_no_crystal_monster_skill_codes",
          not any(AC.is_official(x) for x in absent),
          f"意外存在：{[x for x in absent if AC.is_official(x)]}")

    # T6 折叠
    check("T6_collapse_to_generic_skill",
          AC.collapse("AtkCrystalSkill2") == "AtkSkill2"
          and AC.collapse("AtkMonsterSkill3") == "AtkSkill3"
          and AC.collapse("AtkHeroSkill1") == "AtkHeroSkill1"
          and AC.is_official(AC.collapse("AtkCrystalSkill1")),
          f"{AC.collapse('AtkCrystalSkill2')}/{AC.collapse('AtkMonsterSkill3')}")

    # T7 我们的标签生成器只产出"官方或可折叠"的标签
    gen = []
    for tk in (T_HERO, T_SOLDIER, T_TOWER, T_CRYSTAL, T_MONSTER, T_NONE):
        for slot in (0, 1, 2, 3, 4):
            gen.append((tk, slot, _label(tk, slot)))
    bad = [(tk, s, lab) for tk, s, lab in gen
           if not AC.is_official(lab) and not AC.is_official(AC.collapse(lab))]
    check("T7_generated_labels_official_or_collapsible", not bad,
          f"{bad[:4]}")

    # 统计已知缺口（不判失败，只打印）
    gap = sorted({lab for tk, s, lab in gen
                  if not AC.is_official(lab) and AC.is_official(AC.collapse(lab))})
    print(f"\n[已知缺口] 我们能产出但官方枚举没有的标签 {len(gap)} 个："
          + ", ".join(gap))
    print(f"          折叠后分别为：" + ", ".join(f"{g}->{AC.collapse(g)}" for g in gap))

    print(f"\n结果: {len(_PASS)} PASS / {len(_FAIL)} FAIL")
    if _FAIL:
        print("失败: " + ", ".join(_FAIL))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
