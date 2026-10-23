# 按官方 AI 的做法改造我们的 AI：方案与待办

> 依据：`docs/wzry-ai-data-inputs.md`（官方数据面逆向）+ `docs/wzry-ai-reverse.md`（规则链）。
> 原则：**只借架构与口径，不复刻权重**；每一步都能单独开关、单独回退。
> 现状基线：`wzry/decision/official_ai/`（复刻实现）、`wzry/decision/optimizer.py`（legacy 价值场 argmax）。
> 最后更新：2026-10-21 00:30。§4 待办现状：P0.1–P0.3、P0.5、P1–P4 全部完成；仅剩 **P0.4（buff，需一次局内吃 buff）**、
> **P5.2（10 局胜负率，需 20 局 `--mem` 录制）** 与 §10.6 的规则长尾。三者都已给出可复现步骤。

---

## 一、差距诊断（7 个维度）

| 维度 | 官方做法（有证据） | 我们的起点 | 差距等级 | 本轮后 |
| --- | --- | --- | --- | --- |
| 取数组织 | 按**单位类别**分表：英雄 10 人 / 兵线 / 22 具名建筑 / 野怪 / 血包 | 扁平 `ui.*` + `extra.mem_units` 原始列表 | 🔴 结构不同 | 🟢 已对齐（22 槽位按名索引） |
| 特征 | 每类单位定长向量 + `min_max/one_hot` 归一化 + **动作历史** | 原始量直接进 `evaluate()`，无归一化 | 🔴 缺一层 | 🟢 6 张表 + 归一化；动作历史进打分器 |
| 视野 | `view_distance` 裁剪 + 按距离取前 N（`units_num_list`） | 小地图全量点，无裁剪 | 🟡 效率 | 🟢 兵/野/血包按 `view_distance` 裁剪 + top-N |
| 动作空间 | **双手动作**：目标手（AtkHero/AtkSoldier/AtkTower/AtkCrystal/AtkMonster）× 技能手（Skill1..3） | `gen_candidates()` 手工拼的走位点 + 少量技能 | 🔴 语义不同 | 🟢 标签逐字对齐 |
| 决策 | 模型概率 top-k → 规则集**否决/改写** → `[UnpredictProcess]` 回退 | 价值场 argmax，规则只在候选生成阶段前置过滤 | 🟡 顺序不同 | 🟢 同序（top-1 → 规则 → 回退） |
| 节拍 | `feature_extract_interval` / `predict_interval_*`（**按场景分档**）+ `delay_frame` | 每帧全算 | 🟡 性能/拟人 | 🟢 6 个场景档 + 观测延迟 |
| 技能状态 | `SkillInfo{usable, cd}` 参与决策 | 上一轮刚打通 | 🟢 已对齐 | 🟢 10/10 英雄 3 槽 |

**结论（不变）**：不需要换掉价值函数，缺的是它**上游的特征层**和**下游的动作语义**。
本轮把这层补上后，剩余硬缺口只有三个：装备栏、buff 标记、以及 141 条规则里 124 条尚未实现（其中 137 条连反汇编都没做）。

---

## 二、目标架构（L0–L3）

```
L0 取数   内存优先（MemWorker 后台 2Hz） → 视觉兜底（只在内存缺失时启用）
             │  state_dict: ui.* / extra.mem_*
             │  纪律由机制保证：Snapshot.data_src ∈ {mem,mixed,vision,none} + require_mem 严格模式
             │  本轮补齐：ui.mp（法力比例）、22 槽位、技能 CD
L1 特征   UnitFeatures（与官方同构的 6 张表 + 归一化 + 动作历史）
             │  FeaturePack（1381 维：self16 global9 hero170 soldier168 organ936 monster64 cake18）
             │  本轮补齐：organ 走槽位序、view_distance 裁剪、ep/ep_rate
L2 决策   ① 双手动作枚举（目标手 × 技能手）+ 网格动作（走位，8 向 + 价值场最优点）
           ② 打分器（LinearScorer，接口不变；将来可换模型）
           ③ 规则覆盖层（VETO / REWRITE / PASS，顺序短路，17 条）
             │  Action（官方风格标签 AtkHeroSkill3 / GridBest / ReturnCity …）
L3 执行   现有 ADB/HID 层（不变）；bridge 转 map_move / skill / attack
```

关键取舍：**官方是"模型出概率"，我们保持"打分器出分数"**。
`policy.Scorer` 是唯一替换点，将来拿到可用模型只换一个类。

---

## 三、分阶段方案（P0–P5）与完成度

### P0 数据面补全

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 技能 CD（10 英雄） | ✅ | `+0x19D4` 三槽 stride 0x78；`+0x10` 是位域（bit0=可用） |
| 技能等级 | ✅ | `+0x0C` |
| 当前法力 `ep` | ✅ | 内存 `+0x1C4`(cur) / `+0x1C8`(max)（施法差分定位）；HUD 蓝条比例作兜底（§10.1） |
| 装备栏 | ✅ | `+0x1928` count + `+0x192C` 起 stride 4 的 ID 数组（10 英雄同帧比对，`gold_spent` 正相关）；复活甲判定仍缺 ID→名称表（§10.2） |
| buff 标记 | ⛔ 仅差一次局内事件 | 平坦数组里不存在；差分探针已就绪（`reverse/probe_fields.py`），需"吃红 buff"那一下（§10.3） |
| 建筑 22 槽位 | ✅（静态拓扑） | 用固定地图拓扑 + 位置最近邻落槽，**不依赖 cid**（§7.2） |

### P1 特征层 ✅

`features.build()` 输出 6 类定长向量 + 归一化（none/one_hot/min_max/min_max_one_hot）。
新增/修正：

- organ 段改**槽位序**（第 k 段永远是同一个语义建筑），不再按距离排序；
- hero 段含自己（对齐 `all_10_hero_feature` 的 10 人语义）；
- 兵/野/血包按 `snap.view_distance` 裁剪；
- `ep/ep_rate` 接法力比例；`nearby_emy_soldier_num`/`organ_emy_soldier_cnt` 实算（原来恒 0）；
- 动作历史 `snap.last_actions` 进打分器（`repeat_penalty`），**不伪造特征维**——官方那段是 `add_last_n_yaction_feature_size`，我们拿不到 N，硬编维数会污染口径。

### P2 动作空间 ✅

| 官方 label type | 对应实现 | 示例 |
| --- | --- | --- |
| `grid action` | `enumerate_moves()` 8 向 + `field.best_point()` | `Move0` / `GridBest` |
| `two hand action` | `enumerate_actions()` 目标手 × 技能手 | `AtkHeroSkill1` |
| `hero action` | 高层动作（回城/回防/卡墙脱困） | `ReturnCity` / `DefensePosition` / `StuckBreak` |

前置过滤：射程 `dist <= atk_range×1.15`；技能需 `cd_left_ms==0`；标签唯一（同类目标只留最近）。

### P3 规则覆盖层 ✅（17/141）

- 三态接口 `PASS / VETO / REWRITE`，命中即短路，顺序近似官方调度器槽位序；
- **证据分级**：`evidence="disasm"`（5 条：地址 + 常量都可核）/ `"name"`（12 条：只有规则名或只有日志串地址，常量是工程值）；
- 开关 `--ai-rules {all, disasm}`：默认全跑；做 A/B 或怀疑误伤时只跑有汇编证据的 5 条；
- 命中带 `rule/verdict/reason/evidence/addr`，m2 侧把首条命中拼进 `action.reason` 便于回放对账。

### P4 节拍与延迟 ✅

