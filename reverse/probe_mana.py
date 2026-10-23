# -*- coding: utf-8 -*-
"""定向实验：找"当前法力 / 技能剩余 CD / 装备栏"。

背景：`attr` 组件（slot8）里 `+0xc0/+0xc8` 是最大/当前 HP，
但**当前法力不在 slot8**（`+0x110/+0x128` 是上限常量、`+0x1A0` 是静态配置）。
所以必须把 15 个组件槽全部 dump，用"施法/购物前后差分"来定位。

用法（三种模式，按可信度从高到低）：

    # 1) 施法差分（最可信）：先 dump -> 点技能 -> 再 dump -> 打印变化字段
    venv/Scripts/python.exe -X utf8 reverse/probe_mana.py --tap 1500 712
    #    （把 --tap 换成你的技能 1 按钮坐标；脚本会等 0.4s 再读）

    # 2) 外部施法差分：你自己在手机上施法，脚本在 3s 窗口内高频采样
    venv/Scripts/python.exe -X utf8 reverse/probe_mana.py --watch 8

    # 3) 长时观察（找锯齿/倒计时）：对每个字段统计"下降后回升"的次数
    venv/Scripts/python.exe -X utf8 reverse/probe_mana.py --watch 40 --sawtooth

判读要点：
  - **当前法力**：施法瞬间**下降**（幅度 30~400），随后**单调回升**（回复 ~5-40/s）。
  - **技能剩余 CD**：施法瞬间从 0 **跳到 N**，然后**线性降到 0**（单位可能是秒或毫秒）。
  - 排除项：`slot8+0xb0` 是 ~200/s 的循环计数器（会回绕），不是法力；
    `slot7+0x130` 是单调递增的帧计时器（Q16.16）；`slot0+0x1b0`/`slot4+0x158`
    是同一个 +1 计数器的两份拷贝。

告警（实测 2026-10-20 23:10）：以 2~3Hz 连续 dump 15 个组件槽（约 7.5KB/次）
持续 60s 后，游戏进程重启（pid 62263 -> 35449），对局丢失。**先单次差分，确认有信号
再做长时观察**；长时观察把 `--hz` 压到 1.0。

输出示例：
    slot4 +0x158  1043 -> 712 -> 719 -> 726 ...   ← 命中：法力
"""
from __future__ import annotations

import argparse
import struct
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from wzry.vision.mem_source import MemSource  # noqa: E402

ADB = str(_ROOT / "tools" / "scrcpy" / "adb.exe")
SER = "127.0.0.1:16384"
N_SLOTS = 15
SLOT_STRIDE = 0x18
SLOT_BASE = 0x110
SLOT_LEN = 0x200


def slot_ptrs(src: MemSource, actor: int) -> dict:
    out = {}
    for k in range(N_SLOTS):
        p = src._u64(actor + SLOT_BASE + k * SLOT_STRIDE)
        if p:
            out[k] = p
    return out


def dump(src: MemSource, ptrs: dict) -> dict:
    out = {}
    for k, p in ptrs.items():
        try:
            b = src.m.read(p, SLOT_LEN)
        except Exception:
            continue
        if len(b) < SLOT_LEN:
            continue
        for off in range(0, SLOT_LEN, 4):
            out[(k, off)] = struct.unpack_from("<i", b, off)[0]
    return out


def pick_actor(src: MemSource, cid: int | None):
    snap = [u for u in src.snapshot() if u.actor_type == 0]
    if not snap:
        return None, None
    if cid is None:
        hs = src.read_hero_states()
        cid = hs[0]["id"] if hs else snap[0].config_id
    u = next((x for x in snap if x.config_id == cid), None)
    return (u, cid) if u else (None, cid)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default=SER)
    ap.add_argument("--cid", type=int, default=None, help="英雄配置 ID（默认取数组 slot0=本地玩家）")
    ap.add_argument("--tap", nargs=2, type=int, default=None,
                    metavar=("X", "Y"), help="dump 后点这个坐标（施法），再 dump")
    ap.add_argument("--watch", type=float, default=0.0, help="持续观察秒数")
    ap.add_argument("--hz", type=float, default=2.0, help="观察采样频率（别开太高，见文末告警）")
    ap.add_argument("--sawtooth", action="store_true", help="统计锯齿（下降后回升）次数")
    args = ap.parse_args()

    src = MemSource(adb=ADB, serial=args.serial)
    src.connect()
    u, cid = pick_actor(src, args.cid)
    if not u:
        print(f"找不到英雄 actor（cid={cid}）")
        return 1
    ptrs = slot_ptrs(src, u.actor)
    print(f"cid={cid} actor=0x{u.actor:x} hp={u.hp}/{u.hp_max} slots={sorted(ptrs)}")

    # ---- 模式 1：施法差分 ----
    if args.tap:
        before = dump(src, ptrs)
        print(f"点击 ({args.tap[0]},{args.tap[1]}) 施法 ...")
        subprocess.run([ADB, "-s", args.serial, "shell",
                        f"input tap {args.tap[0]} {args.tap[1]}"])
        time.sleep(0.4)
        after = dump(src, ptrs)
        diff = [(k, o, before[(k, o)], after[(k, o)])
                for (k, o) in before if before[(k, o)] != after.get((k, o))]
        print(f"变化字段 {len(diff)} 个：")
        for k, o, a, b in sorted(diff):
            tag = "  <= 疑似法力" if (b < a and 5 <= (a - b) <= 3000) else ""
            print(f"  slot{k:<2} {hex(o):<6} {a:>10} -> {b:<10}{tag}")
        return 0

    # ---- 模式 2/3：时间序列 ----
    n = max(1, int(args.watch * args.hz))
    ser: dict = {}
    t0 = time.time()
    for _ in range(n):
        d = dump(src, ptrs)
        for k, v in d.items():
            ser.setdefault(k, []).append(v)
        time.sleep(1.0 / max(0.2, args.hz))
    dt = time.time() - t0
    print(f"采样 {n} 帧 / {dt:.1f}s")

    def downs(v):
        """统计"明显下降"次数（下降 > 20 且随后回升）。"""
        c = 0
        for i in range(1, len(v) - 1):
            if v[i - 1] - v[i] > 20 and v[i + 1] > v[i]:
                c += 1
        return c

    rows = []
    for (k, o), v in ser.items():
        if len(v) < 4 or len(set(v)) == 1:
            continue
        rows.append((k, o, min(v), max(v), len(set(v)), downs(v), v[:6]))
    rows.sort(key=lambda r: (-r[5], -r[4]))
    print(f"{'slot':<5}{'off':<8}{'min':>9}{'max':>9}{'uniq':>6}{'锯齿':>5}  样例")
    for k, o, lo, hi, uq, sw, sample in rows[:25]:
        print(f"slot{k:<2}{hex(o):<8}{lo:>9}{hi:>9}{uq:>6}{sw:>5}  {sample}")
    if args.sawtooth:
        cand = [r for r in rows if r[5] >= 2]
        print(f"\n锯齿候选（>=2 次下降后回升）：{len(cand)} 个")
        for k, o, lo, hi, uq, sw, _ in cand:
            print(f"  slot{k} {hex(o)}  range=[{lo},{hi}] 锯齿={sw}  <- 优先怀疑法力")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
