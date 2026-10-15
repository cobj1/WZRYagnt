# -*- coding: utf-8 -*-
"""laya-fusion C4/C5/C6 离线管线自测: 合成对局数据, 不需要模拟器/GPU/laya 包。

运行: venv/Scripts/python.exe -X utf8 scripts/test_laya_pipeline.py
覆盖:
  D1  C4 build_laya_dataset: 合成 states+actions -> 记录落盘/按会话切分
  D2  C4 人类正例: swipe 方向 -> 最近候选 support_red0
  D3  C4 结果回溯: hp 归零 -> died -50; 屏内敌英消失 -> kill +20
  D4  C5 build_pairs: 人类正例 + 规则 argmax 作 rejected
  D5  C6 load_records + RuleScorer: top-1 一致率计算正确
"""
import json
import math
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_laya_dataset as B            # noqa: E402
import eval_scorers as E                  # noqa: E402
from train_laya import build_pairs        # noqa: E402

_PASS = []


def check(name, cond, extra=""):
    if cond:
        _PASS.append(name)
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {extra}")
        raise SystemExit(1)


def make_states():
    """21s 合成局, 事件窗口互不重叠:
    - t=4.5 屏内敌英消失(击杀 +20; 窗口 t0 in (~-0.5, 4.5])
    - t=12 hp 0.9->0.4(塔区内掉血 -> 被塔打 -20)
    - t=16 hp 归零(阵亡 -50), t=17 复活
    t=1.0 有一次朝敌兵方向的 swipe(人类移动正例)。"""
    states = []
    for k in range(40):
        t = 1.0 + 0.5 * k
        hp = 0.9
        if 12.0 <= t < 16.0:
            hp = 0.4
        elif 16.0 <= t < 17.0:
            hp = 0.0
        elif t >= 17.0:
            hp = 0.9
        n_enemy = 0 if t >= 4.5 else 1
        units = ([{"cls": "enemy_hero", "screen": [0.60, 0.52]}] if n_enemy
                 else []) + [{"cls": "enemy_minion", "screen": [0.62, 0.58]},
                             {"cls": "enemy_turret", "screen": [0.80, 0.80]}]
        states.append({
            "t": t, "ui": {"hp": hp, "mp": 0.8},
            "minimap": {"found": True, "dots": {
                "red": [[0.70, 0.75]], "blue": [[0.55, 0.50]],
                "green": [[0.50, 0.50]]}},
            "units": units,
        })
    return states


def make_actions():
    """t=1.0 朝红点方向 swipe(支持正例), t=12 无操作段。"""
    d = math.hypot(0.2, 0.25)
    return [{"t": 1.0, "type": "swipe", "x1": 500, "y1": 500,
             "x2": 500 + 200 / d, "y2": 500 + 250 / d, "duration": 300}]