- `SCENE_INTERVAL`：`return_city 0.25s / dy 0.10s / atk_dragon 0.20s / atk_monster 0.18s / atk_soldier 0.18s / stuck_wall 0.33s`（对齐官方 `predict_interval_*` 键名，**数值是工程标定**）；
- `delay_frames`：观测延迟（用 N 帧前快照）；
- `--ai-predict-interval` / `--ai-delay-frames` 覆盖档位；
- 卡墙检测：连续 N 帧"下了移动指令但位移 < 60 世界单位" → `stuck_frames`。

### P5 评估与灰度 ✅（离线）/ ⛔（真机指标）

- 开关：`--ai-arch legacy|official`（默认 legacy，行为不变）+ `--ai-level` + `--ai-rules`；
- 离线对照台 `scripts/bench_official_ai.py`（§9）；
- 10 局胜负率：⛔ 需设备，见 §10.5。

**离线对照台已跑通**（`scripts/bench_official_ai.py`，输出存 `out/bench_official_ai.md`）：

| 场景 | legacy 候选 | official 候选 | 降幅 | official 决策 | 特征 | 槽位 |
| --- | --- | --- | --- | --- | --- | --- |
| lane_1v1 | 31 | 17 | 45% | 2.07ms | 0.65ms | 22/22 |
| teamfight_2v2 | 32 | 17 | 47% | 2.04ms | 0.53ms | 22/22 |
| outnumbered_low | 33 | 17 | 48% | 2.80ms→ReturnCity(rule) | 0.49ms | 22/22 |
| critical_hp | 29 | 14 | 52% | 3.00ms→ReturnCity(rule) | 0.71ms | 22/22 |
| push_no_enemy | 29 | 14 | 52% | 2.40ms | 0.56ms | 22/22 |
| jungle_full | 28 | 11 | 61% | 2.19ms→GridBest | 0.46ms | 22/22 |

均值：候选 **30.3 → 15.0（-51%）**，单帧 2.41ms（其中特征 0.57ms）。

---

## 四、待办清单

### P0 数据面
- [x] **P0.1** 技能槽容错解析 + 暴露 — 10/10 英雄 3 槽
- [x] **P0.2** 当前法力**内存**定位 — **已定位 `+0x1C4`（当前）/ `+0x1C8`（上限）**，施法差分实测；`mem_source` 已暴露 `ep/ep_max/ep_rate`，见 §10.1
- [x] **P0.3** 装备栏 — **已定位**：`+0x1928`(count) + `+0x192C`(ID, stride 4)；`mem_source` 暴露 `equip_ids`，`snap.self_equip` 可用（§10.2）
- [ ] **P0.4** buff 标记 — 差分探针已备（`reverse/probe_fields.py --event buff`），**只差局内吃一次红 buff**（§10.3）
- [x] **P0.5** 建筑 22 槽位 — 静态拓扑落槽，22/22 命中；真机复核脚本已备（§10.4）

### P1 特征层
- [x] **P1.1** 6 张表 + 归一化（`schema.py` + `features.py`）
- [x] **P1.2** `self` / `heroes` 接 `extra.mem_heroes`（含技能向量、法力）
- [x] **P1.3** `minions` / `monsters` / `cakes` 接 `extra.mem_units` + 视野裁剪
- [x] **P1.4** `organs` 接 22 槽位表（槽位序，不遍历）
- [x] **P1.5** 动作历史环形缓冲（`last_actions`，默认 8 步，进打分器）
- [x] **P1.6** 单测 — T36–T43、T55–T59 覆盖槽位/法力/裁剪/节拍/延迟档位

### P2 动作空间
- [x] **P2.1** 目标手 × 技能手枚举 + 可行性过滤
- [x] **P2.2** `optimizer.gen_candidates()` 保留旧逻辑，official 走新枚举（互不影响）
- [x] **P2.3** 标签唯一化（同目标类型只留最近），无前缀冲突
- [x] **P2.4** 候选数/耗时对比 — 候选均值 **30.3 → 15.0（−51%）**，见 §9

### P3 规则覆盖
- [x] **P3.1** `rule_override` 三态接口（`rules.apply_rules`）
- [x] **P3.2** 迁移 `official_rules` 三个谓词为 VETO/REWRITE 规则
- [x] **P3.3** 新增越塔/血包/出生/回防/卡墙/打野/无目标/泉水共 12 条名字级规则
- [x] **P3.4** 命中日志（rule/verdict/reason/evidence/addr；m2 写入 action.reason）

### P4 节拍
- [x] **P4.1** `--ai-predict-interval` + 场景化 `SCENE_INTERVAL`
- [x] **P4.2** `--ai-delay-frames`
- [x] **P4.3** 计时埋点（`Decision.scene/stuck`、`stats()` 含场景计数，复用 `--timing`）

### P5 评估
- [x] **P5.1** `--ai-arch {legacy,official}` 开关与回退（异常自动退回 legacy）
- [ ] **P5.2** 10 局指标 — 回放工具与 3 局实测已出（§10.5）；**胜负率**需 20 局人机（10 局 × 2 架构），当前只有 3 局带 `--mem` 录制
- [x] **P5.3** 文档与交接更新（本文 + `docs/wzry-ai-data-inputs.md` §11）

---

## 五、明确不做

1. **不复刻模型权重**：`model_weight_file` / `AILab/ai_config/*` 不随包（已逐字节确认）。
2. **不重建行为树**：behaviac 树资源不在包内，只有 733 个节点名词表。
3. **不训练神经网络**：先用"同构特征 + 线性/规则打分"验证收益，再决定是否上模型。
4. **不碰陪玩开关**：`stop_atk_for_player` 系列是"陪玩不抢人头"，与对局 AI 无关。
5. **不为了凑 141 条规则而编造常量**：名字级规则必须显式标 `evidence="name"`，
   且能用 `--ai-rules disasm` 一键关掉；反汇编不出常量的规则保持 `implemented=False`。

---

## 六、风险与对策

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| 内存读取被反作弊关注 | 封号 | 只读、离线对局、不上线上真人对局（既有纪律） |
| 特征层拖慢主循环 | 决策延迟 | 实测特征 0.57ms、整帧决策 2.13ms；节拍点重算 + 缓存 |
| 内存字段跨版本失效 | 数据静默错误 | 保留校验阈值（如 `0<total<=300000`），失败返回 None 而非 0 |
| 双手动作标签改动打断历史标定 | 价值函数失真 | legacy 路径完全不动，official 独立枚举；两套并存开关 |
| 22 槽位标定跨局漂移 | 塔判断错 | 固定拓扑 + 位置最近邻 + 门限 0.10；`verify_organ_slots.py --live` 一局一验 |
| 坐标口径再次分裂 | 走位整局镜像 | 只允许 `geo.py` 一处换算；T31–T35 锁死（含桥接层/视觉兜底同源断言） |
| 名字级规则误伤 | 行为不可控 | 证据分级 + `--ai-rules disasm` + 每条命中可回放 |
| 低血场景被 `CheckReturnCity` 短路 | 其它低血规则失效 | 风险类阈值放在 0.60~0.70 的"还能打"窗口；文档显式记录该交互 |

---

## 七、本轮已落地的改动（2026-10-20 23:5x）

### 7.1 新增文件

| 文件 | 作用 |
| --- | --- |
| `wzry/decision/official_ai/geo.py` | **唯一**世界坐标 ↔ 小地图归一化口径（z 轴反序） |
| `wzry/decision/official_ai/organ_slots.py` | 22 具名槽位：固定拓扑 + 位置最近邻 + 门限 |
| `scripts/bench_official_ai.py` | legacy vs official 离线对照台（候选/耗时/规则命中） |
| `scripts/verify_organ_slots.py` | 槽位静态自检 + 真机校验（`--live`） |

### 7.2 修掉的两个真实缺陷

