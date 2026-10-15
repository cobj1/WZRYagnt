# -*- coding: utf-8 -*-
"""行为克隆训练（M4 前置）：从 npz 特征库训练 BCNet / BCNetMem。

两种数据形态自动识别：
  1) 单帧库（data/datasets/bc_v1.npz，states_to_dataset 产出）
       units (N,20,U) -> 训练 BCNet
  2) 序列库（data/datasets/bc_seq_v1.npz，states_to_sequences 产出）
       units (W,L,20,U) + reset (W,L) -> 训练 BCNetMem（BPTT）

loss = CE(move_theta) + MSE(move_r) + CE(act) + MSE(target)，按 valid 掩码加权：
  - 规则 Agent 的 map_move 只有 target 可信（theta 由执行器现场航位推算）
  - 手势/录像反推的 move 只有 theta 可信
  -> 不掩码就等于让 theta 头去拟合规则数据里恒为 0 的假方向

用法：
    venv\\Scripts\\python.exe scripts\\train\\bc_train.py --data data\\datasets\\bc_v1.npz --epochs 20
    venv\\Scripts\\python.exe scripts\\train\\bc_train.py --data data\\datasets\\bc_seq_v1.npz --epochs 6 --batch 8
"""
import argparse
import sys
import time
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from wzry.policy.model import BCNet, BCNetMem, N_ACT  # noqa: E402

ACT_NAMES = ["move", "skill1", "skill2", "skill3", "attack",
             "buy", "recall", "summoner", "restore", "none"]
F = torch.nn.functional


def decode_actions(actions):
    """动作向量 -> (onehot idx, theta bin, r, target, theta_valid, target_valid)"""
    a = actions.float()
    act_idx = a[..., :10].argmax(-1)
    theta_rad = (a[..., 10] * (2 * 3.141592653589793 + 1e-6)) % (2 * 3.141592653589793)
    theta_bin = ((theta_rad / (2 * 3.141592653589793)) * 8).long() % 8
    r = a[..., 11]
    target = a[..., 12:14]
    if a.shape[-1] >= 16:
        tv, gv = a[..., 14], a[..., 15]
    else:
        tv = torch.ones_like(r)
        gv = torch.ones_like(r)
    return act_idx, theta_bin, r, target, tv, gv


def masked_mean(x, mask):
    return (x * mask).sum() / mask.sum().clamp(min=1.0)


def losses(out, act_idx, theta_bin, r, target, tv, gv, w):
    l_act = F.cross_entropy(out["act"], act_idx)
    l_theta = masked_mean(F.cross_entropy(out["move_theta"], theta_bin, reduction="none"), tv)
    l_r = F.mse_loss(out["move_r"].squeeze(-1), r)
    l_tg = masked_mean(
        F.mse_loss(out["target"], target, reduction="none").mean(-1), gv)
    return w[0] * l_theta + w[1] * l_r + w[2] * l_act + w[3] * l_tg


def report(d, n):
    print("动作标签分布:", {k: v for k, v in
                       sorted(d.items(), key=lambda kv: -kv[1])})
    print(f"监督覆盖: theta {int(n['tv'])}/{n['all']}  target {int(n['gv'])}/{n['all']}")


