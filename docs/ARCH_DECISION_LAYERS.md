# ARCH · 决策层分层重构方案（8 层链路）

> 基线代码：`WZRYagnt` @ 当前工作区 ｜ 体例对齐 `ARCH_MOVE_PUSH.md` / `ARCH_PUSH_PRIORITY.md` / `ARCH_UI_QUERY_LAYER.md`
> 状态：**方案冻结（接口先行，模型后置）**。本文只定义"层与层之间怎么接"，不承诺本期训模型。

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| 目标形态 | 感知 → 场景理解 → 事件记忆 → 局势判断 → 策略决策（战斗/成长/查询）→ 动作生成 → 合法性约束 → 执行 |
| 现状形态 | **单函数直出操作**：`decide()` 一个函数 740 行直接从状态 `return` 鼠标级动作 |
| 已有（不需新建） | 层 1 感知、层 8 执行；**层 2 共享编码器、层 3 GRU 记忆、层 6 多头动作头已有模型原型**（`BCNet` / `BCNetMem`） |
| 真正缺口 | **层 4 局势判断**、**层 5 意图选择**、**统一合法性 mask（层 7）**、**低频分支调度器** |
| 一句话 | 不是"层不够"，是**编码器到动作头之间少了两级语义**，模型只能跳过"我该干什么"直接猜"手往哪点" |

---

## 1. 现状：逐层锚点（读代码得出）

| # | 层 | 现状 | 代码锚点 |
|---|---|---|---|
| 1 | 感知 | ✅ 具备 | `wzry/capture/`、`wzry/vision/`（detector / minimap / ui_reader）、`wzry/uiquery/panel.py` |
| 2 | 场景理解 | 🟡 有融合、无 embedding | `wzry/state/fuser.py:build_state` → `schema.py:GameState`（**结构化 dict，不是向量**）；`policy/model.py:UnitEncoder/GridEncoder` 是共享编码器原型 |
| 3 | 事件记忆 | 🟡 有落盘、未入模型 | `wzry/memory/event_log.py:EventLog`（jsonl + 冷却估算）；`policy/model.py:BCNetMem`（**GRUCell 已实现**）；`m2_agent_v2.py` 的 `cooldowns` dict 就是跨拍状态 |
| 4 | 局势判断 | ❌ 缺 | 现散落为 `decide()` 内 inline 变量：`_engage_ok` / `_kill_confirm` / `_press` / `_recall_clear`（`m2:622-665`）、`push_ok`/`push_target`/`_push_eval`（`m2:298/348/425`） |
| 5 | 策略决策 | ❌ 缺 | 战斗= `decide()` 主体；成长= `upgrade_policy.suggest()` / `item_policy.plan()`（**只出建议、未接主循环**）；查询= `uiquery/panel.py:PanelNavigator` |
| 6 | 动作生成 | 🟡 仅移动 | `decision/optimizer.py:gen_candidates/_grid_best`（4096 网格，只出 `map_move`）；`policy/model.py` 四头 `act/target/move_theta/move_r` |
| 7 | 合法性约束 | 🟡 分散、事后 | `m2:603 ready()` + `m2:1244 update_cooldowns`、`m2:1290 _policy_guard`、`hero_profile.py:78 legal_upgrades`、`item_policy.py:82 purchase_gate`、`optimizer.py:106/369` 白名单 + `optimizer.py:378` 保命过滤 |
| 8 | 执行 | ✅ 具备 | `wzry/action/executor_v2.py`、AdbExecutor / ESP32 HID 桥、面板 tap 复用 |

**插入点（唯一）**：主循环 `m2_agent_v2.py:2748 action = decide(state_dict, cooldowns)`。BC 通道另在 `m2:2847 _policy.decide(...)`。所有分层改造都从这两行切入，**不改上游感知、不改下游执行**。

**已存在的"可插拔层"范式（必须复用）**：本项目已有三处经真机验证的 `off / shadow / on` 接线——
`--laya off|shadow|ensemble`、`--policy off|shadow|bc`、`--push off|shadow|on`。
新层一律按同一范式接入：**off = 逐字节零变化，shadow = 只落盘不改动作流，on = 才接管**。

---

## 2. 目标与非目标

**目标**
- 把"状态 → 动作"之间的黑箱，拆成**两级显式语义**：局势摘要（层4）→ 高层意图（层5）。
- 合法性统一为**一处 mask**，同时服务"候选前置过滤"与"出口最终门"。
- 战斗 / 成长 / 查询三条线**按不同频率调度**，互不干扰。