1. **z 轴反向（两处）**：`bridge.world_to_norm` 与 `snapshot._screen_to_world` 都按 z 正序换算，
   而小地图 ny 是向下（`mem_source.minimap_norm` 才是对的标定）。
   后果：official 架构下发的 `map_move` 与视觉兜底坐标**整体沿对角镜像**——追人变逃、撤退变进攻。
   现在两处都走 `geo`，并用 T31–T35 锁死（蓝方出生点必须落在 (0.02,0.98)）。

2. **organ 特征随帧漂移**：原来 organ 段按"距离排序"填，第 k 段每帧可能是不同建筑，模型学不到东西。
   改成槽位序后，第 1 段永远是 `friend_crystal`（T40 断言）。

### 7.3 补齐的六件事

| 项 | 实现 | 验证 |
| --- | --- | --- |
| 22 槽位 | 11×2 具名表 + `classify()` 最近邻 + `MAX_SLOT_ERR=0.10` 门限 | T36–T39：22/22 命中、0 冲突、河道中央建筑被拒 |
| 法力 ep | `ui.mp` → `Snapshot.self_ep_rate` → `Unit.ep/ep_rate` → `VEC_SELF.ep/ep_rate` | T42/T43 |
| 视野裁剪 | `view_distance` 裁剪兵/野/血包，英雄与建筑不裁 | T10（top-N）+ 档位参数 |
| 动作历史 | `last_actions` + `repeat_penalty`（防原地抖） | T56 不劣化 |
| 场景节拍 | `SCENE_INTERVAL` 6 档 + `scene_of()` | T55：残血走 0.25s 档 |
| 规则扩链 | 5 → 28 条（5 汇编 + 23 名字级，带 `evidence`） | T44–T54、T80–T101 |

### 7.4 回归

```
scripts/test_official_ai_replica.py   59/59 通过   （本轮 +29 条）
scripts/test_new_modules.py          198/198 通过
scripts/test_laya_fusion.py           53/53 通过
```

---

## 八、复刻实现结构（不变部分）

### 8.1 目录

```
wzry/decision/official_ai/
  schema.py      特征键名 / 归一化口径 / 22 个具名建筑槽位
  geo.py         世界坐标 <-> 小地图（唯一口径）        ← 本轮新增
  organ_slots.py 22 槽位拓扑与分配                     ← 本轮新增
  snapshot.py    state_dict -> 按类别分组的单位集合
  features.py    抽样 / 视野裁剪 / 拼定长向量
  actions.py     双手动作：目标手 × 技能手
  field.py       走位价值场（grid action 打分的替身）
  rules.py       规则覆盖层（17 条，证据分级）
  policy.py      主管线：特征 → 打分 → top-k → 规则 → 回退
  config.py      难度参数四元组 + 场景间隔
  bridge.py      决策 -> m2 的 action dict
scripts/
  test_official_ai_replica.py   56 条回归（无需设备）
  run_official_ai_replica.py    --fixture / --live 演示
  bench_official_ai.py          legacy vs official 对照台   ← 本轮新增
  verify_organ_slots.py         槽位校验（静态 / --live）    ← 本轮新增
```

### 8.2 复刻 vs 替换

| 环节 | 官方 | 本实现 | 性质 |
| --- | --- | --- | --- |
| 数据分组 | 5 类 Vec + self + global | 同（键名逐字一致） | ✅ 复刻 |
| 归一化 | none/one_hot/min_max/min_max_one_hot | 同 | ✅ 复刻 |
| 视野裁剪 | view_distance + 按距离取前 N | 同 | ✅ 复刻 |
| 建筑索引 | 22 具名槽位 | 同（位置拓扑落槽） | ✅ 复刻 |
| 动作空间 | 目标手 × 技能手 + grid action | 同 | ✅ 复刻 |
| 决策顺序 | 概率 top-k → 规则否决 → 回退 | 同（MODEL/RULE/FALLBACK/CACHED） | ✅ 复刻 |
| 节拍 | predict_freq / delay_frame / 场景间隔 | 同（`AIParams` + `SCENE_INTERVAL`） | ✅ 复刻 |
| 规则链 | 141 条 | **28 条**（5 汇编 + 23 名字级；60+ 条已定位到函数地址） | ⚠️ 部分 |
| **模型前向** | tensorflow/caffe 权重 | `LinearScorer`（可替换） | ⚠️ **替换** |
| 走位打分 | 模型输出 grid action 概率 | `field.py` 显式价值场 | ✅ 形式等价 |

### 8.3 用法

```bash
# 离线夹具（不需要设备）
venv/Scripts/python.exe -X utf8 scripts/run_official_ai_replica.py --fixture --frames 5

# 真机内存驱动（需要一局对局 + memscan）
venv/Scripts/python.exe -X utf8 scripts/run_official_ai_replica.py --live --frames 6 --level hard

# legacy vs official 对照
venv/Scripts/python.exe -X utf8 scripts/bench_official_ai.py --repeat 20

# 槽位校验
venv/Scripts/python.exe -X utf8 scripts/verify_organ_slots.py --live --serial 192.168.0.103:5555

# 代码里用
from wzry.decision.official_ai import OfficialAI, from_state_dict
ai = OfficialAI(level="hard", rule_mode="all")
dec = ai.decide(from_state_dict(state_dict, self_id=106, atk_range=650))
print(dec.label, dec.prob, dec.scene, [h.name for h in dec.hits])
```

m2 侧开关：

```bash
python scripts/m2_agent_v2.py ... --mem --ai-arch official --ai-level hard \
    --ai-rules all --ai-predict-interval 0 --ai-delay-frames -1
```

---

## 九、量化验证（离线，无需设备）

`scripts/bench_official_ai.py --repeat 20`（6 个典型局面，同一份局面喂两条链路）：

| 场景 | legacy 候选 | official 候选 | 降幅 | legacy solve ms | official decide ms | 特征 ms | official 动作 | 来源 | 场景 | 槽位 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| lane_1v1 | 31 | 17 | 45% | 0.01 | 2.23 | 0.67 | AtkHeroSkill3 | model | dy | 22/22 |
| teamfight_2v2 | 32 | 17 | 47% | 0.01 | 2.06 | 0.55 | AtkHeroSkill3 | model | dy | 22/22 |
| outnumbered_low | 33 | 17 | 48% | 0.00 | 2.11 | 0.67 | ReturnCity | rule | dy | 22/22 |
| critical_hp | 29 | 14 | 52% | 0.01 | 1.87 | 0.49 | ReturnCity | rule | return_city | 22/22 |
| push_no_enemy | 29 | 14 | 52% | 0.00 | 1.77 | 0.52 | AtkSoldierSkill3 | model | atk_soldier | 22/22 |
| jungle_full | 28 | 11 | 61% | 0.00 | 1.82 | 0.51 | GridBest | model | default | 22/22 |

**均值：候选 30.3 → 15.0（−51%）；特征构建 0.57ms；整帧决策 1.98ms。**

> 表内为 `--repeat 20` 的一次代表性运行；同一台机器的逐次波动约 ±0.2ms（见 §9.1 自检记录）。

口径提醒（防止误读）：

- legacy 的 `solve()` 只负责移动/战术候选，技能与攻击动作由上层规则给；official 的 `decide()`
  包含特征构建 + 候选枚举 + 打分 + 规则 + 价值场网格搜索。**两者耗时不可直接对比优劣**，
  可比的是"候选规模"与"是否稳定低于 5ms"。
- 夹具是构造的典型局面，不是真机录包；真机指标（金币/分钟、参团率、胜率）见 §10.5。

### 9.1 交付自检记录（2026-10-21 00:15）

