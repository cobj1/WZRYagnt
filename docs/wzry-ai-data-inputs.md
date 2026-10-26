# 对局 AI 的数据面：它读什么、怎么用

> 目标：回答两个问题 —— ① 王者荣耀对局里那套 AI 到底用了哪些数据；② 它怎么用这些数据。
> 方法：对 `libGameCore.so`（82 MB，`.text` 43.7 MB）做全量静态扫描 + 字符串取证。
> 约束：**纯静态只读**。包内有 TP/ACE 反作弊（`libtersafe` / `libTPCore`），不注入、不改客户端。
>
> 姊妹文档：`docs/wzry-ai-reverse.md`（规则链、锁敌、实时内存 Route A/B/C 的交接说明）。
> 落地复核：§11（2026-10-20 实现侧验证与更正：坐标口径、22 槽位可静态化 → 后经 §11.9 更正为活体 cid 分配、法力口径、规则证据分级）；内存取数成果见 §11（续）。

---

## 零、一句话结论

**官方 AI 的数据来源与我们 Route C 直读的是同一份战斗逻辑单元数据**；差别不在"它能看见隐藏数据"，
而在"它把数据加工成什么"：

```
战斗逻辑单元（单位表）
  → 按类别抽成定长特征向量（英雄10个 / 兵线N个 / 建筑24个 / 野怪 / 血包）
  → 归一化（none / one_hot / min_max / min_max_one_hot）
  → 模型前向（tensorflow / caffe）
  → 概率 → top-1「双手动作」= 目标类型 × 技能槽位
  → 141 条 RuleTactics 规则做硬覆盖
  → 操作命令（move_degree / cmd）
```

所以想改进我们的决策层，**不需要再找"官方数据源"**；要做的是照抄它的**特征口径**和**动作空间切分**。

---

## 一、证据与可复现方法

本轮新写的扫描器（`reverse/ai_scan.py`）单遍扫完整个 `.text`，把三类事件抽出来：

| 事件 | 含义 | 数量 |
| --- | --- | --- |
| 字符串引用 | `adrp+add/ldr` 还原出的 `.rodata` 地址 → 串内容 | 58,747 |
| 全局槽位加载 | `ldr xN, [page, #off]`，运行期由 `R_AARCH64_RELATIVE` 填充 | 1,670,437 |
| 间接调用 | `blr xN`，且 `xN` 来自上面的槽位 | 499,825 |

- 扫描指令数：**10,938,471**
- 函数入口数（`bl` 目标启发式）：**19,377**
- 字符串表（`.rodata` / `.data.rel.ro` / `.data`）：31,869 条

> 说明：`libGameCore.so` 的 `.symtab` 已剥离，`.dynsym` 只有 libc 导入符号（2,498 条，全是
> `malloc`/`memcpy` 一类），**没有任何内部函数名**。所以下面所有"函数身份"都靠
> **它自己引用的日志/断言串**反推（这是本仓库一贯的做法，也是唯一可行的做法）。

产物：
`reverse/out/il2cpp/gc.strrefs.tsv`（58,747 行）、`gc.slots.tsv`（167 万行）、`gc.icalls.tsv`（50 万行）。

---

## 二、先分清两套流水线（很容易搞混）

二进制里有**两份功能相同、命名不同的特征抽取器**。搞混会把"陪玩 AI 的数据面"当成"对局 AI 的数据面"。

| 命名空间 | 特征类入口 | 归属 |
| --- | --- | --- |
| `feature::VecSoldier / VecOrgan / VecMonster / VecCake / VecFeatureHero` | `0x26fe594` / `0x2704214` / `0x270ba90` / `0x272e4c8` / `0x2710da8` | **对局 AI**（`ai_tactics::`） |
| `feature_aivoice::Vec*` | `0x26173c8` / `0x261cc0c` / `0x2624600` / `0x262e880` | **AI 语音 / 陪玩**（`ai_tactics_aivoice::`、`game_aivoice::AiProcess`） |

两套的**特征键名完全一致**（同一份 `feature_name_list` 口径），但决策倾向相反：陪玩那套带大量
"别抢玩家的人头"开关（`stop_atk_for_player`、`stop_atk_for_peiwan_and_player`、`coward_4_player`、
`radical_for_peiwan_and_frd`、`GetSSGStopAtkPlayerWhenWeakness` …）。**本文只讨论第一套。**

对应的入口函数（日志串已确认）：

| 函数 | 地址 | 日志串 |
| --- | --- | --- |
| `ai_tactics::GameTacticsManager::FeatureProcess()` | `0x2751af4` | `[FeatureProcess] vec_feature_len:%d` |
| `ai_tactics::GameTacticsMaster::SetTacticsResult()` | `0x2558f0c` | `frame=%d,tactics_prob_size=%d,cmd_idx=%d` |
| `RuleTactics->Process` | `0x27d1614` | 141 条规则的调度器 |
| `AITargetSearcher::GetNearestEnemy` | `0x34e037c` → 真身 `0x34de91c` | 锁敌 |

---

## 三、怎么用：完整流水线

### 3.1 采样节拍 —— 不是每帧都算

特征抽取有独立节拍，配置键：`feature_extract_interval`、`predict_interval`、`predict_freq_for_special_juben`、
`min_predict_interval`、`tactics_predict_interval`，并且**按场景分别设**：

```
predict_interval_return_city / predict_interval_atk_dragon / predict_interval_atk_monster_alone
predict_interval_atk_soldier_alone / predict_interval_stuck_wall / predict_interval_dy
caiwenji_scene_predict_freq / min_organ_scene_predict_freq / dajuguan_predict_interval
```

还有一层**反应延迟**：`delay_frame`、`min_delay_frame_no`（日志 `Skip common feature !frame_no:%d, min_delay_frame_no:%d`）。
→ 官方 AI 在"开团/回城/打龙"等场景切换时改预测频率，这解释了为什么它的反应看起来"有节奏"。

### 3.2 视野裁剪与数量截断 —— 它并不是全图全知

- `view_distance`：每个特征类的可见半径（`VecSoldier`/`VecOrgan`/`VecMonster`/`VecCake`/`VecHero` 各有一份）。
- `sorted_by_dist_flag` + `units_num_list` + `one_unit_feature_num`：按距离排序后**只取前 N 个单位**，
  超出直接丢弃（日志 `more than units limitation, pass`）。
- 视野外另有一条规则：`outside_human_vision_predict_interval`、`invisible_to_visible_delay`、
  `invisible_to_visible_delta`、`invisible_visible_rule_case1/2`，
  对应 `ai_tactics::GameTacticsMaster::checkEnemyInvisibletoVisible()` 与
  `game_aivoice::AiProcess::ModifyEnemyInvisibleHeroPosition`（"进草消失 / 出草可见"的平滑处理）。
- 难度越高，`god_vision`（上帝视野）时长越长；陪玩模式下强制走人类视野。

### 3.3 特征拼装 —— 按"单位类别"分表

每一类单位有自己的配置（`vec_soldier_config` / `vec_organ_config` / `vec_monster_config` /
`vec_cake_config` / `vec_hero_config`），配置里给出 `feature_name_list` + `feature_size_list` +
`feature_type` + `normType`。代码只负责**按键名取值**：

```
key:%d start_idx %d,  feature_size:%d     ← 每个键在向量里的起始下标与宽度
local_vec_feature:%d                       ← 拼完后的局部向量长度
```

英雄还分两段：`all_10_hero_feature`（场上 10 个英雄，`ten_hero_vec_size`）与
`main_hero_feature`（自己，`main_hero_vec_size`），`main_hero_first` 控制主英雄是否排在首位。

### 3.4 归一化

`FeatureNorm::Init()`（`0x273b088`）支持的 `normType`：
`none` / `one_hot` / `min_max` / `min_max_one_hot`，另有 `mean_std`、`normType` 非法时打
`unknown_norm_type`。一维配置错位会打 `fail_to_get_correct_size_for_normType[%d]_in_feature[%s]`。

### 3.5 模型

- 框架：串里有 `tensorflow`、`caffe`；模型描述三件套 `model_struct_file` / `model_weight_file` / `model_name`。
- 输入输出：`input_node_info`、`prob_name_list`、`feature_vn_size_list`。
- 序列结构：`seq_inner_step` / `seq_outter_step` / `time_step` / `num_heads` / `action_capacity`
  → 不是单帧 MLP，而是带时间步的序列模型（英雄还有 `location_info_time_series` + `hero_time_series_fea_size`）。
