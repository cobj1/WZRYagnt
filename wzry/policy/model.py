# -*- coding: utf-8 -*-
"""策略网络 v0（行为克隆骨架）：结构化 GameState -> 动作分布。

输入（维度直接取自 wzry/train/encoding.py，避免两处手写漂移）：
  units     (B, N=20, UNIT_DIM)  单位特征（one-hot cls + 屏幕坐标 4 + conf + trust + dead）
  unit_mask (B, N)
  grid      (B, 40, 40, GRID_CH) 小地图栅格（蓝/红/黄/绿密度）
  ui        (B, len(UI_KEYS))    UI 数值（hp/mp/gold/level/kills/deaths/skillN_ready/death_banner，缺失 -1）

输出：
  move_theta  (B, 8)   8 向移动离散分布（行为克隆常用离散化）
  move_r      (B, 1)   移动幅度（sigmoid）
  act         (B, 8)   离散动作分布：none/skill1/skill2/skill3/attack/buy/recall/summoner
  target      (B, 2)   技能目标点（归一化 0-1，sigmoid）

训练阶段仅需前向；M5 强化学习时复用 encoder 与 head。
"""
import torch
import torch.nn as nn

from wzry.train.encoding import CLASSES, GRID, GRID_CH, UI_KEYS, UNIT_DIM

N_UNITS = 20
UI_DIM = len(UI_KEYS)
N_MOVE_DIR = 8
N_ACT = 10   # move/skill1/skill2/skill3/attack/buy/recall/summoner/restore/none（与 encode_action 对齐）


class UnitEncoder(nn.Module):
    """单位列表编码：per-unit MLP -> 平均池化（mask 感知）。"""

    def __init__(self, hidden=64):
        super().__init__()
        self.mlp = nn.Sequential(
            nn.Linear(UNIT_DIM, hidden), nn.ReLU(),
            nn.Linear(hidden, hidden), nn.ReLU(),
        )

    def forward(self, units, mask):
        # units: (B,N,16), mask: (B,N)
        x = self.mlp(units)                       # (B,N,64)
        m = mask.unsqueeze(-1)                    # (B,N,1)
        s = (x * m).sum(dim=1)                    # (B,64)
        n = m.sum(dim=1).clamp(min=1.0)
        return s / n


class GridEncoder(nn.Module):
    """小地图栅格编码：小 CNN。"""

    def __init__(self, out_dim=64):
        super().__init__()
        self.cnn = nn.Sequential(
            nn.Conv2d(GRID_CH, 16, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),   # 20x20
            nn.Conv2d(16, 32, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),  # 10x10
            nn.Conv2d(32, 32, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),  # 5x5
            nn.Flatten(),
            nn.Linear(32 * 5 * 5, out_dim), nn.ReLU(),
        )

    def forward(self, grid):
        # grid: (B,40,40,3) -> (B,3,40,40)
        x = grid.permute(0, 3, 1, 2)
        return self.cnn(x)


class BCNet(nn.Module):
    def __init__(self, hidden=128):
        super().__init__()
        self.unit_enc = UnitEncoder(hidden)
        self.grid_enc = GridEncoder(hidden)
        self.fc = nn.Sequential(
            nn.Linear(hidden * 2 + UI_DIM, hidden), nn.ReLU(),
            nn.Linear(hidden, hidden), nn.ReLU(),
        )
        self.move_theta = nn.Linear(hidden, N_MOVE_DIR)
        self.move_r = nn.Linear(hidden, 1)
        self.act = nn.Linear(hidden, N_ACT)
        self.target = nn.Linear(hidden, 2)

    def forward(self, units, unit_mask, grid, ui):
        u = self.unit_enc(units, unit_mask)        # (B,hidden)
        g = self.grid_enc(grid)                    # (B,hidden)
        x = self.fc(torch.cat([u, g, ui], dim=-1))
        return {
            "move_theta": torch.log_softmax(self.move_theta(x), dim=-1),
            "move_r": torch.sigmoid(self.move_r(x)),
            "act": torch.log_softmax(self.act(x), dim=-1),
            "target": torch.sigmoid(self.target(x)),
        }


