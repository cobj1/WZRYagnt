# -*- coding: utf-8 -*-
"""直接内存感知源（实验）：从 libGameCore 的 CActorManager 读取单位身份。

已实测可读（见 docs/wzry-ai-reverse.md §13.10 / reverse/out/mem/ACTOR_FINDINGS.md）：
  CActorManager* = *( *( *(*(0x4ff4520)) ) + 0x388 )
  数组 [mgr+0x30]，元素步长 0x18，首 qword = actor*
  actor + 0x28 = 配置 ID（英雄 ID / 小兵 1690-1699 / 塔 92,95 / 野怪 16100）
  actor + 0x30 = 类型（0=英雄, 2=小兵/塔/野怪, 1=其他）
  actor + 0x34 = 阵营（1=蓝, 2=红）
  actor + 0x1d0 -> attr 组件：+0xc0 = 最大 HP，+0xc8 = 当前 HP
  actor + 0x230 -> pos  组件：+0x128 = x，+0x12c = y，+0x130 = z

世界坐标有符号：蓝方为负、红方为正，量级 ±60000。
actor + 0x2a0 起的那块是**静态配置**，不要当实时状态用。

全英雄金币（无需打开战绩面板）：
  连续 10 个 C# 状态对象，stride 0x2AD8；+0x16C=英雄 ID，+0x200=当前金币。
  首次用 memscan idbag 定位（scudo-only，约 5s），之后每帧读一块 ~110KB。
  read_hero_states() / read_hero_gold()；to_game_state() 写入 ui["hero_gold"]。

用法：
  python -m wzry.vision.mem_source            # 读一帧并打印
  python -m wzry.vision.mem_source --loop 5   # 连续 5 帧 + 耗时
  python reverse/all_hero_gold.py             # 10 名英雄金币/等级/HP
"""
from __future__ import annotations

import argparse
import re
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "reverse"))

from livemem import DEFAULT_ADB, LiveMem  # noqa: E402

BATTLE_GLOBAL = 0x4FF4520
ACTOR_MGR_OFF = 0x388
ACTOR_HDR = 0x40
ACTOR_ATTR_OFF = 0x1D0
ACTOR_POS_OFF = 0x230
ACTOR_SPAWN_OFF = 0x278      # 出生/编队点 (i32 x, y, z)；建筑与野怪即实时位置

ATTR_MAX_HP = 0xC0
ATTR_HP = 0xC8
POS_X = 0x128
POS_Y = 0x12C
POS_Z = 0x130

# 本地英雄 C# 状态对象（2026-10-20 实测，见 reverse/out/mem/ACTOR_FINDINGS.md §5g）
# 对象不挂在 actor 组件图上，首次需用 memscan herostate 按 ID+坐标定位。
SELF_OBJ_ID = 0x16C
SELF_OBJ_LEVEL = 0x19C
SELF_OBJ_HP = 0x1A4
SELF_OBJ_HP_MAX = 0x1A8
# ⚠️ 2026-10-20 二次定性：+0x1A0 **不是法力上限**，而是**攻击距离 × 10**。
#   证据：10 个英雄逐一吻合 —— 106 小乔 6500(650)、121 孙尚香 6000(600)、
#   107 兰陵王 3000(300)、167 亚瑟 3000(300)、187 甄姬 6500(650)、
#   173 干将莫邪 7500(750)、174 女娲 8250(825)、105 廉颇 2800(280)。
#   且与等级无关（与"射程是英雄静态属性"一致），旧名 SELF_OBJ_MP 属误标。
SELF_OBJ_ATK_RANGE = 0x1A0
SELF_OBJ_MP = SELF_OBJ_ATK_RANGE        # 兼容旧名（语义已废弃，勿再当法力用）
ATK_RANGE_SCALE = 0.1                   # 记录里是 1/10 单位

# 法力（2026-10-21 施法差分实测）：+0x1C4 当前值、+0x1C8 上限。
#   证据：小乔施放 1 技能后 868 -> 838（-30），随后 838 -> 842 -> 850（约 +3/s 回复），
#   同时技能槽 1 的 cd_left_ms 0 -> 3152 起跳 —— 施法确实生效，0x1C4 是唯一"消耗后回复"的量。
#   ⚠️ 绝对值的单位/口径未与面板对账（小乔面板法力高于 868），
#   所以**决策层只用比值 `ep_rate`**，不要用绝对值。
EP_CUR = 0x1C4
EP_MAX = 0x1C8

# 复活倒计时（2026-10-21 实测）：+0x15D4，单位**秒**，每秒减 1。
#   证据：英雄 110 血量归零的那一帧该字段=59，随后 59→58→…；存活时恒为 0。
#   官方 RuleTactics::CheckReturnCity 的分支 `REVIVE_DIS_NOT_RETURN_CITY` 用的就是它。
RESPAWN_LEFT = 0x15D4
# 装备列表（2026-10-21 实测，vector 结构）：`+0x1920` 是 vector 头 [0,0,count]，
#   ID 从 `+0x192C` 起，stride 4（u32）。
#   证据（10 名英雄同帧比对）：
#     - count 8~12，与 `gold_spent` 正相关（17200→8，27830→11）；
#     - 每名英雄的 ID 序列互不相同，但 1337（鞋类）几乎人人都有；
#     - 我们的小乔（106）：1424,1233,1232,1231,1240,1238,1337,1242，符合出装路径。
#   ⚠️ 语义是"**已购买的装备（含合成小件）**"，不等于 6 个成品槽；ID→名称需自建映射
#   （`equip_config_id_file` 不随包）。"买装备后新增 ID 落在下一个空位"仍待一次实测确认。
SELF_EQUIP_COUNT = 0x1928
SELF_EQUIP_IDS = 0x192C
SELF_EQUIP_STRIDE = 4
SELF_EQUIP_MAX = 16
SELF_OBJ_X = 0x184
SELF_OBJ_Y = 0x188
SELF_OBJ_Z = 0x18C
SELF_OBJ_EXP = 0x1BC
SELF_OBJ_KILLS = 0x1F4
SELF_OBJ_DEATHS = 0x1F8
SELF_OBJ_ASSISTS = 0x1FC
SELF_OBJ_GOLD = 0x200
# v16.3 语义修正（2026-10-20 真机对账）：`+0x200` 是**总经济（累计获得金币）**，
#   不是可用金币。可用金币（= HUD 右上角那个数）在 `+0x15D0`。
#   证据：同一时刻 HUD 945 / `+0x200`=4772 / `+0x15D0`=948；差值 3824 = 已花费，
#   且连续 3 次采样 `+0x15D0` 始终只比 HUD 多 3~6（截图延迟 0.5~1.5s 的收入）。
#   旧结论"买装备会下降"是错的 —— `+0x200` 从不下降，降的是 `+0x15D0`。
SELF_OBJ_GOLD_AVAIL = 0x15D0
SELF_OBJ_END = 0x15D4
# 技能冷却槽（2026-10-20 实测，见 ACTOR_FINDINGS.md §5i）：
#   3 个槽，stride 0x78，槽内：+0x08 技能ID(10610/10620/10630)、
#   +0x0C 技能等级(0=未学)、+0x10 状态位域（**bit0 = 可用**，其余位含义未定，
#   实测出现过 0x500/0x501）、+0x14 剩余毫秒、+0x18 总CD毫秒。
#   验证：大招施法后 ult 槽 0->32558/35000，冷却结束回 0；
#   1技能施法后 s1 槽 0->4472/5000，仅该槽变化。
#   ⚠️ 2026-10-20 二次实测修正：+0x10 不是 0/1 布尔，而是位域（0/1/1280/1281），
#   旧校验 `state in (0,1)` 会把 6/10 个英雄的整组技能丢弃 —— 现改为 bit0 判读。
SELF_SKILL_BASE = 0x19D4
SKILL_SLOT_STRIDE = 0x78
# 槽位实测（2026-10-20 深夜，训练营连续施放验证）：
#   1..3 = 英雄技能；4 = 空槽；5 = 恢复(90003, 60s)；6 = 召唤师技能(80xxx,
#   闪现 80115/120s)；7 = 回城(90005, 无CD，state bit0=可用，吟唱中为0)。
#   施放证据：闪现后槽6 0→119076/120000 递减；恢复后槽5 0→59076/60000；
#   点回城后槽7 state 1→0；召唤师/恢复的剩余CD与英雄技能同一套字段。
SKILL_SLOT_COUNT = 7
SKILL_OFF_ID = 0x08
SKILL_OFF_LEVEL = 0x0C
SKILL_OFF_STATE = 0x10
SKILL_OFF_READY = SKILL_OFF_STATE      # 兼容旧名
SKILL_OFF_LEFT_MS = 0x14
SKILL_OFF_TOTAL_MS = 0x18
SKILL_ID_HEAL = 90003
SKILL_ID_RECALL = 90005
SUMMONER_ID_LO = 80_000
SUMMONER_ID_HI = 90_000
SELF_OBJ_SKILL_END = SELF_SKILL_BASE + SKILL_SLOT_STRIDE * SKILL_SLOT_COUNT
MEMSCAN = "/data/local/tmp/memscan"