- **动作历史也进输入**：`add_last_n_yaction_feature_size`（把最近 N 步自己的动作拼进特征）。
- 选模型：`frame:%d, config_id: %d, ai_level: %d, match_type: %d, model_id: %s`，
  按 `hero_config_id × ai_level × match_type` 选；找不到就打 `can't find model, model str is %s`。
- 另有 `multi_sess_model_config_file` / `single_model_hero_list` / `small_map_model_id_list` 做多会话与小地图特化。

### 3.6 输出：不是"高层战术"，而是「双手动作」

`SetTacticsResult()`（`0x2558f0c`）反汇编（`reverse/out/il2cpp/ai_settacticsresult.txt`）：

```
0x02559414  ldr  x10, [x20, #0x50]      ; 概率数组
0x02559418  ldr  w10, [x10, x9]         ; prob_idx
0x0255941c  str  w10, [x20, #0x1c8]     ; → label0
0x02559420  ldr  s0,  [x8, x9]          ; 概率值
0x02559428  str  s0,  [x20, #0x1cc]     ; → prob
0x0255942c  ldr  w8,  [x8, x9]          ; → command_idx
...
日志：frame=%d,label0=%d,prob=%.6f,rank=%d,command_idx=%d
```

动作空间按 `label_type` 分成四类（`[SelectTactics] label type is ...`）：

| label 类型 | 含义 |
| --- | --- |
| `grid action` | 网格动作（走位/占点） |
| `hero action` | 英雄级动作 |
| `two hand action` | 双手动作（目标手 + 技能手） |
| `multi task two hand action` | 多任务双手动作（同时管移动与技能） |

双手动作的标签就是字符串表里那一族（`0x4431` 起连续 30 余条）：

```
目标手：AtkHero / AtkSoldier / AtkTower / AtkCrystal / AtkMonster / AtkNorm / NoneAction
技能手：AtkSkill1..4 / AtkHeroSkill1..3 / AtkSoldierSkill1..3 / AtkTowerSkill1..3 / AtkUnitUseSkill
高层：  BackTower / DefensePosition / Retreat / EscapeFunc / TacticsAttackMonster / ReturnCity / AtkSoldierInFrdHigh
```

`[UnpredictProcess] choose target type:%d` / `choose skill slot:%d` / `select tactics:%d` 说明
**预测不可用时会走启发式回退**（`[UnpredictProcess] main hero dead`、`select tactics`）。
这就是"模型缺失/不适用 → 规则兜底"的入口，也是离线对局在没下发模型时仍能跑的原因。

### 3.7 规则覆盖层

模型输出之后，`RuleTactics->Process`（`0x27d1614`）按固定槽位顺序跑 141 条规则（见姊妹文档第八节），
返回值按位判读并写回 ctx 的 `+0x1040 / +0x1044 / +0x1700 / +0x1780`。
规则的作用是**否决/改写**模型给的命令（回城、避塔、卡墙、别抢血包…）。

### 3.8 SSG 脚本层

`ssg::SSGScrMg`（`0x273784c`）按 `ssg_scr_class` / `ssg_script_config` / `ssg_script_taowa_config`
跑另一套脚本化策略，参数如 `taowa_lane_priority`（分路优先级）、`ssg_units_att_config`。
`ssg::SSGHelper::ParseAILevelIndexConf` 负责把 `AILevel` 映射成上面的参数组。

---

## 四、用的哪些数据（按类别，键名为二进制原文）

> 下列键名都是 `.rodata` 里的字面量，语义由命名本身给出；它们构成 `feature_name_list` 的**全量词表**。
> 配置里选哪些、以什么顺序拼，取决于 `AILab/ai_config/5v5/tactics/feature/*.txt`（**不随包**，见姊妹文档 2.5），
> 所以"官方实际开了哪几维"无法静态确定，但"它能用哪些维"是确定的。

### 4.1 英雄（场上 10 个 + 自己）

| 键 | 语义 |
| --- | --- |
| `dist_from_each_hero` | 到每个英雄的距离（10 维） |
| `under_atk_by_each_hero` | 是否正被每个英雄攻击 |
| `whether_atk_each_main_camp_hero` | 是否在攻击我方每个英雄 |
| `relative_location_x` / `relative_location_z` | 相对主英雄坐标 |
| `x_diff` / `z_diff` | 坐标差 |
| `buff_marks` | buff 标记（`hero_buff_marks` 配置了关注的 buff id 与层数区间） |
| `is_in_hero_atk_range` | 是否在对方攻击距离内 |
| `is_kill_by_hero_norm_atk` | 是否会被普攻补掉（斩杀线判断） |
| `atk_range` / `phy_def` | 攻击距离 / 物防 |
| `is_atk_main_hero` | 是否在攻击主英雄 |
| `past_1s_attack_soldier` / `_hprate` / `_lowhp` | 过去 1 秒是否在补兵、所补兵的血量比例、是否残血兵 |
| `has_spec_buffs` | 是否带特定 buff |
| `location_info_time_series` | 位置时间序列（喂序列模型） |

配套配置：`hero_main_job_config`（分路/职业）、`hero_ep_type_list` + `ep_max_value_dict`（蓝条类型与上限）、
`hero_skill_stage_buff_configids`、`use_4_skill_feature`、`equip_config_id_file`、`all_hero_config_id_list`。

### 4.2 兵线（`feature::VecSoldier`）

单兵特征：`atk`、`hp_rate`、`max_hp`、`location_x`、`location_z`、`kill_income`、`soldier_type`、
`in_main_hero_sight`、`belong_to_main_camp`、`soldier_sin`、`soldier_cos`（朝向正余弦）、
`is_soldier_alive`、`unit_buff_num`、`soldier_buff_marks`。

聚合口径：`unit_type_list`（兵种）、`config_id_list` / `config_id_new_list`（普通/进化兵）、
`units_num_list`（每类保留几个）、`camp1_id_list` / `camp2_id_list`（双方对称校验，
`camp1_id_list_size != camp2_id_list_size` 会告警）、`evolution_units_list1..13`、
`config_id_wxld_list`（无限火力）、`super_soldier_list`（超级兵）、`soldier_num`。

### 4.3 建筑（`feature::VecOrgan`）

单建筑特征（30 维）：

```
location_x / location_z / hp_rate / max_hp / kill_income / type / atk / attack_range
is_alive / belong_to_main_camp / in_main_hero_view / cake_need_scds
conti_attack / attack_target                      ← 它正在打谁、连击计数
whether_under_each_hero_atk / whether_organ_atk_each_hero
whether_under_frd_hero_atk / whether_under_emy_hero_atk
distance_from_each_hero / relative_location_x / relative_location_z
organ_invincible_state(+_time) / organ_incompetent_state(+_time)   ← 无敌/失效状态与剩余时间
nearby_emy_soldier_num / organ_emy_soldier_cnt / organ_emy_soldier_min_dist
is_emy_soldier_around_organ / is_organ_attack_emy_soldier / is_organ_attack_main_hero
is_organ_now_outside
```

**22 个具名建筑槽位**（顺序固定，等于一张地图拓扑表）：

```
friend/enemy_crystal
friend/enemy_spring_tower
friend/enemy_{mid,top,bottom}_high_tower
friend/enemy_{mid,top,bottom}_norm_tower_1st
friend/enemy_{mid,top,bottom}_norm_tower_2nd
```

→ 官方 AI 不是"遍历所有塔再判断"，而是**按固定语义索引取塔**。这一点比我们按颜色/位置识别塔稳得多。

### 4.4 野怪（`feature::VecMonster`）

基线与兵线同构（`config_id_list`、`units_num_list`、`evolution_units_list1..13`、`camp1/2_id_list`），
外加 `monster_buff_marks`、`GetMonsterHpRate`（`VecMonster::GetMonsterHpRate` 有独立告警
`PB WARNING:moster has negative max_hp :%d`）。

### 4.5 血包（`feature::VecCake`）

`cake_type`、可见性（`[Fea-1]/[Fea-2] ... is_visiable %d`）、`dist_2_ckae_map`（到血包的距离映射）、
`cake_need_scds`。规则侧有 `LeaveCake` / `SelectCake` / `UseMultiPersonCake` / `FrdFzDonotTakeBloodPack`。