按"交付前自查"重跑了一遍，发现并修掉 4 个问题，其余核验项通过：

| 检查项 | 方法 | 结果 |
| --- | --- | --- |
| 有观测延迟的档位（hard=1 / normal=3 / easy=6）下，规则层是否读到**当前**状态 | spy 包 `rules.apply_rules`，在 `delay_frames=1` 下连跑 4 帧，比对规则侧 `stuck_frames` 与实例计数 | ❌ 读到 N 帧前旧值（`seen=[0,0,1,2]` vs `ai=3`）。已修：抽出 `policy._prepared()` 用当前值覆盖历史快照；并做**变异回退验证**——去掉修复后 T58 立刻 FAIL |
| 法力是否只有一种表示 | 全仓 grep `ep` / `mp` 字段与读取点 | ❌ `Unit` 上同时存在 `ep/ep_rate` 与 `mp/mp_max` 两套且只接了一条。已统一为 `ep/ep_max` + `ep_rate` 属性（与官方 `HeroBaseInfo.ep` 同名），`ui.mp` 作为数据来源 |
| 规则命中是否污染训练标签 | 追 `action["reason"]` 落盘路径（`state_action_recorder` 写数据集） | ❌ 原实现把规则名拼进 `reason`。已改为旁挂字段 `ai_rule` / `ai_scene`，`reason` 保持原样 |
| 规则证据分级是否名副其实 | 逐条核对 `evidence` 与手头实际证据（地址 / 常量 / 日志串） | ❌ `FrdFzDonotTakeBloodPack` 原标 `disasm`，实际只有日志串地址、阈值是工程值。已降 `name` 并补 `addr=0x027d9030`；统计变为 **4 disasm + 13 name** |
| 编译 / 三套回归 | `py_compile` + 3 个 test 脚本 | ✅ 59/59、198/198、53/53 |
| 槽位表跨视角 | 以红方视角（`self_camp=2`）重建 22 栋再分配 | ✅ 22/22，`friend/enemy` 前缀正确翻转（T59） |
| 视野裁剪真实生效 | 1200 vs 5000 两种可见半径，比较特征里的兵数 | ✅ 1 → 2（T57） |
| 文档数字与实测一致 | 重跑 bench 对照文档表 | ✅ 已同步为本次运行值（均值 1.98ms） |
| 阵营字段类型是否一致 | 真机跑 `verify_organ_slots.py --live` | ❌ 内存侧 `camp` 是字符串 `"blue"/"red"`，视觉侧是 `1/2`；混用让 `organ_slots.assign` 静默跳过、`geo_table` 只生成 11 项。已在 `snapshot._camp_id()` 统一成 int（T64 锁死） |
| 槽位"几何"与"活体"是否该分开 | 真机读数：建筑的 `pos` 指针 `0x0`、`attr` 回指校验失败 | ❌ 原设计把两者绑在一起，导致"读不到建筑"就等于"没有槽位"。已拆成 `organ_geo`（静态，恒 22）与 `organ_slots`（活体，可为空）+ `organ_alive`（视觉存活）（T65） |
| 装备栏是否能只靠结构定位 | 扫描 `[0,0,count]+ids` 结构，与 `gold_spent` 做相关性 | ✅ 命中 `+0x1928/+0x192C`，10/10 英雄解析成功（T66/T67） |
| 撤退/回城是否真的位移 | 构造快照跑 `enumerate_moves(retreat=True)` 与 `CheckReturnCity`，比目标点到泉水的距离 | ❌ 两处都返回 `point=(self_x,self_z)` —— 标签叫 Retreat/ReturnCity，实际原地不动（残血站桩）。已改为统一走 `Snapshot.retreat_point()` / 己方泉水坐标（T14b、T15b） |
| 规则在"活体槽位为空"时会不会整体失效 | 真机确认 `organ_slots` 恒空后，逐条看规则的入口守卫 | ❌ `ProcessLingerInSpring` 的 `if not snap.organ_slots: return None` **恒真**，等于这条规则从来没生效过。已改为用 `organ_geo` 兜底（T52b 锁死方框边界） |
| **取数纪律（内存优先）**是否真落到字段上 | 真机跑 `MemWorker.apply` → `from_state_dict`，打印 `data_src` / `hp_src` / `alive_src` | ❌ 两处仍视觉优先：`mem_battle` **从不写 `ui.hp`**（自血量一直走视觉血条）、`organ_alive` 无条件读视觉。已改：`ui.hp` 由内存 `hp/hp_max` 写入并标 `hp_src=mem`；`organ_alive` 受 `require_mem` 门禁且计为 `mixed`（T68） |

未能离线验证的仍是 §10 那 4 项（法力内存偏移 / 装备栏 / buff / 真机 10 局指标）——都需要设备与对局，不做"看起来通过"的假验证。

---

## 十、阻塞项与下一步（都需要设备/样本，已给出复现步骤）

### 10.1 当前法力（内存偏移）—— 已解决（2026-10-21 00:20）

**结论：`+0x1C4` = 当前法力，`+0x1C8` = 法力上限。**

证据（`reverse/probe_ep_cast.py`，一次施法差分，小乔 cid=106）：

```
施法前            0x1C4 = 868   （满，868/868）
放 1 技能后 +0.2s  0x1C4 = 838   （-30，技能消耗）
         +0.5s    838 → 838
         +1.0s    838 → 842
         +2.0s    842 → 850      （约 +3/s，符合回蓝）
同帧技能槽 1 的 cd_left_ms: 0 → 3152 → … → 1172   （施法确实生效，排除误触）
```

判读要点：

- 施法瞬间**只有这一个字段**呈"降后回升"，其余变化都是 CD/金币/位置，可直接排除；
- `+0x1C8` 全程不变 → 是上限；
- **绝对值口径未与面板对账**（小乔面板法力高于 868），所以**决策层只用比值**：

```python
# mem_source._parse_self_state
"ep": i32(EP_CUR), "ep_max": i32(EP_MAX),
"ep_rate": round(ep / ep_max, 4)   # 0<max<=50000 且 0<=cur<=max 才有效，否则 None
```

- `mem_battle` 写入 `extra.mem_ep_rate`，并在 `ui.mp` 缺失时用它兜底（不覆盖视觉）。
- 复刻版特征 `VEC_SELF` 的 `ep_rate` 现在有真实值（此前恒 0）。

**未做**：绝对值与面板数值的换算（若要显示"还剩多少蓝"，需要一次面板截图对账）。

### 10.2 装备栏 —— 已解决（2026-10-21 00:25）

**结论：`+0x1928` 是数量，`+0x192C` 起 stride 4 的 u32 数组是"已购买的装备 ID（含合成小件）"。**

证据（同一帧读 10 名英雄，`reverse/probe_fields.py` + 直接扫描）：

```
slot id    gold_spent  count  ids
 0   106      17200      8     1424,1233,1232,1231,1240,1238,1337,1242
 1   107      28005     11     1511,1523,1423,1137,1134,11311,1533,1138,1337,1127,1157
 4   167      27712     10     1511,1523,1425,1133,1136,1131,1533,1132,1337,1157
 8   174      23751      9     1425,1133,1136,1158,1155,11210,1141,1337,1156
```

判读要点：

- 结构是 vector 头 `[0, 0, count]` 后接 ID，`count` 与 `gold_spent` 正相关（17.2k→8，28k→11）；
- 十名英雄的 ID 序列互不相同，但 1337（鞋类）几乎人人都有 → 不是"推荐出装"常量表；
- 小乔（106）的 `1424,1233,1232,1231,1240,1238,1337,1242` 符合一条出装路径；
- 多数是 4 位 ID，实测也有 5 位（`11311` / `11210`），所以校验放宽到 `[100, 99999]`；
- 该 vector 在"买装备同帧"会短暂移位 → 解析**逐个校验、遇非法值在处截断**（`equip_truncated`），
  不整表丢弃也不猜。