def main():
    tmp = Path(tempfile.mkdtemp())
    mdir = tmp / "data" / "matches" / "TEST_SESSION_A"
    mdir.mkdir(parents=True)
    (mdir / "states.jsonl").write_text(
        "\n".join(json.dumps(s) for s in make_states()) + "\n", encoding="utf-8")
    (mdir / "actions.jsonl").write_text(
        "\n".join(json.dumps(a) for a in make_actions()) + "\n", encoding="utf-8")

    # ---- C4 CLI 全流程 ----
    out = tmp / "dataset"
    rc = B.main.__wrapped__() if hasattr(B.main, "__wrapped__") else None
    sys.argv = ["build_laya_dataset.py", "--matches-dir", str(tmp / "data" / "matches"),
                "--out", str(out)]
    check("D1a_build_runs", B.main() == 0)
    splits = {k: out / f"{k}.jsonl" for k in ("train", "val", "test")}
    check("D1b_splits_exist", all(p.exists() for p in splits.values()))
    recs = [json.loads(x) for p in splits.values()
            for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    check("D1c_records_written", len(recs) >= 20, f"n={len(recs)}")
    check("D1d_single_session_one_split",
          len({r["session"] for r in recs}) == 1
          and sum(bool(p.exists() and p.read_text(encoding='utf-8').strip())
                  for p in splits.values()) == 1,
          "按对局切分: 单会话应只落一个 split")

    # ---- D2 人类正例: swipe 意图点(0.594,0.617) 最近候选 = clear_lane(敌兵方向) ----
    hits = [r for r in recs if r["human_label"] == "clear_lane"]
    check("D2_human_positive_mapped", len(hits) >= 1 and hits[0]["t"] == 1.0,
          f"hits={len(hits)} sample={[r['human_label'] for r in recs[:6]]}")
    check("D2b_labels_topk_shape",
          all(len(r["labels"]) == len(r["rule_scores"]) and r["labels"]
              for r in recs), "labels/rule_scores 对齐且非空")
    check("D2c_labels_valid",
          all(r["human_label"] is None or r["human_label"] in r["labels"]
              for r in recs)
          and sum(1 for r in recs if r["human_label"]) >= 5,
          "正例只来自人类映射(含 stand), 且必在 top-16 标签集内")
    import re
    def _no_pruned(rec):
        """决策时 hp<0.5(经保命硬过滤)的帧, 标签不应含被剪枝的候选族。"""
        m = re.search(r"hp=([\d.]+)", rec["state"])
        if not m or float(m.group(1)) >= 0.5:
            return True
        banned = ("support", "attack", "skill", "micro", "kite", "clear", "hook")
        return not any(any(lab.startswith(b) for b in banned) for lab in rec["labels"])
    check("D2d_hardfilter_aligned", all(_no_pruned(r) for r in recs),
          "决策时 hp<0.5 的帧应只含保命族候选(对齐线上 v2.55 过滤)")

    # ---- D3 结果回溯 ----
    died = [r for r in recs if r["events"].get("died")]
    kill = [r for r in recs if r["events"].get("kill")]
    check("D3a_died_negative", died and all(r["advantage"] <= -50 + 1e-6 for r in died),
          f"died={len(died)} adv={[r['advantage'] for r in died[:3]]}")
    check("D3b_kill_positive", kill and all(r["advantage"] >= 20 - 1e-6 for r in kill),
          f"kill={len(kill)} adv={[r['advantage'] for r in kill[:3]]}")

    # ---- D5 C6 读取与 rule 基线 ----
    class A:
        dataset = str(splits["train"])
        shadow = None
    recs6 = E.load_records(A())
    check("D5a_eval_loads", len(recs6) == len(recs))
    ok = True
    for r in recs6:
        rs = E.RuleScorer(None).score(r["state"], r["labels"], r)
        top = r["labels"][max(range(len(rs)), key=lambda i: rs[i])]
        ok = ok and top == r["labels"][0] and abs(sum(rs) - 1.0) < 1e-6
    check("D5b_rule_argmax_matches_rule_scores", ok)

    # ---- D4 C5 build_pairs(纯函数, 不依赖 torch) ----
    base = {"labels": ["retreat", "support_red0", "lane_mid", "stand"],
            "rule_scores": [0.5, 0.4, 0.2, 0.0], "human_label": "support_red0",
            "advantage": 0.0, "advantage_label": None}
    # 人类正例 + 规则 argmax 兜底 rejected
    pairs = build_pairs([dict(base)], neg_rule=True)
    check("D4a_pair_pref_human",
          pairs and pairs[0][1] == "support_red0" and pairs[0][2] == "retreat",
          f"{pairs}")
    # 结果回溯负优势(不同于人类选择) -> rejected=adv_lab
    neg = dict(base, human_label="lane_mid", advantage=-50.0,
               advantage_label="support_red0")
    pairs_n = build_pairs([neg], neg_rule=False)
    check("D4b_pair_rejected_adv",
          pairs_n and pairs_n[0][1] == "lane_mid" and pairs_n[0][2] == "support_red0",
          f"{pairs_n}")
    # 证据矛盾(人类正例同时负优势) -> 跳过
    check("D4c_conflict_skipped",
          build_pairs([dict(base, advantage=-50.0,
                            advantage_label="support_red0")]) == [],
          "同一候选既正例又负优势应跳过")
    # 纯结果正例(无人类映射)
    pos = dict(base, human_label=None, advantage=20.0, advantage_label="support_red0")
    pairs_p = build_pairs([pos], neg_rule=True)
    check("D4d_outcome_positive",
          pairs_p and pairs_p[0][1] == "support_red0" and pairs_p[0][2] == "retreat",
          f"{pairs_p}")

    print(f"ALL PASS ({len(_PASS)})")


if __name__ == "__main__":
    main()