# 全英雄 C# 状态数组（2026-10-20 实测，无需打开战绩面板）：
#   连续 10 个对象，stride 0x2AD8；每个对象与上面的本地状态对象同布局。
#   +0x200 = 当前金币（与 HUD/战绩面板“经济”列一致，会随手买装备下降）。
# 地址每局变：用 actor 表里的 10 个英雄 ID 跑 `memscan idbag` 定位。
HERO_ARR_STRIDE = 0x2AD8
HERO_ARR_COUNT = 10
HERO_ARR_MIN_VALID = 8

TYPE_HERO = 0
TYPE_OTHER = 1
TYPE_UNIT = 2

CAMP_BLUE = 1
CAMP_RED = 2

# 世界坐标 -> 小地图归一化坐标的标定（近似）。
# 依据：蓝方出生点 (-56379,-53367) ≈ 小地图 (0.02,0.98)，
#       红方出生点 ( 55757, 57652) ≈ 小地图 (0.98,0.02)。
# 精确到像素仍需一局双方散开时用小地图色点做最小二乘对账。
MAP_X_MIN, MAP_X_MAX = -58700, 58100
MAP_Z_MIN, MAP_Z_MAX = -55700, 60000
MAP_HALF = 60000  # 兼容旧调用

_CLS = {TYPE_HERO: "hero", TYPE_UNIT: "unit", TYPE_OTHER: "other"}


def stats_at_level(s: dict, level: int) -> dict:
    """基础属性块 + 等级 → 当前基础属性（不含装备/增益）。"""
    n = max(0, int(level) - 1)
    return {
        "hp": s["hp_base"] + s["hp_growth"] * n,
        "mp": s["mp_base"] + s["mp_growth"] * n,
        "ad": s["ad_base"] + s["ad_growth"] * n,
        "armor": s["armor_base"] + s["armor_growth"] * n,
        "mr": s["mr_base"] + s["mr_growth"] * n,
        "as": s["as_base"] + s["as_growth"] * n,
        "hp_regen": s["hp_regen_base"] + s["hp_regen_growth"] * n,
        "mp_regen": s["mp_regen_base"] + s["mp_regen_growth"] * n,
    }


@dataclass
class MemUnit:
    actor: int
    config_id: int
    actor_type: int
    camp: str
    kind: str
    x: Optional[int] = None
    y: Optional[int] = None
    z: Optional[int] = None
    # 出生/编队点（actor+0x278）。建筑/野怪静止，出生点即当前位置；
    # 英雄与移动单位这里是旧值，**不能**当实时坐标用。
    spawn_x: Optional[int] = None
    spawn_y: Optional[int] = None
    spawn_z: Optional[int] = None
    hp: Optional[int] = None
    hp_max: Optional[int] = None
    dead: bool = False
    extra: Dict[str, int] = field(default_factory=dict)

    @property
    def is_hero(self) -> bool:
        return self.actor_type == TYPE_HERO

    def minimap_norm(self) -> Optional[List[float]]:
        """世界坐标 -> 小地图归一化坐标（近似，x 向右、z 向上）。"""
        if self.x is None or self.z is None:
            return None
        return [(self.x - MAP_X_MIN) / (MAP_X_MAX - MAP_X_MIN),
                (MAP_Z_MAX - self.z) / (MAP_Z_MAX - MAP_Z_MIN)]