### 4.6 自身状态（`game_aivoice::AiProcess::PrintHeroInfo` 打出来的字段就是完整清单）

```
HeroBaseInfo  runtime_id, config_id, hp, ep, atk_range, pos
SkillInfo     id, config_id, usable, cd              ← 技能是否可用 + 剩余 CD
BuffSkillState config_id, start_time, times
BuffMarkState config_id, layer, origin_actorId
Abilities     config_id, value
Movement      cur_pos, cmd, move_degree, mov_spd, forward_degree, forward
```

> **这条直接回答了我们的一个卡点**：官方 AI 确实用"技能剩余 CD"，但它取自 AI 侧的
> `SkillInfo` 结构（由战斗逻辑/PB 快照喂入），**不在我们直读的 actor 属性块里**。
> 这和我们"在 actor/attr 里扫不到倒计时字段"的结论一致 —— 不是我们找错地方，是它本来就在另一处。

### 4.7 全局态势（战术打分与规则共用的标量）

日志里明确打印的判据：

```
[CurrentTacticsCheck] tactics_size:%d select_tactics:%d rank:%d
                      grid_tactics_state:%d arrived_strategy_target:%d enemy_hero_min_dis:%d
[FinalSelectTactics]  rank:%d grid_tactics_state:%d arrived_strategy_target:%d enemy_hero_min_dis:%d
[AttackEnemyTower]    hero_tower_dis:%d hero_soldier_dis:%d real_tower_atk_range:%d
                      enemy_hero_min_dis:%d atk_range:%d
organ:  in_organ_attacked_flag:%d organ_conti_attack_val:%d enemy_hero_min_dist:%d
        dist_organ:%d organ_hp_rate:%f hero_hp_rate:%f main_hero.x:%d main_hero.z:%d
ctx:    m_enemy_hero_min_dis:%d m_in_dangerous_place:%d m_ally_crystal_is_atked:%d
```

另外：`GetCampTotalCoin` / `GetCoinNum` / `GetHeroAllMoney`（经济）、`GetTowerInfo`、
`GetUnderTowerHeros`、`GetHeroKDA`、`GetCampScore`、`GetGameStartTime`、`GetCurFrameNum`。

### 4.8 动作历史

`add_last_n_yaction_feature_size`：最近 N 步的**自己**的动作（`cmd` / `move_degree`）作为特征输入，
避免"原地抖动"。这是纯工程细节，但很值得抄。

---

## 五、汇编级判据补充（可直接落到我们规则里）

| 规则 | 用到的数据 | 常量/阈值 |
| --- | --- | --- |
| `AttackEnemyTower` | `hero_tower_dis`、`hero_soldier_dis`、`real_tower_atk_range`、`enemy_hero_min_dis`、`atk_range` | 五个量同时参与，塔的真实攻击距离来自建筑自身而非配置常量 |
| `CheckReturnCity`（`0x27df394`） | `cur_hp/max_hp`、`cur_ep/max_ep`、gank 时段、复活倒计时、复活甲可用 | `70` / `301` / `527`，动作编号 `1.5 / 2.5 / 3` |
| `GotoTargetPosWhenFirstBorn`（`0x27dd40c`） | 最近敌距、是否在泉水、当前动作类型、计时 | `301`（危险半径）、`451` 帧 ≈ 30 s @15 fps |
| 锁敌 `AITargetSearcher::GetNearestEnemy` | 10 个过滤参数（可见性、类型、距离、阵营…） | 见姊妹文档 9.3 |
| `TacticsMultiTaskTwoHandAction::IsHeroWillReduceHP` | 预测"这一步会不会掉血" | 决定是否值得放技能 |

**`301` 在回城规则与出生规则里复用** → 官方共享的"危险半径"常量，我们已经落在
`wzry/decision/official_rules.py` 里。

---

## 六、它被允许看多少：难度参数

每个 `AILevel` 对应一组参数，日志格式已经写死：

```
[ChangeAiLevelDynamic] ... model_id: %d, predict_freq: %d, delay_frame: %d,
                            delay_buy_time: %d, god_vision: %d
```

| 参数 | 作用 |
| --- | --- |
| `model_id` | 用哪份模型（`small_map_model_id_list` 小地图另有专模型） |
| `predict_freq` | 预测频率（越低越迟钝） |
| `delay_frame` | 观测延迟帧数（`min_delay_frame_no` 为下限） |
| `delay_buy_time` | 购买装备的延迟 |
| `god_vision` | 上帝视野持续时长 |

**动态难度（橡皮筋）**也是数据驱动的，用到的正是"我们的数据"：

```
[ChangeAiLevelDynamic] hero_config_id:%d, ai_level:%d, ally_money:%d, enemy_money:%d!
[ChangeAiLevelDynamic] limit ailevel gap, end cur_level_index:%d m_cur_ai_level:%d
GetSSGChangeAILevelDependsOnDistToSpring        ← 离泉水越远，难度/行为变
```

配置来源：`ai_level_index_file`、`ai_level_model_type_file`、
`ai_level_ability_and_economic_gap_file_dynamic`、`ai_level_exchange`（**均不随包**）。

---

## 七、与 Route C 的对账

| 数据项 | 官方 AI 从哪拿 | 我们能否直读 | 结论 |
| --- | --- | --- | --- |
| 单位坐标 | 战斗逻辑单元（`GetActorLogicPos` 族访问器） | ✅ 已打通（actor 表 + 位置槽） | 同源，够用 |
| 血量/血量比 | `GetActorHpRate` / `GetHPPercent` | ✅ 内存优先（`ui.hp` + `hp_src`），视觉血条仅作对照 | 同源（2026-10-21 改为不先走视觉） |
| 蓝量/蓝比 | `GetActorEpRate` / `GetActorEpFinalRate` + `ep_max_value_dict` | ✅ `+0x1C4` / `+0x1C8` 已定位；HUD 蓝条比例兜底 | 绝对值未与面板对账；见 §11.3、§11.9 |
| 经济/金币 | `GetCoinNum` / `GetCampTotalCoin` / `GetHeroAllMoney` | ✅（+0x200 总经济 / +0x15D0 可用金币） | 同源 |
| 等级/经验 | `GetHeroLevel` | ✅ | 同源 |
| 技能可用/CD | AI 侧 `SkillInfo{usable, cd}` | ✅（`+0x19D4`，本地英雄 C# 状态对象） | **不在 attr 组件**，在状态对象里；见 §11.6 |
| 装备 | `GetAllEquipID` / `GetEquipInfo` + `equip_config_id_file` | ✅ 已购买 ID 列表（`+0x1928` count + `+0x192C` 数组） | 语义是"已购含小件"，ID→名称需自建表（§11.7） |
| buff/标记 | `buff_marks` / `hero_buff_marks` / `BuffMarkState` | ❌ | 同上（§10.3） |
| 建筑（塔/水晶）状态 | 22 个具名槽位 + `hp_rate` / 无敌状态 | ✅ 22 具名槽位；活体用 `assign_measured()`（cid 定层级），静态拓扑兜底 | 见 §11.2、§11.9 |
| 兵线 | `VecSoldier` 聚合 | ✅ 视觉有兵线检测 | 内存 cid 未定位 |
| 野怪 | `GetAllMonsters` / `GetMonsterHpRate` | ⚠️ cid 未定位；`16100/16101` 是**红方高地塔**，不是野怪 | 官方也只看"在场"的 |
| 草丛/可见性 | `is_in_grass`、`invisible_to_visible_*` | ⚠️ 部分 | 官方的平滑规则值得抄 |
| 局内时间 | `GetGameStartTime` / `GetCurFrameNum` | ✅ | 同源 |
| 血包 | `VecCake` | ✅ 视觉 | — |

**核心结论**：不存在"官方有、我们完全够不着"的数据类别；真正的差距在
① 技能 CD / 装备 / buff 这三块的**取数位置**（在 AI 侧结构而非 actor 块），
② 特征工程（归一化 + 时间序列 + 动作历史），
③ 动作空间切分（双手动作 vs 我们的单步候选）。

---

## 八、对项目的可执行结论

1. **抄动作空间切分，别抄"高层战术"**。官方模型输出的是 `(目标手, 技能手)` 的联合标签，
   高层战术（回城/守家/打龙）由规则链与 `tactics_list` 概率共同决定。我们现在的
   `optimizer.py` 是"候选枚举 + argmax"，可以改成两层：先选目标类型，再选技能槽。
