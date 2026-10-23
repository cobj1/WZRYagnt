# -*- coding: utf-8 -*-
"""laya-fusion-plan C4: 构建学习型打分器训练数据集 (方案 §5)。

两种标签来源 (不用规则自己的选择当标签):
  A. 人类对局: data/matches/<session>/{states,actions}.jsonl ->
     每帧离线跑 _feat + gen_candidates + evaluate + _forecast(与线上 solve 完全同参),
     把人的实际移动(actions.jsonl 的 swipe 事件流反推方向)映射到最近候选 = 正例。
     **前提: 该会话必须有 swipe(人类输入)证据**。data/matches/ 里 agent 自对局记录的是
     agent 自己的输出(map_move/skill/attack), swipe 数为 0 —— 这类会话不做来源 A 标注
     (否则会把"根本没有人类输入"误标成正例 stand)。全无证据时脚本中止, 需 --allow-no-human。
  B. 按结果回溯: 决策后 N 秒(默认 5s)内事件按 docs/REWARD_STANDARD.md 的
     **离线可估计子集**计分 (kill +20 / tower_kill +10 / escape_alive +5 /
      be_tower_attacked -20 / be_attacked -3 / died -50), 给已选候选打优势标签,
      正负都要。事件由状态序列离线估计(屏内敌英减少=击杀、hp 归零=阵亡、
      塔区内掉血=被塔打 ...)。

样本是 listwise 的: 每帧 = 状态 + 全部 top-k 候选 + 规则分 + 人类选择 + 规则 argmax
(rule_top/neg_rule), 供 train_laya 直接构造 (preferred=human_label,
rejected=rule_top) 对比对, 而不是只标一个"已选候选好坏"。

划分: 按对局(session)切 train/val/test(70/15/15, md5 确定性), 不按帧切, 防数据泄漏。
输出: runs/laya/dataset/{train,val,test}.jsonl + meta.json

运行: venv/Scripts/python.exe -X utf8 scripts/build_laya_dataset.py \
        [--window 5.0] [--sessions s1,s2] [--camp blue]
"""
import argparse
import bisect
import hashlib
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.decision import optimizer                       # noqa: E402
from wzry.decision.laya_scorer import (TOP_K,             # noqa: E402
                                       build_state_text, candidate_label)

# docs/REWARD_STANDARD.md 的**离线可估计子集**(不是全表): 助攻(+15)/清兵(+2)/
# 大招(+5)/空转(-2)/未恢复未回城(-5) 无法从 states 序列可靠反推, 未计分。
# 与运行时 reward.on_event 同分值, 但事件集合更小 -> 优势标签量级偏保守。
REWARD = {"kill": 20.0, "tower_kill": 10.0, "escape_alive": 5.0,
          "died": -50.0, "be_tower_attacked": -20.0, "be_attacked": -3.0}


def load_session(mdir):
    states, actions = [], []
    try:
        for ln in (mdir / "states.jsonl").read_text(encoding="utf-8").splitlines():
            if ln.strip():
                states.append(json.loads(ln))
        for ln in (mdir / "actions.jsonl").read_text(encoding="utf-8").splitlines():
            if ln.strip():
                actions.append(json.loads(ln))
    except Exception:
        pass
    states.sort(key=lambda s: s.get("t", 0))
    actions.sort(key=lambda a: a.get("t", 0))
    return states, actions


def state_snap(st):
    """GameState -> (hp, n_enemy_scr, n_turret, in_turret_zone)。hp=0 是合法值(阵亡)。"""
    ui = st.get("ui") or {}
    hp = ui.get("hp")
    hp = 1.0 if hp is None else float(hp)
    units = st.get("units") or []
    n_enemy = sum(1 for u in units if u.get("cls") == "enemy_hero"
                  and optimizer.enemy_hero_trusted(u))
    turs = [u for u in units if u.get("cls") == "enemy_turret"]
    d_t = 1.0
    if turs:
        s = turs[0].get("screen") or [0.5, 0.5]
        d_t = math.hypot(float(s[0]) - 0.5, float(s[1]) - 0.5)
    return hp, n_enemy, len(turs), 1.0 if (turs and d_t < 0.45) else 0.0


