# -*- coding: utf-8 -*-
"""对局内战斗数据（内存直读 / Route C）：局内时间、单位身份-血量-死亡、野怪存活、经济等级。

**为什么单独一个模块**：`mem_source.py` 只负责"把内存读出来"（Route C 的取数层），
本模块负责"把它变成决策层能用的对局字段"，并把**每个字段的可信度**写清楚。

字段来源与可信度（2026-10-20 实测，MuMu 12 / 127.0.0.1:16384 / 1920x1080）：

| 字段 | 来源 | 状态 |
| --- | --- | --- |
| `gold` / `level` / `kills` / `deaths` / `assists` | 10 英雄 C# 数组（stride 0x2AD8） | ✅ 已与战绩面板逐一核对 |
| `exp` | 同数组 `+0x1BC` | ⚠️ 数值随对局变化，语义按"经验"用，未做等级-经验曲线对账 |
| `units[].hp / hp_max / dead / camp / cid` | CActorManager → actor → attr 组件 | ✅ HP 与画面血条一致（死亡时 cur=0、max 不变） |
| `clock_s`（局内时间，秒） | 差分扫描定位"每秒 +1"的 u32 | ✅ 与 HUD 时钟逐秒吻合（1112→1122 @10s） |
| `skills[]`（技能 id/等级/剩余 CD/总 CD） | 英雄记录 `+0x19D4`，3 槽 stride 0x78 | ✅ **10/10 英雄全通**（2026-10-20 二次实测修正，见下） |
| `summoner` / `heal` / `recall` | 同数组槽 5/6/7（`90003` 恢复 60s、`80xxx` 召唤师、`90005` 回城） | ✅ 实测：闪现 120s 倒计时、恢复 60s、回城 state 随吟唱翻转 |
| 基础属性+成长（HP/MP/AD/护甲/魔抗/攻速/回复） | actor slot0 组件 `+0x460` | ✅ 10/10，HP/MP 公式与实时值对账（差额=装备） |
| `monsters[]`（野怪存活） | actor `config_id ∈ {16100,16101}` | ⚠️ **只能判"在场且 hp>0"**；不在列表里**不等于已死** |
| `ep`（当前法力比例） | C# 状态 `+0x1C4` 当前值 / `+0x1C8` 上限 | ✅ 2026-10-21 施法差分验证；`mem_source` 写 `SelfState.mp`，`mem_battle` 写 `extra.mem_ep_rate` |
| 装备栏 | C# 状态 `+0x1928` count / `+0x192C` ID 数组 | ◐ ID 已可读（`mem_source.equip_ids`）；复活甲可用性未单独验证 |

> **2026-10-25 偏移版本化**：以上偏移已收入 `configs/mem_offsets.json`（version / validated_at / evidence），
> `MemSource.connect()` 会比对代码常量与注册表，不一致直接拒绝读取。

### 技能 CD 的判读修正（2026-10-20 晚）

`+0x10` **不是** 0/1 布尔，而是位域（实测出现 `0 / 1 / 1280 / 1281`）。旧校验
`state in (0,1)` 会让 6/10 个英雄的整组技能被丢弃，表现为"技能 CD 读不到"。
现改为：**`ready = state & 1`**，且**逐槽容错**（单槽异常不影响其它槽）。
另新增 `+0x0C` = 技能等级（0 = 未学）。

⚠️ 唯一例外：**本地英雄（数组 slot 0）** 的 `level` 与 `state` 恒为 0（两份副本一致），
只有 `cd_left_ms` 会正常变化。所以判"自己技能能不能放"要退化为 `cd_left_ms == 0`，
不能用 `ready`。

## 历史排查记录（法力/装备已于 2026-10-21 打通，保留过程避免重复劳动）

1. **当前法力 `ep`**
   - `attr+0x110` 与 `attr+0x128` 恒等，且 45s 采样内**从不变化** → 是**法力上限**，不是当前值。
   - 英雄数组 `+0x1A0`（旧名 "MP 上限"）**已重新定性为"攻击距离 × 10"**：
     106 小乔 6500(650)、121 孙尚香 6000(600)、107 兰陵王 3000(300)、
     167 3000、187 甄姬 6500、173 干将莫邪 7500、174 女娲 8250、105 廉颇 2800，
     10 个英雄逐一吻合，且与等级无关（射程本就是静态属性）。
     现由 `mem_source` 暴露为 `atk_range`（世界单位），写入 `ui.atk_range`。
   - `attr+0x200..0x600` 全量差分 40s：**零字段变化** → 当前法力不在 attr 组件后半段。
   - 结论：当前法力要么在另一组件槽，要么只在 C# 侧。**需要一次"施法扣蓝"的定向实验**：
     开一局 → 记录施法前全组件 → 施放 1 技能 → 再记录 → 找"下降后缓慢回升"的字段。

2. **技能剩余 CD（已打通，2026-10-20 深夜）**
   - 英雄记录 `+0x19D4` 起 stride 0x78：`+0x14` 剩余毫秒、`+0x18` 总 CD 毫秒。
   - 施法验证：大招 0→32558/35000 递减、1技能 0→4472/5000；截图 35→31s 与字段一致。
   - 同一数组还带槽 5/6/7：恢复 `90003`(60s)、召唤师 `80xxx`(闪现 80115/120s)、
     回城 `90005`(无 CD，`state` bit0 = 可用，吟唱中为 0)。
   - **施法必须用 `input motionevent`（DOWN/MOVE/UP）**；`input tap` / `input swipe`
     在局内不会触发施法（多次实测）。

3. **装备栏 / 复活甲**
   - 0x2AD8 记录里 `+0x204` 之后到记录尾（约 0x28D4 字节）**尚未逐字段定性**；
     装备 ID 若为 u16，需要按 u16 步长重扫（本轮只扫了 i32）。
   - 复活甲是装备的一种，装备栏打通后 `revive_armor_usable` 自然可得。

## 用法

```python
from wzry.vision.mem_battle import MemBattle
mb = MemBattle(); mb.connect()
print(mb.read_clock())          # 局内秒数
print(mb.read_monsters())       # 在场野怪
mb.patch_state(state_dict, self_id=106)   # 写入 ui.* / extra.mem_*
```

CLI（单次探测，便于真机核对）：

```
venv/Scripts/python.exe -X utf8 -m wzry.vision.mem_battle --probe
venv/Scripts/python.exe -X utf8 -m wzry.vision.mem_battle --probe --clock 1046
```

> 合规：只读内存，不注入、不修改客户端。**不要**把读取器接到线上真人对局。
"""
from __future__ import annotations