2. **建 22 槽位建筑表**。这是投入产出比最高的一项：塔的语义索引一旦固定，
   塔下强杀/越塔判断、`real_tower_atk_range` 这类判据都能直接落地。
3. **补三块取数**：技能 CD、装备栏、buff 标记。方向明确 —— 去 AI 侧结构（`SkillInfo`/`BuffMarkState`）
   或从 HUD 视觉读，不要再在 actor 属性块里扫。
4. **特征归一化 + 动作历史**：`min_max` 归一化与 `add_last_n_yaction` 是消除"决策抖动"的低成本手段。
5. **视野裁剪**：`view_distance` + 按距离截断前 N 个单位，能显著降低我们感知层的算力开销。
6. **别碰陪玩那套开关**：`stop_atk_for_player` 系列是为"陪玩不抢人头"设计的，跟对局 AI 无关。

---

## 九、局限与未验证项

1. **行为树结构拿不到**：引擎用 behaviac（`behaviac_release_file`、`XmlStringItem`），
   树资源不随包，我们只有 **733 个 `Get*/Is*/Has*` 数据节点名**（词表），没有树形连接。
2. **特征开关拿不到**：`AILab/ai_config/5v5/tactics/feature/*.txt` 不随包（姊妹文档 2.5 已逐字节确认），
   所以"官方实际启用了哪几维、什么顺序"无法静态确定，只能给全量词表。
3. **虚函数调用无法静态解析**：特征计算大量走 `blr`（虚表），
   本次扫描到 499,825 个间接调用点，但槽位目标依赖运行期 vtable，静态只能到"哪个表 + 偏移"。
4. **"对局 AI 与陪玩 AI 共用同一份特征实现"是强推断**：两份 `Vec*` 实现的特征键名逐字相同，
   但配置不同；本文把 `feature::` 判给对局 AI 的依据是它所在的模块区间与 `ai_tactics::` 同区。
5. 未做动态验证（MuMu 实例未启动）。若要闭环，建议：离线对局中在 `GameTacticsManager` 的
   特征缓冲上打断点式 dump（**只读**，不改逻辑），把 `vec_feature_len` 与配置项对齐。

---

## 十、产物与复现命令

| 脚本 | 作用 |
| --- | --- |
| `reverse/ai_scan.py` | 全量扫描：字符串引用 / 全局槽位 / 间接调用（单遍，约 90 s） |
| `reverse/ai_inputs.py` | 把 AI 区的间接调用解析成"调了哪个实现"，按频次汇总 |
| `reverse/find_node_registry.py` | 尝试定位节点注册表（结论：名字不在 `.data.rel.ro` 重定位里，走静态注册） |
| `reverse/func_dump.py` | 单函数反汇编 + 字符串/调用注解（已有） |

```bash
cd D:/work/aiKings/WZRYagnt/reverse
PY="D:/app/python/python.exe"          # 该解释器有 capstone；venv 里没有

$PY ai_scan.py out/il2cpp/lib/arm64-v8a/libGameCore.so out/il2cpp/gc
$PY ai_inputs.py out/il2cpp/lib/arm64-v8a/libGameCore.so out/il2cpp/gc \
    "0x27d0000-0x27e6000,0x2750000-0x2760000,0x34d0000-0x34f0000"
$PY func_dump.py out/il2cpp/lib/arm64-v8a/libGameCore.so 0x2558f0c 1000 \
    out/il2cpp/ai_settacticsresult.txt
$PY func_dump.py out/il2cpp/lib/arm64-v8a/libGameCore.so 0x2751af4 1200 \
    out/il2cpp/ai_featureprocess.txt
```

关键产物：`out/il2cpp/gc.strrefs.tsv`、`gc.icalls.tsv`、`gc.slots.tsv`、
`ai_settacticsresult.txt`、`ai_featureprocess.txt`。

---

## 十一、落地复核（2026-10-20 晚，本方案的实现侧证据）

> 本节只写"实现时被验证/被推翻"的事实，不引入新的静态逆向结论。
> 对应实现：`wzry/decision/official_ai/`；对应方案：`docs/AI_UPGRADE_ALIGN_OFFICIAL.md`。

### 11.1 坐标口径：z 轴必须反序（可一行复核）

`mem_source.py` 的标定注释给出了两个锚点，用它们就能判定方向：

```
蓝方出生点 (-56379, -53367) ≈ 小地图 (0.02, 0.98)   ← 左下
红方出生点 ( 55757,  57652) ≈ 小地图 (0.98, 0.02)   ← 右上
```

即：世界 x 向右、z 向上；小地图 ny 向下。所以 `ny = (Z_MAX - z) / (Z_MAX - Z_MIN)`。

本轮发现两处实现按 z 正序换算（`bridge.world_to_norm`、`snapshot._screen_to_world`），
它们彼此"自洽"（互为逆），但与视觉侧相反 —— 也就是说**闭环测试查不出来**，
只会在真机上表现为"走位整局镜像"。现已统一到 `geo.py` 一处，并用
`test_official_ai_replica.py` T31–T35 锁死。

### 11.2 22 个具名槽位与活体分配（2026-10-24 更正）

> 早期结论“根本不需要 cid”已被 §11.9 与真机复核推翻：真机层级由 `cid` 决定
> （`organ_slots.CID_TIER` + `assign_measured()`），静态拓扑只作兜底；几何自洽 22/22 不等于世界坐标正确。

原判断是“内存只有 `cid 92/95` 两种，无法区分分路/层级 → 需一次性标定”。
复核后**不能**只靠静态拓扑：固定地图拓扑适合兜底，真机活体分配必须用 `cid` 定层级。

证据与推导：

1. `wzry/vision/mm_rules_v7.py` 的 `BLUE_TOWER_PTS` / `RED_TOWER_PTS` 是 s01–s12 真机帧聚类的固定点位
   （每侧 10 点），本身就是"塔不会移动"这一事实的实测结果；
2. 每侧 = **1 水晶 + 9 塔**（3 分路 × 3 层）。分路顺序用"到本方基地角落的距离"判定：
   `norm_tower_1st > norm_tower_2nd > high_tower`（单调递减，已验证）；
3. 分路归属用方位角：蓝方 `top=左边缘 / bottom=下边缘 / mid=对角线`，红方为其 180° 旋转
   （蓝方 top 与红方 bottom 是同一条物理分路，符合镜像命名）；
4. **泉水塔在标定集里没有独立点**（被基地建筑遮挡），按"水晶再向本方角落内推 35%"给出**近似值**，
   只用于回城/泉水停留这类方向性判断，代码里以 `SPRING_PTS` 单列，不冒充实测；
5. 9 个塔位彼此最小间距 **0.135**（归一化），最近邻落槽无歧义；超过 **0.10** 判为
   "不属于这 22 栋"（例如河道中央的临时建筑），不硬塞槽位。

于是“cid 无法区分槽位”这个问题降级为“静态表兜底 + 活体 `assign_measured()` 精确定位”；真机几何自洽 22/22 不等于世界坐标一致（§11.9 实测偏差约 15k–18k）。
真机复核脚本：`scripts/verify_organ_slots.py --live`（期望 22/22、无 conflict/far）。

### 11.3 法力：官方走 `GetActorEpRate`，我们走 HUD 比例，语义等价

- 官方侧：`GetActorEpRate` / `GetActorEpFinalRate` 给出比例，上限来自 `ep_max_value_dict`；
  也就是说**官方特征是"当前/上限"，不是绝对值**。
- 我们侧：`+0x1A0` 已被证伪（是射程×10，见 `mem_source.py` 注释），actor 属性块里扫不到 cur_ep；
  但 HUD 蓝条比例一直是可读的（`wzry/vision/self_bars.py`、`ui_reader.find_mp_bar`），
  并且 `state_dict["ui"]["mp"]` 早已在链路上。
- 结论：把 `ui.mp` 直接当 `ep_rate` 用，**与官方归一化口径一致**；误差只来自 HUD 读条精度，
  不是语义差异。内存直读仍待一次差分（升级文档 §10.1），属于"减少一次视觉依赖"的优化，不是功能缺口。

### 11.4 规则链：141 条里"能落地"的 17 条与证据分级