def human_move_dir(actions, t0, win_s):
    """[t0, t0+win] 内 swipe 事件 -> 归一化移动方向(屏幕系), 无 swipe 返回 None。"""
    ts = [a.get("t", 0) for a in actions]
    i0 = bisect.bisect_left(ts, t0)
    vx = vy = 0.0
    n = 0
    for a in actions[i0:]:
        if a.get("t", 0) > t0 + win_s:
            break
        if a.get("type") != "swipe":
            continue
        dx = float(a.get("x2", 0)) - float(a.get("x1", 0))
        dy = float(a.get("y2", 0)) - float(a.get("y1", 0))
        d = math.hypot(dx, dy)
        if d < 1e-3:
            continue
        vx += dx / d
        vy += dy / d
        n += 1
    if not n or math.hypot(vx, vy) < 1e-3:
        return None
    return vx / math.hypot(vx, vy), vy / math.hypot(vx, vy)


def nearest_map_move(point, cands):
    """最近 map_move 候选 -> (tag, dist)。"""
    best, bd = None, 9.9
    for a, tag in cands:
        if a.get("type") != "map_move":
            continue
        d = math.hypot(float(a.get("nx", 0.5)) - point[0],
                       float(a.get("ny", 0.5)) - point[1])
        if d < bd:
            best, bd = tag, d
    return best, bd


def agent_chosen(actions, t0, win_s, cands):
    """v14.2 来源 B 的锚点: [t0, t0+win] 内 agent 实际下发的 map_move -> 最近候选。

    actions.jsonl 的 map_move 带 nx/ny(与候选同坐标系)与 reason, 因此 agent 自对局
    (无 swipe)也能得到"已选候选"标签 —— 原先 advantage_label 硬绑 human_label,
    没有人类输入时来源 B 一并失效, 整份数据没有任何可用标签。
    """
    ts = [a.get("t", 0) for a in actions]
    i0 = bisect.bisect_left(ts, t0)
    for a in actions[i0:]:
        if a.get("t", 0) > t0 + win_s:
            break
        if a.get("type") != "map_move":
            continue
        return nearest_map_move((float(a.get("nx", 0.5)), float(a.get("ny", 0.5))), cands)
    return None, 9.9


def outcome_advantage(states, times, hp_s, ene_s, tur_s, zon_s, i0, t0, win_s):
    """(t0, t0+win] 事件回溯 -> (advantage, events dict)。状态序列离线估计。"""
    j = bisect.bisect_right(times, t0 + win_s)
    j = min(j, len(states) - 1)
    if j <= i0:
        return 0.0, {}
    hp0, hp1 = hp_s[i0], hp_s[j]
    adv, ev = 0.0, {}
    drop = hp0 - min(hp_s[i0:j + 1])
    # 阵亡: hp 归零(估) -> 大负, 且不再叠加挨打分
    died = any(h <= 0.02 for h in hp_s[i0:j + 1])
    if died:
        adv += REWARD["died"]
        ev["died"] = True
    # 被塔打: 塔区内掉血
    if drop >= 0.15 and any(zon_s[i0:j + 1]):
        adv += REWARD["be_tower_attacked"]
        ev["be_tower_attacked"] = True
    elif drop >= 0.15 and not died:
        adv += REWARD["be_attacked"]
        ev["be_attacked"] = True
    # 击杀: 屏内敌英减少且窗口末未回升
    if ene_s[i0] > ene_s[j]:
        adv += REWARD["kill"]
        ev["kill"] = True
    # 推塔: 敌塔减少
    if tur_s[i0] > tur_s[j]:
        adv += REWARD["tower_kill"]
        ev["tower_kill"] = True
    # 成功撤退存活: 低血进窗口, 未死且血量回升
    if hp0 < 0.35 and not died and hp1 > hp0 + 0.10:
        adv += REWARD["escape_alive"]
        ev["escape_alive"] = True
    return adv, ev


