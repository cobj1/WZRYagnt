# 反馈层奖惩标准（以 `wzry/feedback/reward.py` 为准）

> 权威实现：`wzry/feedback/reward.py` 的 `EVENT_SCORES`（v3.2 用户系数）。
> 分数是**增量累计**，`RewardSystem.total` 从 0 起算，不是 0–100 量表。
> 校准准则：**鼓励高效击杀/推塔/生存，重罚无脑送死/空转浪费**。

## 计分事件

| 事件 | 分 | 说明 |
|---|---|---|
| 胜利 victory | +80 | 对局胜利 |
| 失败 defeat | -80 | 对局失败 |
| 勾到并击杀 hook_kill | +30 | 勾中后完成击杀 |
| 击杀 kill | +20 | 击杀敌方英雄 |
| 勾到 hook | +10 | 勾中但未击杀 |
| 阵亡 died | -50 | 死亡（送死重罚） |
| 推塔 tower_kill | +10 | 拆除敌方防御塔 |
| 助攻 assist | +15 | 参与击杀 |
| 清兵 minion_clear | +2 | 每个敌方小兵 |
| 被塔打 be_tower_attacked | -20 | 塔攻击范围内硬吃塔伤 |
| 被攻击 be_attacked | -3 | 每次被英雄攻击 |
| 回城被打断 recall_interrupted | -10 | 回城读条被打断 |
| 支援 supporting | 0 | 用户明确“这是应该做的”，不计分 |

去重窗口见 `EVENT_DEDUP_S`（如 `kill` 3s、`hook` 2s、`died` 5s、`be_attacked` 1s）。

## 只在旧设计稿中出现、代码未实现

以下条目不再计分；如要启用需先改 `EVENT_SCORES` 并补测试：

- `ult_combo +5`（勾→晕→大连招）
- `escape_alive +5`（低血安全脱离）
- `wasted_skill -2`（无目标释放技能）
- “血蓝 <80% 未恢复 / <15% 未回城” -5

这些标签目前只在离线数据集（如 `scripts/build_laya_dataset.py` 的 `escape_alive`）里使用，不进入在线 RewardSystem。

## 学习目标（用户手打局为基准）

- 评分 ≥ 8.0（用户局 MVP 水平）、KDA 击杀≥2/死亡≤1/助攻≥2
- 参团率 ≥ 60%、输出占比 ≥ 15%（钟馗辅助位）
- 全图支援：中/下路 45/45；无上路无脑单机

## 待用户校准项

1. 各事件分值（可整体缩放）
2. 是否增加“资源控制”（龙/兵线权重）
3. 团战不参战惩罚
