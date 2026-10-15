# -*- coding: utf-8 -*-
"""laya-fusion-plan P1 预案: 把 laya (ModernBERT DecisionModel) 导出为 ONNX。

laya 官方 ONNXAgent 期望的图契约 (见 laya/onnx_agent._infer_batch):
  inputs : input_ids[B,L] i64 / attention_mask[B,L] i64 / marker_pos[B,K] i64 /
           marker_mask[B,K] bool / qtype[B] i64
  outputs: logits[B,K] f32 / act_logits[B,K] f32
Agent.model (laya.common.DecisionModel) 的 forward 与之一致, 直接 trace 导出。
ONNXAgent 文案指向的官方 export_onnx.py 在 laya-ts 子模块, 公开渠道拿不到,
故本脚本按同契约自产。仅覆盖 choice 类问题(我们的全部用例)。

运行: venv/Scripts/python.exe -X utf8 scripts/export_laya_onnx.py [--out runs/laya/laya.onnx]
"""
import argparse
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    ap = argparse.ArgumentParser(description="export laya DecisionModel to ONNX")
    ap.add_argument("--out", default=str(ROOT / "runs" / "laya" / "laya.onnx"))
    ap.add_argument("--opset", type=int, default=17)
    args = ap.parse_args()

    import torch
    import laya
    from laya.common import QTYPES, build_sequence, collate_items

    print("[export] 加载 torch Agent (fp32, ~800MB)...")
    t0 = time.time()
    # ModernBERT 会命中 torch TransformerEncoderLayer 融合 fastpath(aten::
    # _transformer_encoder_layer_fwd, 不可 trace); 禁用后走普通前向
    torch.backends.mha.set_fastpath_enabled(False)
    ag = laya.load()
    model = ag.model.eval()
    tok = ag.tok
    print(f"[export] 就绪 {time.time()-t0:.0f}s, model={type(model).__name__}")

    # 真实形状的样例批: 单状态 x 16 选项 choice 问题 (对齐线上 top-16)
    # build_sequence 需要内部化格式 {"t","ins","crit"} (同 ONNXAgent._to_internal)
    q = {"t": "choice", "ins": "Choose the best option for the hero's next action.",
         "crit": {f"cand{i}": "candidate action" for i in range(16)}}
    state = ("MOBA hero positioning. camp=blue hp=0.90 self=(0.50,0.50) "
             "enemies_map=2 allies_map=2 enemy_heroes_screen=1 enemy_turrets=0 "
             "nearest_enemy_d=0.15 turret_d=1.00 in_turret_zone=0. "
             "enemy_pos=0.70,0.75; ally_pos=0.55,0.50 0.45,0.45.")
    seq, markers, _stats, _ss = build_sequence(
        tok, state, q, 512, 192, return_stats=True,
        return_truncation_stats=True)
    items = [{"ids": seq, "markers": markers, "qtype": QTYPES["choice"],
              "options": _stats, "state_stats": _ss}]
    b = collate_items([items], tok.pad_token_id)
    sample = (b["input_ids"],
              b["attention_mask"],
              b["marker_pos"].long(),
              b["marker_mask"].bool(),
              b["qtype"].long())
    print(f"[export] 样例批: ids={tuple(sample[0].shape)} "
          f"markers={tuple(sample[2].shape)}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"[export] torch.onnx.export -> {out} (首次数分钟)...")
    t0 = time.time()
    with torch.no_grad():
        torch.onnx.export(
            model, sample, str(out),
            input_names=["input_ids", "attention_mask", "marker_pos",
                         "marker_mask", "qtype"],
            output_names=["logits", "act_logits"],
            dynamic_axes={
                "input_ids": {0: "B", 1: "L"},
                "attention_mask": {0: "B", 1: "L"},
                "marker_pos": {0: "B", 1: "K"},
                "marker_mask": {0: "B", 1: "K"},
                "qtype": {0: "B"},
                "logits": {0: "B", 1: "K"},
                "act_logits": {0: "B", 1: "K"},
            },
            opset_version=args.opset,
            dynamo=False,
        )
    print(f"[export] 完成 {time.time()-t0:.0f}s, "
          f"大小 {out.stat().st_size/1e6:.0f}MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
