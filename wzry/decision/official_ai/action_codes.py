# -*- coding: utf-8 -*-
"""官方动作码 ↔ 双手动作标签的对照表（`evidence=disasm`）。

来源：`libGameCore.so` 的标签→码转换函数 `0x4a4a6b0`（`reverse/dump_action_codes.py`）。
该函数按**串长**分派（入口处 `sub x8, x8, #7; cmp x8, #9; b.hi` → 只接受长度 7..16），
组内按长度递增做 `strncmp`/`strcmp` 前缀匹配，命中后 `mov w8, #<code>`
并落到公共出口 `str w8, [x20, #0x1c]`。

    code 1..5   : 目标手（无技能槽）  AtkNorm / AtkHero / AtkSoldier / AtkTower / AtkCrystal
    code 6..17  : 技能槽 1..3 × 四类目标
                  slot1: 6 AtkSkill1 / 7 AtkHeroSkill1 / 8 AtkSoldierSkill1 / 9 AtkTowerSkill1
                  slot2: 10..13（同序）      slot3: 14..17（同序）
    code 18     : AtkSkill4（只有通用，无按目标细分）
    code 19     : AtkMonster（只有通用，无技能槽细分）

**规律**：`code = 6 + 4*(slot-1) + target_idx`，target_idx = Hero 1 / Soldier 2 / Tower 3；
无目标时为 `6 + 4*(slot-1)`。

未覆盖（官方枚举里**不存在**，实测无对应 strcmp 分支）：
`AtkCrystalSkill1..3`、`AtkMonsterSkill1..3`、`AtkNormSkill1..3`。
即「打水晶/野怪用技能」在官方标签体系里没有独立码，只能落到通用 `AtkSkillN`。

`NoneAction` 的码**未验证**：该函数不比较这个串，推测为结构体默认 0，但无指令级证据。
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

# 目标手（无技能槽）
TARGET_ONLY: Dict[int, str] = {
    1: "AtkNorm",
    2: "AtkHero",
    3: "AtkSoldier",
    4: "AtkTower",
    5: "AtkCrystal",
}

# 技能槽 1..3 × 目标（target_idx: 0=通用, 1=Hero, 2=Soldier, 3=Tower）
SKILL_TARGETS = {0: "", 1: "Hero", 2: "Soldier", 3: "Tower"}

# 尾部的非规律项
TAIL: Dict[int, str] = {
    18: "AtkSkill4",
    19: "AtkMonster",
}

CODE_TO_LABEL: Dict[int, str] = dict(TARGET_ONLY)
for _slot in (1, 2, 3):
    for _idx, _tk in SKILL_TARGETS.items():
        CODE_TO_LABEL[6 + 4 * (_slot - 1) + _idx] = f"Atk{_tk}Skill{_slot}"
CODE_TO_LABEL.update(TAIL)

LABEL_TO_CODE: Dict[str, int] = {v: k for k, v in CODE_TO_LABEL.items()}

# NoneAction：**标签本身是官方的**（`[UnpredictProcess]` 与多处日志都出现），
# 但它不在上面那个标签→码的转换函数里，所以**码未验证**（推测为结构体默认 0）。
NONE_ACTION = "NoneAction"
NONE_ACTION_CODE_UNVERIFIED = 0


def code_of(label: str) -> Optional[int]:
    """标签 → 官方动作码。不在官方枚举内、或码未验证（NoneAction）返回 None。"""
    return LABEL_TO_CODE.get(label)


def label_of_code(code: int) -> Optional[str]:
    """官方动作码 → 标签。未知码返回 None。"""
    return CODE_TO_LABEL.get(code)


def official_labels() -> Tuple[str, ...]:
    return tuple(sorted(LABEL_TO_CODE, key=LABEL_TO_CODE.get))


def is_official(label: str) -> bool:
    """该标签是否存在于官方双手动作枚举（含码未验证的 NoneAction）。"""
    return label in LABEL_TO_CODE or label == NONE_ACTION


def collapse(label: str) -> str:
    """把官方枚举里没有的标签折叠到等价的官方标签。

    官方**没有**按目标细分的情况只有两种：
      ① 技能槽 4 只有通用 `AtkSkill4`（18）→ `Atk{Hero,Soldier,Tower,Crystal,Monster}Skill4`
      ② 水晶/野怪/无目标不细分技能槽 → `Atk{Crystal,Monster,Norm}SkillN` → `AtkSkillN`
    其余原样返回。
    """
    if is_official(label):
        return label
    # ① 技能槽 4 折叠为通用
    if label.endswith("Skill4") and label.startswith("Atk") and label != "AtkSkill4":
        return "AtkSkill4"
    # ② 目标类型不细分 → 通用技能
    for prefix in ("AtkCrystalSkill", "AtkMonsterSkill", "AtkNormSkill"):
        if label.startswith(prefix):
            slot = label[len(prefix):]
            generic = f"AtkSkill{slot}"
            if is_official(generic):
                return generic
    return label


def split_code(code: int) -> Optional[Tuple[str, int]]:
    """动作码 → (目标类型, 技能槽)。目标类型用 `actions.py` 的英文名（Hero/Soldier/...）。"""
    lab = CODE_TO_LABEL.get(code)
    if lab is None:
        return None
    if code in TARGET_ONLY:
        return ({"AtkNorm": "None", "AtkHero": "Hero", "AtkSoldier": "Soldier",
                 "AtkTower": "Tower", "AtkCrystal": "Crystal"}[lab], 0)
    if code == 19:
        return ("Monster", 0)
    if code == 18:
        return ("None", 4)
    # 6..17：反解 slot / target
    rel = code - 6
    slot = rel // 4 + 1
    idx = rel % 4
    tk = SKILL_TARGETS[idx] or "None"
    return (tk, slot)