def split_session(name):
    """md5(session) 确定性切分: <70 train / <85 val / else test。"""
    h = int(hashlib.md5(name.encode()).hexdigest()[:8], 16) % 100
    return "train" if h < 70 else ("val" if h < 85 else "test")


def process_session(mdir, args, out_fhs, stats):
    states, actions = load_session(mdir)
    if len(states) < 10:
        stats["skipped"].append(mdir.name)
        return
    # v14.2 来源 A 的前提: 会话里必须有"人类输入"证据(swipe)。
    #   实测 data/matches/ 的 33 个会话 swipe 数全为 0 —— 记录的是 agent 自己的输出
    #   (map_move/skill/attack), 不是人类操作。此时 human_move_dir 恒为 None,
    #   旧的 `if d is None: human_label = "stand"` 会把"根本没有人类输入"误标成正例
    #   stand, 使整个数据集退化为 (preferred=stand, rejected=rule_top) ——
    #   照此训练等于教 agent 原地不动。故按会话级证据决定是否做来源 A 标注。
    n_swipe = sum(1 for a in actions if a.get("type") == "swipe")
    has_human_input = n_swipe > 0
    if not has_human_input:
        stats["no_human_sessions"].append(mdir.name)
    times = [s.get("t", 0) for s in states]
    hp_s = [state_snap(s)[0] for s in states]
    ene_s = [state_snap(s)[1] for s in states]
    tur_s = [state_snap(s)[2] for s in states]
    zon_s = [state_snap(s)[3] for s in states]
    split = split_session(mdir.name)
    n_pos = n_adv = n_out = n_chosen = 0
    for i0, st in enumerate(states):
        t0 = times[i0]
        hp = hp_s[i0]
        try:
            f = optimizer._feat(st, hp)
        except Exception:
            continue
        if not f["reds"]:                      # solve 仅在有敌人证据时激活, 对齐线上分布
            continue
        cands = optimizer.gen_candidates(f)
        # 对齐线上 _solve_core v2.55 保命硬过滤(敌群>=6 或 血<0.5 -> 只留保命族):
        # 线上 scorer 只见过过滤后的候选, 数据分布必须一致
        if len(f["reds"]) >= 6 or hp < 0.5:
            keep_prefix = ("stand", "follow", "hold", "retreat", "stand_")
            cands = [c for c in cands
                     if any(c[0].get("reason", "").startswith(p) for p in keep_prefix)
                     or c[0].get("type") == "none"]
        if len(cands) < args.min_labels:
            continue
        scored = sorted(((optimizer.evaluate(c, f, args.camp)
                          + 0.35 * optimizer._forecast(c, f), c) for c in cands),
                        key=lambda x: -x[0])[:args.top_k]
        labels = [candidate_label(c) for _, c in scored]
        rule_scores = [round(float(r), 4) for r, _ in scored]
        if len(set(labels)) < args.min_labels:
            continue
        # 来源 A: 人类移动 -> 最近候选正例(仅当该会话存在人类输入证据)
        human_label = None
        if has_human_input:
            d = human_move_dir(actions, t0, args.human_window)
            if d is None:
                if "stand" in labels:          # 窗口内人没有推杆 = 原地
                    human_label = "stand"
            else:
                gx, gy = f["g"]
                # 屏幕方向 -> 小地图意图点(轴同向假设; 若录像坐标系翻转用 --flip-y 校正)
                pt = (gx + args.swipe_gain * d[0],
                      gy + (-1 if args.flip_y else 1) * args.swipe_gain * d[1])
                tag, dist = nearest_map_move(pt, [c for _, c in scored])
                if tag is not None and dist <= args.match_tol:
                    human_label = tag
                else:
                    # v14.2 度量 top-16 截断损失: 人的意图点匹配不到任一 top-k 候选
                    #   (候选被规则分截断在 16 名之外, 或映射增益/翻转未标定)
                    n_out += 1
        # 来源 B 的锚点: 优先人类选择, 无人类输入时退到 agent 实际下发的 map_move
        chosen_label, chosen_src = human_label, ("human" if human_label else None)
        if chosen_label is None:
            tag_b, dist_b = agent_chosen(actions, t0, args.human_window,
                                         [c for _, c in scored])
            if tag_b is not None and dist_b <= args.match_tol:
                chosen_label, chosen_src = tag_b, "agent"
        # 结果回溯优势标签(标在已选候选上)
        adv, ev = outcome_advantage(states, times, hp_s, ene_s, tur_s, zon_s,
                                    i0, t0, args.window)
        adv_label = chosen_label
        rule_top = labels[0]        # 规则 argmax(rule_scores 已按 r 降序)
        rec = {"session": mdir.name, "t": round(t0, 3), "camp": args.camp,
               "state": build_state_text(f, args.camp), "labels": labels,
               "rule_scores": rule_scores, "rule_top": rule_top,
               "human_label": human_label,
               "chosen_label": chosen_label, "chosen_src": chosen_src,
               # v14.2 listwise 对比项: 已选候选 vs 规则 argmax(供 train_laya 默认负例)
               "neg_rule": (rule_top if (chosen_label and rule_top != chosen_label)
                            else None),
               "advantage": round(adv, 1), "advantage_label": adv_label,
               "events": ev}
        out_fhs[split].write(json.dumps(rec, ensure_ascii=False) + "\n")
        stats["records"][split] += 1
        if human_label:
            n_pos += 1
            stats["human_label_hist"][human_label] = \
                stats["human_label_hist"].get(human_label, 0) + 1
        if chosen_label:
            n_chosen += 1
            stats["chosen_src_hist"][chosen_src] = \
                stats["chosen_src_hist"].get(chosen_src, 0) + 1
        if ev and adv_label:
            n_adv += 1
    stats["sessions"][mdir.name] = {"split": split, "states": len(states),
                                    "swipe_actions": n_swipe,
                                    "human_pos": n_pos, "adv_labeled": n_adv,
                                    "chosen_labeled": n_chosen,
                                    "human_out_of_topk": n_out,
                                    "records": n_pos + n_adv}
    print(f"[session] {mdir.name} -> {split}: 状态 {len(states)}, "
          f"swipe {n_swipe}, 人类正例 {n_pos}, 已选候选标签 {n_chosen}, "
          f"结果回溯 {n_adv}, 意图点落在 top-k 外 {n_out}")