import argparse
import re
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from wzry.vision.mem_source import MemSource, MemUnit, stats_at_level  # noqa: E402

# ---- 单位配置 ID 归类（来自 ACTOR_FINDINGS.md §3 / 实测 actor 列表）----
# ⚠️ 2026-10-21 真机实测更正：原来这套划分是错的。
#   真机（对局内 24 个 type=2 actor，全部静止且 hp 合理）按"到本方泉水角的距离"排序：
#     cid 44/46  距角落 0       -> 泉水
#     cid 21/35  距角落 ~5.3k   -> 水晶
#     cid 92/95  距角落 ~20k    -> 基地塔
#     cid 1696/97/98（蓝）、1699/16100/16101（红） 距角落 ~32k -> 高地塔
#     cid 1691/1694  距角落 ~53k -> 二塔
#     cid 1690/1693  距角落 ~70-85k -> 一塔
#   也就是 1690-1699 是**防御塔**，16100/16101 是红方高地塔，都不是小兵/野怪。
#   精确的层级/分路不在这里硬编码：`organ_slots.classify()` 用几何判定。
CID_STRUCT = frozenset({44, 46, 21, 35, 92, 95,
                        1690, 1691, 1693, 1694,
                        1696, 1697, 1698, 1699, 16100, 16101})
CID_MINION: tuple = ()              # 真正的小兵 cid 尚未确定（本局未出现）
CID_TURRET = tuple(CID_STRUCT)
CID_BASE: tuple = ()
CID_MONSTER: tuple = ()

# ---- 局内时间：差分扫描参数 ----
# 期望值只作为"扫描窗口"的锚，**不作为判据**：时钟在两次调用之间会自己往前走，
# 截图/OCR 拿到的期望值可能已经过期 1~2 分钟（实测 106s 的期望值，扫到时已是 181s）。
CLOCK_SCAN_BACK = 90        # 期望值往前（过去）的搜索宽度（秒）
CLOCK_SCAN_FWD = 180        # 期望值往后（未来）的搜索宽度（秒）
CLOCK_SCAN_DELAY_MS = 10000 # 差分窗口（毫秒）
# 局内时间的**结构签名**：同一秒值在 10 个单位记录里各存一份，stride 0x28。
# 用"同值 + stride 0x28 连续 ≥6 项"做判据，比单点"值接近期望"稳得多。
CLOCK_STRIDE = 0x28
CLOCK_CHAIN_MIN = 6


def unit_kind(cid: int, actor_type: int) -> str:
    """配置 ID + actor 类型 -> 语义类别。"""
    if actor_type == 0:
        return "hero"
    if cid in CID_MINION:
        return "minion"
    if cid in CID_STRUCT:
        return "turret"
    if cid in CID_MONSTER:
        return "monster"
    if cid in CID_BASE:
        return "building"
    return "other"


def skill_vec(skills: Optional[List[dict]], local: bool = False) -> Dict[str, list]:
    """把技能槽压成决策层要的三个等长列表（按 slot 排序，缺槽补 None）。

    - `cd`   : 剩余秒数（float）
    - `ratio`: 剩余比例 0..1（**模型/打分最该用的量**，与官方特征口径一致）
    - `ready`: 能否施放

    为什么 `local` 要区别对待：本地英雄记录的 `state` 位域与 `level` 恒为 0
    （两份数组副本一致，2026-10-20 实测），只有 `cd_left_ms` 会正常变化。
    所以本地英雄只能按 `cd_left_ms == 0` 判可用，不能信 `ready`。
    """
    out: Dict[str, list] = {"id": [], "cd": [], "ratio": [], "ready": [], "level": []}
    if not skills:
        return out
    for s in sorted(skills, key=lambda x: x.get("slot", 0)):
        total = int(s.get("cd_total_ms") or 0)
        left = int(s.get("cd_left_ms") or 0)
        out["id"].append(int(s.get("id") or 0))
        out["cd"].append(round(left / 1000.0, 2))
        out["ratio"].append(round(left / total, 3) if total > 0 else 0.0)
        out["ready"].append(bool(left <= 0) if local else bool(s.get("ready")))
        out["level"].append(int(s.get("level") or 0))
    return out