| 证据等级 | 条数 | 规则 |
| --- | --- | --- |
| `disasm`（地址 + 常量/分支可核） | 4 | CheckReturnCity 0x027df394、KeepAwayFromEnemyTower 0x027dbd98、IsInDangerousPlace 0x027db2ac、GotoTargetPosWhenFirstBorn 0x027dd40c |
| `name`（仅规则名或仅日志串地址，常量是工程值） | 13 | AvoidKilledByOrgan、SituationAssessmentDangerous、AtkSoldierInFrdHigh、ProcessLingerInSpring、SelectCake、LeaveCake、UseMultiPersonCake、StayWithMonster、IsStuckWall、LongTimeMoveStopRule、LongTimeOnlyAtk、NotargetOptimizeProcess、FrdFzDonotTakeBloodPack（日志串 `[ProcessRule] FrdFzDonotTakeBloodPack` @0x027d9030，阈值未还原） |
| 未实现（数据源缺失） | 1 | WaitResurrectionArmor（需装备栏/复活甲） |

两条实现时才发现、但会影响设计的事实：

1. **顺序短路的副作用**：`CheckReturnCity` 的血量门限是 0.60（占位值，官方真值未知），
   它排在链首，会把同帧所有低血规则吃掉。因此名字级"风险判断"类规则的阈值必须放在
   0.60~0.70 这个"还能打但不健康"的窗口，否则形同摆设。
2. **规则名不等于可实现**：`AvoidNezhaSkill3` / `AvoidZhenjiBingZhu` 一类是"针对特定英雄技能"，
   需要技能级数据；`UseChuansongSkill` 系列需要传送点拓扑。它们保持未实现比硬编更安全。

### 11.5 场景化预测频率：键名是证据，数值不是

官方配置里每个场景一个键（§3.1）。我们把键名映射成我方 `SCENE_INTERVAL`，
**数值全部是工程标定**（写在 `config.py` 注释里，不做官方等价声明）：

| 官方配置键 | 我方场景键 | 间隔（秒） | 判据 |
| --- | --- | --- | --- |
| `predict_interval_return_city` | `return_city` | 0.25 | hp < 0.30 |
| `predict_interval_dy` | `dy` | 0.10 | 最近敌英雄 < 900 |
| `predict_interval_atk_dragon` | `atk_dragon` | 0.20 | 预留（需龙坑数据） |
| `predict_interval_atk_monster_alone` | `atk_monster` | 0.18 | 最近野怪 < 900 或目标为野怪 |
| `predict_interval_atk_soldier_alone` | `atk_soldier` | 0.18 | 最近兵线在射程内 |
| `predict_interval_stuck_wall` | `stuck_wall` | 0.33 | 连续 8 帧无位移 |

### 11.6 需要更正的一条旧表述

§4.6 曾写"技能 CD 在 AI 侧 `SkillInfo`，不在 actor 属性块 → 需另找 AI 侧结构或从 HUD 视觉读"。
现在可以更准确地表述为：

> 技能 CD 确实不在 **attr 组件**（`ACTOR_ATTR_OFF`）里，但它在**本地英雄的 C# 状态对象**里
> （`+0x19D4`，3 槽 stride 0x78，`+0x10` 为位域 bit0=可用）。所以"从 HUD 视觉读"只是备选，
> 不是必须路径。这一条与 §4.6 的结论方向一致，但"取数位置"的描述更精确。

### 11.7 装备栏：`+0x1928` + `+0x192C`（已购装备 ID 列表）

原判断是"装备在 equip 组件槽里，未打通"。实际它在**本地英雄记录**（stride `0x2AD8`）的尾部，
形态是一个 vector：`[0, 0, count]` 后接 `count` 个 u32 ID（stride 4）。

实测（同一帧 10 名英雄，`reverse/probe_fields.py`）：

| slot | id | gold_spent | count | 前若干 ID |
| --- | --- | --- | --- | --- |
| 0 | 106 | 17200 | 8 | 1424,1233,1232,1231,1240,1238,1337,1242 |
| 1 | 107 | 28005 | 11 | 1511,1523,1423,1137,1134,11311,1533,1138,1337,1127,1157 |
| 4 | 167 | 27712 | 10 | 1511,1523,1425,1133,1136,1131,1533,1132,1337,1157 |
| 8 | 174 | 23751 | 9 | 1425,1133,1136,1158,1155,11210,1141,1337,1156 |

要点：

- `count` 与 `gold_spent` 正相关，且**每名英雄的 ID 序列不同** → 不是"推荐出装"常量表；
- 1337（鞋类）几乎人人都有，符合"已购装备"的语义；
- 大多数是 4 位 ID，实测也有 5 位（`11311` / `11210`），校验区间取 `[100, 99999]`；
- 该 vector 在"买装备同帧"会短暂移位 → 逐项校验、遇非法值在处截断（`equip_truncated`），
  不整表丢弃也不猜；
- **语义是"已购买（含合成小件）"，不等于 6 个成品槽**。`equip_config_id_file` 不随包，
  ID→名称要自建（常用 30~50 件即可）。

仍未做：一次"买装后新 ID 落在下一个空位"的确认（结构+相关性已经有了，缺最后一步反证）。

---

## 十一（续）、内存取数成果清单（偏移表，2026-10-21 00:40）

英雄 C# 状态数组（stride `0x2AD8`，10 槽，用 `memscan idbag` 定位）：

| 字段 | 偏移 | 说明 | 状态 |
| --- | --- | --- | --- |
| `id` / `level` / `exp` | `0x16C` / `0x19C` / `0x1BC` | 英雄配置 ID / 等级 / 经验 | ✅ |
| `hp` / `hp_max` | `0x1A4` / `0x1A8` | 与 HUD 血条一致 | ✅ |
| `atk_range` | `0x1A0` | **攻击距离 × 10**（需 ÷10） | ✅ |
| `ep` / `ep_max` | `0x1C4` / `0x1C8` | 当前 / 上限法力（施法差分定性） | ✅ |
| `gold_total` / `gold` | `0x200` / `0x15D0` | 总经济 / 可用金币 | ✅ |
| `respawn_s` | `0x15D4` | **复活倒计时（秒，每秒 −1）** | ✅ 本轮新增 |
| 技能槽 1–7 | `0x19D4` + n×`0x78` | `+0x08 id` `+0x0C 等级` `+0x10 状态位域` `+0x14 剩余` `+0x18 总CD` | ✅ |
| ├ slot1–3 | | 英雄技能（id//100 == 英雄 id） | ✅ |
| ├ slot5 | `0x1BB4` | 治疗类（90003，60s） | ✅ 本轮新增 |
| ├ slot6 | `0x1C2C` | 召唤师/装备（80115，120s ≈ 闪现） | ✅ 本轮新增 |
| └ slot7 | `0x1CA4` | 回城（90005，total=0） | ✅ 本轮新增 |
| `equip_ids` | vector 头 `+0x1920` / count `+0x1928` / 数组 `+0x192C` 起 | 装备 ID 列表（stride 4） | ✅ |

时间点：局内时间 `clock_s` 由差分扫描定位（stride `0x28` 连续 ≥6 项同值）。

### 还没拿到的（按性价比排序）

| 目标 | 难度 | 方法 |
| --- | --- | --- |
| **当前锁定目标 / 上次攻击目标** | — | ❌ **已实测放弃**（§12.6）：三条判据全部退化（偏移不稳定 / 无 u64 指针 / 伤害对齐 194 噪声），根因是拿不到 runtime_id 当锚点。且官方 AI 的目标手是**模型输出**而非读入状态 —— 这个字段没有消费者 |
| **buff 列表（id + 层数）** | 中 | 官方 `buff_marks`；需红 buff 前后差分（击杀红 buff 可控） |
| **移动状态 / 朝向（move_degree, forward_degree）** | 中 | ⚠️ 本轮已**排除 0x180–0x1AC**（静止采样 3 秒零变化）。需在移动中重新差分 |
| **兵线批次 / 刷怪倒计时** | 中 | 官方 soldier wave；按 30s 节拍出现的字段 |
| **塔无敌 / 失效状态** | 中 | `organ_invincible_state_time`；推塔瞬间差分 |
| **护盾值** | 低 | `GetActorShieldValue`，护盾生效前后差分 |
| **召唤师技能类型** | — | 80115/90003 → 名称需游戏数据表，**静态拿不到** |

### 11.8 兵/野/塔的坐标：在 `BattleLogicUnit` 侧（0xD0 记录，静态已还原）