落地：

```python
# mem_source._parse_self_state
n = i32(SELF_EQUIP_COUNT)          # 0x1928
ids = [i32(SELF_EQUIP_IDS + k*4) for k in range(n)]   # 0x192C
# mem_battle: extra["mem_equip"] = ids / ui["equip_count"] = len(ids)
# snapshot:  Unit.equip / Snapshot.self_equip
```

**仍未做**：

1. **一次买装确认**：`--save before` → 买 1 件 → `--diff before`，确认新 ID 落在下一个空位
   （现在只是"结构 + 相关性"证据，还没做这一步）；
2. **ID→名称映射**：`equip_config_id_file` 不随包，需自建 `configs/equip_ids.json`
   （常用 30~50 件即可）；有了它 `WaitResurrectionArmor` 才能启用。

### 10.3 buff 标记 —— 只差一次局内事件

已确认的事实：**buff 不在英雄记录的平坦数组里**。用同样的 `[0,0,count]+ids` 结构扫描
整个 `0x2AD8` 记录，只命中 `+0x1920`（装备），没有第二组 buff 向量；
所以它是**链表/指针结构**（官方 `BuffMarkState{config_id, layer, origin_actorId}`），
需要跟随指针才能读，而且空列表时根本没东西可跟。

因此这一步必须以"事件差分"推进（工具已就绪）：

```bash
# 1) 吃 buff 前存基线
venv/Scripts/python.exe -X utf8 reverse/probe_fields.py --event buff --save base
# 2) 局内去吃红 buff，等 3 秒
# 3) 比对：只报 0↔非0 翻转与小幅整数（层数/剩余时间）
venv/Scripts/python.exe -X utf8 reverse/probe_fields.py --event buff --diff base
```

判据：某字段吃红后变非 0、buff 到期后**精确回到原值**。命中后把偏移写进 `mem_source`
并加 `0<=layer<=10` 之类阈值校验，再决定是否进特征。

### 10.4 22 槽位真机复核 —— 几何已通，活体被建筑坐标卡住（2026-10-21 00:22）

真机实测（`scripts/verify_organ_slots.py --live --serial 127.0.0.1:16384`）：

```
建筑总数=6  camp=1  几何槽位=22/22  活体槽位=0/22  视觉存活=0/22
带坐标的建筑=0/6   ← 建筑 pos 组件为空，活体槽位无法分配
结论: 几何 PASS / 活体 BLOCKED(建筑 pos 组件为空)
```

两个结论：

1. **几何层 22/22 通过**：把"槽位几何"从"活体单位"里拆出来后（`organ_geo`），
   地图拓扑永远可用 —— 规则/价值场不再依赖能不能读到建筑。
   这是本轮的设计修正：**几何是静态地图数据，血量/存活才是观测**。
2. **活体层被新发现卡住**：建筑的 `ACTOR_POS_OFF(0x230)` 处指针是 `0x0`，
   属性组件的回指校验也不通过 —— 即建筑（cid 92/95/21/35/44/46）**不共用小兵/英雄的组件布局**。
   查过 actor 对象 `0x000..0x400` 全范围，没有找到回指自身的组件指针，
   说明静态建筑的坐标/属性在另一套结构里（很可能在 `SGCore::BattleLogicUnit` 侧）。

因此槽位的**存活/血量**当前只能来自视觉（`mm_rules_v7` 的塔点检测，按颜色判活），
`organ_alive` 已接好这条链路（`alive_from_vision`，兼容旧录制里的扁平点列）。
下一步要用活体血量，就得按"建筑是逻辑单元而非渲染 actor"这个方向重新定位组件，
或者干脆只依赖视觉存活 + 静态几何（对规则已经够用）。

#### 10.4.1 上面那个猜测被证实了：`BattleLogicUnit` 侧的 0xD0 记录数组（2026-10-21 09:40）

从日志串 `solder configid:%d location:(%d,%d) not visible`（rodata `0x1165430`，
引用点 `0x0246c43c`）反查到兵线可见性判定函数 `0x0246c1bc`，它遍历的是一个
**0xD0（208）字节步长**的记录数组：

```
0x0246c1e0  ldp  x28, x29, [x1, #0xd0]   ; 容器 +0xD0/+0xD8 = begin/end
0x0246c278  add  x28, x28, #0xd0         ; 步长 0xD0
0x0246c284  ldr  w8, [x28, #0xc]
0x0246c288  cmp  w8, #0xb                ; +0x0C == 11 -> 小兵
0x0246c290  ldr  w8, [x20, #0x24]        ; 容器 +0x24 = 自己阵营
0x0246c294  ldr  w3, [x28, #8]           ; +0x08 = 阵营
0x0246c298  sub  w9, w22, w8             ; 3 - 自己阵营 = 敌方
0x0246c2dc  ldr  x1, [x25, #0x14]!       ; +0x14 起 8 字节（x, y）
0x0246c2e0  ldr  w2, [x25, #8]           ; +0x1C（z）
0x0246c42c  ldr  w3, [x28]               ; "solder configid:%d"
0x0246c430  ldr  w4, [x28, #0x14]        ; "location:(%d,%d)" 的 x
0x0246c434  ldr  w5, [x28, #0x1c]        ; 日志里的 z
```

记录字段（已还原）：

| 偏移 | 含义 | 依据 |
| --- | --- | --- |
| +0x00 | id（config id） | 日志首个 `%d` |
| +0x08 | 阵营 1/2 | 与 `3 - 自己阵营` 比较 |
| +0x0C | 类型，**11 = 小兵** | `cmp w8, #0xb` |
| +0x14 / +0x18 / +0x1C | x / y / z（i32 世界坐标） | 8 字节装载 + `+0x1C`，日志打印 x/z |
| +0x68 / +0x70 | 可见性位图指针 / bit 数 | `ldr x9,[x28,#0x70]` + `tbz` 判可见 |

**取数纪律**：不靠指针链猜。在 `libGameCore` 可写段里找相邻两个 u64 组成的
`(begin, end)`，差值必须能被 0xD0 整除、条数 2..800、抽样记录字段全合法；
命中后再**用英雄的精确坐标做交叉验证**（同 id 偏差 ≤ 500 且命中 ≥ 3 只）——
不通过就放弃，绝不把未验证的数据灌进决策。实现见 `wzry/vision/logic_units.py`，
开关 `WZRY_LOGIC_COORD=1`（默认关，未在真机对局里跑通前不动线上行为）。

一条命令验证（**需在对局中**，大厅里数组为空）：

```bash
python scripts/verify_logic_units.py          # 发现 -> 英雄交叉验证 -> 统计补到多少坐标
# 或手动定位：
python reverse/find_logic_units.py            # 扫 .so 可写段；--scan-heap 256 再扫堆
python reverse/probe_unit_pos.py --watch 3    # 另一条路：直接扫 actor 体内的坐标三元组
```

接上之后，`read_units` 会给缺坐标的兵/野/塔补 x/z 并标 `coord_src="logic"`
（actor 读到的一律不覆盖），official AI 的"打兵/打野/推塔"候选才会真正出现。

#### 10.4.2 对局内实测：`0xD0` 数组没找到，但找到了 `+0x278` 与一处坐标系错位（2026-10-21 10:10）

在真机对局里（`pid 35449`，76 个 actor）把几条路都试了一遍，**结论要先写清楚，避免下一轮重复劳动**。

**1）`actor+0x278` 是"出生/编队点"，不是实时位置**