@torch.no_grad()
def eval_acc(net, mode, units, mask, grid, ui, actions, reset=None):
    net.eval()
    act_idx, theta_bin, r, target, tv, gv = decode_actions(actions)
    if mode == "seq":
        W, L = units.shape[0], units.shape[1]
        hidden = None
        correct_act = 0
        tot = 0
        theta_ok = 0
        tv_sum = 0
        tg_err = 0.0
        gv_sum = 0
        for t in range(L):
            rt = reset[:, t]
            if hidden is not None and rt.any():
                hidden = hidden * (~rt).unsqueeze(-1).float()
            out, hidden = net(units[:, t], mask[:, t], grid[:, t], ui[:, t], hidden)
            pred = out["act"].argmax(-1)
            correct_act += int((pred == act_idx[:, t]).sum())
            tot += W
            pb = out["move_theta"].argmax(-1)
            m = tv[:, t] > 0
            theta_ok += int(((pb == theta_bin[:, t]) & m).sum())
            tv_sum += int(m.sum())
            tg_err += float(((out["target"] - target[:, t]).abs().mean(-1) * gv[:, t]).sum())
            gv_sum += int(gv[:, t].sum())
    else:
        out = net(units, mask, grid, ui)
        n = units.shape[0]
        correct_act = int((out["act"].argmax(-1) == act_idx).sum())
        tot = n
        m = tv > 0
        theta_ok = int(((out["move_theta"].argmax(-1) == theta_bin) & m).sum())
        tv_sum = int(m.sum())
        tg_err = float(((out["target"] - target).abs().mean(-1) * gv).sum())
        gv_sum = int(gv.sum())
    net.train()
    return {"act_acc": round(correct_act / max(tot, 1), 4),
            "theta_acc": round(theta_ok / max(tv_sum, 1), 4) if tv_sum else None,
            "target_mae": round(tg_err / max(gv_sum, 1), 4) if gv_sum else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data" / "datasets" / "bc_v1.npz"))
    ap.add_argument("--epochs", type=int, default=20)
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--out", default=None)
    ap.add_argument("--w-move", type=float, default=1.0)
    ap.add_argument("--w-r", type=float, default=1.0)
    ap.add_argument("--w-act", type=float, default=1.0)
    ap.add_argument("--w-target", type=float, default=1.0)
    args = ap.parse_args()

    data_path = Path(args.data)
    if not data_path.exists():
        print(f"数据集不存在: {data_path}（先跑 states_to_dataset / states_to_sequences）")
        return 1
    import numpy as np
    z = np.load(data_path)
    units = torch.from_numpy(z["units"]).float()
    mask = torch.from_numpy(z["unit_mask"]).float()
    grid = torch.from_numpy(z["grid"]).float()
    ui = torch.from_numpy(z["ui"]).float()
    actions = torch.from_numpy(z["actions"]).float()
    reset = torch.from_numpy(z["reset"]).bool() if "reset" in z.files else None
    # 序列库布局 (W, L, N, UNIT_DIM) = 4 维；单帧库 (N, N, UNIT_DIM) = 3 维
    seq_mode = units.dim() == 4
    if seq_mode and reset is None:
        reset = torch.zeros(units.shape[0], units.shape[1], dtype=torch.bool)
        reset[:, 0] = True

    act_idx, theta_bin, r, target, tv, gv = decode_actions(actions)
    dist = {}
    for i in act_idx.reshape(-1).tolist():
        dist[ACT_NAMES[int(i)]] = dist.get(ACT_NAMES[int(i)], 0) + 1
    n_all = int(act_idx.numel())
    report(dist, {"all": n_all, "tv": int(tv.sum()), "gv": int(gv.sum())})

    if seq_mode:
        W, L = units.shape[0], units.shape[1]
        print(f"序列数据集: {W} 窗口 x {L} 帧  units={tuple(units.shape)}")
        net = BCNetMem()
        n_batches = max(1, W // args.batch)
    else:
        n = units.shape[0]
        print(f"单帧数据集: {n} 样本  units={tuple(units.shape)}")
        net = BCNet()
        n_batches = max(1, n // args.batch)

    out_path = Path(args.out) if args.out else (
        ROOT / "runs" / "bc" / ("bc_mem_v1.pt" if seq_mode else "bc_v1.pt"))
    opt = torch.optim.Adam(net.parameters(), lr=args.lr)
    w = (args.w_move, args.w_r, args.w_act, args.w_target)
    t0 = time.time()
    net.train()

    @torch.no_grad()
    def eval_now():
        return eval_acc(net, "seq" if seq_mode else "frame",
                        units, mask, grid, ui, actions, reset)

    for epoch in range(args.epochs):
        if seq_mode:
            perm = torch.randperm(W)
            losses_ep = []
            for b in range(n_batches):
                idx = perm[b * args.batch:(b + 1) * args.batch]
                hidden = None
                opt.zero_grad()
                loss = 0.0
                for t in range(L):
                    rt = reset[idx, t]
                    if hidden is not None and rt.any():
                        hidden = hidden * (~rt).unsqueeze(-1).float()
                    out, hidden = net(units[idx, t], mask[idx, t], grid[idx, t],
                                      ui[idx, t], hidden)
                    loss = loss + losses(out, act_idx[idx, t], theta_bin[idx, t],
                                         r[idx, t], target[idx, t], tv[idx, t],
                                         gv[idx, t], w)
                loss = loss / L
                loss.backward()
                opt.step()
                losses_ep.append(float(loss.detach()))
        else:
            perm = torch.randperm(units.shape[0])
            losses_ep = []
            for b in range(n_batches):
                idx = perm[b * args.batch:(b + 1) * args.batch]
                out = net(units[idx], mask[idx], grid[idx], ui[idx])
                loss = losses(out, act_idx[idx], theta_bin[idx], r[idx],
                              target[idx], tv[idx], gv[idx], w)
                opt.zero_grad()
                loss.backward()
                opt.step()
                losses_ep.append(float(loss.detach()))
        if (epoch + 1) % 5 == 0 or epoch == 0:
            ev = eval_now()
            print(f"epoch {epoch + 1}/{args.epochs}  loss={sum(losses_ep) / len(losses_ep):.4f}  "
                  f"act_acc={ev['act_acc']}  theta_acc={ev['theta_acc']}  "
                  f"target_mae={ev['target_mae']}  ({time.time() - t0:.0f}s)")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(net.state_dict(), out_path)
    ev = eval_now()
    print(f"模型已保存: {out_path}  (memory={seq_mode})  末次评估: {ev}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