def main():
    ap = argparse.ArgumentParser(description="build laya dataset (plan C4)")
    ap.add_argument("--matches-dir", default=str(ROOT / "data" / "matches"))
    ap.add_argument("--out", default=str(ROOT / "runs" / "laya" / "dataset"))
    ap.add_argument("--window", type=float, default=5.0,
                    help="结果回溯窗口 N 秒 (方案 §9: 暂定 5s, P2 时调)")
    ap.add_argument("--top-k", type=int, default=TOP_K)
    ap.add_argument("--human-window", type=float, default=0.5,
                    help="帧后多少秒内的 swipe 视为本次移动")
    ap.add_argument("--swipe-gain", type=float, default=0.15,
                    help="swipe 方向 -> 小地图意图点距离增益(标定项)")
    ap.add_argument("--match-tol", type=float, default=0.18,
                    help="意图点匹配候选的容差(超出则不作正例)")
    ap.add_argument("--min-labels", type=int, default=4)
    ap.add_argument("--camp", default="blue", choices=["blue", "red"],
                    help="states.jsonl 未记录阵营时的回退(文档歧义, 见 meta.notes)")
    ap.add_argument("--flip-y", action="store_true",
                    help="屏幕 swipe y 轴与小地图反向时开启(标定项)")
    ap.add_argument("--sessions", default=None, help="逗号分隔, 默认全部")
    ap.add_argument("--allow-no-human", action="store_true",
                    help="允许在'没有任何人类输入(swipe)证据'的数据上产出数据集。"
                         "默认禁止: 此时来源 A 全部为空, 旧行为会退化成把 stand 当正例,"
                         "照此训练等于教 agent 原地不动")
    args = ap.parse_args()

    mroot = Path(args.matches_dir)
    if not mroot.exists():
        print(f"无数据目录 {mroot} — 先完成 P0 录制 (方案 §6 P0)")
        return 1
    sess = sorted(d for d in mroot.iterdir() if (d / "states.jsonl").exists())
    if args.sessions:
        keep = set(args.sessions.split(","))
        sess = [d for d in sess if d.name in keep]
    if not sess:
        print("没有含 states.jsonl 的会话")
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fhs = {k: open(out / f"{k}.jsonl", "w", encoding="utf-8")
           for k in ("train", "val", "test")}
    stats = {"records": {"train": 0, "val": 0, "test": 0},
             "sessions": {}, "skipped": [], "no_human_sessions": [],
             "human_label_hist": {}, "chosen_src_hist": {},
             "window_s": args.window, "reward_table": REWARD,
             "notes": [
                 "camp 不可从 states.jsonl 恢复, 用 --camp 回退; 上线前建议 MatchRecorder 增记 camp",
                 "swipe->小地图增益/翻转需 P0 后用真实录像标定 (--swipe-gain/--flip-y/--match-tol)",
                 "助攻(+15)/清兵(+2)/大招(+5)/空转(-2) 离线不可靠估计, 未计分",
                 "样本为 listwise: labels/rule_scores/rule_top/human_label/"
                 "chosen_label/chosen_src/neg_rule 齐全",
                 "chosen_src=human 表示已选候选来自人类 swipe 映射; "
                 "=agent 表示来自 agent 实际下发的 map_move(nx/ny 与候选同坐标系)",
                 "sessions[*].human_out_of_topk = 人的意图点匹配不到 top-k 候选的帧数,"
                 " 用来度量 top-16 截断损失; 该值偏高说明候选被规则分截断或映射未标定",
                 "sessions[*].swipe_actions = 该会话的人类输入证据数; 为 0 则该会话不做来源 A",
             ]}
    t0 = time.time()
    for d in sess:
        process_session(d, args, fhs, stats)
    for fh in fhs.values():
        fh.close()
    (out / "meta.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    print(f"\n完成 {time.time() - t0:.1f}s -> {out}")
    print(f"记录数: {stats['records']}")
    _nnh = len(stats["no_human_sessions"])
    _proc = len(sess) - len(stats["skipped"])     # v14.2 修: 跳过会话不算进分母
    _hist = stats["human_label_hist"]
    _npos = sum(_hist.values())
    _srcs = stats["chosen_src_hist"]
    print(f"会话: 处理 {_proc} (跳过 {len(stats['skipped'])}), "
          f"其中有人类输入(swipe)证据 {_proc - _nnh}")
    print(f"已选候选标签: 合计 {sum(_srcs.values())} 条, 来源 {dict(_srcs) or '无'}")
    print(f"人类正例: {_npos} 条, 取值 {len(_hist)} 种 "
          f"({', '.join(f'{k}:{v}' for k, v in sorted(_hist.items(), key=lambda x: -x[1])[:6]) or '-'})")
    if _nnh >= _proc and _proc:
        print("\n[阻塞] 没有任何会话带 swipe(人类输入)证据 —— 来源 A 全为空。")
        print("       数据里记录的是 agent 自己的输出(map_move/skill/attack), 不是人类操作。")
        print("       当前数据集只有'来源 B'(agent 已选候选 + 结果回溯优势), 可用于")
        print("       (preferred=chosen_label, rejected=rule_top) 的对比训练;")
        print("       但 P2 门槛要求的'与人类选择 top-1 一致率'无法评估,")
        print("       需补真人带输入的对局(录像反推动作, 或 m2 以人类操作驱动采集)。")
        if not args.allow_no_human:
            print("       已中止。确认只要来源 B 的数据请加 --allow-no-human。")
            return 2
    elif _npos and max(_hist.values()) / _npos > 0.9:
        print(f"\n[警告] 人类正例高度集中在一个标签({max(_hist, key=_hist.get)}), "
              f"来源 A 判别力很弱 —— 检查 --swipe-gain/--flip-y/--match-tol 是否标定。")
    print(f"P2 门槛提示: test 集需有 human_label 的记录才可评 top-1 一致率")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