class MemSource:
    def __init__(self, adb: Optional[str] = None, serial: str = "127.0.0.1:16384"):
        self.m = LiveMem(adb=adb or DEFAULT_ADB, serial=serial)
        self.pid = None
        self._cache = None
        self._self_state_obj = None
        self._hero_arrays: List[int] = []   # 数组对象基址（同帧有 2 份副本）
        self._hero_array_pid = None
        self._hero_camp: Dict[int, str] = {}
        self._hero_locate_fail_ts = 0.0     # 定位失败后的退避时间
        self._hero_spent: Dict[int, int] = {}    # 累计买装备花费（差分推导）
        self._hero_gold_prev: Dict[int, int] = {}
        self._hero_match_key: Optional[frozenset] = None

    def connect(self) -> str:
        self.pid = self.m.find_pid()
        if not self.pid:
            raise RuntimeError("game process not found")
        self.m.refresh_maps()
        return self.pid

    # ---------- 基础读 ----------
    def _region(self, addr: int):
        for st, en, _off, p in self.m.maps:
            if st <= addr < en:
                return (st, en, p)
        return None

    def _u64(self, addr: int) -> Optional[int]:
        if not self._region(addr):
            return None
        b = self.m.read(addr, 8)
        return int.from_bytes(b, "little") if len(b) == 8 else None

    def _u32(self, addr: int) -> Optional[int]:
        if not self._region(addr):
            return None
        b = self.m.read(addr, 4)
        return int.from_bytes(b, "little") if len(b) == 4 else None

    # ---------- 本地英雄 C# 状态对象（金币/等级） ----------
    def _locate_self_state(self, config_id: int, x: int, z: int) -> Optional[int]:
        """首次定位：memscan herostate 按 ID+坐标找到对象，缓存地址。"""
        if not self.pid:
            return None
        cmd = (f"MEMSCAN_ONLY=scudo {MEMSCAN} {self.pid} "
               f"herostate {config_id} {int(x)} {int(z)}")
        try:
            out = self.m._run(["shell", cmd]).decode("utf-8", "replace")
        except Exception as exc:          # 设备端工具缺失/扫描失败
            print(f"[mem_source] herostate locate failed: {exc}", file=sys.stderr)
            return None
        objs = [int(v, 16) for v in re.findall(r"obj=0x([0-9a-f]+)", out)]
        if not objs:
            return None
        self._self_state_obj = objs[0]    # 同帧多副本值一致，取首个
        return self._self_state_obj

    @staticmethod
    def _parse_self_state(raw: bytes, obj: int) -> Optional[dict]:
        need = SELF_OBJ_SKILL_END - SELF_OBJ_ID
        if len(raw) < need:
            return None

        def i32(off):
            return struct.unpack_from("<i", raw, off - SELF_OBJ_ID)[0]

        d = {
            "obj": obj,
            "id": i32(SELF_OBJ_ID),
            "level": i32(SELF_OBJ_LEVEL),
            # gold = **可用金币**（HUD 口径）；gold_earned = 总经济（累计）
            "gold": i32(SELF_OBJ_GOLD_AVAIL),
            "gold_earned": i32(SELF_OBJ_GOLD),
            "hp": i32(SELF_OBJ_HP),
            "hp_max": i32(SELF_OBJ_HP_MAX),
            "atk_range": round(i32(SELF_OBJ_ATK_RANGE) * ATK_RANGE_SCALE, 1),
            "ep": i32(EP_CUR),
            "ep_max": i32(EP_MAX),
            "exp": i32(SELF_OBJ_EXP),
            "kills": i32(SELF_OBJ_KILLS),
            "deaths": i32(SELF_OBJ_DEATHS),
            "assists": i32(SELF_OBJ_ASSISTS),
            "x": i32(SELF_OBJ_X),
            "y": i32(SELF_OBJ_Y),
            "z": i32(SELF_OBJ_Z),
        }
        if not (0 < d["level"] <= 30 and 0 <= d["gold"] < 1_000_000
                and 0 <= d["gold_earned"] < 2_000_000
                and 0 <= d["hp"] <= d["hp_max"] <= 100_000
                and 0 <= d["kills"] <= 200 and 0 <= d["deaths"] <= 200
                and 0 <= d["assists"] <= 200):
            return None
        # 法力比例（唯一可靠口径）：0<max<=50000 且 0<=cur<=max，否则判无效
        if 0 < d["ep_max"] <= 50_000 and 0 <= d["ep"] <= d["ep_max"]:
            d["ep_rate"] = round(d["ep"] / d["ep_max"], 4)
        else:
            d["ep"] = d["ep_max"] = None
            d["ep_rate"] = None
        # 已花费 = 总经济 − 可用（精确值，不需要跨帧差分）
        d["gold_spent"] = max(0, d["gold_earned"] - d["gold"])
        # 复活倒计时（秒）：0 表示存活/未阵亡；0..180 之外判无效
        rs = i32(RESPAWN_LEFT)
        d["respawn_s"] = rs if 0 <= rs <= 180 else None
        d["dead"] = bool(rs > 0) if d["respawn_s"] is not None else (d["hp"] <= 0)
        # 装备列表：vector 头 [+0x1920, +0x1924, count]，ID 从 +0x192C 起 stride 4。
        #   校验：count ∈ [0,16]，ID ∈ [100,99999]（多数是 4 位，实测也有 5 位如 11311）。
        #   遇到非法值**在处截断**而不是整表丢弃 —— 实测该 vector 在"买装备同帧"会短暂移位，
        #   截断能保住前缀，`equip_truncated` 标记出来供上层判断可信度。
        n_eq = i32(SELF_EQUIP_COUNT)
        eq: List[int] = []
        trunc = False
        if 0 <= n_eq <= SELF_EQUIP_MAX:
            for k in range(n_eq):
                v = i32(SELF_EQUIP_IDS + k * SELF_EQUIP_STRIDE)
                if not (100 <= v <= 99999):
                    trunc = True
                    break
                eq.append(v)
        d["equip_ids"] = eq
        d["equip_truncated"] = trunc
        # 技能槽：**逐槽容错**。旧版任槽不合规就整组丢弃，实测 6/10 英雄因此读不到。
        # 前 3 槽是英雄技能；其余槽含恢复/召唤师/回城（回城 total=0 也算有效）。
        skills = []
        slots = []
        for k in range(SKILL_SLOT_COUNT):
            b = SELF_SKILL_BASE + k * SKILL_SLOT_STRIDE
            sid = i32(b + SKILL_OFF_ID)
            level = i32(b + SKILL_OFF_LEVEL)
            state = i32(b + SKILL_OFF_STATE)
            left = i32(b + SKILL_OFF_LEFT_MS)
            total = i32(b + SKILL_OFF_TOTAL_MS)
            if not (0 < sid < 10_000_000) or not (0 <= total <= 300_000):
                continue                       # 该槽无效：跳过，不影响其它槽
            if not (0 <= level <= 10) or not (0 <= left <= total + 5000) or state < 0:
                continue
            if k < 3:
                role = f"skill{k + 1}"
            elif sid == SKILL_ID_HEAL:
                role = "heal"
            elif sid == SKILL_ID_RECALL:
                role = "recall"
            elif SUMMONER_ID_LO <= sid < SUMMONER_ID_HI:
                role = "summoner"
            else:
                role = None
            ent = {"slot": k + 1, "id": sid, "level": level,
                   "state": state, "ready": bool(state & 1),
                   "cd_left_ms": left, "cd_total_ms": total, "role": role}
            slots.append(ent)
            if role and role.startswith("skill"):
                skills.append(ent)
        if slots:
            d["slots"] = slots
        if skills:
            d["skills"] = skills
        for ent in slots:
            if ent["role"] in ("summoner", "recall", "heal"):
                d[ent["role"]] = ent
        return d

    def read_self_state(self, config_id: int) -> Optional[dict]:
        """读本地英雄实时金币/等级/HP/坐标（内存 C# 状态对象）。

        地址在局内稳定；跨局或对象失效会触发一次重新定位（~1s）。
        首次调用前应先 connect()，且需 device 上存在 /data/local/tmp/memscan。
        """
        if not self.pid:
            return None
        need = SELF_OBJ_SKILL_END - SELF_OBJ_ID
        obj = self._self_state_obj
        if obj:
            raw = self.m.read(obj + SELF_OBJ_ID, need)
            d = self._parse_self_state(raw, obj)
            if d and d["id"] == config_id:
                return d
            self._self_state_obj = None

        self_unit = None
        for u in self.snapshot():
            if u.is_hero and u.config_id == config_id and u.x is not None:
                self_unit = u
                break
        if not self_unit:
            return None
        obj = self._locate_self_state(config_id, self_unit.x, self_unit.z)
        if not obj:
            return None
        raw = self.m.read(obj + SELF_OBJ_ID, need)
        d = self._parse_self_state(raw, obj)
        if not d or d["id"] != config_id:
            self._self_state_obj = None
            return None
        return d

    # ---------- 全英雄 C# 状态数组（金币/等级，无需打开战绩面板） ----------
    def _read_hero_array_at(self, base: int) -> List[dict]:
        """按 stride 读 10 个槽位；base 是对象起点（不是 ID 字段地址）。"""
        stride = HERO_ARR_STRIDE
        raw = self.m.read(base, stride * HERO_ARR_COUNT)
        if len(raw) < stride * HERO_ARR_COUNT:
            return []
        out: List[dict] = []
        for slot in range(HERO_ARR_COUNT):
            off = slot * stride
            d = self._parse_self_state(
                raw[off + SELF_OBJ_ID: off + SELF_OBJ_SKILL_END], base + off)
            if not d or d["id"] <= 0:
                continue
            d["slot"] = slot
            out.append(d)
        return out

    def _locate_hero_arrays(self) -> List[int]:
        """用 actor 表里的英雄 ID 找连续数组：memscan idbag（scudo-only）。

        成功时通常返回 2 个基址（同帧两份副本）；两份互为兜底。
        """
        if not self.pid:
            return []
        units = [u for u in self.snapshot() if u.is_hero]
        ids: List[int] = []
        for u in units:
            if u.config_id not in ids:
                ids.append(u.config_id)
            self._hero_camp[u.config_id] = u.camp
        # 定位阶段必须拿齐 10 个英雄 ID，且扫描要求 10 槽全部命中：
        # minhits=8 会让数组内的滑动窗口也通过，产生多个重叠假基址。
        if len(ids) < HERO_ARR_COUNT:
            return []
        id_set = set(ids)
        want = HERO_ARR_COUNT
        cmd = (f"MEMSCAN_ONLY=scudo {MEMSCAN} {self.pid} idbag "
               f"{','.join(map(str, ids))} 0x{HERO_ARR_STRIDE:x} {want}")
        try:
            out = self.m._run(["shell", cmd]).decode("utf-8", "replace")
        except Exception as exc:          # 设备端工具缺失/扫描失败
            print(f"[mem_source] hero-array locate failed: {exc}", file=sys.stderr)
            return []
        bases: List[int] = []
        for v in re.findall(r"base=0x([0-9a-f]+)", out):
            idbase = int(v, 16)
            base = idbase - SELF_OBJ_ID
            got = {r["id"] for r in self._read_hero_array_at(base)}
            if len(got & id_set) < want or base in bases:
                continue
            # 边界校验：前后相邻槽不是英雄 ID（排除数组内滑动窗口）
            prev_id = self._u32(base - HERO_ARR_STRIDE + SELF_OBJ_ID)
            next_id = self._u32(base + HERO_ARR_COUNT * HERO_ARR_STRIDE
                                + SELF_OBJ_ID)
            if prev_id in id_set or next_id in id_set:
                continue
            bases.append(base)
        return bases

    def read_hero_states(self, force_relocate: bool = False,
                         expected_ids: Optional[set] = None) -> List[dict]:
        """读全部英雄的实时金币/等级/HP/坐标（只读内存，不打开面板）。

        - 首次定位 ~5s（scudo-only idbag），之后每帧一次 ~110KB 读取；
        - 同帧两份副本按 (exp, gold) 取新，单槽损坏可由另一份补齐；
        - expected_ids：传入当前局 actor 的英雄 ID 集合，用于防止结算后
          缓存数组仍返回上一局数据（不匹配会重定位一次，再失败返回空）；
        - 金币：`gold` = **可用金币**（HUD 右上角口径，`+0x15D0`）；
          `gold_earned`/`gold_total` = **总经济**（`+0x200`，战绩面板"经济"列口径）；
          `gold_spent` = 两者之差（精确）。v16.3 修正 —— 旧版把 `+0x200` 当可用金币，
          真机对账时发现它从不随买装备下降。
        """
        if not self.pid:
            return []
        if (force_relocate or self._hero_array_pid != self.pid
                or not self._hero_arrays):
            now = time.time()
            if not self._hero_arrays and now < self._hero_locate_fail_ts:
                return []               # 上次定位失败：退避，避免每帧全扫
            self._hero_arrays = self._locate_hero_arrays()
            self._hero_array_pid = self.pid
            if not self._hero_arrays:
                self._hero_locate_fail_ts = now + 2.0
                return []
            self._hero_locate_fail_ts = 0.0
        merged: Dict[int, dict] = {}
        # 主副本一帧只读一次（~110KB）；缺槽时才读另一份补齐。
        if self._hero_arrays:
            for r in self._read_hero_array_at(self._hero_arrays[0]):
                merged[r["id"]] = r
        need = min(HERO_ARR_MIN_VALID, HERO_ARR_COUNT)
        if len(merged) < HERO_ARR_COUNT:
            for base in self._hero_arrays[1:]:
                for r in self._read_hero_array_at(base):
                    old = merged.get(r["id"])
                    if old is None or (r["exp"], r["gold"]) > (old["exp"], old["gold"]):
                        merged[r["id"]] = r
                if len(merged) >= HERO_ARR_COUNT:
                    break
        if expected_ids is not None and set(merged) != set(expected_ids):
            # 结算后缓存仍指向上一局数组 / 数组被重建：重定位一次
            if self._hero_arrays and not force_relocate:
                self._hero_arrays = []
                return self.read_hero_states(force_relocate=True,
                                             expected_ids=expected_ids)
            return []
        states = sorted(merged.values(), key=lambda r: r["slot"])
        if len(states) < need and self._hero_arrays and not force_relocate:
            # 数组被搬移/重建：重定位一次
            self._hero_arrays = []
            return self.read_hero_states(force_relocate=True,
                                         expected_ids=expected_ids)
        # v16.3 金币语义修正：`gold`=可用（HUD 口径）、`gold_earned`=总经济、
        #   `gold_spent`=总经济−可用（精确，无需差分）。旧版把 +0x200 当可用金币，
        #   导致"已花费"只能靠跨帧差分推导（中途接入就会漏算）。
        for r in states:
            r["camp"] = self._hero_camp.get(r["id"])
            r["gold_total"] = r.get("gold_earned", r["gold"])
            r.setdefault("gold_spent", max(0, r["gold_total"] - r["gold"]))
        return states

    def read_hero_gold(self) -> Dict[int, int]:
        """{config_id: 当前金币}，一帧内 10 名英雄一次读完。"""
        return {r["id"]: r["gold"] for r in self.read_hero_states()}

    # 英雄基础属性块（2026-10-21 实测，三份副本 slot0/1/4 内容一致）：
    #   actor + 0x110 (slot0 组件) + 0x460，22 个 i32：
    #   [攻速基础,攻速成长, HP基础,HP成长, HP回复基础,HP回复成长, pad,
    #    MP基础,MP成长, MP回复基础,MP回复成长, pad×3,
    #    AD基础,AD成长, pad×2, 护甲基础,护甲成长, 魔抗基础,魔抗成长]
    #   成长量均为 ×1e-4（如 HP 2050000 → 205/级）。
    #   验证：HP/MP 公式与实时值逐英雄吻合（差额=装备加成），
    #   106 Lv2 HP 3200+205=3405、Lv6 MP 640+45.7143×5≈868.6 均对上。
    STAT_BLOCK_OFF = 0x460
    STAT_BLOCK_LEN = 0x58

    @staticmethod
    def _page_at(bufs_map, addr, size):
        b = bufs_map.get(addr, b"")
        off = addr & 0xFFF              # read_spans 返回页对齐数据
        if len(b) < off + size:
            return b""
        return b[off:off + size]

    def read_hero_base_stats(self) -> Dict[int, dict]:
        """{config_id: 基础属性/成长}，10 名英雄一帧读完（含攻击/护甲/魔抗）。"""
        if not self.pid:
            return {}
        units = [u for u in self.snapshot() if u.is_hero]
        if not units:
            return {}
        bufs = dict(self.m.read_spans([(u.actor + 0x110, 8) for u in units]))
        comps = []
        for u in units:
            raw = self._page_at(bufs, u.actor + 0x110, 8)
            if len(raw) == 8:
                p = struct.unpack("<Q", raw)[0]
                if 0x1000000000 < p < 0x7FFFFFFFFFFF:
                    comps.append((u, p))
        if not comps:
            return {}
        spans = []
        for _, p in comps:
            spans.append((p + 0x10, 8))
            spans.append((p + self.STAT_BLOCK_OFF, self.STAT_BLOCK_LEN))
        b2 = dict(self.m.read_spans(spans))
        out: Dict[int, dict] = {}
        for u, p in comps:
            back = self._page_at(b2, p + 0x10, 8)
            raw = self._page_at(b2, p + self.STAT_BLOCK_OFF, self.STAT_BLOCK_LEN)
            if len(back) != 8 or len(raw) != self.STAT_BLOCK_LEN:
                continue
            if struct.unpack("<Q", back)[0] != u.actor:
                continue
            v = struct.unpack("<22i", raw)
            d = {
                "as_base": v[0], "as_growth": v[1] / 10000.0,
                "hp_base": v[2], "hp_growth": v[3] / 10000.0,
                "hp_regen_base": v[4], "hp_regen_growth": v[5] / 10000.0,
                "mp_base": v[7], "mp_growth": v[8] / 10000.0,
                "mp_regen_base": v[9] / 10000.0,
                "mp_regen_growth": v[10] / 10000.0,
                "ad_base": v[14], "ad_growth": v[15] / 10000.0,
                "armor_base": v[18], "armor_growth": v[19] / 10000.0,
                "mr_base": v[20], "mr_growth": v[21] / 10000.0,
            }
            if not (100 <= d["hp_base"] <= 20000 and 100 <= d["ad_base"] <= 2000
                    and -100 <= d["armor_base"] <= 2000 and -100 <= d["mr_base"] <= 2000):
                continue
            out[u.config_id] = d
        return out

    # 物理攻击力候选字段（2026-10-21 受控实验 ❌ 否定）：
    #   actor + 0x110 + slot4*0x18 = 0x170 -> 组件指针，组件 +0x158 = 候选值。
    #   判定过程：① 1 级 10 人候选值 298..426，与基础物攻 162..175 无固定比例；
    #   ② 买铁剑（+20 物攻，275 金，已确认进背包）前后小乔 321→325/324/326/341，
    #      无 +20 位移（噪声 ±10）；③ 同英雄 2s 内可跳 13~185。
    #   结论：不是可用的面板属性；保留方法仅作逆向探针，**不写 ui/extra 给决策层**。
    ATK_COMP_BASE = 0x110 + 4 * 0x18
    ATK_OFF = 0x158

    def read_hero_attacks(self) -> Dict[int, int]:
        """{config_id: +0x158 候选值}，10 名英雄一帧读完（2 次批量读）。"""
        if not self.pid:
            return {}
        units = [u for u in self.snapshot() if u.is_hero]
        if not units:
            return {}
        base = self.ATK_COMP_BASE
        bufs = dict(self.m.read_spans([(u.actor + base, 8) for u in units]))

        def at(bufs_map, addr, size):
            b = bufs_map.get(addr, b"")
            off = addr & 0xFFF          # read_spans 返回页对齐数据
            if len(b) < off + size:
                return b""
            return b[off:off + size]

        comps = []
        for u in units:
            raw = at(bufs, u.actor + base, 8)
            if len(raw) == 8:
                p = struct.unpack("<Q", raw)[0]
                if 0x1000000000 < p < 0x7FFFFFFFFFFF:
                    comps.append((u, p))
        if not comps:
            return {}
        spans = []
        for _, p in comps:
            spans.append((p + 0x10, 8))
            spans.append((p + self.ATK_OFF, 4))
        b2 = dict(self.m.read_spans(spans))
        out: Dict[int, int] = {}
        for u, p in comps:
            back = at(b2, p + 0x10, 8)
            raw = at(b2, p + self.ATK_OFF, 4)
            if (len(back) == 8 and len(raw) == 4
                    and struct.unpack("<Q", back)[0] == u.actor):
                v = struct.unpack("<i", raw)[0]
                if 0 < v < 20_000:
                    out[u.config_id] = v
        return out

    # ---------- 全单位 transform 表（小兵/塔/野怪位置） ----------
    # 2026-10-20 发现：scudo:secondary 大池里有一张全单位位置表，节点布局
    #   {tagged 自指针, size, x:i32, 0, z:i32, 256}（0x18 粒度，x@+8 对齐不定），
    # 全场所有同步单位（英雄/小兵/塔/野怪/飞行物）各一条，持续更新不消失。
    # 英雄记录可用 actor 层坐标锚定身份；小兵身份关联尚未打通（refs 噪声大），
    # 但位置+移动方向可用（蓝方兵朝红方泉水走）。
    TRANSFORM_SIG = 256
    _tf_pool: Optional[List[int]] = None      # [start, end, lo, hi] 读窗口
    _tf_pool_pid: Optional[str] = None

    @staticmethod
    def _parse_tf_records(raw: bytes, base: int) -> Dict[int, tuple]:
        out: Dict[int, tuple] = {}
        # 记录签名：+12 处 u32==256（LE: 00 01 00 00），y==0，x/z 为世界坐标。
        # 用 C 级 bytes.find 扫签名，避免 48MB 逐 4 字节的 Python 循环。
        pat = b"\x00\x01\x00\x00"
        pos = raw.find(pat)
        while pos != -1:
            nxt = raw.find(pat, pos + 1)
            o = pos - 12
            if o >= 4:
                # 真记录前面是节点头：{tagged 自指针, size=0x8000xxxx}
                h = struct.unpack_from("<I", raw, o - 4)[0]
                if 0x80000000 <= h < 0x80100000 and raw[o + 4:o + 8] == b"\x00\x00\x00\x00":
                    x = struct.unpack_from("<i", raw, o)[0]
                    z = struct.unpack_from("<i", raw, o + 8)[0]
                    if 1000 <= abs(x) <= 65000 and 1000 <= abs(z) <= 65000:
                        out[base + o] = (x, z)
            pos = nxt
        return out

    def _find_transform_pool(self) -> Optional[List[int]]:
        """用英雄实时坐标当锚点，在 scudo:secondary 大池里找 transform 表。"""
        if self._tf_pool and self._tf_pool_pid == self.pid:
            return self._tf_pool
        heroes = [u for u in self.snapshot()
                  if u.is_hero and u.x is not None]
        if len(heroes) < 5:
            return None
        big = [(st, en) for st, en, _off, path in self.m.maps
               if path and "scudo:secondary" in path
               and en - st >= 32 * 1024 * 1024]
        for st, en in big:
            raw = self.m.read(st, min(en - st, 0x3000000))
            if not raw:
                continue
            recs = self._parse_tf_records(raw, st)
            if len(recs) < 20:
                continue
            # 池读取耗时数秒，英雄坐标要用读完后最新的，容差放宽
            heroes = [u for u in self.snapshot()
                      if u.is_hero and u.x is not None]
            hit = sum(1 for (x, z) in recs.values()
                      for u in heroes if abs(x - u.x) <= 500 and abs(z - u.z) <= 500)
            if hit >= 3:
                addrs = sorted(recs)
                lo = max(st, addrs[0] - 0x8000)
                hi = min(en, addrs[-1] + 0x8000)
                self._tf_pool = [st, en, lo, hi]
                self._tf_pool_pid = self.pid
                return self._tf_pool
        return None

    def read_transform_records(self) -> Dict[int, dict]:
        """读全单位位置表。

        返回 {rec_addr: {"x","z","hero_cid"(能锚定时)}}。
        首次定位 ~2-4s（扫 48MB 池），之后每帧只读记录窗口（~1MB）。
        """
        pool = self._find_transform_pool()
        if not pool:
            return {}
        _st, _en, lo, hi = pool
        raw = self.m.read(lo, hi - lo)
        recs = self._parse_tf_records(raw, lo)
        heroes = [u for u in self.snapshot() if u.is_hero and u.x is not None]
        out: Dict[int, dict] = {}
        for a, (x, z) in recs.items():
            d = {"x": x, "z": z, "hero_cid": None}
            best, bd = None, 301
            for u in heroes:
                dd = max(abs(x - (u.x or 0)), abs(z - (u.z or 0)))
                if dd < bd:
                    best, bd = u, dd
            if best is not None:
                d["hero_cid"] = best.config_id
            out[a] = d
        return out

    # ---------- 定位 ----------
    def actor_manager(self) -> Optional[int]:
        """CActorManager*（四段解引用 + 0x388 服务槽）。"""
        rt = self.m.resolve("libGameCore.so", BATTLE_GLOBAL)
        if rt is None:
            return None
        a = self._u64(rt)
        b = self._u64(a) if a else None
        t = self._u64(b) if b else None
        return self._u64(t + ACTOR_MGR_OFF) if t else None

    def _actor_table(self):
        mgr = self.actor_manager()
        if not mgr or not self._region(mgr):
            return None, None, 0, []
        arr = self._u64(mgr + 0x30)
        cnt = self._u32(mgr + 0x4C)
        sel = self._u32(mgr + 0x70)
        if sel:
            cnt = self._u32(mgr + 0x6C)
        if not arr or not cnt or cnt > 4000:
            return mgr, arr, 0, []
        raw = self.m.read(arr, cnt * 0x18)
        if len(raw) < cnt * 0x18:
            cnt = len(raw) // 0x18
        actors = [struct.unpack_from("<Q", raw, i * 0x18)[0] for i in range(cnt)]
        return mgr, arr, cnt, actors

    def _actor_ptrs(self):
        _, _, cnt, actors = self._actor_table()
        return actors, cnt

    @staticmethod
    def _pf(data, addr, off, fmt):
        """从页缓存读字段，支持跨 4KB 页。"""
        if not addr:
            return None
        size = struct.calcsize(fmt)
        a = addr + off
        page = a & ~0xFFF
        o = a - page
        buf = data.get(page)
        if not buf:
            return None
        if o + size <= len(buf):
            return struct.unpack_from(fmt, buf, o)
        nxt = data.get(page + 0x1000)
        if not nxt:
            return None
        raw = buf[o:] + nxt[:o + size - len(buf)]
        return struct.unpack_from(fmt, raw, 0)

    # ---------- 慢路径：重建缓存 ----------
    def _refresh(self):
        mgr, arr, cnt, actors = self._actor_table()
        actors = [a for a in actors if a and self._region(a)]
        if not actors:
            return [], None
        pages = {a & ~0xFFFF for a in actors}
        page_data = {p: self.m.read(p, 0x10000) for p in pages}

        units: List[MemUnit] = []
        comps: List[tuple] = []
        comp_pages = set()
        for a in actors:
            p = a & ~0xFFFF
            buf = page_data.get(p, b"")
            off = a - p
            need = max(ACTOR_POS_OFF, ACTOR_ATTR_OFF, ACTOR_SPAWN_OFF + 0xC) + 0x10
            if off + need > len(buf):
                raw = self.m.read(a, need)
                base = 0
            else:
                raw = buf
                base = off
            if base + need > len(raw):
                continue
            cid = struct.unpack_from("<i", raw, base + 0x28)[0]
            typ = struct.unpack_from("<i", raw, base + 0x30)[0]
            camp_id = struct.unpack_from("<i", raw, base + 0x34)[0]
            if camp_id not in (CAMP_BLUE, CAMP_RED) or typ == TYPE_OTHER:
                continue
            attr = struct.unpack_from("<Q", raw, base + ACTOR_ATTR_OFF)[0]
            pos = struct.unpack_from("<Q", raw, base + ACTOR_POS_OFF)[0]
            # 槽位自带的 handle id：引擎用 [slot+8] == [comp+0xc] 校验对象存活
            attr_sid = struct.unpack_from("<I", raw, base + ACTOR_ATTR_OFF + 8)[0]
            pos_sid = struct.unpack_from("<I", raw, base + ACTOR_POS_OFF + 8)[0]
            u = MemUnit(
                actor=a, config_id=cid, actor_type=typ,
                camp="blue" if camp_id == CAMP_BLUE else "red",
                kind=_CLS.get(typ, "unit"),
                extra={"camp_id": camp_id},
            )
            try:
                sx, sy, sz = struct.unpack_from("<3i", raw, base + ACTOR_SPAWN_OFF)
                if all(-90000 <= v <= 90000 for v in (sx, sz)) and not (sx == 0 and sz == 0):
                    u.spawn_x, u.spawn_y, u.spawn_z = sx, sy, sz
            except struct.error:
                pass
            units.append(u)
            comps.append((u, attr, attr_sid, pos, pos_sid))
            if attr:
                comp_pages.add(attr & ~0xFFF)
                comp_pages.add((attr + ATTR_HP + 4) & ~0xFFF)   # 跨页
            if pos:
                comp_pages.add(pos & ~0xFFF)
                comp_pages.add((pos + POS_Z + 4) & ~0xFFF)

        comp_data = {p: self.m.read(p, 0x10000)
                     for p in {x & ~0xFFFF for x in comp_pages}}

        def fieldv(addr: int, off: int, fmt: str):
            if not addr:
                return None
            page = addr & ~0xFFFF
            buf = comp_data.get(page, b"")
            o = addr - page + off
            if o + struct.calcsize(fmt) > len(buf):
                return None
            return struct.unpack_from(fmt, buf, o)

        def field(addr: int, off: int, fmt: str):
            v = fieldv(addr, off, fmt)
            return v[0] if v else None

        for u, attr, attr_sid, pos, pos_sid in comps:
            # 引擎同款校验：slot id == comp+0xc，且 comp+0x10 回指 actor
            attr_ok = (attr and field(attr, 0xC, "<I") == attr_sid
                       and field(attr, 0x10, "<Q") == u.actor)
            if attr_ok:
                mx = field(attr, ATTR_MAX_HP, "<i")
                cur = field(attr, ATTR_HP, "<i")
                if mx is not None and cur is not None:
                    if mx < cur:
                        mx, cur = cur, mx
                    if 0 <= cur <= mx < 300000:
                        u.hp_max, u.hp = mx, cur
                        u.dead = cur <= 0
            # 签名首尾固定 256，中间两位随状态变化（0/1 或 65536/65536）
            sig = fieldv(pos, 0x100, "<4i")
            pos_ok = (pos and field(pos, 0xC, "<I") == pos_sid
                      and field(pos, 0x10, "<Q") == u.actor
                      and sig is not None and sig[0] == 256 and sig[3] == 256)
            if pos_ok:
                x = field(pos, POS_X, "<i")
                y = field(pos, POS_Y, "<i")
                z = field(pos, POS_Z, "<i")
                if (x is not None and z is not None
                        and -90000 <= x <= 90000 and -90000 <= z <= 90000):
                    u.x, u.y, u.z = x, y, z

        meta = {"mgr": mgr, "arr": arr, "cnt": cnt, "actors": actors,
                "span": (min(actors), max(actors) + 0x240),
                "comp_pages": comp_pages}
        return units, meta

    # ---------- 快路径：两批 span 读（管理/actor 一批，组件一批） ----------
    def _spans_for(self, items, margin=0, merge_gap=0x40000):
        """把地址按“邻近”聚成若干连续 span（不按整段 region 合并）。

        合并间隔 merge_gap：相邻两个地址的间隙小于它才合并；这样既覆盖
        同池新分配的对象，又不会把 40MB 的 scudo region 整段读进来。
        """
        entries = []
        for item in items:
            addr, size = item[0], item[1]
            mg = item[2] if len(item) > 2 else margin
            if not addr:
                continue
            r = self._region(addr)
            if not r:
                return None
            entries.append((addr, size, mg, r))
        entries.sort(key=lambda x: x[0])
        clusters = []
        for addr, size, mg, r in entries:
            lo = max(r[0], addr - mg)
            hi = min(r[1], addr + size + mg)
            if (clusters and clusters[-1][2][:2] == r[:2]
                    and lo - clusters[-1][1] <= merge_gap):
                clusters[-1][1] = max(clusters[-1][1], hi)
            else:
                clusters.append([lo, hi, r])
        spans = []
        for lo, hi, (rlo, rhi, _p) in clusters:
            start = lo & ~0xFFF
            end = min((hi + 0xFFF) & ~0xFFF, rhi)
            if end > start:
                spans.append((start, end - start))
        return spans

    @staticmethod
    def _spf(spans, addr, off, fmt):
        if not addr:
            return None
        size = struct.calcsize(fmt)
        a = addr + off
        for start, data in spans:
            if start <= a and a + size <= start + len(data):
                return struct.unpack_from(fmt, data, a - start)
        return None

    def _snapshot_fast(self):
        """4 次 adb 往返：manager / actor 数组 / actor 区 / 组件区。

        不缓存 actor 列表和数组指针，因此小兵刷新、数组 realloc 不会导致整帧失败；
        只有战斗管理器本身换了才回退慢路径。
        """
        c = self._cache
        if not c or not c.get("mgr"):
            return None
        mgr = c["mgr"]
        mb = self.m.read(mgr, 0x80)
        if len(mb) < 0x80:
            return None
        arr = struct.unpack_from("<Q", mb, 0x30)[0]
        cnt = struct.unpack_from("<I", mb, 0x4C)[0]
        if struct.unpack_from("<I", mb, 0x70)[0]:
            cnt = struct.unpack_from("<I", mb, 0x6C)[0]
        if not arr or not cnt or cnt > 4000:
            return None
        raw = self.m.read(arr, cnt * 0x18)
        if len(raw) < cnt * 0x18:
            return None
        actors = [a for a in (struct.unpack_from("<Q", raw, i * 0x18)[0]
                              for i in range(cnt)) if a and self._region(a)]
        if not actors:
            return None

        spans = self._spans_for([(a, 0x240, 0x1000) for a in actors],
                                merge_gap=0x40000)
        if not spans:
            return None
        d1 = self.m.read_spans(spans)
        if not d1:
            return None

        units: List[MemUnit] = []
        metas = []
        comp_items = []
        for a in actors:
            hdr = self._spf(d1, a, 0x28, "<i")
            typ = self._spf(d1, a, 0x30, "<i")
            camp = self._spf(d1, a, 0x34, "<i")
            if not hdr or not typ or not camp:
                return None
            cid, typ, camp_id = hdr[0], typ[0], camp[0]
            if camp_id not in (CAMP_BLUE, CAMP_RED) or typ == TYPE_OTHER:
                continue          # 子弹/特效等非单位 actor
            attr = self._spf(d1, a, ACTOR_ATTR_OFF, "<Q")
            pos = self._spf(d1, a, ACTOR_POS_OFF, "<Q")
            attr_sid = self._spf(d1, a, ACTOR_ATTR_OFF + 8, "<I")
            pos_sid = self._spf(d1, a, ACTOR_POS_OFF + 8, "<I")
            if not attr:
                return None
            if typ == TYPE_HERO and not pos:
                return None
            u = MemUnit(
                actor=a, config_id=cid, actor_type=typ,
                camp="blue" if camp_id == CAMP_BLUE else "red",
                kind=_CLS.get(typ, "unit"),
                extra={"camp_id": camp_id},
            )
            # 出生/编队点（actor+0x278）：建筑与野怪静止，出生点即当前位置。
            # 快路径同样要读，否则 `spawn` 为空 -> 建筑拿不到坐标。
            sp = self._spf(d1, a, ACTOR_SPAWN_OFF, "<3i")
            if sp:
                sx, sy, sz = sp[0], sp[1], sp[2]
                if all(-90000 <= v <= 90000 for v in (sx, sz)) and not (sx == 0 and sz == 0):
                    u.spawn_x, u.spawn_y, u.spawn_z = sx, sy, sz
            units.append(u)
            metas.append((u, attr[0], attr_sid[0] if attr_sid else None,
                          pos[0] if pos else 0,
                          pos_sid[0] if pos_sid else None))
            comp_items.append((attr[0], 0x100))
            if pos:
                comp_items.append((pos[0], 0x140))

        spans2 = self._spans_for(comp_items, merge_gap=0x100000)
        if not spans2:
            return None
        d2 = self.m.read_spans(spans2)
        if not d2:
            return None
        def spf1(addr, off, fmt):
            v = self._spf(d2, addr, off, fmt)
            return v[0] if v else None

        for u, attr, attr_sid, pos, pos_sid in metas:
            # 引擎同款校验：槽 id + owner 回指；失败则本帧重建
            attr_ok = (attr and attr_sid is not None
                       and spf1(attr, 0xC, "<I") == attr_sid
                       and spf1(attr, 0x10, "<Q") == u.actor)
            if attr_ok:
                mxv = self._spf(d2, attr, ATTR_MAX_HP, "<i")
                curv = self._spf(d2, attr, ATTR_HP, "<i")
                if mxv is None or curv is None:
                    if u.actor_type == TYPE_HERO:
                        return None
                else:
                    mx, cur = mxv[0], curv[0]
                    if mx < cur:
                        mx, cur = cur, mx
                    if 0 <= cur <= mx < 300000:
                        u.hp_max, u.hp = mx, cur
                        u.dead = cur <= 0
                    elif u.actor_type == TYPE_HERO:
                        return None
            elif u.actor_type == TYPE_HERO:
                return None
            sv = self._spf(d2, pos, 0x100, "<4i")
            pos_ok = (pos and pos_sid is not None
                      and spf1(pos, 0xC, "<I") == pos_sid
                      and spf1(pos, 0x10, "<Q") == u.actor
                      and sv is not None and sv[0] == 256 and sv[3] == 256)
            if pos_ok:
                xv = self._spf(d2, pos, POS_X, "<i")
                yv = self._spf(d2, pos, POS_Y, "<i")
                zv = self._spf(d2, pos, POS_Z, "<i")
                if xv is None or yv is None or zv is None:
                    return None
                x, z = xv[0], zv[0]
                if not (-90000 <= x <= 90000 and -90000 <= z <= 90000):
                    return None      # 指针失效/组件换了 -> 重建
                u.x, u.y, u.z = x, yv[0], z

        self._cache = {"mgr": c["mgr"], "arr": arr, "cnt": cnt,
                       "actors": [u.actor for u in units],
                       "span": (min(u.actor for u in units),
                                max(u.actor for u in units) + 0x240),
                       "comp_pages": set()}
        return units

    def snapshot(self) -> List[MemUnit]:
        if self._cache:
            units = self._snapshot_fast()
            if units is not None:
                return units
        units, meta = self._refresh()
        self._cache = meta
        return units

    def dump(self) -> dict:
        t0 = time.time()
        units = self.snapshot()
        return {
            "ts": t0,
            "ms": (time.time() - t0) * 1000,
            "pid": self.pid,
            "units": units,
        }

    # ---------- 对接 GameState ----------
    @staticmethod
    def _canonical_skill(s: dict) -> dict:
        """内存技能槽 -> 统一技能口径（CD 统一用秒）。"""
        return {
            "slot": s.get("slot"),
            "id": s.get("id"),
            "level": s.get("level"),
            "ready": bool(s.get("ready")),
            "cd_left_s": round((s.get("cd_left_ms") or 0) / 1000.0, 2),
            "cd_total_s": round((s.get("cd_total_ms") or 0) / 1000.0, 2),
            "role": s.get("role"),
        }

    def to_game_state(self, self_config_id: Optional[int] = None,
                      screen_size: Optional[List[int]] = None,
                      include_hero_gold: bool = True):
        """只输出有世界坐标的单位（当前为英雄），避免用假坐标污染决策。

        统一口径（v1.1）：世界坐标进 `pos_world`，小地图坐标进 `pos_minimap`，
        `screen` 只留给视觉的屏幕归一化坐标（内存侧为 None）。
        """
        from wzry.state.schema import GameState, HeroStats, SelfState, Unit

        items = self.snapshot()
        usable = [u for u in items if u.x is not None and u.z is not None]
        now = time.time()
        st = GameState(t=now, t_capture=now, t_state=now,
                       phase="in_match", source="memory",
                       screen_size=screen_size or [1920, 1080],
                       sources={"units": "memory", "ui": "memory",
                                "minimap": "memory"})
        reds, blues = [], []
        self_unit = None
        if self_config_id is not None:
            self_unit = next((u for u in usable
                              if u.is_hero and u.config_id == self_config_id), None)
        self_camp = self_unit.camp if self_unit else "blue"
        for u in usable:
            mm = u.minimap_norm()
            if u.dead:
                continue          # 死亡单位不进决策视野
            if u.is_hero:
                if u.camp != self_camp:
                    cls = "enemy_hero"
                    reds.append(mm)
                else:
                    cls = "ally_hero"
                    blues.append(mm)
            else:
                continue
            hp = (u.hp / u.hp_max) if (u.hp is not None and u.hp_max) else None
            st.units.append(Unit(
                cls=cls, camp=u.camp,
                uid=f"{u.camp}:hero:{u.config_id}",
                kind="hero", hero_cid=u.config_id,
                visible=None,       # 内存是全图数据，是否在视野内未知
                pos_world=[float(u.x), float(u.z)],
                pos_minimap=[float(mm[0]), float(mm[1])],
                coord_src="memory",
                hp=hp, hp_abs=u.hp, hp_max=u.hp_max,
                dead=u.dead, conf=1.0, camp_src="memory",
                src="memory", last_seen_t=now,
            ))
        if self_unit is not None:
            mm = self_unit.minimap_norm()
            st.minimap = {"found": True, "red": reds, "blue": blues,
                          "green": [mm], "dots": {"red": reds, "blue": blues,
                                                  "green": [mm]}}
        else:
            st.minimap = {"found": bool(reds or blues), "red": reds,
                          "blue": blues, "green": [],
                          "dots": {"red": reds, "blue": blues, "green": []}}

        hs: List[dict] = []
        if include_hero_gold:
            hero_ids = {u.config_id for u in items if u.is_hero}
            hs = self.read_hero_states(
                expected_ids=hero_ids if len(hero_ids) >= HERO_ARR_COUNT else None)
            if hs:
                st.ui["hero_gold"] = {str(r["id"]): r["gold"] for r in hs}
                st.ui["hero_level"] = {str(r["id"]): r["level"] for r in hs}
                st.ui["hero_kda"] = {str(r["id"]): [r["kills"], r["deaths"],
                                                    r["assists"]] for r in hs}
                st.ui["hero_gold_total"] = {str(r["id"]): r["gold_total"]
                                            for r in hs}
                st.ui["hero_gold_src"] = "memory"
                st.extra["hero_states"] = hs
                st.scoreboard = [
                    HeroStats(
                        cid=r["id"], camp=r.get("camp"),
                        gold=r.get("gold"), gold_total=r.get("gold_total"),
                        gold_spent=r.get("gold_spent"), level=r.get("level"),
                        exp=r.get("exp"), kills=r.get("kills", 0),
                        deaths=r.get("deaths", 0), assists=r.get("assists", 0),
                        hp=r.get("hp"), hp_max=r.get("hp_max"),
                        equip=list(r.get("equip_ids") or []),
                        respawn_s=r.get("respawn_s"), dead=bool(r.get("dead")),
                        x=r.get("x"), z=r.get("z"), src="memory",
                    )
                    for r in hs
                ]
                by_cid = {r["id"]: r for r in hs}
                for unit in st.units:
                    r = by_cid.get(unit.hero_cid)
                    if not r:
                        continue
                    unit.level = r.get("level")
                    unit.equip = list(r.get("equip_ids") or [])
                    unit.respawn_s = r.get("respawn_s")
                    unit.dead = bool(r.get("dead", unit.dead))
                    if r.get("hp") is not None:
                        unit.hp_abs = r["hp"]
                    if r.get("hp_max"):
                        unit.hp_max = r["hp_max"]
                    if unit.hp_abs is not None and unit.hp_max:
                        unit.hp = unit.hp_abs / unit.hp_max

        # 本地状态优先取自全英雄数组，保证 ui.gold 与 hero_gold 同源同帧；
        # 数组不可用时才回退到单对象 herostate 定位。
        ss = None
        if self_config_id is not None:
            ss = next((r for r in hs if r["id"] == self_config_id), None)
            if ss is None:
                ss = self.read_self_state(self_config_id)
        if ss:
            st.ui.update({
                "gold": ss["gold"], "level": ss["level"],
                "self_hp": ss["hp"], "self_hp_max": ss["hp_max"],
                "self_atk_range": ss.get("atk_range"),
                "gold_src": "memory", "level_src": "memory",
            })
            if ss.get("skills"):
                st.ui["self_skill_cd"] = [
                    {"slot": s["slot"], "id": s["id"], "ready": s["ready"],
                     "left_s": round(s["cd_left_ms"] / 1000.0, 2),
                     "total_s": round(s["cd_total_ms"] / 1000.0, 2)}
                    for s in ss["skills"]
                ]
            if ss.get("summoner"):
                sm = ss["summoner"]
                st.ui["self_summoner_cd"] = {
                    "id": sm["id"], "ready": sm["ready"],
                    "left_s": round(sm["cd_left_ms"] / 1000.0, 2),
                    "total_s": round(sm["cd_total_ms"] / 1000.0, 2)}
            if ss.get("recall"):
                st.ui["self_recall_ready"] = bool(ss["recall"]["ready"])
            st.extra["self_state"] = ss
            st.self_state = SelfState(
                cid=ss.get("id", self_config_id), camp=self_camp,
                hp=ss.get("hp"), hp_max=ss.get("hp_max"),
                mp=ss.get("ep"), mp_max=ss.get("ep_max"),
                gold=ss.get("gold"), gold_total=ss.get("gold_earned"),
                gold_spent=ss.get("gold_spent"), level=ss.get("level"),
                exp=ss.get("exp"), kills=ss.get("kills", 0),
                deaths=ss.get("deaths", 0), assists=ss.get("assists", 0),
                atk_range=ss.get("atk_range"),
                skills=[self._canonical_skill(s) for s in (ss.get("skills") or [])],
                summoner=(self._canonical_skill(ss["summoner"])
                          if ss.get("summoner") else None),
                heal=(self._canonical_skill(ss["heal"])
                      if ss.get("heal") else None),
                recall=(self._canonical_skill(ss["recall"])
                        if ss.get("recall") else None),
                equip=list(ss.get("equip_ids") or []),
                respawn_s=ss.get("respawn_s"), dead=bool(ss.get("dead")),
                x=ss.get("x"), z=ss.get("z"), visible=True,
                src="memory", ts=now,
            )
        st.sources["scoreboard"] = "memory" if hs else "none"
        st.sources["self"] = "memory" if ss else "none"
        bs = self.read_hero_base_stats()
        if bs:
            lv = {r["id"]: r["level"] for r in hs}
            st.extra["hero_base_stats"] = bs
            st.extra["hero_stats"] = {
                str(cid): stats_at_level(s, lv.get(cid, 1))
                for cid, s in bs.items()}
        # 物攻候选字段已否定（见常量区注释），不输出给决策层。
        return st


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="127.0.0.1:16384")
    ap.add_argument("--loop", type=int, default=1)
    a = ap.parse_args()
    src = MemSource(serial=a.serial)
    print("pid", src.connect())
    for k in range(a.loop):
        d = src.dump()
        heroes = [u for u in d["units"] if u.is_hero]
        print(f"--- frame {k}  {d['ms']:.0f}ms  units={len(d['units'])}  heroes={len(heroes)}")
        for u in d["units"][:40]:
            print(f"  {u.kind:<5} camp={u.camp:<4} id={u.config_id:<6} "
                  f"pos=({u.x},{u.y},{u.z}) hp={u.hp}/{u.hp_max} dead={u.dead} "
                  f"actor=0x{u.actor:x}")
        time.sleep(0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