| 证据 | 结果 |
| --- | --- |
| 34 个单位连续采样 3 秒 | `+0x278` **0/34 变化**（英雄 `pos` 组件同期变了 1094 / 13732） |
| 蓝 1690 `(-11849,-9015)` vs 红 1693 `(11849,9015)` | 完全镜像 → 编队点特征 |
| 本机英雄 106 | `+0x278 == pos 组件`，仅因为他正站在泉水 |

对**建筑/野怪**来说出生点就是当前位置，所以这个字段对它们是可用的真值。

**2）小兵 = `type 2`（cid 1690–1699），`type 1` 是"单位池/原型"**

- cid 1690–1699：`type=2`、camp 1/2、**血量真实**（抓到 7236/7500、6806/7500 的战损值），
  但 `actor+0x230` 的 pos 组件为 `0`，`+0x278` 是编队点 → 活体坐标确实不在 actor 上。
- `type=1` 共 42 个，cid `166xxx` / `266xxx`：**有** pos 组件，但坐标恒等于编队点、
  camp 字段（+0x34）恒为 0、3 秒不动 → 是单位池/原型，不是活着的小兵。
  （旧注释把 `type 1` 写成"子弹/特效"，方向对了但语义不准，已在此更正。）

**3）actor 的指针目标里没有实时坐标**

对一个小兵 actor 取 37 个指针、去重 24 页，连续 3 次采样（间隔 1.5s）：
361 个坐标三元组稳定存在，**发生变化的全是数值噪声**（如 23196→23198），没有单位位移。

**4）堆扫描（三条签名，全部落空）**

| 方法 | 规模 | 结果 |
| --- | --- | --- |
| `.so` 可写段找 `(begin,end)` 且差值 % 0xD0 == 0 | 2.58 MB | 无配对 |
| 全可写堆同一判据（纯算术预筛） | 3.76 GB / 280s | 30659 个算术候选，**0** 通过记录校验 |
| 小段（4KB–4MB）扫"连续 4 条同类型 + 坐标合法" | 87 MB | 只命中 0x10 步长的全局表（误报） |

**判定**：`0xD0` 步长的遍历代码确实在 `.so` 里（§10.4.1 的指令可复核），
但运行期**没有以我假设的形态常驻内存**——要么容器在未覆盖的段里，
要么记录字段偏移与假设不符。**下一轮不要再用"扫指针对"硬找**，改用坐标锚点：

```bash
python reverse/find_by_coord.py -44700 -44700    # 用蓝方建筑真实坐标反查记录基址
```

一次命中即可 `base = pos - 0x14` 反推，再用 `verify_organ_slots.py --anchors` 交叉验证。

**5）附带发现（比坐标更严重）：22 槽位静态表与世界坐标不在同一坐标系**

真机建筑（`+0x278`，静止所以即真值）：

| 单位 | 真实世界坐标 | 22 槽位表给的最近槽位 | 偏差 |
| --- | --- | --- | --- |
| cid 44（泉水） | `(-58900, -58900)` | `friend_spring_tower` = `(-52475, -44194)` | **~17k** |
| cid 21（基地塔） | `(-55200, -55100)` | `friend_crystal` = `(-49122, -37998)` | **~18k** |
| cid 92（高地塔） | `(-44700, -44700)` | `friend_mid_high_tower` = `(-38961, -30593)` | **~15k** |

三座建筑**全部落在地图对角线上**（|x| == |z|），而静态表给出的点偏离对角线数千单位。
也就是说之前"几何 22/22 通过"只是**自洽**（表与 `geo` 用同一套换算），
并不等于与真机世界坐标一致。这会直接影响 `KeepAwayFromEnemyTower`、塔下惩罚、
`retreat_point`/`own_spring_pt` 这些用 `organ_geo` 的路径。

下一轮必须先解决这一条：用 `verify_organ_slots.py --anchors` 一次打印全部锚点，
按真值重标定槽位表（或直接让槽位用 `+0x278` 的真实坐标，静态表只做兜底）。

#### 10.4.3 同一天已解决：建筑坐标走 `+0x278`，槽位用**实测标定**分配（2026-10-21 10:50）

上面两条（槽位表坐标系错位、建筑无坐标）已经修掉，做法如下。

**1）建筑坐标来源：`actor+0x278`（出生点即实时位置）**

`mem_source` 现在会解析这个字段（快/慢两条路径都读），但**只对建筑生效**：

```python
# mem_battle._fill_static_coords：kind == "turret" 且没有 pos 组件时才用
u["x"], u["z"] = spawn[0], spawn[2]; u["coord_src"] = "spawn"
```

移动单位（兵/野/英雄）一律不用这个字段，否则会把编队点当成实时位置。

**2）cid 语义更正（旧映射整个是错的）**

| cid | 真实含义（按"到本方泉水角距离"严格单调判定） | 旧映射 |
| --- | --- | --- |
| 44 / 46 | 泉水（距离 0，hp 6000） | building |
| 21 / 35 | 水晶（~5.3k，hp 10000） | building |
| 92 / 95 | 基地塔（~20k，hp 9000） | turret |
| 1696/97/98、1699/16100/16101 | 高地塔（~32k，hp 9000） | **小兵 / 野怪** |
| 1691 / 1694 | 二塔（~53k，hp 8500） | 小兵 |
| 1690 / 1693 | 一塔（~70–85k，hp 7500） | 小兵 |

也就是说：`1690–1699` 是**防御塔**、`16100/16101` 是红方高地塔，不是小兵/野怪。
真正的小兵 cid 尚未确定（这一局根本没出兵），所以 `CID_MINION` 暂为空。

**3）槽位分配改用 `assign_measured()`：cid 定层级 + z 序定分路**

- 层级由 cid 直接给出（`CID_TIER`），分路由同层三个实例的 **z 升序**给出（下/中/上，两阵营一致）；
- 基地塔（92/95）不在 22 槽位表内，进 `notes` 而不是硬塞；
- 分配后用**实测坐标**覆盖 `organ_geo`（静态表偏差最大 18k）。

实测结果：**24 栋 → 22 槽位命中，2 栋基地塔如实标注**，无冲突。
`MAX_SLOT_ERR` 保持 0.10 —— 曾经想靠放宽门限兼容真机，但那样会把地图中央的建筑
也塞进槽位（回归 T39 就是防这个），所以改为实测标定，门限不动。

回归：`T74`（24→22 + 2 基地塔不硬塞）、`T75`（泉水 44 / 水晶 21 / 一塔 1690 语义）、
`T76`（高地塔分路 1696/97/98 与 1699）已锁死，全量 **79/79**。

**4）仍未解决**：真正的小兵坐标（本局未出兵，无法验证 `type=2` 小兵的实时位置来源）。
下一步等有兵线时用 `find_by_coord.py` 以某只小兵的已知坐标做锚点反查。

### 10.5 10 局指标 —— 回放已能跑，胜负率仍需真打

**能做的（已做）**：`scripts/ab_replay.py` 用**已录制的对局**跑两套决策，出决策层对照表：

```
$ venv/Scripts/python.exe -X utf8 scripts/ab_replay.py --games 10

| 对局 | 帧数 | 动作一致率 | legacy 无动作率 | 危险区占比 | official 技能率 | 规则改写率 | 回退率 | 单帧 ms |
| 20261020_225206_601007 | 38 | 100% | 0% | 0% | 0% | 0% | 0% | 0.05 |
| 20261020_224928_770332 | 57 | 100% | 0% | 0% | 0% | 0% | 0% | 0.03 |
| 20261020_224657_708566 |  7 | 100% | 0% | 0% | 0% | 0% | 0% | 0.23 |
| 合计/均值              | 102 | 100% | 0% | 0% | 0% | 0% | 0% | 0.05 |
```