def button_vec(rec: Optional[dict]) -> Optional[Dict[str, float]]:
    """召唤师技能/恢复/回城槽 → 决策层字段（回城 total=0，只有 ready 有意义）。"""
    if not rec:
        return None
    total = int(rec.get("cd_total_ms") or 0)
    left = int(rec.get("cd_left_ms") or 0)
    return {"id": int(rec.get("id") or 0),
            "cd": round(left / 1000.0, 2),
            "ratio": round(left / total, 3) if total > 0 else 0.0,
            "ready": bool(rec.get("ready"))}


class MemBattle:
    """对局级内存读取器（在 MemSource 之上补"对局字段"）。"""

    def __init__(self, adb: Optional[str] = None, serial: str = "127.0.0.1:16384"):
        self.src = MemSource(adb=adb, serial=serial)
        self.pid: Optional[str] = None
        self._clock_addr: Optional[int] = None
        self._clock_pid: Optional[str] = None
        self._clock_last: Optional[tuple] = None   # (value, monotonic_ts)
        self._clock_fail_ts = 0.0
        self._lu = None            # 逻辑单元数组（0xD0），兵/野/塔坐标的补充来源
        self._lu_tried = False
        self._lu_reason = ""

    # ---------- 逻辑单元（兵/野/塔坐标） ----------
    def logic_units(self):
        """发现并校验 0xD0 逻辑单元数组；失败返回 None（不影响原有行为）。

        坐标纪律：actor 组件优先；兵/野/塔没有 pos 组件（actor+0x230 为 0），
        才用这里补。数组必须通过英雄坐标交叉验证才启用（见 logic_units.py）。
        开关：环境变量 WZRY_LOGIC_COORD=1（默认关，避免未验证路径影响线上行为）。
        """
        if self._lu_tried:
            return self._lu
        self._lu_tried = True
        import os
        if os.environ.get("WZRY_LOGIC_COORD", "") not in ("1", "true", "yes"):
            self._lu_reason = "未开启（WZRY_LOGIC_COORD）"
            return None
        try:
            from .logic_units import open_if_possible
            self._lu = open_if_possible(self.src.m, self.read_heroes())
            self._lu_reason = self._lu.reason if self._lu else "发现或校验失败"
        except Exception as e:                     # 任何异常都不影响主流程
            self._lu = None
            self._lu_reason = f"{type(e).__name__}: {e}"
        return self._lu

    # ---------- 连接 ----------
    def connect(self) -> str:
        self.pid = self.src.connect()
        return self.pid

    def _sh(self, cmd: str, timeout: int = 60) -> str:
        """跑一条 adb shell 命令（memscan 等）。"""
        out = self.src.m._run(["shell", cmd])
        if isinstance(out, bytes):
            out = out.decode("utf-8", "ignore")
        return out or ""

    def _pid(self) -> str:
        if not self.pid:
            self.connect()
        return self.pid  # type: ignore[return-value]

    # ---------- 局内时间 ----------
    def locate_clock(self, expected_s: int, force: bool = False) -> Optional[int]:
        """差分扫描定位"每秒 +1"的 u32。

        expected_s: 期望的局内秒数（用 wall clock 的 match_start_t 推得即可）。
                    它**只用来定扫描窗口**，不作为判据 —— 该值过期一两分钟也能命中。

        判据（按可靠性排序）：
          1) 结构签名：同一秒值在 stride 0x28 上连续出现 ≥6 次（10 个单位记录各存一份）；
          2) 时间签名：同一地址隔 2s 读数恰好 +2。
        两者都不满足 -> 返回 None，不缓存、不猜。
        """
        if self._clock_addr is not None and not force:
            return self._clock_addr
        now = time.time()
        if now - self._clock_fail_ts < 20 and not force:
            return None
        pid = self._pid()
        lo = max(1, int(expected_s) - CLOCK_SCAN_BACK)
        hi = int(expected_s) + CLOCK_SCAN_FWD
        out = self._sh(f"MEMSCAN_ONLY=scudo /data/local/tmp/memscan {pid} "
                       f"diff {lo} {hi} {CLOCK_SCAN_DELAY_MS} 8 12")
        hits: Dict[int, int] = {}
        for ln in out.splitlines():
            m = re.match(r"(0x[0-9a-f]+)\s+(\d+) -> (\d+)", ln.strip())
            if m:
                hits[int(m.group(1), 16)] = int(m.group(2))

        def chain_len(addr: int) -> int:
            v = hits[addr]
            n = 1
            while hits.get(addr + n * CLOCK_STRIDE) == v:
                n += 1
            return n

        # 1) 优先结构签名
        ranked = sorted(hits, key=chain_len, reverse=True)
        for addr in ranked[:6]:
            if chain_len(addr) < CLOCK_CHAIN_MIN:
                break
            if self._verify_ticking(addr):
                self._clock_addr, self._clock_pid = addr, pid
                return addr
        # 2) 退化：逐点时间签名（值必须落在窗口内，避免误认别的计数器）
        for addr in ranked[:20]:
            v = hits[addr]
            if not (lo <= v <= hi + 60):
                continue
            if self._verify_ticking(addr):
                self._clock_addr, self._clock_pid = addr, pid
                return addr
        self._clock_fail_ts = time.time()
        return None

    def _verify_ticking(self, addr: int, gap: float = 2.0) -> bool:
        """隔 gap 秒读两次，增量应等于**实测**经过的秒数。

        注意：必须用 time.monotonic() 量真实间隔，不能假定 sleep(gap) 就是 gap 秒 ——
        每次读要走一次 adb 往返，实际间隔常比 gap 大几秒；按名义 gap 比较会系统性判负。
        """
        t0 = time.monotonic()
        v1 = self._read_i32(addr)
        if v1 is None or not (0 <= v1 < 60 * 60 * 3):
            return False
        time.sleep(gap)
        v2 = self._read_i32(addr)
        dt = time.monotonic() - t0
        if v2 is None:
            return False
        return abs((v2 - v1) - dt) <= 1.5

    def read_clock(self) -> Optional[int]:
        """读局内秒数。未定位 -> None（**不猜**，调用方自己决定退路）。"""
        if self._clock_addr is None or self._clock_pid != self.pid:
            return None
        v = self._read_i32(self._clock_addr)
        if v is None or not (0 <= v < 60 * 60 * 3):
            self._clock_addr = None      # 指针失效 -> 下次重新定位
            return None
        # 单调性自检：时间只能往前走，回退说明读到了别的对象
        if self._clock_last is not None:
            pv, pt = self._clock_last
            dt = time.monotonic() - pt
            if v < pv or (dt > 3.0 and v - pv > dt * 3):
                self._clock_addr = None
                return None
        self._clock_last = (v, time.monotonic())
        return v

    # ---------- 基础读 ----------
    def _read_i32(self, addr: int) -> Optional[int]:
        try:
            b = self.src.m.read(addr, 4)
        except Exception:
            return None
        if not b or len(b) != 4:
            return None
        return struct.unpack("<i", b)[0]

    # ---------- 单位 ----------
    def read_units(self) -> List[dict]:
        """全部 actor -> 结构化单位（身份 / 阵营 / 血量 / 死亡 / 世界坐标）。

        hp / dead 由 attr 组件给出；非英雄单位没有位置组件（x/z 为 None），这是已知限制。
        """
        out: List[dict] = []
        try:
            snap: List[MemUnit] = self.src.snapshot()
        except Exception:
            return out
        for u in snap:
            cid = int(u.config_id or 0)
            out.append({
                "cid": cid,
                "kind": unit_kind(cid, int(u.actor_type or 0)),
                "camp": u.camp,
                "hp": u.hp,
                "hp_max": u.hp_max,
                "hp_ratio": (float(u.hp) / u.hp_max) if (u.hp is not None and u.hp_max) else None,
                "dead": bool(u.dead),
                "x": u.x,
                "z": u.z,
                "actor": u.actor,
                "coord_src": "actor" if u.x is not None else "",
                "spawn": ((u.spawn_x, u.spawn_y, u.spawn_z)
                          if u.spawn_x is not None else None),
            })
        self._fill_static_coords(out)
        self._fill_coords_from_logic(out)
        return out

    @staticmethod
    def _fill_static_coords(units: List[dict]) -> None:
        """静态建筑（塔/水晶/泉水）用出生点当坐标。

        为什么可以：这些单位不动，`actor+0x278` 的出生点就是当前位置（真机 3 秒采样
        0/34 变化，而英雄 pos 组件同期移动了 1094/13732）。**只对建筑类生效**，
        移动单位（兵/野/英雄）绝不用这个字段，否则会把编队点当成实时位置。
        """
        for u in units:
            if u.get("x") is not None or u.get("kind") != "turret":
                continue
            sp = u.get("spawn")
            if not sp or sp[0] is None:
                continue
            u["x"], u["z"] = sp[0], sp[2]
            u["coord_src"] = "spawn"

    def _fill_coords_from_logic(self, units: List[dict]) -> None:
        """给没有坐标的单位（兵/野/塔）补位置；只补缺失，绝不覆盖 actor 读数。"""
        lacking = [u for u in units if u.get("x") is None]
        if not lacking:
            return
        lu = self.logic_units()
        if lu is None:
            return
        idx = lu.index()
        camp_of = {"blue": 1, "red": 2}
        for u in lacking:
            key = (int(u.get("cid") or 0), camp_of.get(u.get("camp"), 0))
            pool = idx.get(key)
            if not pool:
                continue
            rec = pool.pop(0)                      # 同 cid 多个时按出现顺序一一对应
            u["x"], u["z"] = rec["x"], rec["z"]
            u["coord_src"] = "logic"

    def read_monsters(self) -> List[dict]:
        """在场野怪（**只报在场者**）。

        ⚠️ 语义边界：actor 列表是"当前存在的单位"。列表里没有某只野怪，
        只能说明"读不到"，**不能**推断"已被击杀" —— 营地未被激活/读取漏项都会这样。
        所以下游必须把 `alive=True` 当"确实活着"，把"缺席"当"未知"。
        """
        return [u for u in self.read_units() if u["kind"] == "monster"]

    def read_heroes(self) -> List[dict]:
        """10 名英雄：金币 / 等级 / 经验 / KDA / 血量 / 世界坐标（C# 数组）。"""
        try:
            hs = self.src.read_hero_states()
        except Exception:
            return []
        return list(hs or [])

    # ---------- 写入 state_dict ----------
    def patch_state(self, state_dict: dict, self_id: Optional[int] = None,
                    expected_clock: Optional[int] = None,
                    locate_clock: bool = False) -> dict:
        """把内存读数写进 m2 的 state_dict（**纯增量**，不覆盖视觉已有字段）。

        写入位置：
          ui.gold / ui.level / ui.exp / ui.kills / ui.deaths / ui.assists
          ui.match_clock                        （秒，整数）
          ui.enemy_hp_min                       （**精确**，仅在视觉缺失时补）
          ui.skill_cd / ui.skill_cd_ratio / ui.skill_ready   （本地英雄 3 个技能槽）
          extra.mem_heroes                      （10 名英雄全量）
          extra.mem_skills                      （本地英雄技能向量）
          extra.mem_hero_skills                 （10 名英雄技能向量，按 cid 索引）
          extra.mem_units                       （全部 actor）
          extra.mem_monsters                    （在场野怪）
          extra.mem_clock_s
        返回摘要 dict（便于日志/自检）。
        """
        ui = state_dict.setdefault("ui", {})
        extra = state_dict.setdefault("extra", {})
        summary: Dict[str, object] = {}

        # 1) 局内时间
        if locate_clock and expected_clock is not None:
            self.locate_clock(int(expected_clock))
        clk = self.read_clock()
        if clk is not None:
            ui["match_clock"] = int(clk)
            extra["mem_clock_s"] = int(clk)
            state_dict["clock_s"] = int(clk)
            summary["clock"] = int(clk)

        # 2) 英雄（经济/等级/KDA）
        heroes = self.read_heroes()
        if heroes:
            extra["mem_heroes"] = heroes
            me = None
            if self_id is not None:
                me = next((h for h in heroes if h.get("id") == self_id), None)
            if me is None:
                # 没给 self_id：取金币最小的当自己？不行 —— 不猜。只写全量。
                pass
            else:
                for k_src, k_dst in (("gold", "gold"), ("level", "level"), ("exp", "exp"),
                                     ("kills", "kills"), ("deaths", "deaths"),
                                     ("assists", "assists"), ("atk_range", "atk_range")):
                    v = me.get(k_src)
                    if isinstance(v, (int, float)):
                        ui[k_dst] = int(v)
                summary["self_gold"] = me.get("gold")
                summary["self_level"] = me.get("level")
                # 血量：**内存优先**（视觉血条会被遮挡/动画影响）。
                #   官方口径同样是"战斗逻辑单元的 hp/hp_max"，不是 HUD 血条。
                if me.get("hp") is not None and me.get("hp_max"):
                    ui["hp"] = round(float(me["hp"]) / float(me["hp_max"]), 4)
                    ui["hp_src"] = "mem"
                    ui["hp_abs"] = int(me["hp"])
                if isinstance(me.get("ep_rate"), (int, float)):
                    extra["mem_ep_rate"] = float(me["ep_rate"])
                    if ui.get("mp") is None:
                        ui["mp"] = float(me["ep_rate"])
                eq = list(me.get("equip_ids") or [])
                if eq:
                    extra["mem_equip"] = eq
                    ui["equip_count"] = len(eq)
                sv = skill_vec(me.get("skills"), local=True)
                if sv["id"]:
                    ui["skill_cd"] = sv["cd"]
                    ui["skill_cd_ratio"] = sv["ratio"]
                    ui["skill_ready"] = sv["ready"]
                    extra["mem_skills"] = sv
                    summary["skills"] = sv["id"]
                sm = button_vec(me.get("summoner"))
                if sm:
                    ui["summoner_id"] = sm["id"]
                    ui["summoner_cd"] = sm["cd"]
                    ui["summoner_cd_ratio"] = sm["ratio"]
                    ui["summoner_ready"] = sm["ready"]
                    extra["mem_summoner"] = sm
                hl = button_vec(me.get("heal"))
                if hl:
                    ui["heal_cd"] = hl["cd"]
                    ui["heal_ready"] = hl["ready"]
                    extra["mem_heal"] = hl
                rc = me.get("recall")
                if rc:
                    ui["recall_ready"] = bool(rc.get("ready"))
                    extra["mem_recall"] = {"id": int(rc.get("id") or 0),
                                           "ready": bool(rc.get("ready"))}
            # 其余 9 名英雄的技能 CD（敌方技能是否交掉，是"能不能打"的关键判据）
            hs_all = {h["id"]: skill_vec(h.get("skills"))
                      for h in heroes if h.get("skills")}
            if hs_all:
                extra["mem_hero_skills"] = hs_all
            bs = self.src.read_hero_base_stats()
            if bs:
                lv = {h["id"]: (h.get("level") or 1) for h in heroes}
                extra["mem_hero_stats"] = {
                    str(cid): stats_at_level(st, lv.get(cid, 1))
                    for cid, st in bs.items()}
            # 敌方血量比例（精确，优于视觉的条长估计）
            my_camp = (me or {}).get("camp")
            if my_camp:
                enemy = [h for h in heroes
                         if h.get("camp") and h.get("camp") != my_camp
                         and h.get("hp_max")]
                ratios = [h["hp"] / h["hp_max"] for h in enemy if h.get("hp") is not None]
                if ratios:
                    extra["mem_enemy_hp"] = {h["id"]: round(h["hp"] / h["hp_max"], 4)
                                             for h in enemy if h.get("hp") is not None}
                    if ui.get("enemy_hp_min") is None:
                        ui["enemy_hp_min"] = round(min(ratios), 4)
                    summary["enemy_hp_min"] = round(min(ratios), 4)
                ally = [h for h in heroes if h.get("camp") == my_camp and h.get("hp_max")]
                aratios = [h["hp"] / h["hp_max"] for h in ally if h.get("hp") is not None]
                if aratios and ui.get("ally_hp_min") is None:
                    ui["ally_hp_min"] = round(min(aratios), 4)

            # 统一 schema v1.1：同一次内存读数同时写 canonical 字段，
            # 旧 ui.* / extra.mem_* 保留给现有决策层，不覆盖视觉字段。
            state_dict.setdefault("schema_version", "1.1")
            state_dict["scoreboard"] = [
                {
                    "cid": h.get("id"), "camp": h.get("camp"),
                    "gold": h.get("gold"), "gold_total": h.get("gold_total"),
                    "gold_spent": h.get("gold_spent"), "level": h.get("level"),
                    "exp": h.get("exp"), "kills": h.get("kills", 0),
                    "deaths": h.get("deaths", 0), "assists": h.get("assists", 0),
                    "hp": h.get("hp"), "hp_max": h.get("hp_max"),
                    "equip": list(h.get("equip_ids") or []),
                    "respawn_s": h.get("respawn_s"), "dead": bool(h.get("dead")),
                    "x": h.get("x"), "z": h.get("z"), "src": "memory",
                }
                for h in heroes
            ]
            sources = state_dict.get("sources")
            if not isinstance(sources, dict):
                sources = {}
                state_dict["sources"] = sources
            sources["scoreboard"] = "memory"
            if me is not None:
                state_dict["self_state"] = {
                    "cid": me.get("id"), "camp": me.get("camp"),
                    "hp": me.get("hp"), "hp_max": me.get("hp_max"),
                    "mp": me.get("ep"), "mp_max": me.get("ep_max"),
                    "gold": me.get("gold"), "gold_total": me.get("gold_earned"),
                    "gold_spent": me.get("gold_spent"),
                    "level": me.get("level"), "exp": me.get("exp"),
                    "kills": me.get("kills", 0), "deaths": me.get("deaths", 0),
                    "assists": me.get("assists", 0),
                    "atk_range": me.get("atk_range"),
                    "skills": [MemSource._canonical_skill(s)
                               for s in (me.get("skills") or [])],
                    "summoner": (MemSource._canonical_skill(me["summoner"])
                                 if me.get("summoner") else None),
                    "heal": (MemSource._canonical_skill(me["heal"])
                             if me.get("heal") else None),
                    "recall": (MemSource._canonical_skill(me["recall"])
                               if me.get("recall") else None),
                    "equip": list(me.get("equip_ids") or []),
                    "respawn_s": me.get("respawn_s"),
                    "dead": bool(me.get("dead")),
                    "x": me.get("x"), "z": me.get("z"),
                    "visible": True, "src": "memory", "ts": time.time(),
                }
                state_dict["sources"]["self"] = "memory"

        # 3) 单位 / 野怪
        units = self.read_units()
        if units:
            extra["mem_units"] = units
            mons = [u for u in units if u["kind"] == "monster"]
            extra["mem_monsters"] = mons
            summary["n_units"] = len(units)
            summary["monsters"] = [(u["cid"], u["hp"], u["dead"]) for u in mons]
            # 野怪存活（只报"在场且未死"，缺席=未知）
            alive = [u for u in mons if not u["dead"] and (u["hp"] is None or u["hp"] > 0)]
            extra["mem_monsters_alive"] = [u["cid"] for u in alive]

        state_dict["t_state"] = time.time()
        return summary