之前 §11 里"非英雄单位没有位置组件"是个**结论**，不是解释：`actor+0x230` 那格确实是 0，
但坐标并没有丢，只是不在 actor 上，而在**逻辑单元数组**里。

证据链（全部来自 `.so`，可复核）：

| 线索 | 地址 |
| --- | --- |
| 日志串 `solder configid:%d location:(%d,%d) not visible` | rodata `0x1165430` |
| 引用点（`add x2, x2, #0x430`） | `0x0246c43c` |
| 兵线可见性判定函数（遍历数组） | `0x0246c1bc` |
| 同一函数的 `end` 指针装载 | `0x0246c1e0  ldp x28, x29, [x1, #0xd0]` |
| 步长 | `0x0246c278  add x28, x28, #0xd0` |

记录布局（0xD0 = 208 字节）：`+0x00` id、`+0x08` 阵营(1/2)、`+0x0C` 类型（11=小兵）、
`+0x14/+0x18/+0x1C` = x/y/z（i32 世界坐标）、`+0x68/+0x70` 可见性位图指针/bit 数。

取数方式写在 `wzry/vision/logic_units.py`：扫 `.so` 可写段的 `(begin,end)` 指针对，
用步长整除 + 字段合法性筛，最后**用英雄坐标交叉验证**（英雄坐标另有精确来源，
同 id 偏差 ≤500 且命中 ≥3 才算通过）。开关 `WZRY_LOGIC_COORD=1`，默认关。

对"数据面"的意义：这补上了 §11 表格里"兵/野/塔"唯一缺的一项（坐标），
而且顺带解释了为什么 `organ_slots` 在真机恒空 —— 建筑同样不在 actor 侧。

### 11.9 更正：`1690–1699` 是防御塔，不是小兵（2026-10-21 真机实测）

`mem_battle` 原来把 cid `1690–1699` 当小兵、`16100/16101` 当野怪，是错的。
按"到本方泉水角的距离"严格单调判定：

| 距离 | cid | 含义 | 血量 |
| --- | --- | --- | --- |
| 0 | 44 / 46 | 泉水 | 6000 |
| ~5.3k | 21 / 35 | 水晶 | 10000 |
| ~20k | 92 / 95 | 基地塔 | 9000 |
| ~32k | 1696/97/98（蓝）、1699/16100/16101（红） | 高地塔 | 9000 |
| ~53k | 1691 / 1694 | 二塔 | 8500 |
| ~70–85k | 1690 / 1693 | 一塔 | 7500 |

建筑坐标来自 `actor+0x278`（出生点即实时位置，3 秒采样 0/34 变化），
所以 **22 个槽位现在有真实坐标与血量**，`organ_slots` 从"恒空"变成可用。
真正的小兵 cid 尚未确定（该局未出兵）。

---

## 十二、锁定目标：判据修正与受控实验协议（2026-10-21）

### 12.1 前两轮为什么必然落空 —— 是判据错，不是位置错

前两轮用的判据是「字段值 == 场上英雄 config_id（106/107 这类）」。这条判据站不住：

- `config_id` 是**静态属性**（同一个英雄一场里可能有两个），引擎不会用它标识个体；
- 静态侧有直接反证：`.rodata 0x03aba240` / `0x03adb544` 的日志串
  `[GetLastHitTarget] runtime_id:%d pos:%d,%d,%d ...` —— 引擎记录"上次命中目标"用的是
  **runtime_id**，不是 config_id；
- 同一口径的串还有 `[SetAtkCmd] target id:%d, pos:%d,%d,%d`（`0x044f1464`）、
  `[Process] attack target id:%d pos:%d,%d,%d`（`0x044ef87c`）、
  `[CanHitTarget] main_hero:%d,%d, target_id:%d`。

所以上一轮"在记录/组件里扫不到等于 cid 的字段"**只证明 config_id 口径不对**，
不能推出"目标字段不在记录里"。§11 里"两次探测未找到"的结论据此修正为：
**判据错误，位置仍未知，需重做**。

另外确认了一个已有事实的语义：`attack_target` / `attack_target:%d` 这两个字面量由
`0x0261dfb0`（`feature_aivoice::Vec*`）与 `0x027055f0`（`feature::VecOrgan`）引用，
即官方的 `attack_target` 是**建筑特征**（"这座塔正在打谁"），不是英雄自己的当前目标。
找英雄侧目标不能沿着这两个串走。

### 12.2 新判据：三选一命中 + 唯一回指

不再预设值域，改为把三类候选同时喂给差分结果：

| 候选 | 形态 | 说明 |
| --- | --- | --- |
| A `actor_ptr` | u64 | 等于场上某个 actor 基址 |
| B `actor_id` | u32 | 在**全场 actor 头部 0x100 词表**里出现，且**只回指 1 个 actor**（唯一性检验） |
| C `config_id` | u32 | 兜底 + 反证：若只命中 C，说明又找错了 |

采样面同时覆盖三处，避免"只扫记录"的盲区：

1. 英雄记录 `0x2AD8 × 10`（C# 状态对象）；
2. 英雄 actor 头部 `0x260`；
3. 英雄 actor 的 **15 个组件对象**（`0x110 + k*0x18` 指针 → 每个读头部 `0x100`）。

判据三条同时成立才算定性：

1. 某偏移出现 **2–4 次阶跃**（离散跳变，且每段 ≥2 帧；连续量如 HP/CD 会被排除）；
2. 首尾静态段恒为同值（通常 0）；
3. 阶跃后的值 ∈ A/B 且回指唯一。

### 12.3 协议与命令

```bash
cd D:/work/aiKings/WZRYagnt
# 进训练营/人机，站到能同时看到 2 个以上目标（小兵或敌人）的位置
venv/Scripts/python.exe -X utf8 reverse/probe_target.py --secs 24 --self-cid <本人英雄id>
```

看到 `采样中` 后的时间线（**只做这一件事**）：

| 时间 | 操作 |
| --- | --- |
| 0–6s | 不操作，人物站住（基线，目标应为 0/无） |
| 6–12s | 普攻 A 目标，一直按住 |
| 12–18s | 点头像或点另一个目标，继续普攻（这 6 秒内完成的切换就是关键那一刀） |
| 18–24s | 停止攻击、站住（回到基线） |

产物落在 `temp/probe_target/<name>.npz`，可 `--analyze <name>` 反复重跑，
换 `--max-changes / --min-run` 不需要重新采样。

### 12.4 工具自检

`probe_target.py --selftest` 覆盖三件事，均 PASS：

1. `stepped()` 能正确切出注入的 3 段阶跃，且**排除**连续量（每帧变化）、恒定列、单帧抖动列；
2. `_match()` 候选命中计数正确（只命中 `actor_id` 时返回 1 次 + 标签）；
3. 合成 dump 走完整 `report()`，注入列被排到 `★` 且带出 `actor_id` 来源；
   组件块能把 `c9 +0x0080` 的同款阶跃报出来（证明组件面确实参与判定，不是摆设）。

### 12.5 接入前置（拿到偏移之后才做，不预先写）

命中后按既有纪律办，**不先写常量再验证**：

1. 在 `mem_source.py` 加 `TARGET_HANDLE` 常量 + 校验（值必须能在 actor 表回指，否则判 `None`）；
2. 暴露 `mem_target_handle`，与 `read_hero_states` 同帧读，**不要多一次 adb 往返**；
3. 在 `snapshot` 出 `Unit.target_config_id`（handle → cid 的映射同帧建立）；
4. 只有当"切换目标 → handle 跟随"在**两局**都复现时，才允许进 `features`；
5. 记录一次反向验证：目标死亡/消失的那一帧，字段应回 0（若不回 0，说明它记的是"上次目标"而不是"当前目标"，语义要改名，不能混用）。

### 12.6 实测结果与结论修正（2026-10-21 10:10，真机 3 轮采样）

受控实验做了，**结论要往回收**：这个字段不在当前可达范围，且价值被高估。

**先修掉的两个工程问题**

| 问题 | 现象 | 修法 |
| --- | --- | --- |
| 帧率 0.7 fps | 每帧 dd 148 个区间（10 actor + 135 组件） | `PLAN_GAP=0x8000` 合并区间 + `--slots self` 只采本人 → **7.6 fps**（10×） |
| 本人槽位猜错 | 默认 `--self-cid 106`（slot0） | 实测 slot0 在 341 帧内**位移 0 次、血量恒 3815、金币却在涨** → 那是泉水挂机 AI，不是玩家 |

