# -*- coding: utf-8 -*-
"""laya-fusion-plan C6: 离线横评多个 Scorer (方案 §6-P5)。

数据源:
  --dataset runs/laya/dataset/test.jsonl   (有 human_label -> top-1 一致率)
  --shadow  runs/laya/shadow.jsonl         (有 chosen/probs/latency_ms -> 线上一致率+延迟)

参评 Scorer (--scorers 逗号分隔):
  rule   规则 r argmax 基线(恒可用)
  laya   laya.Router 零样本/官方模型 (未安装 laya 则跳过)
  hf     微调后 HF 模型 (--hf-model runs/laya/finetuned, 读 calibration.json 温度)
  clef   Clef 打分器 (未安装则跳过)
  small  自训小模型 (--small-model, 同 HF 接口)

指标: top-1 一致率 / 与规则 argmax 分歧率 / 覆盖率 / 打分耗时 p95。
P5 门槛: 一致率胜出者上线 (脚本直接给结论)。

运行: venv/Scripts/python.exe -X utf8 scripts/eval_scorers.py \
        --scorers rule,hf --hf-model runs/laya/finetuned
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from wzry.decision.laya_scorer import build_state_text  # noqa: E402,F401  (复用状态模板)


def load_records(args):
    recs = []
    if args.dataset and Path(args.dataset).exists():
        recs += [json.loads(x) for x in
                 Path(args.dataset).read_text(encoding="utf-8").splitlines() if x.strip()]
    if args.shadow and Path(args.shadow).exists():
        for x in Path(args.shadow).read_text(encoding="utf-8").splitlines():
            if not x.strip():
                continue
            r = json.loads(x)
            if r.get("labels") and r.get("rule_scores"):
                recs.append({"state": r.get("state") or "", "labels": r["labels"],
                             "rule_scores": r["rule_scores"],
                             "human_label": r.get("chosen"),
                             "probs": r.get("probs"),
                             "latency_ms": r.get("latency_ms"),
                             "src": "shadow"})
    return recs


# ---------- Scorer 适配器: score(state, labels) -> Optional[list[float]] ----------
class RuleScorer:
    name = "rule"

    def __init__(self, rec):
        pass

    def score(self, state, labels, rec):
        rs = rec.get("rule_scores") or []
        if len(rs) != len(labels):
            return None
        m = max(rs) or 1.0
        s = [(x - min(rs)) / (m - min(rs) or 1.0) for x in rs]
        z = sum(s) or 1.0
        return [x / z for x in s]


class LayaRouterScorer:
    name = "laya"

    def __init__(self, model_path=None):
        from laya import Router                       # 未安装 -> init 失败即跳过
        if model_path:
            self.router = Router(models={"english": str(model_path)},
                                 default="english")
        else:
            self.router = Router()
        self.instr = INSTR

    def score(self, state, labels, rec):
        from wzry.decision.laya_scorer import _extract_probs
        q = {"action": {"type": "choice", "instructions": self.instr,
                        "criteria": {lab: lab for lab in labels}}}
        res = self.router.predict(state, q)
        return _extract_probs(res, labels)


class HFScorer:
    """C5 微调产物(或任意 HF 因果 LM), 长度归一化 logprob + calibration 温度。"""

    def __init__(self, model_dir):
        from train_laya import INSTR, LMScorer        # noqa: F401
        self.instr = INSTR
        self.scorer = LMScorer(str(model_dir))
        cal = Path(model_dir) / "calibration.json"
        self.T = float(json.loads(cal.read_text(encoding="utf-8"))["temperature"]) \
            if cal.exists() else 1.0

    def score(self, state, labels, rec):
        import math
        prompt = (f"{state}\n{self.instr}\nOptions: {', '.join(labels)}\nBest option:")
        raw = [self.scorer.label_logp(prompt, lab) for lab in labels]
        s = [x / self.T for x in raw]
        mx = max(s)
        e = [math.exp(x - mx) for x in s]
        z = sum(e) or 1.0
        return [x / z for x in e]


class ClefScorer:
    name = "clef"

    def __init__(self):
        import clef                                   # 未安装 -> 跳过
        self.client = (clef.Client() if hasattr(clef, "Client") else clef)

    def score(self, state, labels, rec):
        try:
            res = self.client.choose(context=state, options=labels) \
                if hasattr(self.client, "choose") else self.client.predict(state, labels)
            if isinstance(res, dict):
                for k in ("probabilities", "probs", "scores"):
                    d = res.get(k)
                    if isinstance(d, dict) and d:
                        out = [float(d.get(lab, 0.0)) for lab in labels]
                        s = sum(out)
                        return [v / s for v in out] if s > 0 else None
            if isinstance(res, str) and res in labels:
                return [1.0 if x == res else 0.0 for x in labels]
        except Exception:
            return None
        return None


INSTR = ("Choose the best option for the hero's next action. "
         "Prefer options that enable kills with allies nearby, avoid enemy "
         "clusters and turret zones when low HP, and stay on the objective path.")


def build_scorers(args):
    out, notes = [], []
    out.append(("rule", RuleScorer(None)))
    for name in [s.strip() for s in args.scorers.split(",") if s.strip()]:
        if name == "rule":
            continue
        try:
            if name == "laya":
                out.append(("laya", LayaRouterScorer(args.laya_model)))
            elif name == "hf":
                assert args.hf_model and Path(args.hf_model).exists(), \
                    f"--hf-model 不存在: {args.hf_model}"
                out.append(("hf", HFScorer(args.hf_model)))
            elif name == "clef":
                out.append(("clef", ClefScorer()))
            elif name == "small":
                assert args.small_model and Path(args.small_model).exists(), \
                    f"--small-model 不存在: {args.small_model}"
                out.append(("small", HFScorer(args.small_model)))
            else:
                notes.append(f"未知 scorer: {name}")
        except Exception as e:
            notes.append(f"跳过 {name}: {type(e).__name__}: {e}")
    return out, notes


def main():
    ap = argparse.ArgumentParser(description="offline scorer eval (plan C6/P5)")
    ap.add_argument("--scorers", default="rule,hf")
    ap.add_argument("--dataset", default=str(ROOT / "runs" / "laya" / "dataset" / "test.jsonl"))
    ap.add_argument("--shadow", default=str(ROOT / "runs" / "laya" / "shadow.jsonl"))
    ap.add_argument("--hf-model", default=str(ROOT / "runs" / "laya" / "finetuned"))
    ap.add_argument("--small-model", default=None)
    ap.add_argument("--laya-model", default=None)
    ap.add_argument("--limit", type=int, default=500, help="每 scorer 最多评多少条")
    ap.add_argument("--out", default=str(ROOT / "runs" / "laya" / "eval"))
    args = ap.parse_args()

    recs = load_records(args)
    evals = [r for r in recs if r.get("human_label") in (r.get("labels") or [])]
    print(f"记录: 总 {len(recs)}, 可评(有 human/chosen 标签) {len(evals)}")
    if not evals:
        print("无可评记录 — 先 C4 建数据集 / P1 shadow 采集")
        return 1
    scorers, notes = build_scorers(args)
    for n in notes:
        print(f"[note] {n}")

    report = {}
    for name, sc in scorers:
        agree = div = n = 0
        times = []
        for rec in evals[:args.limit]:
            labels = rec["labels"]
            t0 = time.time()
            try:
                probs = sc.score(rec["state"], labels, rec)
            except Exception as e:
                print(f"[{name}] err {type(e).__name__}: {e}")
                probs = None
            times.append((time.time() - t0) * 1000)
            if probs is None:
                continue
            n += 1
            top = labels[max(range(len(probs)), key=lambda i: probs[i])]
            if top == rec["human_label"]:
                agree += 1
            if labels[0] != top:
                div += 1
        p95 = sorted(times)[int(0.95 * (len(times) - 1))] if times else 0.0
        report[name] = {"n": n, "coverage": round(n / max(1, min(len(evals), args.limit)), 3),
                        "top1_agree": round(agree / n, 4) if n else None,
                        "diverge_rule": round(div / n, 4) if n else None,
                        "p95_ms": round(p95, 1)}
        print(f"[{name:6s}] n={n} top1一致率={report[name]['top1_agree']} "
              f"与规则分歧={report[name]['diverge_rule']} p95={report[name]['p95_ms']}ms")

    # P5 门槛: 一致率胜出者上线(排除覆盖率过低的; rule 是基线不参评)
    cand = {k: v for k, v in report.items() if k != "rule"
            and v["top1_agree"] is not None and v["coverage"] >= 0.9}
    if cand:
        win = max(cand, key=lambda k: cand[k]["top1_agree"])
        base = report.get("rule", {}).get("top1_agree")
        if base is not None and cand[win]["top1_agree"] <= base:
            print(f"\nP5 结论: 无 scorer 胜过规则基线({base}) — 保持现状(方案 P5)")
        else:
            report["winner"] = win
            print(f"\nP5 结论: 上线 {win} (一致率 {cand[win]['top1_agree']}); "
                  f"当前基线 rule={base}")
    else:
        print("\nP5 结论: 无可参评 scorer(需 --hf-model/--scorers laya,clef,small)")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
    print(f"报告: {out / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