# ---------------------------------------------------------------- 后台刷新
class MemWorker:
    """后台按固定间隔刷新内存快照，主循环只取最新值。

    **为什么必须后台化**：`snapshot()` 每帧 250–330ms、`read_hero_states()` 50–60ms、
    `locate_clock()` 首次要 10~15s（差分扫描）。这些放进主循环会把 8.5Hz 的感知节拍
    直接压到 1Hz 以下。所以：读在后台，主循环只做一次 dict 合并（零 adb 往返）。

    语义：
      - `latest` 是"最近一次成功读数"，带 `ts`；
      - 超过 `stale_after` 秒没更新 -> `apply()` 什么都不写（宁可缺，不给陈旧值）；
      - `locate_clock()` 只在第一轮跑一次（后台），失败后每 30s 重试，最多 3 次。
    """

    def __init__(self, mb: MemBattle, self_id: Optional[int] = None,
                 interval: float = 0.5, stale_after: float = 6.0,
                 expected_clock_fn=None):
        self.mb = mb
        self.self_id = self_id
        self.interval = interval
        self.stale_after = stale_after
        self.expected_clock_fn = expected_clock_fn   # () -> int|None（用 wall clock 推）
        self.latest: Dict[str, object] = {}
        self.ts = 0.0
        self.err: Optional[str] = None
        self.clock_err: Optional[str] = None   # 时钟扫描专用：不被 _run 的成功轮擦掉
        self.n_pub = 0                 # 成功发布的快照数（自检用）
        self._stop = threading.Event()
        self._th: Optional[threading.Thread] = None
        self._cth: Optional[threading.Thread] = None
        self._clock_tries = 0

    def start(self):
        if self._th and self._th.is_alive():
            return self
        self._th = threading.Thread(target=self._run, name="mem-worker", daemon=True)
        self._th.start()
        # 时钟定位单独一条线程：它要跑 10~15s 的差分扫描，绝不能挡住快照发布，
        # 否则 apply() 会因"快照陈旧"整段时间什么都不写（实测 80 帧里只成功 7 帧）。
        if self.expected_clock_fn is not None:
            self._cth = threading.Thread(target=self._clock_run, name="mem-clock", daemon=True)
            self._cth.start()
        return self

    def _clock_run(self):
        # 扫描失败才算一次尝试；"还没进对局所以拿不到锚点"只是等待，不烧次数。
        # 旧实现是一个 for _ in range(3)：进程在开局前启动时，三次等待全花在大厅里，
        # 进局后再也不扫，整局 clock_s 恒缺（实测 1284 帧 0 命中）。
        tries = 0
        while not self._stop.is_set() and tries < 3:
            if self.mb.read_clock() is not None:
                return
            try:
                exp = self.expected_clock_fn()
            except Exception:
                exp = None
            if exp is None:
                self._stop.wait(2.0)
                continue
            tries += 1
            try:
                self.mb.locate_clock(int(exp))
            except Exception as e:
                self.clock_err = f"clock: {type(e).__name__}: {e}"
            if self.mb.read_clock() is not None:
                return
            self._stop.wait(5.0)

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                snap = {
                    "clock": self.mb.read_clock(),
                    "heroes": self.mb.read_heroes(),
                    "units": self.mb.read_units(),
                }
                if snap["heroes"] or snap["units"]:
                    self.latest = snap
                    self.ts = time.time()
                    self.n_pub += 1
                    self.err = None
                    # 时钟是唯一没有视觉旁路的字段（HUD 计时器从不进 state），
                    # 扫不到就整局静默缺列，所以入局 30s 仍无值时显式喊一次。
                    if snap["clock"] is None and self.n_pub == 60:
                        print(f"[mem] 时钟未定位: {self.clock_err or '差分扫描无命中'}"
                              f" —— 本局 clock_s 将全程缺失", flush=True)
            except Exception as e:      # 读失败不清空 latest（下一轮再试）
                self.err = f"{type(e).__name__}: {e}"
            self._stop.wait(self.interval)

    # ---- 主循环侧（无 adb 往返）----
    def apply(self, state_dict: dict) -> dict:
        """把最新快照合并进 state_dict（纯增量，不覆盖视觉已有字段）。"""
        if not self.latest or (time.time() - self.ts) > self.stale_after:
            return {}
        ui = state_dict.setdefault("ui", {})
        extra = state_dict.setdefault("extra", {})
        summary: Dict[str, object] = {}

        sources = state_dict.setdefault("sources", {})
        clk = self.latest.get("clock")
        if isinstance(clk, int) and clk >= 0:
            ui["match_clock"] = clk
            extra["mem_clock_s"] = clk
            # canonical 字段：录制/闸门读的是顶层 clock_s，光写 ui.match_clock
            # 会让 coverage["clock_s"] 永远是 0（历史 44 局 0.15% 的直接原因）。
            if state_dict.get("clock_s") is None:
                state_dict["clock_s"] = int(clk)
                sources["clock_s"] = "memory"
            summary["clock"] = clk

        heroes = self.latest.get("heroes") or []
        if heroes:
            extra["mem_heroes"] = heroes
            # canonical scoreboard（patch_state 里早就有，apply 漏了 —— 这就是
            # 历史 scoreboard 覆盖率 0% 的原因）。字段与 patch_state 保持一致。
            if not state_dict.get("scoreboard"):
                state_dict["scoreboard"] = [
                    {"cid": h.get("id"), "camp": h.get("camp"),
                     "gold": h.get("gold"), "gold_total": h.get("gold_total"),
                     "gold_spent": h.get("gold_spent"), "level": h.get("level"),
                     "exp": h.get("exp"), "kills": h.get("kills", 0),
                     "deaths": h.get("deaths", 0), "assists": h.get("assists", 0),
                     "hp": h.get("hp"), "hp_max": h.get("hp_max"),
                     "equip": list(h.get("equip_ids") or []),
                     "respawn_s": h.get("respawn_s"), "dead": bool(h.get("dead")),
                     "x": h.get("x"), "z": h.get("z"), "src": "memory"}
                    for h in heroes
                ]
                sources["scoreboard"] = "memory"
            me = None
            if self.self_id is not None:
                me = next((h for h in heroes if h.get("id") == self.self_id), None)
            if me is None:
                # 实测两局：数组 slot 0 都是本地玩家（小乔）。仅作兜底，self_id 优先。
                me = heroes[0]
            for k in ("gold", "level", "exp", "kills", "deaths", "assists"):
                v = me.get(k)
                if isinstance(v, (int, float)):
                    ui[k] = int(v)
            if isinstance(me.get("atk_range"), (int, float)) and me["atk_range"] > 0:
                ui["atk_range"] = float(me["atk_range"])   # 射程（世界单位），官方也是单位自身属性
            # 血量：内存优先（同 patch_state；视觉血条只作对照）
            if me.get("hp") is not None and me.get("hp_max"):
                ui["hp"] = round(float(me["hp"]) / float(me["hp_max"]), 4)
                ui["hp_src"] = "mem"
                ui["hp_abs"] = int(me["hp"])
            if isinstance(me.get("ep_rate"), (int, float)):
                extra["mem_ep_rate"] = float(me["ep_rate"])
                if ui.get("mp") is None:                  # 视觉没读到才补（比值口径）
                    ui["mp"] = float(me["ep_rate"])
            # 金币双口径（v16.3）：gold=可用（HUD 口径，item_policy 要的就是这个）；
            # gold_total=总经济；gold_spent=已花费（= 总经济 − 可用，精确值）。
            if isinstance(me.get("gold_total"), (int, float)):
                ui["gold_total"] = int(me["gold_total"])
            if isinstance(me.get("gold_spent"), (int, float)):
                ui["gold_spent"] = int(me["gold_spent"])
            eq = list(me.get("equip_ids") or [])
            if eq:
                extra["mem_equip"] = eq
                ui["equip_count"] = len(eq)
            sv = skill_vec(me.get("skills"), local=True)
            if sv["id"]:
                ui["skill_cd"] = sv["cd"]
                ui["skill_cd_ratio"] = sv["ratio"]
                ui["skill_ready"] = sv["ready"]
                extra["mem_skills"] = sv
            sm = button_vec(me.get("summoner"))
            if sm:
                ui["summoner_id"] = sm["id"]
                ui["summoner_cd"] = sm["cd"]
                ui["summoner_cd_ratio"] = sm["ratio"]
                ui["summoner_ready"] = sm["ready"]
                extra["mem_summoner"] = sm
            hl = button_vec(me.get("heal"))
            if hl:
                ui["heal_cd"] = hl["cd"]
                ui["heal_ready"] = hl["ready"]
                extra["mem_heal"] = hl
            rc = me.get("recall")
            if rc:
                ui["recall_ready"] = bool(rc.get("ready"))
                extra["mem_recall"] = {"id": int(rc.get("id") or 0),
                                       "ready": bool(rc.get("ready"))}
            hs_all = {h["id"]: skill_vec(h.get("skills"))
                      for h in heroes if h.get("skills")}
            if hs_all:
                extra["mem_hero_skills"] = hs_all
            bs = self.src.read_hero_base_stats()
            if bs:
                lv = {h["id"]: (h.get("level") or 1) for h in heroes}
                extra["mem_hero_stats"] = {
                    str(cid): stats_at_level(st, lv.get(cid, 1))
                    for cid, st in bs.items()}
            summary["self"] = {k: me.get(k) for k in
                               ("id", "gold", "gold_total", "level", "exp")}
            my_camp = me.get("camp")
            if my_camp:
                enemy = [h for h in heroes if h.get("camp") and h.get("camp") != my_camp
                         and h.get("hp_max") and h.get("hp") is not None]
                if enemy:
                    r = {h["id"]: round(h["hp"] / h["hp_max"], 4) for h in enemy}
                    extra["mem_enemy_hp"] = r
                    if ui.get("enemy_hp_min") is None:      # 视觉没读到才补
                        ui["enemy_hp_min"] = min(r.values())
                ally = [h for h in heroes if h.get("camp") == my_camp
                        and h.get("hp_max") and h.get("hp") is not None]
                if ally and ui.get("ally_hp_min") is None:
                    ui["ally_hp_min"] = min(round(h["hp"] / h["hp_max"], 4) for h in ally)

        units = self.latest.get("units") or []
        if units:
            extra["mem_units"] = units
            mons = [u for u in units if u["kind"] == "monster"]
            extra["mem_monsters"] = mons
            extra["mem_monsters_alive"] = [u["cid"] for u in mons
                                           if not u["dead"] and (u["hp"] is None or u["hp"] > 0)]
            summary["n_units"] = len(units)
        return summary


