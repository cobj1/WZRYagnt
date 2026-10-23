# -*- coding: utf-8 -*-
"""map_move 的"到达"闸门 —— 无真实位置反馈下的停止判据（v16.2）。

背景（为什么要这个）
------------------
`self_est` 是**开环航位推算**：按摇杆方向 × STICK_SPEED × 时间累积，没有真实位置反馈。
原实现在 `dist < MAPMOVE_NEAR`（约 0.04）时把估计**强制重置回本方基地**再继续推进，注释
理由是"否则估计到点后就再也离不开 → 英雄站立不动"（实测 180s 只发 19 次 swipe、被连杀
4 次、0 击杀）。

但这个防僵死手段本身带来**过冲**：英雄实际已在目标附近时，`θ = atan2(-dy, dx)` 变成
"基地 → 目标"方向的角 —— 沿着这个方向走就是从目标继续往外走。旧实现只在每次 swipe
结束后松手，过冲被"走走停停"掩盖；换成持久摇杆（连续移动）后过冲会持续累积。

本闸门给出的取舍
--------------
- **到达即停**：`dist < near` → 保持停推（调用方应松手）。这是正常路径，消除过冲。
- **保险丝**：若在同一位置连续停推超过 `idle_max` 秒，说明估计很可能已经失真（英雄
  其实不在目标上），此时返回 `"fuse"` 让调用方走回旧的"重锚回基地 + 继续推进"。
  这样既拿回了停止语义，又保留了原实现的防僵死兜底；`fuse_count` 可观测其触发频率
  —— 频繁触发说明估计失真严重，该去补真实位置反馈（小地图自身定位），而不是继续调参。

无状态依赖、无 IO，可离线单测（见 `scripts/test_arrival_gate.py`）。
"""


class ArrivalGate:
    """到达闸门。`decide(now, dist)` 返回 'hold' / 'fuse' / 'push'。"""

    def __init__(self, near=0.04, idle_max=5.0):
        self.near = float(near)
        self.idle_max = float(idle_max)
        self.holding = False      # 当前是否处于"到达后停推"状态
        self.since = 0.0          # 本轮停推的起点时刻
        self.fuse_count = 0       # 保险丝触发次数(可观测)

    def decide(self, now, dist):
        """判断本拍该做什么。

        'hold' -> 已到达, 调用方应松手并停止推进(正常路径)
        'fuse' -> 停推太久, 估计可能失真, 调用方应回退到"重锚回基地 + 继续推进"
        'push' -> 还没到, 正常朝目标推进
        """
        if dist >= self.near:
            self.holding = False
            self.since = 0.0
            return "push"
        if not self.holding:
            self.holding = True
            self.since = now
            return "hold"
        if now - self.since <= self.idle_max:
            return "hold"
        self.fuse_count += 1
        self.holding = False
        self.since = 0.0
        return "fuse"

    def reset(self):
        """目标已更换时调用（可选）。"""
        self.holding = False
        self.since = 0.0