class BCNetMem(nn.Module):
    """BCNet + 记忆层（GRU）：让决策看到"最近几秒"，而不是单帧。

    结构：共享编码器（unit MLP + 格栅 CNN，与 BCNet 同一套模块）
      -> fc -> h_t（单帧表征）
      -> GRUCell(h_t, mem_{t-1}) -> mem_t
      -> fuse([h_t, mem_t]) -> 四个头（与 BCNet 相同：move_theta/move_r/act/target）

    hidden 形状 (B, MEM_DIM)。推理时可跨帧持久携带（见 BCNetInference），
    在"阵亡复活 / 换局 / 阶段切换"处必须重置，否则记忆会跨局污染。
    """

    def __init__(self, hidden=128):
        super().__init__()
        self.unit_enc = UnitEncoder(hidden)
        self.grid_enc = GridEncoder(hidden)
        self.fc = nn.Sequential(
            nn.Linear(hidden * 2 + UI_DIM, hidden), nn.ReLU(),
            nn.Linear(hidden, hidden), nn.ReLU(),
        )
        self.gru = nn.GRUCell(hidden, hidden)
        self.fuse = nn.Sequential(nn.Linear(hidden * 2, hidden), nn.ReLU())
        self.move_theta = nn.Linear(hidden, N_MOVE_DIR)
        self.move_r = nn.Linear(hidden, 1)
        self.act = nn.Linear(hidden, N_ACT)
        self.target = nn.Linear(hidden, 2)

    def init_hidden(self, batch=1, device=None):
        p = next(self.parameters())
        return torch.zeros(batch, self.gru.hidden_size,
                           device=device or p.device, dtype=p.dtype)

    def forward(self, units, unit_mask, grid, ui, hidden=None):
        """返回 (输出dict, mem_t)。hidden=None 时按零初始化。"""
        u = self.unit_enc(units, unit_mask)
        g = self.grid_enc(grid)
        h = self.fc(torch.cat([u, g, ui], dim=-1))
        if hidden is None:
            hidden = self.init_hidden(h.shape[0], h.device)
        mem = self.gru(h, hidden)
        x = self.fuse(torch.cat([h, mem], dim=-1))
        return {
            "move_theta": torch.log_softmax(self.move_theta(x), dim=-1),
            "move_r": torch.sigmoid(self.move_r(x)),
            "act": torch.log_softmax(self.act(x), dim=-1),
            "target": torch.sigmoid(self.target(x)),
        }, mem


if __name__ == "__main__":
    # 冒烟测试：随机输入跑通前向（BCNet 与 BCNetMem 都要过）
    torch.manual_seed(0)
    B = 4
    units = torch.randn(B, N_UNITS, UNIT_DIM)
    units[:, :, :len(CLASSES)] = torch.softmax(units[:, :, :len(CLASSES)], dim=-1)  # one-hot-ish
    mask = (torch.rand(B, N_UNITS) > 0.5).float()
    grid = torch.rand(B, GRID, GRID, GRID_CH)
    ui = torch.randn(B, UI_DIM)

    net = BCNet()
    out = net(units, mask, grid, ui)
    for k, v in out.items():
        print(f"{k:<12} {tuple(v.shape)}")
    assert out["move_theta"].shape == (B, N_MOVE_DIR)
    assert out["act"].shape == (B, N_ACT)
    assert out["target"].shape == (B, 2)
    assert torch.allclose(out["move_theta"].exp().sum(-1), torch.ones(B), atol=1e-4)
    assert torch.allclose(out["act"].exp().sum(-1), torch.ones(B), atol=1e-4)
    print("BCNet 冒烟测试通过")

    memnet = BCNetMem()
    out2, mem = memnet(units, mask, grid, ui)
    out3, mem2 = memnet(units, mask, grid, ui, hidden=mem)
    print("BCNetMem mem:", tuple(mem.shape), "->", tuple(mem2.shape))
    assert mem.shape == (B, 128) and mem2.shape == (B, 128)
    assert out2["act"].shape == (B, N_ACT)
    # 同一输入、不同 hidden -> 输出必须不同（记忆真的参与计算）
    assert not torch.allclose(out2["act"], out3["act"]), "hidden 没有参与计算"
    print("BCNetMem 冒烟测试通过（hidden 跨帧携带有效）")