口径与坑（写进脚本注释，避免下次重踩）：

- 两套决策吃**同一份局面**：把 `extra.mem_heroes` 反投影成视觉点（`_fair_state`），
  否则录制帧里 `dots.red` 常常为空，legacy 会 100% 回退 `none`，对比没有意义；
- legacy 的兜底动作**不能传 `{"type":"none"}`** —— `_solve_core` 的动作白名单含 `"none"`，
  会原样返回，等于不决策（第一次跑就把一致率打成 0%，就是这个坑）；
- 两套动作词表要归一（`map_move`→`move`），否则永远不一致。

**不能做的**：胜负率/经济差。回放改变不了游戏进程，属于反事实问题。
需要 10 局 × 2 架构 = 20 局人机，且录制时要带 `--mem`（当前 45 个 session 里只有 3 个带内存单位）。
在补足录制之前，这一项**不填数字**。

### 10.6 规则长尾 —— 已给出分诊表（59 条待做，不再是"124 条一锅"）

原口径是"141 − 17 = 124 条待做"，但这个数字把**共享函数、重载、桩函数**都算进去了，
不适合当工作清单。用 `reverse/out/il2cpp/rules_table.tsv` 去重、去桩（指令数 < 8 的丢弃）后：

- 表内可识别的具名函数：**67**
- 已实现：**17**（5 汇编 + 12 名字级）
- 待做：**59**，已按**指令数升序**排好（先做短的）：`docs/RULES_TRIAGE.md`

配套工具（本轮新增）：

| 脚本 | 作用 |
| --- | --- |
| `reverse/rule_consts.py <addr>` | 只打"判据相关"指令：`cmp/cmn/subs`、`tbz/tbnz`、`fcmp/fmov`，并把 `.rodata` 里的浮点字面量解析成数值 |
| `reverse/gen_rules_triage.py` | 从 `rules_table.tsv` 生成 `docs/RULES_TRIAGE.md`（按难度排序 + 候选常量） |

每条规则的标准动作（10~20 分钟）：

1. `rule_consts.py <addr>` 取阈值候选；
2. 用 `rules_table.tsv` 的 tags 列（该函数引用的日志串）确认分支语义；
3. 常量写进 `rules.py`，标 `evidence="disasm"` + `addr`，docstring 里贴 3~5 行关键指令；
4. `test_official_ai_replica.py` 加**阈值两侧**各一例（如 T47b：0.52 放行 / 0.48 否决）。

**本轮示范一（已完成）**：`StayWithMonster` 从名字级升级到汇编级，阈值由拍脑袋的 0.45 更正为 **0.50**：

```
0x027e5578  ldr  s0, [x19, #0x1324]   ; max_hp
0x027e557c  fmov s1, #0.5
0x027e5580  fmul s0, s0, s1           ; 0.5 * max_hp
0x027e5584  ldr  s1, [x19, #0x9a4]    ; cur_hp
0x027e5588  fcmp s1, s0
0x027e558c  b.pl 0x27e55fc            ; hp >= 50% -> 放行，否则打 "Ignore, main hero has low hp: %f"
```

**本轮示范二（已完成）**：泉水判定从"半径 900 的圆"更正为**官方方框**（`stay_in_spring @0x027f2a5c`）：

```
0x027f2aec  subs w8, w8, w10     ; dx
0x027f2af0  cneg w8, w8, mi      ; |dx|
0x027f2af4  lsr  w8, w8, #3      ; >>3（8 单位一档）
0x027f2af8  cmp  w8, #0x271      ; 625 -> |dx| <= 5000
0x027f2b14  cmp  w8, #0x270      ; 624 -> |dz| <= 4992
```

顺带发现 `ProcessLingerInSpring` 的守卫 `if not snap.organ_slots` 在真机上**恒真**
（活体槽位一直为空），等于这条规则从未生效 —— 一并修掉，改用 `organ_geo` 兜底。

## 十一、本轮变更文件清单

| 文件 | 变更 |
| --- | --- |
| `wzry/decision/official_ai/geo.py` | 新增：唯一坐标口径 |
| `wzry/decision/official_ai/organ_slots.py` | 新增：22 槽位拓扑与分配 |
| `wzry/decision/official_ai/snapshot.py` | 坐标改走 geo；新增 `ep/ep_max/slot/stuck_frames/last_actions/view_distance/organ_notes/self_ep_rate`；槽位自动分配 |
| `wzry/decision/official_ai/features.py` | organ 槽位序；hero 含自己；view_distance 裁剪；ep/ep_rate；塔下兵数实算 |
| `wzry/decision/official_ai/bridge.py` | `world_to_norm` 改走 geo（修 z 轴反向） |
| `wzry/decision/official_ai/rules.py` | 证据分级 + `mode` + 12 条新规则（17/141） |
| `wzry/decision/official_ai/policy.py` | 场景节拍、卡墙检测、动作历史、`rule_mode`、覆盖参数、stats 扩展 |
| `wzry/decision/official_ai/config.py` | `SCENE_INTERVAL` 六档 |
| `wzry/decision/official_ai/__init__.py` | 导出 geo/organ_slots/field |
| `scripts/m2_agent_v2.py` | `--ai-rules` / `--ai-predict-interval` / `--ai-delay-frames`；命中写进 reason |
| `scripts/run_official_ai_replica.py` | 夹具补 22 栋真实坐标建筑；打印槽位覆盖/法力/场景/证据等级 |
| `scripts/audit_frames.py` | 新增：逐帧不变量审计（I1–I9），用带 `--mem` 的录制跑 |
| `scripts/test_logic_units.py` | 新增：0xD0 记录的离线回归（5 项，含"整体平移应拒绝"） |
| `scripts/verify_logic_units.py` | 新增：对局中一条命令验证数组（发现 → 英雄交叉验证 → 统计） |
| `wzry/vision/logic_units.py` | 新增：0xD0 逻辑单元数组的发现/读取/交叉验证 |
| `wzry/vision/mem_battle.py` | `read_units` 加 `coord_src`；缺坐标时由逻辑单元补（`WZRY_LOGIC_COORD=1`） |
| `reverse/find_logic_units.py` | 新增：扫 .so 可写段定位数组（算术预筛，避免逐指针读内存） |
| `reverse/probe_unit_pos.py` | 新增：另一条路，扫 actor 体内坐标三元组（`--watch` 看谁在变） |
| `reverse/xref_str.py` | 新增：字符串 → 引用点（adrp+add/ldr），本轮的定位主力工具 |
| `scripts/bench_official_ai.py` | 新增：legacy vs official 对照台 |
| `scripts/verify_organ_slots.py` | 新增：槽位校验 |
| `scripts/test_official_ai_replica.py` | 30 → 59 条回归 |


---

## 十二、第二批复刻（2026-10-23 22:20）

### 12.1 新增四个模块

| 模块 | 复刻的官方部件 | 证据 |
| --- | --- | --- |
| `targeting.py` | `AITargetSearcher::GetNearestEnemy` 的**多过滤器检索** | setter 表 `0x508f000`（`+0x250/+0x260` 整型、`+0x2d0` 布尔、`+0x300` 提交）；执行函数 `0x2593820` 把参数序列化成查询缓冲；过滤器语义取自行为树节点名（`GetNearestEnemyWithPriorityWithoutJungleMonster` / `IsTargetCanBeAttacked` / `IsActorFilterVisible` …） |
| `motion.py` | `SetPostOptimze` / `CalMainMoveDegree` / `SceneDeviationDegree` | `ai_tactics::GameTacticsMaster::CalMainMoveDegree`（`dire:%d, cnt:%d`）、`TacticsMultiTaskTwoHandAction::SceneDeviationDegree`；配置项 `max_single_move_degree` / `min_random_deviation_degree` / `max_deviation_degree` |
| `lanes.py` | `ssg::SSGScrMg::InitTaowaLanePriority` | 日志 `Load PriorityConfig [%s] ERROR: wrong lane %s`、`[%s].size = %d (!= 5)`；配置键 `taowa_lane_priority` |
| `tactics.py` | `tactics_config_file` 的**字段 schema** | `[CurrentTacticsCheck]` / `[FinalSelectTactics]` 日志里的 13 个维度名 |