# ---------------------------------------------------------------- CLI
def _probe(args) -> int:
    mb = MemBattle(serial=args.serial)
    pid = mb.connect()
    print(f"pid={pid}")
    t0 = time.time()
    if args.clock is not None:
        a = mb.locate_clock(int(args.clock), force=True)
        print(f"clock_addr={'0x%x' % a if a else None}  ({time.time() - t0:.1f}s)")
    print("clock_s =", mb.read_clock())

    hs = mb.read_heroes()
    print(f"heroes={len(hs)}")
    for h in hs:
        print(f"  cid={h['id']:<5} lv={h['level']:<3} gold={h['gold']:<6} "
              f"hp={h['hp']}/{h['hp_max']:<6} kda={h['kills']}/{h['deaths']}/{h['assists']:<3} "
              f"camp={h['camp']} pos=({h['x']},{h['z']})")

    us = mb.read_units()
    print(f"units={len(us)}")
    for u in us:
        print(f"  {u['kind']:<8} cid={u['cid']:<6} camp={u['camp']:<4} "
              f"hp={u['hp']}/{u['hp_max']} dead={u['dead']}")

    mons = mb.read_monsters()
    print(f"monsters(在场)={len(mons)} -> {[m['cid'] for m in mons]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="127.0.0.1:16384")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--clock", type=int, default=None, help="HUD 上看到的局内秒数，用于定位")
    args = ap.parse_args()
    return _probe(args)


if __name__ == "__main__":
    raise SystemExit(main())