**slot0 陷阱值得单独记一笔**：`read_hero_states` 按 slot 排序返回，slot0 只是数组第一个，
与"谁是玩家"无关。任何"本人"逻辑都必须显式指定 cid 或从视觉侧取，**不能默认 slot0**
（这与既有约定 `--mem-self-id` 缺省 heroes[0] 是同一个坑，现在有了真机反例）。

**失败的三条判据（都试过，都不成立）**

1. **值 == 某个 actor 的头部 u32**：记录里确实有 `0 → 19310 → 20630 → 0`、`0 → 13520 → 0`
   这类"多位整数 + 归零"的漂亮模式，但（a）偏移在同一英雄的不同采样间不稳定
   （193 在 t1 是 `+0x0848`、t3 是 `+0x06f8`），（b）同一偏移上大量派生值也满足该形状；
2. **u64 指针**：补了 uint64 视图后**零命中** —— 记录里没有指回 actor 的指针；
3. **伤害对齐**（跳变帧 ±2 帧内有人掉血 + 值与受伤者双向唯一）：t3 上命中 **119→194 个**
   字段，加上"攻击距离 < 2500"约束后不降反升 —— 因为过滤掉远处噪声后，更多派生值
   （伤害数值、索引、`0x3FFFFFFF`/`0xFFFFFFFF` 哨兵）变成了"一对一"。**判据本质不成立**。

**根因：拿不到 runtime_id**

`[GetLastHitTarget] runtime_id:%d` 证明引擎有 runtime_id，但在 **actor 头 0x800 内找不到**
满足"全 actor 两两不同且非零"的列 —— 扫到的只有 cid(0x28)/type(0x30)/camp(0x34) 和
0x194 起的一片浮点变换数据（`0x3871E218` 这类 float 位模式碰巧两两不同）。
没有这个参照物，"目标字段"就只能靠"值像不像 id"猜，而记录里这样的值太多。

**更要紧的一条：这个字段的价值被高估了**

回看 §3.6：官方模型的输出是 **(目标手, 技能手)** 的联合标签 —— 目标手是**模型要输出的决策**，
不是它读进来的状态。数据面里对应的是 `under_atk_by_each_hero`（**谁在打我**，§4.1），
这是反向特征，官方同样没有"我的当前锁定目标"这一维。我们自己的 `optimizer.py` 也是
"枚举候选 + 打分"，同样不需要读目标。

所以继续抠这个字段，是在补一个**没人需要**的输入。建议停在这里，把同样的探针
（已具备 7.6 fps、三视图、伤害对齐模式）转向下面两项：

| 候选 | 为什么值 | 复用成本 |
| --- | --- | --- |
| **谁在打我（`under_atk_by_each_hero`）** | 官方特征，且我们完全空白；掉血帧 + 距离已经能直接算 | 复用 `--align`，方向反过来（我的 hp 掉时谁在身边） |
| **移动朝向 `forward_degree` / `move_degree`** | `IsStuckWall` 卡墙规则在用；帧率够了才可能测（0.7 fps 时必然测不出） | 复用同一采样，静止段已排除 0x180–0x1AC |

**方法论层面的教训**（比这个字段本身重要）：

- 先确认"这个数据官方到底用不用"，再决定要不要花力气逆；官方用不到的状态，逆出来也没有消费者；
- 判据要能**被证伪**。"值像 id""跳变与掉血同帧"都无法证伪，所以只能得到一堆候选；
  能证伪的判据是"该值必须等于某个已独立确认的标识（runtime_id / actor 指针）"——
  而这次恰恰缺这个锚点，所以三条判据全部退化成猜。

### 12.7 「谁在打我」：先量覆盖率，再决定要不要逆字段（2026-10-21 10:30）

目标换成了 `under_atk_by_each_hero`（官方英雄特征，我们完全空白）。这次不先找字段，
而是先回答一个更便宜的问题：**纯观测推算够不够用？**

**前置：先修掉身份识别（否则一切都是别人的数据）**

新增 `reverse/identify_self.py`：用视觉 `self_bars.self_hp_mp()` 的血/蓝条比例，
去匹配内存 10 个英雄的 `hp_rate / ep_rate`

```
score = |hp_mem - hp_vis| + 0.5 * |ep_mem - ep_vis|     取最小；次优差 < 0.03 判不确定
```

多帧投票；不确定就明说，不硬猜。`--selftest` 三项 PASS（唯一解 / 歧义必须判不确定 /
蓝条辅助能区分同血量的两人）。输出 `SELF_CID=.. SELF_SLOT=..` 供其他脚本取用。

**关键 bug：坐标是有符号 int32**

`probe_target.py` 里把记录中的坐标列按 uint32 直接 `astype(int64)`，负坐标变成
**42 亿**（如 `-56379 → 4294910917`）。后果：所有距离计算全错，`--align` 的
"距离 < 2500"约束完全失效（它把"同一坐标系的负半区"当成"极远"）。
修法：`.view(np.int32).astype(np.int64)`。

这个 bug 的教训是通用的：**C# 记录里凡是有符号语义的列，都不要用 uint32 视图直接算**；
判断方法很简单 —— 城市坐标、血量这类值一旦出现 `4294xxxxxx`，就是没做符号扩展。

**实测覆盖率（t3，341 帧，slot3/cid119 冒充本人试跑）**

| 情形 | 占比 |
| --- | --- |
| 最近敌 < 3000，且唯一（次近远大于它） | 44% |
| 最近敌 > 3000（塔 / 远程英雄 / 野怪 / 技能） | **56%** |

也就是说：**用"掉血 + 距离最近"推算"谁在打我"，只能覆盖近战那一半**。
远距离掉血（塔、野怪、远程平A、技能）在只有 10 个英雄坐标的情况下无法归因。

重跑 `--align`（坐标已修正、距离约束真正生效）仍命中 **192** 个字段 —— 与上一轮结论一致，
伤害对齐判据确实不成立。上一轮写的"加距离约束反而变多"理由要更正为：
**不论约束怎么加，记录里满足"值与受伤者双向唯一"的派生值都太多**，不是距离的问题。

**下一步（两条路，成本差异很大）**

1. **纳入非英雄单位的坐标**：`wzry/vision/logic_units.py`（并线已在做，0xD0 步长逻辑单元数组）
   能给兵/野/塔坐标。把它们加进候选，56% 的远距离掉血才可能归因；
2. **拿真实伤害来源**：需要 `under_atk` 的实现字段，但静态侧
   `under_atk_by_each_hero`（`0x0261a50c`，引用点 `0x0261769c`）反汇编出来是配置容器遍历，
   不是判据 —— 这条路目前无进展。

判断标准：若 (1) 做完后"可唯一归因"能到 85% 以上，就用观测推算，不再逆字段；
否则必须去找伤害事件本身。

### 12.8 身份识别改回「内存优先」（口径纠正，2026-10-21 10:40）

§12.7 里用视觉血条标定本人，违背了本项目的取数纪律（**内存优先，视觉只兜底**）。
纠正后的做法：**主判据在内存，视觉只回答"内存这个候选对不对"**。

**判据（记录头部布尔的"唯一固定角色"）**

```
每帧 10 个槽里恰好 1 个非零  +  值恒为 1  +  跨采样稳定指向同一槽
```

实测（t1 27 帧 / t2 71 帧 / t3 341 帧，同一局、跨约 3 分钟）：

| 偏移 | t1 | t2 | t3 | 判定 |
| --- | --- | --- | --- | --- |
| `+0x0088` / `+0x008c`（u8） | cid118 | cid118 | cid118 | **稳定**，值恒 1 |
| `+0x001a`（u8） | — | — | cid193 | 只在一次采样出现 → **排除**（多半是某个状态位） |
| `+0x004c`（u32） | cid106 | cid106 | cid106 | 值 66，是枚举/字符而非布尔 → 不作身份用 |

`0x0088` 位于记录头部（C# 对象头之后的第一段字段区），符合"对象级布尔标志"的位置特征。

**但它还不能直接写死成 `is_local`**

语义有两种可能：本地玩家 / 相机跟随（观战、托管、队长）。因此 `identify_self.py` 的规则是：

- **内存候选**先出（`--mem` 可用，不需要进对局）；
- **视觉验证**（血条比例匹配）只作为一致性检查，一致才输出 `source=mem+vision 一致 ✓`，
  不一致直接返回码 3 并提示"`0x0088` 可能不是本地玩家标记"；