**非目标（本期不做）**
- 不训层 4 / 层 5 的模型（先用规则实现，接口留出）。
- 不动感知层与执行层。
- 不删除 `decide()` 的任何真机硬规则（回城门禁、幻影敌人过滤、接战门槛）——只做**归位包装**。

---

## 3. 设计

### 3.1 内部张力（必须先解决）

你的设计把 **合法性约束放第 7 层（动作生成之后）**，但原则里写的是 **"合法性必须前置"**。这两条直接冲突，且现状代码已经暴露了后果——`_policy_guard`（`m2:1290`）是"模型先出动作、再被否决"，否决即浪费一次推理，还造成**动作流抖动**。

**解决：把合法性定义为"两个位置、一份规则"**

```
候选前置 mask（喂给层6，缩候选集）   ← 合法性的"输入侧"
           ↓
        动作生成
           ↓
出口最终门（层7，装配/下发前复核）   ← 合法性的"输出侧"
```

同一个 `legal_mask()` 被调用两次，规则只写一遍。这样既满足"前置"，又保留第 7 层作为最后兜底，不违背你的图层划分。

### 3.2 各层接口契约（纯函数，先规则、后可换模型）

```python
# 层2 场景理解：Percept -> SceneVec
scene = build_scene(percept)            # -> {tokens: {...}, vec: np.ndarray, candidates: [...]}

# 层3 事件记忆：SceneVec + 上拍记忆 -> MemVec
mem   = update_memory(scene, mem_prev, events)   # -> {ctx, events, cooldowns}

# 层4 局势判断：SceneVec + MemVec -> Situation（唯一新增的结构化输出）
sit   = judge_situation(scene, mem)     # -> {phase, risk, primary, secondary, opportunity}

# 层5 策略决策：三分支并发，输出统一 Intent（不是动作）
intents = choose_intent(sit, mem, profile)   # -> [Intent, ...]  见 3.3

# 层6 动作生成：仅在该 Intent 的合法候选集内选
acts  = generate_actions(intents, scene, mask)   # -> [Action, ...]

# 层7 合法性约束：出口复核
final, reason = legalize(acts, scene)   # -> Action | 替代动作 | 拒绝原因码

# 层8 执行：不变
```

**层4 输出（局势摘要）—— 只描述"现在是什么情况"**

```json
{"phase": "lane|farm|gank|teamfight|push|defend|retreat|recall|shop|levelup",
 "risk": 0.0,
 "primary": "…", "secondary": "…", "opportunity": "…"}
```

**层5 输出（高层意图）—— 只描述"下一步想干什么"**

```json
{"branch": "combat|growth|info",
 "kind": "support_top|clear_lane|retreat|engage|chase|contest_jungle|buy_item|upgrade_skill|open_panel",
 "target": null, "params": {}, "confidence": 0.0, "ttl": 0.0, "priority": 0}
```

`ttl`（意图生命周期）是实现**原则 1「高层低频」的关键**：意图一旦产生，在 `ttl` 内不重算，层 6 每拍只需在其合法候选集内微调。**现状完全没有这个概念**，每拍全量重算。

### 3.3 三条并行分支与频率

| 分支 | 频率 | 触发 | 现状落点 | 现状问题 |
|---|---|---|---|---|
| 战斗 | 高频（每拍 / 10–15Hz） | 常驻 | `decide()` 主体 + `optimizer` | 无意图层，直出动作 |
| 成长 | 低频（事件触发） | 金币达标 / 获得技能点 / 回泉水 | `upgrade_policy` / `item_policy` | **未接入主循环**，只出建议 |
| 查询 | 按需 | 需要读数 / 复活读秒 | `uiquery/panel.py` | 形态最接近设计 |

**调度器（新增，层 5 与层 6 之间）**：低频分支挂事件触发而非每拍轮询。**硬约束**：必须尊重 `PanelNavigator.busy` 模态锁——`item_policy.py` 注释已明确"商店整屏遮挡 → 对局判断失效"，战斗中开商店会直接打断感知。调度器不得绕过该锁。

### 3.4 三层分工（对应你的"每层只做一件事"）

- 层 4 只出**局势**，不出现动作字段；
- 层 5 只出**意图**，不出现坐标、技能编号；
- 层 6 只出**动作**，不重新判断局势（输入里只有该意图的合法候选集）。

---

## 4. 可观测

每层按 `shadow` 模式落盘一行 jsonl（复用既有 `behavior.jsonl` / laya shadow 记录格式），字段：

