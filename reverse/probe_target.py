# -*- coding: utf-8 -*-
"""受控实验探针：定位「当前锁定目标 / 上次攻击目标」。

为什么前两轮直接扫会失败
------------------------
前两轮找的是"值等于场上英雄 config_id（106 / 107 这类）"的字段。这个判据本身有问题：
**目标几乎不可能用 config_id 存**（同英雄镜像局会撞车，且 config_id 是静态属性），
引擎惯用的是 **actor 指针** 或 **actor handle（runtime id）**。所以本版不预设值域，改为
把三类候选同时喂进去比对：

  A. actor 指针（8 字节，等于场上某个 actor 基址）；
  B. actor 头部的 u32 值集合（自动采集全部在场单位，覆盖 runtime_id / handle）；
  C. hero config_id（兜底 + 反证：如果只命中 C，说明我们又找错了）。

采样对象同时包含**英雄记录**（stride 0x2AD8，10 槽）与**英雄 actor 头部**（0x260），
因为目标既可能挂在逻辑状态对象上，也可能挂在 actor 上。

采集协议（人只做一件事：切换目标）
----------------------------------
    # 进训练营/人机，站到能同时看到「小兵 > 1 个」或「敌人」的位置
    venv/Scripts/python.exe -X utf8 reverse/probe_target.py --secs 24

    看到 "采样中" 之后按时间线操作：
      0–6s    不操作（静态基线，目标应为 0 / 无）
      6–12s   普攻 A 目标（一直按住）
      12–18s  点头像或点另一个目标，继续普攻（这 6s 内完成的切换就是我们要的那一刀）
      18–24s  停止攻击、人物站住（回到基线）

判据（三条同时满足才算定性，只满足 1 的只记为候选）
----------------------------------------------------
  1. 某偏移出现 2–4 次**阶跃**（不是连续变化），且阶跃后的值 ∈ 候选集 A/B；
  2. 首尾静态段恒为同值（通常 0）；
  3. 该值能在 actor 表里唯一回指到**一个**在场单位（唯一性检验，防止撞到 hash/计数器）。

用法补充
--------
    --save NAME      把采样结果存 temp/probe_target/NAME.npz
    --analyze NAME   离线重跑分析（换 --max-changes 等参数不必重新采样）
    --selftest       合成序列自检（不需要设备；验证阶跃检测本身对不对）
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision.mem_source import (HERO_ARR_COUNT, HERO_ARR_STRIDE,  # noqa: E402
                                    SELF_OBJ_ID, MemSource)

CACHE = ROOT / "temp" / "probe_target"
ACTOR_HDR = 0x260
WORDS = HERO_ARR_STRIDE // 4          # 0x2AD8/4 = 2742 列
AHDW = ACTOR_HDR // 4                 # actor 头 152 列
# actor 的 15 个组件槽（0x110 + k*0x18）：目标也可能是组件里的 handle，而不是记录里的字段
COMP_SLOTS = 15
COMP_BASE = 0x110
COMP_STRIDE = 0x18
COMP_HDR = 0x100
CHW = COMP_HDR // 4
ID_HDR = 0x400                        # 找 runtime_id 要看得比 0x100 更深
PLAN_GAP = 0x8000                     # dd 区间合并阈值：太小会把帧率拖到 1 fps 以下

# 已知字段：命中这些偏移的阶跃一律降级到"对照组"，不参与新字段判定。
# （不是丢弃 —— 万一目标就挨着技能槽，还得能看见。）
KNOWN = {
    0x16C: "id", 0x184: "x", 0x188: "y", 0x18C: "z",
    0x19C: "level", 0x1A0: "atk_range", 0x1A4: "hp", 0x1A8: "hp_max",
    0x1BC: "exp", 0x1C4: "ep", 0x1C8: "ep_max",
    0x1F4: "kills", 0x1F8: "deaths", 0x1FC: "assists",
    0x200: "gold_total", 0x15D0: "gold", 0x15D4: "respawn_s",
    0x1928: "equip_count", 0x192C: "equip_ids",
    0x238: "上轮已排除", 0x348: "上轮已排除", 0x380: "上轮已排除",
    0x15DC: "上轮已排除",
}
for _k in range(7):                        # 技能 7 槽 × 0x78
    for _o in (0x08, 0x0C, 0x10, 0x14, 0x18):
        KNOWN[0x19D4 + _k * 0x78 + _o] = f"skill{_k + 1}"


# ---------------------------------------------------------------- 纯分析逻辑
def stepped(mat: np.ndarray, max_changes: int = 10, min_run: int = 2):
    """在 (T, W) 的 u32 矩阵里找"阶跃型"列。

    返回 [{col, runs, changes}]
      runs    [(start_frame, end_frame_exclusive, value)]
      changes [(frame, old, new)]
    只保留：变化次数 1..max_changes、且**每一段**都至少 min_run 帧
    （连续量如 HP/CD 会被段长或变化次数任一条件挡掉）。
    """
    if mat.ndim != 2 or mat.shape[0] < 4:
        return []
    diff = mat[1:] != mat[:-1]
    cols = np.flatnonzero(diff.any(axis=0))
    out = []
    for c in cols:
        ch = (np.flatnonzero(diff[:, c]) + 1).tolist()
        if not ch or len(ch) > max_changes:
            continue
        starts = [0] + ch
        ends = ch + [mat.shape[0]]
        runs, ok = [], True
        for s, e in zip(starts, ends):
            if e - s < min_run:
                ok = False
                break
            runs.append((s, e, int(mat[s, c])))
        if not ok or len({v for _, _, v in runs}) < 2:
            continue
        out.append({
            "col": int(c),
            "runs": runs,
            "changes": [(ch[i], runs[i][2], runs[i + 1][2]) for i in range(len(ch))],
        })
    return out


def _me(cid, self_cid: int) -> str:
    return " ★本人" if self_cid and int(cid) == int(self_cid) else ""


def _fmt_runs(runs, limit=6) -> str:
    vals = [v for _, _, v in runs]
    if len(vals) > limit:
        vals = vals[:limit] + ["…"]
    return " → ".join(str(v) for v in vals)


def _match(values, cand: dict, min_unique: int = 1):
    """values 里有多少落在候选集合内，返回 (命中数, 命中的候选标签)。"""
    hit, tags = 0, set()
    for v in values:
        for tag, s in cand.items():
            if v in s:
                hit += 1
                tags.add(tag)
    return hit, sorted(tags)


# ---------------------------------------------------------------- 采样
def _hero_actors(src):
    units = [u for u in src.snapshot() if u.is_hero]
    return {u.config_id: u.actor for u in units}, {u.config_id: u.camp for u in units}


def _cluster(spans, gap: int = 0x400):
    """把邻近区间合并成少数几个 span，减少 dd 次数（组件常相邻分配）。"""
    out = []
    for s, n in sorted(spans):
        if out and s <= out[-1][0] + out[-1][1] + gap:
            end = max(out[-1][0] + out[-1][1], s + n)
            out[-1] = (out[-1][0], end - out[-1][0])
        else:
            out.append((s, n))
    return out


def _read_plan(m, plan, gap: int = 0x400):
    """plan: [(addr, size, key)]，一次 adb 往返读完，返回 {key: bytes}。

    gap = 相邻区间合并阈值。dd 是"一个 span 一个进程"，span 数直接决定帧率：
    实测 148 个区间在 gap=0x400 下只有 0.7 fps，gap=0x8000 能压到个位数 span。
    """
    if not plan:
        return {}
    spans = _cluster([(a, n) for a, n, _ in plan], gap)
    blobs = m.read_spans(spans) or []
    if len(blobs) != len(spans):
        return {}
    starts = [s for s, _ in spans]
    got = {}
    import bisect
    for addr, size, key in plan:
        i = bisect.bisect_right(starts, addr) - 1
        if i < 0:
            continue
        st, data = blobs[i]
        # read_spans 返回的 start 是**未页对齐**的原地址，但 data 从页首开始
        o = addr - (st & ~0xFFF)
        if o < 0 or o + size > len(data):
            continue
        got[key] = data[o:o + size]
    return got


def _comp_ptrs(src, addrs):
    """读每个英雄 actor 的 15 个组件指针（0x110 + k*0x18），过滤非法地址。"""
    out = [[0] * COMP_SLOTS for _ in addrs]
    idx = [i for i, a in enumerate(addrs) if a]
    plan = [(addrs[i] + COMP_BASE, COMP_SLOTS * COMP_STRIDE, i) for i in idx]
    got = _read_plan(src.m, plan, PLAN_GAP)
    for i, buf in got.items():
        for k in range(COMP_SLOTS):
            v = int.from_bytes(buf[k * COMP_STRIDE:k * COMP_STRIDE + 8], "little")
            if v and src._region(v):
                out[i][k] = v
    return out


def sample(src: MemSource, secs: float, interval: float = 0.0,
           self_cid: int = 0, only_self: bool = True, read_comp: bool = True):
    hs = src.read_hero_states()
    if len(hs) < HERO_ARR_COUNT:
        raise SystemExit(f"只读到 {len(hs)}/10 名英雄：当前不在对局中")
    base = src._hero_arrays[0]
    actor_of, camp_of = _hero_actors(src)
    cids = [h["id"] for h in hs]
    slots = [h["slot"] for h in hs]
    addrs = [actor_of.get(c) or 0 for c in cids]
    comps = _comp_ptrs(src, addrs)
    print(f"数组 0x{base:x}  slot={slots}  cids={cids}", file=sys.stderr)
    print(f"actor={[hex(a) for a in addrs]}", file=sys.stderr)
    print(f"组件指针 {sum(1 for row in comps for c in row if c)}/{HERO_ARR_COUNT * COMP_SLOTS}",
          file=sys.stderr)

    idx = [i for i, a in enumerate(addrs) if a]
    if only_self and idx:
        # 目标只对"本人"有意义；AI 槽位的目标切换频繁，采了也是噪声，还拖慢帧率
        si = cids.index(self_cid) if self_cid in cids else 0
        idx = [i for i in idx if i == si]
    print(f"采样槽位 {idx}（only_self={only_self}）", file=sys.stderr)
    plan = [(addrs[i], ACTOR_HDR, ("act", i)) for i in idx]
    if read_comp:
        for i in idx:
            for k, c in enumerate(comps[i]):
                if c:
                    plan.append((c, COMP_HDR, ("comp", i, k)))

    rec_frames, act_frames, comp_frames, stamps = [], [], [], []
    t0 = time.time()
    last_report = 0.0
    while True:
        ts = time.time() - t0
        if ts >= secs:
            break
        rec = src.m.read(base, HERO_ARR_STRIDE * HERO_ARR_COUNT)
        if len(rec) < HERO_ARR_STRIDE * HERO_ARR_COUNT:
            continue
        got = _read_plan(src.m, plan, PLAN_GAP)
        if not got:
            continue
        act = np.zeros((HERO_ARR_COUNT, AHDW), np.uint32)
        comp = np.zeros((HERO_ARR_COUNT, COMP_SLOTS, CHW), np.uint32)
        for key, buf in got.items():
            if key[0] == "act":
                if len(buf) >= ACTOR_HDR:
                    act[key[1]] = np.frombuffer(buf, np.uint32)
            elif len(buf) >= COMP_HDR:
                comp[key[1], key[2]] = np.frombuffer(buf, np.uint32)
        rec_frames.append(np.frombuffer(rec, np.uint32).reshape(HERO_ARR_COUNT, WORDS))
        act_frames.append(act)
        if read_comp:
            comp_frames.append(comp)
        stamps.append(ts)
        if ts - last_report >= 3.0:
            last_report = ts
            print(f"  t={ts:5.1f}s frames={len(stamps)}", file=sys.stderr)
        if interval:
            time.sleep(interval)
    if not stamps:
        raise SystemExit("没有采到任何帧")
    fps = len(stamps) / max(stamps[-1], 1e-6)
    print(f"采样完成 {len(stamps)} 帧 / {stamps[-1]:.1f}s ≈ {fps:.1f} fps", file=sys.stderr)
    return {
        "rec": np.stack(rec_frames), "act": np.stack(act_frames),
        "comp": np.stack(comp_frames) if comp_frames else np.zeros(0, np.uint32),
        "ts": np.array(stamps), "cids": np.array(cids),
        "slots": np.array(slots), "addrs": np.array(addrs, np.uint64),
        "camp": np.array([str(camp_of.get(c)) for c in cids]),
    }


def collect_actor_ids(src: MemSource):
    """全场 actor 头部 u32 词表：value -> [(actor, off), ...]。

    返回偏移很重要：目标字段的值一定来自 actor 头部的**某一个固定偏移**（runtime_id 的宿主）。
    只记"值出现过"会把 925 个小整数都当成候选，假阳性淹掉真命中。
    """
    _, _, _cnt, actors = src._actor_table()
    actors = [a for a in actors if a]
    if not actors:
        return {}, set()
    plan = [(a, ID_HDR, i) for i, a in enumerate(actors)]
    got = _read_plan(src.m, plan, PLAN_GAP)
    table: dict[int, list[tuple[int, int]]] = {}
    for i, buf in got.items():
        a = actors[i]
        for o in range(0, len(buf) - 3, 4):      # 段尾不足 4 字节自动收口，不越界
            v = int.from_bytes(buf[o:o + 4], "little")
            if v:
                table.setdefault(v, []).append((a, o))
    return table, set(actors)


# ---------------------------------------------------------------- 报告
def _judge(h, ids, hero_ptrs, cids):
    """给一个阶跃列打分。

    核心判据是 shared：阶跃后的**所有**非零值，必须能在 actor 头部找到唯一宿主，
    并且都落在**同一个偏移**上 —— 那才是 runtime_id 的宿主。
    否则只是"某个小整数碰巧在 actor 头里出现过"，属于假阳性（实测 925 个唯一值里
    大量是 163/171 这种属性成长值）。
    """
    vals = [v for _, _, v in h["runs"]]
    nz = [v for v in vals if v]
    tags, score = [], 0
    if any(v in hero_ptrs for v in nz):
        tags.append("actor_ptr")
        score += 3
    votes: dict[int, int] = {}
    for v in nz:
        locs = ids.get(v, [])
        if locs and len({a for a, _ in locs}) == 1:      # 唯一回指一个 actor
            for _, o in locs:
                votes[o] = votes.get(o, 0) + 1
    shared, best_off = 0, -1
    if votes:
        best_off, shared = max(votes.items(), key=lambda kv: kv[1])
    if nz and shared == len(nz):
        tags.append(f"actor+0x{best_off:02x}")
        score += 5
    elif shared:
        tags.append(f"actor+0x{best_off:02x}({shared}/{len(nz)})")
        score += 1
    if h["runs"][0][2] == h["runs"][-1][2]:
        score += 2                                        # 首尾同值 = 回到基线
    if len(h["changes"]) >= 2:
        score += 2                                        # 真的切过目标
    if any(v in cids for v in nz):
        tags.append("config_id")
        score -= 1                                        # 命中 cid 反而可疑
    return score, tags


def _judge64(h, actor_ptrs):
    """u64 视图专用：指针形态的判据。

    C++ 引擎引用"某个单位"最自然的形态是 8 字节 actor 指针。只跑 u32 视图会把它
    拆成高低位，永远匹配不到 —— 这是前几轮漏掉的最大一块。
    """
    vals = [v for _, _, v in h["runs"]]
    nz = [v for v in vals if v]
    tags, score = [], 0
    hit = sum(1 for v in nz if v in actor_ptrs)
    if nz and hit == len(nz):
        tags.append("actor_ptr*")
        score += 6
    elif hit:
        tags.append(f"actor_ptr({hit}/{len(nz)})")
        score += 2
    if h["runs"][0][2] == h["runs"][-1][2]:
        score += 2
    if len(h["changes"]) >= 2:
        score += 2
    return score, tags


def _emit(rows, label):
    if not rows:
        return
    rows.sort(key=lambda r: (-r[0], r[1]))
    print(f"\n--- {label}：候选 {len(rows)}（按分数降序）---")
    for score, off, h, tags, known in rows[:12]:
        mark = "★" if (score >= 5 and known is None) else ("·" if known is None else "已知")
        print(f" {mark} +0x{off:04x}  score={score:<2}  {_fmt_runs(h['runs'])}"
              f"   changes={len(h['changes'])}  {'/'.join(tags) or '-'}"
              f"{'  [' + known + ']' if known else ''}")


def align_report(dump: dict, tol: int = 2, max_changes: int = 20,
                 min_run: int = 2, max_dist: int = 2500):
    """伤害对齐：不依赖 runtime_id 表，直接用「谁掉了血」给字段定性。

    判据（比"值像 id"硬）：
      1. 字段跳变帧 ±tol 帧内，恰好有某个**别的**英雄血量下降（出手帧与命中帧差 1-2 帧）；
      2. 「新值 → 受伤者」与「受伤者 → 新值」**双向唯一**（一个值只绑一个人，
         一个人只被一个值绑）；
      3. 至少覆盖 2 个不同受伤者。

    满足三条即"这个字段记的是我在打谁"。单值单人也算（打同一个目标两次），
    但会被排在后面 —— 因为无法排除"这次伤害值恰好唯一"。
    """
    rec, cids = dump["rec"], dump["cids"]
    T = rec.shape[0]
    hp = rec[:, :, 0x1A4 // 4].astype(np.int64)
    drop = np.zeros((T, hp.shape[1]), bool)
    drop[1:] = hp[1:] < hp[:-1]
    # 世界坐标：只有"它就在我身边"的掉血才可能是我的攻击结果
    px = rec[:, :, 0x184 // 4].view(np.int32).astype(np.int64)   # 坐标是有符号 int32
    pz = rec[:, :, 0x18C // 4].view(np.int32).astype(np.int64)
    print(f"\n伤害对齐：{T} 帧，掉血帧 {int(drop.any(axis=1).sum())} 帧，"
          f"容差 ±{tol} 帧，距离上限 {max_dist}")

    rows = []
    for si in range(rec.shape[1]):
        for h in stepped(rec[:, si, :], max_changes, min_run):
            ev = []
            for t, _old, new in h["changes"]:
                js = set()
                for tt in range(max(0, t - tol), min(T, t + tol + 1)):
                    for j in np.flatnonzero(drop[tt]):
                        if j == si:
                            continue
                        dx = px[tt, si] - px[tt, j]
                        dz = pz[tt, si] - pz[tt, j]
                        if dx * dx + dz * dz <= max_dist * max_dist:
                            js.add(int(j))
                for j in js:
                    ev.append((new, j))
            if len(ev) < 1:
                continue
            v2j, j2v = {}, {}
            for v, j in ev:
                v2j.setdefault(v, set()).add(j)
                j2v.setdefault(j, set()).add(v)
            if any(len(x) > 1 for x in v2j.values()):
                continue
            if any(len(x) > 1 for x in j2v.values()):
                continue
            rows.append((len(j2v), len(v2j), si, h["col"] * 4, v2j))

    rows.sort(key=lambda r: (-r[0], -r[1]))
    print(f"双向唯一命中 {len(rows)} 个字段\n")
    for nv, nval, si, off, v2j in rows[:12]:
        desc = "   ".join(f"{v}→slot{j}(cid{int(cids[j])})"
                          for v, js in list(v2j.items())[:4] for j in js)
        print(f" ★ slot{si}(cid{int(cids[si])}) +0x{off:04x}   "
              f"受伤者={nv} 不同值={nval}   {desc}")
    if not rows:
        print("（无命中：采样期间没有任何英雄掉血，或本人没参与战斗）")


def under_atk_report(dump: dict, self_cid: int, top: int = 10):
    """谁在打我：不找字段，先量「纯观测推算」的覆盖率。

    口径：本人 hp 下降的那一帧，按世界距离排序取最近的敌方英雄；
    若最近者明显近于次近者（d1*1.5 < d2）判为**可唯一判定**，否则记为歧义。

    这一步的意义：官方特征是 `under_atk_by_each_hero`（10 维）——
    如果观测推算的"唯一率"足够高，就不必再去逆那个字段；
    如果很低，说明必须拿到真实的伤害来源，那时再投入。
    """
    rec, cids = dump["rec"], [int(x) for x in dump["cids"]]
    camp = [str(x) for x in dump["camp"]] if "camp" in dump else []
    if self_cid not in cids:
        print(f"本人 cid {self_cid} 不在本局 {cids}")
        return
    si = cids.index(self_cid)
    T = rec.shape[0]
    hp = rec[:, si, 0x1A4 // 4].astype(np.int64)
    px = rec[:, :, 0x184 // 4].view(np.int32).astype(np.int64)   # 坐标是有符号 int32
    pz = rec[:, :, 0x18C // 4].view(np.int32).astype(np.int64)

    drops = [t for t in range(1, T) if hp[t] < hp[t - 1]]
    enemies = [j for j in range(len(cids))
               if j != si and (not camp or camp[j] != camp[si])]
    uniq, amb, far = [], [], []
    for t in drops:
        ds = []
        for j in enemies:
            dx = int(px[t, si]) - int(px[t, j])
            dz = int(pz[t, si]) - int(pz[t, j])
            ds.append((round((dx * dx + dz * dz) ** 0.5), j))
        ds.sort()
        if not ds:
            continue
        d1, j1 = ds[0]
        d2 = ds[1][0] if len(ds) > 1 else 999999
        item = (t, hp[t - 1] - hp[t], cids[j1], d1, d2)
        if d1 * 1.5 < d2:
            uniq.append(item)
        else:
            amb.append(item)
        if d1 > 3000:
            far.append(item)

    n = max(1, len(drops))
    print(f"\n谁在打我：本人 slot{si}/cid{self_cid}，掉血 {len(drops)} 帧 / {T} 帧")
    print(f"  可唯一判定（最近者明显更近）: {len(uniq)} ({len(uniq) / n:.0%})")
    print(f"  多人邻近（无法区分）        : {len(amb)} ({len(amb) / n:.0%})")
    print(f"  最近敌 >3000（远程/技能/塔）: {len(far)} ({len(far) / n:.0%})")
    print(f"\n  明细（前 {top} 次掉血）：")
    for t, dmg, jcid, d1, d2 in (uniq + amb)[:top]:
        tag = "唯一" if d1 * 1.5 < d2 else "歧义"
        print(f"   t={float(dump['ts'][t]):5.1f}s  掉 {dmg:>4}  "
              f"最近 cid{jcid} d={d1:>5}  次近 d={d2:>5}  [{tag}]")
    if not drops:
        print("  （整段没有任何掉血：本人没参与战斗，或采的不是本人）")
    print("\n结论：唯一率高 → 直接用观测推算即可，不必逆字段；"
          "唯一率低 → 必须拿到真实伤害来源。")


def report(dump: dict, ids: dict, max_changes: int, min_run: int,
           self_cid: int = 0, actor_ptrs=None):
    cids = set(int(x) for x in dump["cids"])
    hero_ptrs = set(int(x) for x in dump["addrs"] if x)

    print("\n" + "=" * 78)
    print("英雄记录 0x2AD8 × 10 槽")
    print("=" * 78)
    for si, slot in enumerate(dump["slots"]):
        rows = []
        for h in stepped(dump["rec"][:, si, :], max_changes, min_run):
            score, tags = _judge(h, ids, hero_ptrs, cids)
            off = h["col"] * 4
            rows.append((score, off, h, tags, KNOWN.get(off)))
        _emit(rows, f"slot {int(slot)} (cid {int(dump['cids'][si])})"
                    f"{_me(dump['cids'][si], self_cid)} 记录")

    if actor_ptrs:
        print("\n" + "=" * 78)
        print("英雄记录 u64 视图（指针形态：值 == 某个在场 actor 的基址）")
        print("=" * 78)
        try:
            view = dump["rec"].view(np.uint64)          # (T,10,0x2AD8/8)
        except ValueError:
            view = None
        if view is not None:
            for si, slot in enumerate(dump["slots"]):
                rows = []
                for h in stepped(view[:, si, :], max_changes, min_run):
                    score, tags = _judge64(h, actor_ptrs)
                    if score >= 4:
                        rows.append((score, h["col"] * 8, h, tags, None))
                _emit(rows, f"slot {int(slot)} (cid {int(dump['cids'][si])})"
                            f"{_me(dump['cids'][si], self_cid)} u64")

    print("\n" + "=" * 78)
    print(f"英雄 actor 头部 0x{ACTOR_HDR:x}")
    print("=" * 78)
    for si, slot in enumerate(dump["slots"]):
        if not dump["act"][:, si, :].any():
            continue
        rows = []
        for h in stepped(dump["act"][:, si, :], max_changes, min_run):
            score, tags = _judge(h, ids, hero_ptrs, cids)
            rows.append((score, h["col"] * 4, h, tags, None))
        _emit(rows, f"slot {int(slot)} (cid {int(dump['cids'][si])})"
                    f"{_me(dump['cids'][si], self_cid)} actor 头")

    print("\n" + "=" * 78)
    print(f"英雄 actor 的 15 个组件（每个头部 0x{COMP_HDR:x}）")
    print("=" * 78)
    if dump.get("comp") is not None and dump["comp"].ndim == 4:
        for si, slot in enumerate(dump["slots"]):
            rows = []
            for k in range(dump["comp"].shape[2]):
                mat = dump["comp"][:, si, k, :]
                if not mat.any():
                    continue                       # 该槽指针为空
                for h in stepped(mat, max_changes, min_run):
                    score, tags = _judge(h, ids, hero_ptrs, cids)
                    rows.append((score, h["col"] * 4, h, tags + [f"c{k}"], None))
            _emit(rows, f"slot {int(slot)} (cid {int(dump['cids'][si])})"
                        f"{_me(dump['cids'][si], self_cid)} 组件")

    print("\n判定口径：★ = score≥5 且非已知偏移，即「阶跃后的非零值全部唯一回指同一个 actor 偏移」；"
          "只命中 config_id 会被扣分（那多半是静态属性，不是目标）。")


def selftest() -> int:
    """合成序列自检：注入 3 段阶跃 + 1 个连续量 + 1 个噪声列，验证检测结果。"""
    T, W = 60, 64
    mat = np.zeros((T, W), np.uint32)
    A, B = 0x11223344, 0x55667788
    mat[0:15, 10] = 0
    mat[15:30, 10] = A
    mat[30:45, 10] = B
    mat[45:60, 10] = 0
    mat[:, 11] = np.arange(T, dtype=np.uint32)      # 连续量：必须被排除
    mat[:, 12] = 7                                  # 恒定：不是阶跃
    mat[14, 13] = 99                                # 单帧抖动（段长 1）：必须被排除
    hits = stepped(mat, max_changes=10, min_run=2)
    cols = {h["col"]: h for h in hits}
    ok = True
    if 10 not in cols:
        print("FAIL 未检出注入的阶跃列 10"); ok = False
    else:
        runs = cols[10]["runs"]
        want = [(0, 15, 0), (15, 30, A), (30, 45, B), (45, 60, 0)]
        if runs != want:
            print(f"FAIL 段划分错误 {runs}"); ok = False
    for bad in (11, 12, 13):
        if bad in cols:
            print(f"FAIL 列 {bad} 不该被判定为阶跃"); ok = False
    # 候选匹配：A 在集合里、B 不在 → 命中 2 次
    n, tags = _match([v for _, _, v in cols[10]["runs"]], {"actor_id": {A}})
    if n != 1 or tags != ["actor_id"]:
        print(f"FAIL 候选匹配 {n} {tags}"); ok = False
    # 已知偏移降级表覆盖 7 个技能槽
    if KNOWN.get(0x19D4 + 6 * 0x78 + 0x14) != "skill7":
        print("FAIL KNOWN 表未覆盖第 7 槽"); ok = False

    # _read_plan 的页对齐换算（read_spans 的 start 未对齐、data 从页首开始）
    import struct as _struct
    page = 0x700000000000
    blob = bytearray(0x1000)
    _struct.pack_into("<Q", blob, 0x108, 0xDEADBEEFCAFE)

    class _FakeM:
        def read_spans(self, spans):
            return [(spans[0][0], bytes(blob))]

    got = _read_plan(_FakeM(), [(page + 0x108, 8, "k")])
    if got.get("k") != _struct.pack("<Q", 0xDEADBEEFCAFE):
        print(f"FAIL _read_plan 页对齐换算错误 {got.get('k')!r}"); ok = False

    # 集成自检：合成一整套 dump 走 report()，确认注入列被排到 ★ 且带出正确偏移
    import io
    from contextlib import redirect_stdout
    dump = {
        "rec": np.zeros((T, HERO_ARR_COUNT, WORDS), np.uint32),
        "act": np.zeros((T, HERO_ARR_COUNT, AHDW), np.uint32),
        "ts": np.arange(T, dtype=np.float64),
        "cids": np.array([106 + i for i in range(HERO_ARR_COUNT)], np.int64),
        "slots": np.arange(HERO_ARR_COUNT, dtype=np.int64),
        "addrs": np.array([0x700000000000 + i * 0x1000 for i in range(HERO_ARR_COUNT)],
                          np.uint64),
        "camp": np.array(["blue"] * 5 + ["red"] * 5),
    }
    TARGET_OFF = 0x0C80                      # 必须不在 KNOWN 里
    dump["rec"][0:15, 3, TARGET_OFF // 4] = 0
    dump["rec"][15:30, 3, TARGET_OFF // 4] = A
    dump["rec"][30:45, 3, TARGET_OFF // 4] = B
    dump["rec"][45:60, 3, TARGET_OFF // 4] = 0
    dump["rec"][:, 3, 0x1C4 // 4] = np.arange(T, dtype=np.uint32)   # ep 连续量
    dump["comp"] = np.zeros((T, HERO_ARR_COUNT, COMP_SLOTS, CHW), np.uint32)
    dump["comp"][:, 3, 9, 0x20] = dump["rec"][:, 3, TARGET_OFF // 4]  # 组件里的同一模式
    dump["comp"][:, 3, 9, 0x21] = 1                                   # 存在标记
    buf = io.StringIO()
    with redirect_stdout(buf):
        # A/B 都"唯一回指同一个 actor 偏移 0x0c" —— 这正是真目标的特征
        report(dump, {A: [(0x700000000000, 0x0C)], B: [(0x700000000010, 0x0C)]}, 10, 2)
    text = buf.getvalue()
    want_line = f"★ +0x{TARGET_OFF:04x}"
    if want_line not in text:
        print(f"FAIL report 未把 +0x{TARGET_OFF:04x} 报成 ★"); ok = False
    elif "actor+0x0c" not in text:
        print("FAIL report 未标出 actor 偏移来源"); ok = False
    if "actor+0x0c/c9" not in text:          # 注入列 0x20 → 字节偏移 0x80
        print("FAIL report 未报出组件里的同款阶跃"); ok = False
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=24.0)
    ap.add_argument("--save", default="")
    ap.add_argument("--analyze", default="")
    ap.add_argument("--max-changes", type=int, default=20)
    ap.add_argument("--min-run", type=int, default=2)
    ap.add_argument("--no-comp", action="store_true",
                    help="不采 15 个组件（更快，用于只扫 actor 头部）")
    ap.add_argument("--slots", choices=["self", "all"], default="self",
                    help="self=只采本人 actor/组件（帧率高）；all=10 个全采（慢 10 倍）")
    ap.add_argument("--self-cid", type=int, default=0,
                    help="本人英雄 config_id（用于在报告里标出「本人」槽位）")
    ap.add_argument("--serial", default="127.0.0.1:16384")
    ap.add_argument("--under-atk", action="store_true",
                    help="谁在打我：用「我掉血 + 谁在近处」量推算覆盖率")
    ap.add_argument("--align", action="store_true",
                    help="伤害对齐模式：用「谁掉血」给字段定性，不依赖 runtime_id")
    ap.add_argument("--tol", type=int, default=2, help="出手帧与命中帧的容差")
    ap.add_argument("--max-dist", type=int, default=2500,
                    help="只有当受伤者在此距离内，才算「我打的」")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    CACHE.mkdir(parents=True, exist_ok=True)
    src = MemSource(serial=a.serial)
    src.connect()

    if a.analyze:
        p = CACHE / f"{a.analyze}.npz"
        if not p.exists():
            print(f"找不到 {p}")
            return 2
        z = np.load(p)
        dump = {k: z[k] for k in z.files}
        print(f"从 {p} 载入 {dump['rec'].shape[0]} 帧", file=sys.stderr)
    else:
        dump = sample(src, a.secs, self_cid=a.self_cid,
                      only_self=(a.slots == "self"), read_comp=not a.no_comp)
        name = a.save or time.strftime("t%H%M%S")
        p = CACHE / f"{name}.npz"
        np.savez_compressed(p, **dump)
        print(f"已存 {p}（可 --analyze {name} 重跑）", file=sys.stderr)

    if a.under_atk:                      # 只看掉血/距离，不需要 actor 词表
        under_atk_report(dump, a.self_cid)
        return 0
    ids, actor_ptrs = collect_actor_ids(src)
    print(f"actor 头部 u32 词表 {len(ids)} 项 / 在场 actor {len(actor_ptrs)} 个", file=sys.stderr)
    if a.under_atk:
        under_atk_report(dump, a.self_cid)
    elif a.align:
        align_report(dump, a.tol, a.max_changes, a.min_run, a.max_dist)
    else:
        report(dump, ids, a.max_changes, a.min_run, a.self_cid, actor_ptrs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