- 视觉不可用（镜头里没自己/已阵亡）时，输出标 `source=mem（未经视觉验证）`，不冒充实锤。

**下一步的裁决实验**（一局就够）：进对局后跑 `identify_self.py`，
若内存候选与视觉投票一致，`0x0088` 定性为 `is_local`；不一致则说明它是相机/队长标记，
改去 actor 上找（`ACTOR_HEAD` 区，需 `--slots all` 采一轮）。

### 12.9 更正：slot0 就是本人，上一轮的「挂机 AI」是误判

**结论**：`read_hero_states` 的 slot0 **确实是本人**（至少这两局都是 cid106）。§12.6 里
"slot0 是泉水挂机 AI"的判断是错的 —— 当时用户人在局内但**没有操作**，我把"341 帧位移为 0"
误读成"这是个 AI"。**"不动"不能作为"非本人"的判据**，挂机、看装备、等人也会不动。

**决定性证据（用 HUD 数字对照，不是血条比例）**

截图读到：英雄「恋之微风」（小乔皮肤）、等级 **3**、右上角金币 **968**。

| slot | cid | 等级 | 可用金币 |
| --- | --- | --- | --- |
| **0** | **106** | **3** | **1057** |
| 1..9 | 193/144/142/163/168/118/112/155/128 | 4–5 | 33–296 |

两条独立吻合：

1. **等级唯一定位**：全场只有 slot0 是 3 级，其余都是 4–5 级 → 粗筛一次命中；
2. **金币时间序列**：读内存 1057 vs HUD 968，差 89，按每秒 ~5–10 金币自然增长 ≈ 10–18 秒，
   与"保存截图 → 读内存"的实际间隔吻合 → 精筛通过。

**方法结论：身份判定用「等级唯一 + 金币时间序列对照」**

- 等级在 HUD 血条左侧、金币在右上角，都是**纯数字**，OCR 可靠；
- 金币是**单调增长**量，比血条比例稳得多（血条会被护盾/治疗/皮肤配色干扰）；
- `self_hp_mp()` 在本环境（1920×1080）连续 4 帧都检不出自己血条，
  说明血条比例这条路至少在当前分辨率/皮肤下**不可用**，不要再作为主判据。

**`0x0088` 二次证伪**

§12.8 里把它当成身份候选，实测两次否定：

1. 换到新局（cid 106/193/144/142/163/168/118/112/155/128）→ 10 个槽**全 0**；
2. **同一局内**几分钟后 → 变成 slot5(cid168)=1。

同一个字段在一局内从"全 0"变成"某人=1"，它只能是**状态位**（相机跟随/最近交互/高亮），
不可能是身份标记。同理，`0x001a`、`0x004c` 也都排除。

**另一个结构性发现（避免以后白扫）**

英雄记录的 `0x0000–0x0090` 段**不是每个槽的对象头**：按 `base + n*0x2AD8` 读出来，
该段只有 slot0 有非零数据，其余 9 个槽全 0 —— 说明 `base` 只对 slot0 恰好落在对象起点，
对其他槽落在上一个对象的尾部。所以**任何"记录头部标志位"扫描都不成立**，
`0x16C` 之后的字段区才是每槽独立可用的。

### 12.10 `identify_self` 重写：三个判据的现状与端到端验证（2026-10-22）

把 §12.8/§12.9 的实测结论落成工具规则 —— 之前的版本仍带着两个**已被证伪**的判据，
等于工具是坏的。现在改成三级，按可信度降序：

| 优先级 | 判据 | 说明 |
| --- | --- | --- |
| 1 | `--self-cid` 显式配置 | **推荐**。生产路径本来就是 `--mem-self-id`，一次性配置，无不确定性 |
| 2 | **HUD 金币对照** `source=hud_gold` | 读右上角金币 ↔ 内存 `gold`。金币在 10 人间差异大（实测 33 ~ 1057）且单调增长，与"截图→读内存"的时间差兼容 |
| 3 | 血条比例 `source=bars` | `match_self()` 保留作 fallback，但**本环境不可用**（1920×1080 下连续 4 帧检不出自己血条） |

**端到端验证**：用实机截图 `temp/selfshot.png` 离线跑 `--shot`，读出 **968**，
与截图上的真实金币完全一致。

这里修掉一个真实缺陷：同一 ROI 会带进噪声 blob，直接拼接读出 `9968`；
按置信度过滤（`MIN_CONF=0.6`，噪声 blob 实测 ~0.41）后得到 `968`。
ROI 用**归一化坐标** `(0.935, 0.039, 0.992, 0.080)` 标定，按 frame 宽高自适应，
1280×720 / 1920×1080 都能用。

**已证伪、代码中已移除**（保留记录，避免以后重试）：

1. `+0x0088 / +0x008c` 当身份标记 —— 新局 10 槽全 0，同局几分钟后变 slot5=1，是状态位；
2. 记录 `0x0000–0x0090` 的"唯一非零布尔" —— 该段只有 slot0 有数据，其余槽落在上一对象尾部；
3. "位移为 0 = 挂机 AI" —— 人挂机/看装备/等人也不动，**不动不是非本人的判据**。

**静态新线索（待验证）：`m_main_hero_obj` 可能就在 ctx 里**

引擎日志 `[ParseTargetLabel] m_main_hero_obj is NULL` 说明官方 AI 的上下文对象里
持有"主英雄"指针。反汇编 `0x0277b230-0x0277b2b0` 看到 ctx（`x19`）的两次 64 位写入：

```
0x0277b290  str x10, [x19, #0x170]     ← 64 位对象指针
0x0277b2a4  str x0,  [x19, #0x158]     ← 64 位对象指针
```

两个字段的值来自 `ldp w3,w4,[sp,#0x18]` 一带的解析结果，紧邻的日志格式串是
`0x11d10c77` / `0x113e04fa`。`0x170` 与 `0x158` 是**主英雄指针的首要候选**。
验证需要设备（比对这两个指针与 actor 表里哪个 actor 相同），已列入下一轮。

回归：official **79/79**、`probe_target --selftest` PASS、`identify_self --selftest` 5 项 PASS。

### 12.11 可能的突破口：逻辑单元记录 `+0x04` 是 runtime_id（待验证）

§12.6 判定"目标字段"失败，根因只有一个：**拿不到 runtime_id 当锚点**。
今天在逻辑单元数组（0xD0 步长）里发现一个此前一直空着的字段。

**论据**

- `logic_units.py` 已确认 `+0x00 = config id`：静态证据
  `0x0246c42c ldr w3,[x28]` 对应日志 `solder configid:%d`；
- `+0x04` 紧邻其后，**解析器从未使用**；
- 引擎日志 `[GetLastHitTarget] runtime_id:%d` 证明设计中存在"每个实例唯一"的号，
  而 config id 必然重复（同一兵种同一波会有多个），所以唯一号只能存在别处 —— `+0x04` 是最自然的位置；
- 已排除的候选：actor 头 `0x260`/`0x800` 内没有"非零 + 两两不同 + 值域 < 10^6"的列
  （扫到的全是浮点变换位模式，如 `0x3871E218`）。

**判据（三条同时满足才成立）**

1. **唯一**：所有记录 `+0x04` 两两不同（config id 会重复，唯一号不会）；
2. **值域**：`0 < v < 10^7`（排除浮点/指针）；
3. **与 id 不同源**：`+0x04` 的唯一度必须显著高于 `+0x00`（否则只是 id 副本）。

**工具**：`reverse/verify_runtime_id.py`

```bash
venv/Scripts/python.exe -X utf8 reverse/verify_runtime_id.py --selftest   # 4 项 PASS
venv/Scripts/python.exe -X utf8 reverse/verify_runtime_id.py              # 需对局
```

自检覆盖正例（id 重复、候选唯一）与三个反例（候选是 id 副本 / 全 0 / 值域过大），
确保不会把"看起来唯一"的列误判成锚点。

**若成立，收益**：目标字段终于可判 —— 记录里的值只要等于某个 `+0x04`，
就是"指向某单位"；同时也让"谁在打我"能归因到**小兵/野怪/塔**，不必只靠 10 个英雄坐标。
**若不成立**：说明 runtime_id 在战斗逻辑单元的更深层（或另一张表），
下一步应沿 `+0x04` 之后的字段继续扫，而不是回头去扫 actor。