| 层 | 落盘字段 |
|---|---|
| 4 | `phase, risk, primary, secondary, opportunity` |
| 5 | `branch, kind, ttl, confidence, source(rule/model)` |
| 6 | `n_candidates, n_masked_out, chosen` |
| 7 | `verdict(pass/reject/alt), reason_code` |

验收口径：**任何一层在 `shadow` 下，`behavior.jsonl` 与 `off` 必须逐字节一致**（这是本项目已有的硬门槛，见 `--push shadow` 的实现）。

---

## 5. 验收（L1 / L2 / L3）

- **L1 离线单测**：每层接口的纯函数测试（无 IO）；层 7 的 mask 规则用例 ≥ 现有 `hero_profile.legal_upgrades` 覆盖面。
- **L2 历史真机帧回放**：用 `data/matches/` 会话回放，比对"分层链路的最终动作"与"原 `decide()` 动作"的一致率；不一致处必须能归因到某一层的 `reason_code`。
- **L3 实机全流程**：至少 1 局完整对局，四层 shadow 全开、动作流与基线一致。

---

## 6. 分阶段（每阶段独立可回滚）

| 阶段 | 内容 | 模式 | 完成判据 |
|---|---|---|---|
| **P0** | 抽出 `judge_situation()` 与 `Intent` 数据结构；`decide()` 改为**消费**局势摘要（先等价包装，行为不变） | shadow | 行为逐字节一致 + L1 通过 |
| **P1** | 统一 `legal_mask()`，收编 5 处分散闸门；层 6 只在该 mask 内枚举 | shadow→on | L2 一致率 ≥ 基线，抖动不增加 |
| **P2** | 事件调度器接入成长/查询分支（尊重模态锁）；`Intent.ttl` 生效 | shadow→on | 成长动作首次真正执行且不污染战斗 |
| **P3** | 层 4 / 层 5 换模型（多头分类器 + 层级策略），复用 `BCNetMem` 共享编码器 | shadow→on | 与规则版对比 L2 一致率 |
| **P4** | 层 6 扩展技能/连招候选（`_TACTICAL_ONLY_TYPES` 解禁，见 `laya-fusion-plan` 二期） | off→shadow | 单独验收，不与 P0–P3 混 |

**P0–P2 是纯重构（无学习成分、无新依赖）**，是收益最大且风险最低的部分；P3 才引入训练依赖。

---

## 7. 风险与不做的事（真话）

| 风险 | 等级 | 说明与对策 |
|---|---|---|
| **延迟预算** | 高 | `PLAN.md §6` 决策预算 ≤40ms，现状 **<2ms**；而已实测单模型（Laya，OpenVINO 核显）p95 已 **59.8ms**。因此**层 1/2/3 必须保持规则+轻量**，只有 4/5/6 可学习；且**禁止同拍串行多个大模型**，需按分支频率错峰 |
| **数据依赖** | 高 | 层 4/5 训练需标签；`laya-fusion-plan §5.1` 已记录**来源 A 阻塞**（`data/matches/` 33 会话 swipe=0）。故 P3 之前必须先用规则版跑起来积累 `shadow` 数据 |
| **内部张力（合法性位置）** | 中 | 见 3.1，已给双位置方案 |
| **丢掉真机经验** | 高 | `decide()` 的硬规则（回城门禁/幻影过滤/接战门槛）是成本最高的资产，**重构只做归位包装，不重写规则逻辑** |
| **层 2/3 职责重叠** | 中 | 冷却推算属层 3（`EventLog` 已做），层 2 不得重复计算；否则两处口径漂移 |
| **模态锁被绕过** | 高 | 调度器必须强制检查 `PanelNavigator.busy`；查询分支与战斗分支的写轴互斥 |

**不做的事**
- 不一次重写 `decide()`；
- 不做"每帧全链路大模型"——延迟与数据都不支持；
- 不在 P0–P2 引入任何新的训练依赖或新模型权重。

---

## 附：与既有文档的关系

| 文档 | 关系 |
|---|---|
| `PLAN.md §4.3` | 本文是其"分层执行（宏观 2-4Hz / 微观 10-15Hz）"的**接口级细化** |
| `ARCH_UI_QUERY_LAYER.md` | 本文层 5-C 查询分支 = 该文档的 `PanelNavigator`，不改其契约 |
| `ARCH_MOVE_PUSH.md` / `ARCH_PUSH_PRIORITY.md` | `push_ok/push_target` 应归入**层 4 局势判断**（它们本就是"现在该推进吗"的判断） |
| `laya-fusion-plan.md` | 层 6 打分器（`LayaScorer`）保持可插拔；其"本期只优化 map_move"边界与本文一致 |
