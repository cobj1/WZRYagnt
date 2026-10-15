# -*- coding: utf-8 -*-
"""laya-fusion-plan C5: RLCD 微调 + 温度校准 (方案 §2/§6-P2)。

参照官方 Kaggle notebook 的 RLCD 思路(基于严格适当评分规则的对比后训练):
  对 (state, preferred, rejected) 对:
    loss = -log σ(τ·(lp(preferred) − lp(rejected)))  +  λ · NLL(preferred)
  preferred/rejected 来自 C4 数据集:
    preferred = human_label(人类移动映射) 或 advantage>0 的已选候选
    rejected  = advantage<0 的已选候选, 或(--neg-rule)与人类选择不同的规则 argmax

温度校准: val 集上对每个候选的长度归一化 logprob 除以 T, 网格搜索最小化人类 top-1 NLL。
P2 门槛: test 集 top-1 一致率 明显高于 规则 r 的 argmax(脚本直接输出对比)。

依赖: torch + transformers(--base 指向 laya 基座 HF id 或本地路径)。
运行: venv/Scripts/python.exe -X utf8 scripts/train_laya.py \
        --base <model_id_or_path> [--epochs 2] [--dataset runs/laya/dataset]
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTR = ("Choose the best option for the hero's next action. "
         "Prefer options that enable kills with allies nearby, avoid enemy "
         "clusters and turret zones when low HP, and stay on the objective path.")


def load_split(d, name):
    p = d / f"{name}.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def build_prompt(rec):
    return (f"{rec['state']}\n{INSTR}\n"
            f"Options: {', '.join(rec['labels'])}\nBest option:")


def build_pairs(train, neg_rule=True):
    """RLCD 对: (rec, preferred, rejected)。证据优先级:
      1) 结果回溯负优势候选 -> rejected (方案 §5-B: 正负都要)
      2) 人类移动映射 -> preferred (方案 §5-A)
      3) 纯结果正例(adv>0 的已选候选) -> preferred
      同一候选既是人类正例又负优势时证据矛盾 -> 跳过该帧。
      rejected 兜底: --neg-rule 时用与 preferred 不同的规则 argmax(仅作反例, 非标签)。"""
    pairs = []
    for rec in train:
        labels = rec.get("labels") or []
        if len(labels) < 2:
            continue
        hum = rec.get("human_label")
        adv = rec.get("advantage") or 0.0
        adv_lab = rec.get("advantage_label")
        rule_top = labels[0]        # rule_scores 已按 r 降序(C4 保证)
        pref = rej = None
        if adv < 0 and adv_lab in labels:
            rej = adv_lab
            pref = hum if (hum in labels and hum != rej) else None
        elif hum in labels:
            pref = hum
            if adv > 0 and adv_lab in labels and adv_lab != pref:
                rej = adv_lab
        elif adv > 0 and adv_lab in labels:
            pref = adv_lab
        if pref is None:
            continue
        if rej is None and neg_rule and rule_top != pref:
            rej = rule_top
        if rej is not None and rej != pref:
            pairs.append((rec, pref, rej))
    return pairs


# ---------- 模型打分(torch/transformers 可用时) ----------
class LMScorer:
    """长度归一化 label logprob 打分器。"""

    def __init__(self, base, device=None):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self.torch = torch
        self.tok = AutoTokenizer.from_pretrained(base)
        self.model = AutoModelForCausalLM.from_pretrained(
            base, torch_dtype=(torch.float16 if torch.cuda.is_available() else None))
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.model.to(self.device).eval()

    def _lp_tokens(self, prompt_ids, comp_ids):
        import torch
        ids = prompt_ids + comp_ids
        with torch.no_grad():
            logits = self.model(torch.tensor([ids], device=self.device)).logits[0]
        lp = torch.log_softmax(logits.float(), dim=-1)
        n = len(comp_ids)
        total = 0.0
        for k in range(n):
            pos = len(prompt_ids) - 1 + k
            total += float(lp[pos, comp_ids[k]])
        return total, n

    def label_logp(self, prompt, label):
        tok = self.tok
        p_ids = tok.encode(prompt)
        c_ids = tok.encode(" " + label)
        total, n = self._lp_tokens(p_ids, c_ids)
        return total / max(1, n)          # 长度归一化(标签 3-4 token, 防短标签偏置)

    def rlcd_step(self, opt, batch, tau, lam):
        import torch
        losses = []
        for prompt, pref, rej in batch:
            p_ids = self.tok.encode(prompt)
            pre_ids = self.tok.encode(" " + pref)
            rej_ids = self.tok.encode(" " + rej)

            def lp_of(comp_ids):
                ids = p_ids + comp_ids
                logits = self.model(torch.tensor([ids], device=self.device)).logits[0]
                lps = torch.log_softmax(logits.float(), dim=-1)
                s = 0.0
                for k in range(len(comp_ids)):
                    pos = len(p_ids) - 1 + k
                    s += lps[pos, comp_ids[k]]
                return s

            lp_p, lp_r = lp_of(pre_ids), lp_of(rej_ids)
            contrast = -torch.logsigmoid(tau * (lp_p - lp_r))
            nll = -lp_p / max(1, len(pre_ids))
            losses.append(contrast + lam * nll)
        loss = torch.stack(losses).mean()
        opt.zero_grad()
        loss.backward()
        opt.step()
        return float(loss.detach())


def train(args):
    ds = Path(args.dataset)
    train = load_split(ds, "train")
    val = load_split(ds, "val")
    test = load_split(ds, "test")
    if not train:
        print(f"无训练数据 {ds}/train.jsonl — 先跑 scripts/build_laya_dataset.py (C4)")
        return 1
    pairs = build_pairs(train, neg_rule=not args.no_neg_rule)
    print(f"数据: train={len(train)} val={len(val)} test={len(test)} RLCD对={len(pairs)}")
    if len(pairs) < args.min_pairs:
        print(f"RLCD 对不足 {len(pairs)} < {args.min_pairs} — 先补数据(方案 P2: 补数据/改状态模板)")
        return 1
    try:
        sc = LMScorer(args.base)
    except Exception as e:
        print(f"模型加载失败({e}) — 需 torch+transformers 且 --base 指向 laya 基座")
        return 1

    import torch
    opt = torch.optim.AdamW(sc.model.parameters(), lr=args.lr)
    t0 = time.time()
    step = 0
    for ep in range(args.epochs):
        for i in range(0, len(pairs), args.batch):
            batch = [(build_prompt(rec), p, r) for rec, p, r in
                     pairs[i:i + args.batch]]
            loss = sc.rlcd_step(opt, batch, args.tau, args.lam)
            step += 1
            if step % 20 == 0:
                print(f"ep{ep} step{step}/{(len(pairs) + args.batch - 1)//args.batch} "
                      f"loss={loss:.4f} {time.time()-t0:.0f}s")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    sc.model.save_pretrained(out)
    sc.tok.save_pretrained(out)

    # ---- 温度校准(val): 网格搜 T 最小化人类 top-1 NLL ----
    best_T, best_nll = 1.0, float("inf")
    for T in [round(0.25 * k, 2) for k in range(2, 17)]:      # 0.50..4.00
        nll, n = 0.0, 0
        for rec in val:
            if not rec.get("human_label"):
                continue
            s = [sc.label_logp(build_prompt(rec), lab) / T for lab in rec["labels"]]
            mx = max(s)
            e = [math.exp(x - mx) for x in s]
            z = sum(e)
            idx = rec["labels"].index(rec["human_label"])
            nll += -(math.log(e[idx] / z)) if idx < len(s) and e[idx] > 0 else 20.0
            n += 1
        if n and nll / n < best_nll:
            best_nll, best_T = nll / n, T
    (out / "calibration.json").write_text(
        json.dumps({"temperature": best_T, "val_nll": round(best_nll, 4),
                    "base": args.base}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"温度校准: T={best_T} (val NLL={best_nll:.4f})")

    # ---- P2 门槛: test top-1 一致率 vs 规则 r argmax ----
    agree_m, agree_r, n_eval = 0, 0, 0
    for rec in test:
        if not rec.get("human_label"):
            continue
        n_eval += 1
        s = [sc.label_logp(build_prompt(rec), lab) / best_T for lab in rec["labels"]]
        if rec["labels"][max(range(len(s)), key=lambda i: s[i])] == rec["human_label"]:
            agree_m += 1
        if rec["labels"][0] == rec["human_label"]:       # rule_scores 降序 -> labels[0]
            agree_r += 1
    if n_eval:
        rep = {"test_n": n_eval,
               "model_top1_agree": round(agree_m / n_eval, 4),
               "rule_top1_agree": round(agree_r / n_eval, 4),
               "p2_gate": (agree_m / n_eval) > (agree_r / n_eval)}
        (out / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
        print(f"P2 门槛: 模型 top-1 一致率 {agree_m}/{n_eval}={rep['model_top1_agree']:.3f} "
              f"vs 规则 r argmax {agree_r}/{n_eval}={rep['rule_top1_agree']:.3f} "
              f"-> {'通过' if rep['p2_gate'] else '未过(补数据/改状态模板)'}")
    else:
        print("test 集无 human_label 记录, P2 一致率不可评(需人类对局数据)")
    print(f"产物: {out}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="train laya scorer (plan C5)")
    ap.add_argument("--base", required=True, help="laya 基座 HF id 或本地路径")
    ap.add_argument("--dataset", default=str(ROOT / "runs" / "laya" / "dataset"))
    ap.add_argument("--out", default=str(ROOT / "runs" / "laya" / "finetuned"))
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--lr", type=float, default=1e-5)
    ap.add_argument("--tau", type=float, default=1.0, help="RLCD 对比温度")
    ap.add_argument("--lam", type=float, default=0.2, help="NLL 辅助项权重")
    ap.add_argument("--min-pairs", type=int, default=200)
    ap.add_argument("--no-neg-rule", action="store_true",
                    help="不用规则 argmax(与人类不同)作 rejected")
    args = ap.parse_args()
    sys.exit(train(args))


if __name__ == "__main__":
    main()