### 12.2 规则链 20 → 22

新增 `IsLingerAroundDragon`（`0x027eafb4`，敌人 301 内不打龙）、
`ReturnCityBetween2TowerInEmyHigh`（`0x027f24b0`，敌方高地深处残血回城）。

### 12.3 接线位置

- `actions.enumerate_actions` 的候选过滤 → 走 `targeting` 的"可攻击 / 不在泉水"判据；
- `policy.decide` 在返回前 → 走 `motion.apply_post`（方向量化 15° + 单步限幅 2500 + 场景偏差）；
- `field` 的分路偏置 → 可用 `lanes.lane_bonus`（尚未默认开启，留作 A/B）。

### 12.4 回归

复刻版 **90/90**（新增 T83–T90）。


---

## 十三、第三批：泉水三条 + 剩余规则分类（2026-10-23 22:30）

### 13.1 新增（22 → 25）

| 规则 | 地址 | 判据（日志原文 / 立即数） |
| --- | --- | --- |
| `GotoTargetPosInSpring` | `0x027dd190` | `enemy hero around, stop` / `in spring, rest`；立即数 150 / 301 / 451 |
| `MoveStopInSpringWhenRevival` | `0x027f2b98` | `In revival delay, elapsed:%d, total:%d`；立即数 548 ≈ 0.55s 起身延迟 |
| `MovePosWhenLongTimeOnlyAtk` | `0x027eb200` | `longtime_only_atk / atk_cnt:%d` —— 健康状态下久攻不动也要换角度 |

`RuleStuckWallV3`（`0x027f8528`）**并入** `IsStuckWall`：两者判定同源（连续 N 帧下移动指令无位移），
分开实现只会重复触发。

### 13.2 剩余 21 条为什么不做（分类）

| 类别 | 规则 | 原因 |
| --- | --- | --- |
| **英雄专属技能**（11） | `UseSunquanSkill3`、`UseWangWeiSkill3`、`UseLubandashiSkill4HookEscape`、`UseV113Xuebao`、`UsingSanguoEquipSkill`、`UseKongkongerTeleport`、`UseMapSpecialSkill`、`AvoidZhenjiBingZhu`、`AttackDayuDing`、`LocalUseV112DaYuChuanSong`、`ReturnCityInHaiYueSkill3Alone` | 需要"某英雄的某技能在什么条件下该放"，必须逐英雄做真机验证；只靠名字推断会误伤 |
| **模式专属**（4） | `Use10v10LaneRule`、`UseZhanjudianRule`、`CarryBombNearFrd`、`RideStateAction` | 10v10 / 占领点 / 炸弹 / 骑乘，标准 5v5 不出现 |
| **地图物件**（3） | `PushHeroOutofPortal`、`AttackHeroInPortal`、`AvoidStuckDoor` | 需要传送门/门的坐标表，内存里没有对应实体 |
| **已在别处覆盖**（3） | `RuleStuckWallV3`、`AvoidDasimingStuckWall`、`AvoidFeiStuckWall` | 都是"卡墙"的特化，已由 `IsStuckWall` + 价值场脱困点覆盖 |

**结论**：能通用复刻的规则基本做完了（25/37 有地址的），剩下的都需要逐英雄/逐模式的真机标定，
属于"再做也只能靠猜"的区间。


### 13.3 第四批（22:40）

| 项 | 内容 | 证据 |
| --- | --- | --- |
| `DangerousPlaceRetreat` | `0x027de1dc`，危险区的**动作**部分（`IsInDangerousPlace` 只判"危不危险"，这条决定"往哪退"） | 日志 `[DangerousPlaceRetreat] DANGEROUS_PLACE` |
| `targeting.in_main_camp` | `IsInMainHeroCamp @0x027e287c`，半场判定 | 日志 `IsInEnemyHeroCamp` / `IsInMainHeroCamp`，立即数 10000 |
| 特征 +2 维 | `summoner_cd` / `heal_cd`（内存 7 槽已可读） | `0x1BB4` / `0x1C2C` |

**`CheckUseZhiliao`（治疗）暂不落地**：规则本身能写（日志 `not zhiliao, skill_config_id:%d`），
但我们的执行层只支持技能槽 1–3 的点击，召唤师技能按钮走的是另一套标定点，
硬接会在执行时抛错。等控制层支持 `summoner` 动作再接。


### 13.4 执行层扩展：召唤师技能（22:55）

之前 `CheckUseZhiliao` 卡在"能判不能执行"，本轮把执行链路打通：

| 层 | 改动 |
| --- | --- |
| `executor_v2.py` | 注册 `skills[5] = PTS["summoner"]`（标定点 `summoner: [852,647]` 已有）；标定缺失时不注册，调用会明确报 `未知技能: 5` 而不是乱点 |
| `operate_commands.py` | 新增 `OperateType.SUMMONER_SKILL = "SummonerSkill"` + `_summoner_skill()` + 别名 `summoner` |
| `bridge.py` | `Action(kind="summoner")` → `{"type": "SummonerSkill", "mode": "tap"}` |
| `rules.py` | `CheckUseZhiliao @0x027e14ec` 落地：残血 0.40 + 治疗就绪 + 15 帧防连发 |

测试 T97–T99；操作层 59 项全通过。

**这一层解锁的不止治疗**：闪现、惩击、净化等所有召唤师技能共用同一个按钮，
后续 `UseChuanSongSkill`、`StopShanxianUseInUncorrectTimeing`（闪现时机）都可以直接接。


### 13.5 闪现时机（23:05）

`StopShanxianUseInUncorrectTimeing @0x02558f0c`，反汇编取到三个立即数：

| 立即数 | 十进制 | 含义 |
| --- | --- | --- |
| `0x168` | 360 | 角度上限 |
| `0x169` | 361 | 越界即放行（日志 `361 pass`） |
| `0x59` | **89** | **方向差阈值**：`diff is too large, stop shanxian` |

已落地两条（可判可执行）：闪现 CD 中 → 否决；1500 内无敌人 → 否决。

**第三条（夹角 > 89° 否决）暂不落地**：它需要"带方向的闪现"（拖拽施法）。
我们的执行层目前对召唤师技能只发 `tap`，拿不到方向。
等执行层支持 `mode="drag"` 的召唤师技能后，这条可以直接接上——阈值 89° 已经在代码里了。


### 13.6 闪现方向差落地 + 坐标空间分离（23:10）

`StopShanxianUseInUncorrectTimeing` 的第三条（方向差 > 89°）已接上。

实现时踩到一个**必须记住的坐标空间问题**：

| 字段 | 坐标系 | 用途 |
| --- | --- | --- |
| `meta["target"]` | **世界坐标** | 只给规则算夹角（与 `main_move_degree` 同系） |
| `meta["target_frame"]` | **帧坐标 1280×720** | 只给执行层 `skill_cast(drag)` |

两者混用会把闪现拖到完全错误的位置。桥接层的处理是：只有帧坐标才透传 `drag`，
只有世界坐标时**退化为 tap** 并在 reason 里标 `no_frame_target` —— 宁可不拖，也不乱拖。

测试 T102–T105（含坐标空间退化用例）。
