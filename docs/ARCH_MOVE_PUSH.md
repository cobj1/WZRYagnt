# ARCH · 移动决策层的"推进"方案（Push Policy）

> 版本：草案 v0.1　日期：2026-10-15
>
> **数字快照（2026-10-25 复核）**：当前 `test_new_modules.py` 198/198、`test_laya_fusion.py` 工作树 56/56（HEAD 53）。下文 127/144/154/180/191 均为各阶段历史值；绝对行号会漂移，以函数/常量名为准。
> 范围：`scripts/m2_agent_v2.py` 的**移动决策块**（decide() 内 ~699-773 行）、以及它需要的两处前置修复。
> 定位：这是"占优推进"（接战门槛放宽，已落地）之后的**下一步** —— 让英雄真的**朝敌方塔压线**，
> 而不只是"往发育路方向走"。
> 状态：**P0 + P1 + P2 全部落地**，真机共跑**四局** shadow（只读 90s + 三局 420s `--action`，含一局口径修正复跑）。
> **会推恒为 0** —— 干净口径下定位到真因：**82% 的拍到不了移动块**，推进只在 `target is None` 兜底抢机会
> （见 §8 ⑤）。✅ 副产品已到手：P0-F1 塔撤实机生效（110 拍塔相关决策）、F2/F3 感知补全、
> `behavior.jsonl` 静默失败修复、`push_info` 陈旧 bug 修复。离线回归当前 **198/198**（写作时为 180/180；含 E 系列 T31~T33）。
> 本文行号为 2026-10-15 写作时快照；代码已位移，查找请以函数/常量名为准。
> **方案 E（推进优先级改造）的设计已单独成文 → [ARCH_PUSH_PRIORITY.md](ARCH_PUSH_PRIORITY.md)**：§8 选项 E 的落地设计。

---

## 0. 结论摘要

- 现有"推进"= `dps_lane_push`：**静态点**（发育路中心 +0.10，朝敌方），不看塔、不看兵线、不看优劣势。
- 要做的是把它升级为：**在占优且安全时，朝"最近的那座敌方塔（塔外站位）"或"最前那条我方兵线"压线**。
- 但直接做会踩三个坑，**必须先修**（P0，且各自都有独立价值）：
  1. YOLO 的 turret/minion 类被整类跳过 → 塔/兵线相关逻辑（含"塔下撤"）是**死代码**；
  2. 小地图的**分阵营**数据（塔/兵线/英雄）没注入 `state_dict` → 决策层看不到；
  3. 没有"敌方存活数/视野外敌人"概念 → 推进要防"人不见了在蹲"。
- 落地方式：**只新增一个移动分支**（条件不成立时行为与现在完全一致）+ `off|shadow|on` 三档开关，
  先 shadow 跑一局收数据，再决定开不开。

---

## 1. 现状（读代码结论）

### 1.1 移动决策块（decide() 内）
`if target is None:` 之后选一个 `target`（**小地图归一化坐标**）+ 一个 `reason`，最后由 `map_move`
转成摇杆推杆（`STICK_SPEED`、`MAPMOVE_MIN_MS=350`/`MAPMOVE_MAX_MS=900`、`MAPMOVE_NEAR=0.04`）。
现有 reason 大致分三类：
- 支援/跟队：`support_dps_lane`、`support_dps_lane_red`、`follow_ally_lowhp`、`follow_ally_minimap`；
- 推进：`dps_lane_push`（**静态点**：`发育路中心 +0.10`，蓝方 `_down_c=(0.75,0.72)` 朝右下）；
- 逃生：`disengage_retreat`、`hp_drop_escape`、`escape_turret*`、`avoid_turret`。
另有 `TARGET_HOLD_S` 目标保持（防抖动）✓，以及 `_u_eng_hp`（按小地图点数的优劣势门，0.55/0.60/0.65）。

### 1.2 三个硬事实（都已在代码里核实）

| # | 事实 | 证据 | 后果 |
|---|---|---|---|
| 1 | `units` 里 `enemy_turret/ally_turret/enemy_minion/ally_minion/crystal` 被 `continue` 跳过（v8.0 用户决定，故意的） | [m2 196-198](file:///d:/work/aiKings/WZRYagnt/scripts/m2_agent_v2.py#L196-L198) 在 elif 链**之前** | `turrets`/`minions`/`ally_minions` **恒为空** → 塔威胁曾是死代码（**P0-F1 已改走小地图分阵营塔**，不再依赖这两个 list）；`minions` 仍恒空 → `follow_ally_clearlane` 仍不可达 |
| 2 | 小地图 tracker 已产出分阵营数据，但注入 state 时只拷了 `dots`+`towers`（**P0-F2 已补齐**） | [mm_tracker_v7.py 286-299](file:///d:/work/aiKings/WZRYagnt/wzry/vision/mm_tracker_v7.py#L286-L299) 产出 `towers_v7{ally,enemy}` / `v7{self,ally,enemy,minions{ally,enemy},monster,buff}`；[m2 2038-2040](file:///d:/work/aiKings/WZRYagnt/scripts/m2_agent_v2.py#L2038-L2040) 此前只传了 `found/center/radius/dots/towers` | decide() **看不到**分阵营的塔与兵线（现已注入） |
| 3 | `mm["towers"]` 是**未分类**的塔列表（两边混在一起） | 重锚 `_snap_self_est_to_tower` 用 `mm["towers"]` 当普通 `[x,y]` 列表 | 不能直接当"敌塔"用 |

其余可用输入（已验证）：`ui.hp`、`extra.enemy_hp_scene`/`ui.enemy_hp_min`（真机团战已验证，含 0.6s 有界缓存）、
`ui.lock_target*`（真机 19 帧验证）、`mm.dots{blue,red,green}`、航位推算 + 塔重锚纠偏。

**不可用**：经济差（`ally_gold/enemy_gold` 来自战绩面板，而 m2 **从不主动开面板**，见 ARCH 主文档 §10.5）→
方案里**不许依赖经济差**。塔的血量/小兵血量也都读不到（`digit_rois`/`top_bar` 未标定）。

---

## 2. 目标与非目标

**目标**：占优且安全时，把移动目标从"静态推进线"改成**"最近敌塔的塔外站位点"或"最前那条我方兵线"**，
使英雄表现出"压线/推塔"行为。

**非目标（明确不做）**：
- 不改接战门控（那一层已单独做完：常规 0.60 / 占优推进 0.50 / 收人头 0.35）。
- 不做全图游走/Gank 规划、不做分推/换线、不做越塔强杀。
- 不依赖经济差（读不到）。

**不变量（不许破坏）**：
1. 残血先退/回城优先于一切推进；2. 不占优不接战；3. 塔下无兵线不站；
4. 推进目标也要吃 `TARGET_HOLD_S`（塔坐标来自检测、会跳）；5. 只在**没有更高优先级动作**时接管移动（即仍是 `target is None` 分支里的一支）。

---

## 3. P0 前置修复（各自独立有价值，先做）

**F1. 恢复塔威胁判据（用分阵营塔，而不是恢复 YOLO 塔类）** ✅ 已落地
`turret_threat` 现在基于 `towers_v7.enemy`（`mm_rules_v7` 的**固定点表** + 阵营判定 + 存活过滤，
比 YOLO 塔类可靠得多）。距离在**小地图归一化空间**量：`自身 self_est`（航位推算，走过塔时被塔重锚纠偏）↔ 最近的敌塔固定点。

| 条件 | 行为 |
|---|---|
| `d < TURRET_MM_ESCAPE(0.06)` | 在火力圈内 → `escape_turret`；并记 `cooldowns["turret_esc"]`，下方 1.5s `escape_turret_hold` **只对"真的从塔逃过"的拍**生效 |
| `d < TURRET_MM_THREAT(0.09)` | 靠近敌塔 → 屏内无敌人时 `avoid_turret`；有敌人时置 `cooldowns["turret_threat"]=1` |
| 拿不到 `towers_v7` | **不判**（返回空）—— 绝不用未分类的 `mm["towers"]`（自家塔混在里面 → 会无端后撤） |

- 保留"开局 8s 禁用"；屏幕空间专用的 `_is_base_building` 已随旧判据一起删除。
- ⚠️ **0.06 / 0.09 是估计值、尚未真机标定**（按 防御塔射程 ~900 / 全图 ~14000 折算 ~0.06；
  相邻塔固定点间距实测 0.14~0.21，见 `mm_rules_v7`）。保守取小半径 = 宁缺勿滥。
- 顺带修好一条真实的漏判：`hp_drop_escape` 在"屏内无敌人"时以前无目标可逃（`_turs_f` 恒空），
  现在改为**朝最近敌塔反向撤**（塔伤也是伤）。

**F2. 把小地图分阵营数据注入 state** ✅ 已落地
`build_state(..., minimap={... 旧键 ..., "towers_v7": mm.get("towers_v7"), "v7": mm.get("v7"),
"minions": mm.get("minions")})`（旧键**不删**，向后兼容）。此前 `mm_tracker_v7` 早就产出了
`towers_v7{ally,enemy}` / `v7{self,ally,enemy,minions{...}}`，只是注入时被漏拷 → 决策层看不见阵营。

**F3. 视野外敌人的粗判据** ✅ 已落地（纯函数，尚未被消费）
`enemy_out_of_view(state) -> {"red_seen","scene_seen","ally_seen","suspect_gank"}`。
拿不到"已死敌数"（面板读数，m2 从不主动开面板），所以只用保守近似：
**屏内敌英（units 可信敌英 + 小地图红点）全无 且 视野内我方 ≥2 人** → `suspect_gank=True`
→ 对面 5 个都不在视野 = 很可能在蹲/抱团 → **不许孤身推进**。P1 的 `push_ok` 会消费它。

---

## 4. 推进判据（Advantage Gate）

纯函数 `push_ok(state, cooldowns) -> (ok, reason)`，逐条串行，任一不满足即 `(False, "push_blocked_*")`（可观测），全过 `(True, "push_ok")`。✅ **已落地 + 17 项单测（T29a~n）**。

| 序 | 判据 | 实际常量 | 说明 |
|---|---|---|---|
| 1 | 自己血量 ≥ `PUSH_SELF_HP_MIN` | 0.45 | 低于此不推（v16.0 P3-A 由 0.55 放宽；回城仍在 hp<0.40 发起） |
| 2 | 视野内**我方**点数 ≥ `PUSH_MIN_ALLY` **或** 身边就有队友 | 1 / `PUSH_ALLY_NEAR=0.15` | 孤身不推（v16.0 P3-A 由 2 / 0.10 放宽）。"身边有队友"用**小地图蓝点** |
| 3 | 视野内敌方点数 ≤ 我方点数 | — | 人数不劣才推 |
| 4 | 敌方塔里存在"最近且在我方推进走廊内"的一座 | `PUSH_CORRIDOR_K=0.30` | 没目标塔就不推（回退原行为） |
| 5 | 有**我方兵线**在推进方向上（`v7.minions.ally` 离我 ≤ 该值） | `PUSH_WAVE_NEAR=0.30` | 无兵线不压塔（推塔要靠兵线） |
| 6 | 距离敌塔 > `PUSH_STANDOFF`（塔外站位） | **0.10** | 站塔圈外、别贴脸；**必须 > `TURRET_MM_THREAT(0.09)`**，否则会和 F1 的"靠近敌塔就撤"自相打架 |
| 7 | 未在 `hp_drop_t`(1.2s) / `escape_t`(1.5s) / 刚复活(5s) 窗口内 | — | 窗口全部复用现有状态，不新造 |
| 8 | **视野外敌人**（F3 `suspect_gank`）且身边没有队友 | — | 对面可能在蹲 → 只允许"跟队友推"，不许孤身压塔 |

`blocked_reason` 命名：`push_blocked_hp / _escaping / _alone / _outnumbered / _gank / _no_tower / _no_wave / _too_close`。

---

## 5. 目标点算法（Push Target）

`push_target(state, cooldowns) -> (target, reason)`，**纯函数**，`target=(nx,ny)` 或 `None`（None 时 reason 是放弃原因）。✅ **已落地 + 单测**。按优先级：

1. **跟兵线（首选）**：取 `v7.minions.ally` 里"离我最近且更靠敌方"的那条兵线 → 目标点 = 兵线前方一点点（`+PUSH_LEAD=0.03`，方向 = 自身→最近敌塔；没有敌塔就自身→敌方基地）。理由：推线本质是贴着兵线走。
   → `reason = "push_follow_wave"`（选择器 `_push_pick_wave`：先筛"更靠敌方"，再在其中取最近；一条都没有就先退回全体最近的）
2. **压塔（次选）**：取 `towers_v7.enemy` 里"最近且在我方推进走廊内"的塔 T → 目标点 = T 向我方回退 `PUSH_STANDOFF(0.10)`，保证站在 `TURRET_MM_THREAT(0.09)` 之外。
   → `reason = "push_lane_tower"`（选择器 `_push_pick_tower`：先在走廊内筛，再取最近）
3. **走廊约束（硬性）**：算出的点必须落在发育路走廊内（`_lane_center(cooldowns)` + `PUSH_CORRIDOR_K`），
   否则 `(None, "push_blocked_corridor")`（防止"推着推着跑到河道/对抗路"）。
4. **抖动保护**：把这两个 reason 加进 `TARGET_HOLD_S` 的白名单（与 `support_*` 同级）—— **P2 接线时做**。

> 走廊中心已统一到 `_lane_center()`（内部直接返回已有的 `LANE_DIR_BLUE/RED`），decide() 里的 `_down_c`
> 也改成调它 —— 否则"推进目标"和"回退目标"会用两份可能漂移的常量。

---

## 6. 安全闸（缺一个都会变成送）

| 闸 | 来源 | 行为 |
|---|---|---|
| 塔下撤 | P0-F1 修好后的 `turret_threat` | 进塔圈 → `escape_turret`（已有动作） |
| 无兵线不站塔下 | 判据 5 | 不生成推进动作，退回清兵线/跟随 |
| 人数不劣 | 判据 2/3 | 不推 |
| 血量急降 | 已有 `hp_drop_escape`（0.6s 内降 >0.18） | 强撤 ✓ 沿用 |
| 回城/阵亡/复活中 | 已有前置 return | 不推 ✓ 沿用 |
| 视野外敌人 | P0-F3 | 只能"跟队友推"，不许孤身压塔 |

---

## 7. 与现有逻辑的接法（最小侵入）

- **插入位置**：在 `decide()` 的移动块里、`# 3) 无队友/无敌人也去射手推进线` 那一支**之前**（[m2 968-990](file:///d:/work/aiKings/WZRYagnt/scripts/m2_agent_v2.py#L968-L990)）加一支，
  且整支都在 `if target is None` 内 —— 支援（`support_dps_lane*`）/ 追敌 / 逃生等高优先级动作照旧优先。
  → 条件不成立时**行为与 `off` 完全一致**（原 `dps_lane_push` 照旧），这是本方案最重要的安全性质。✅ 已落地
- **开关**：`cooldowns["push_mode"] = off | shadow | on`，CLI `--push`（默认 `off`）✅ 已落地
  - `off`：**完全不评估**（连 `push_info` 都不产生，零开销、零行为变化）；
  - `shadow`：照常评估，但**不采用**推进目标 —— 只写 `cooldowns["push_info"]` 并节流打印 `[push-shadow]`；
  - `on`：真正接管移动。
- **可观测**：`cooldowns["push_info"] = {mode, ok, why, reason, target}` 每拍产生（off 时无），
  主循环把它挂到 `state_dict["extra"]["push"]`，随 `temp/live_annot/behavior.jsonl` **每拍落盘**
  —— 一局 shadow 就能统计"会推的帧占比 / 被各闸挡掉的占比"。节流打印：`[push-shadow] 会推/挡 …`。
- **常量集中**：`PUSH_SELF_HP_MIN / PUSH_MIN_ALLY / PUSH_ALLY_NEAR / PUSH_CORRIDOR_K /
  PUSH_WAVE_NEAR / PUSH_STANDOFF / PUSH_LEAD`（[m2 94-103](file:///d:/work/aiKings/WZRYagnt/scripts/m2_agent_v2.py#L94-L103)）。
- **防抖白名单**：`push_follow_wave` / `push_lane_tower` 已加入 `TARGET_HOLD_S` 白名单与 map_move 名单 ✅

---

## 8. 验收与指标

**离线（`scripts/test_new_modules.py`，纯函数可测）**
1. 占优 + 有兵线 + 敌塔在前 → `push_follow_wave`/`push_lane_tower`，且目标点在走廊内、离塔 ≥ `PUSH_STANDOFF`； ✅ T29b/b2/m
2. 无兵线 → 不推进（回退，`blocked = _no_wave`）； ✅ T29c
3. 我方 1 人 / 敌方 3 人 → 不推进； ✅ T29d/d2
4. 血 0.45 → 不推进； ✅ T29e
5. 拿不到 `towers_v7` → 不判敌塔（返回空），**不许**用未分类的 `mm["towers"]`； ✅ T27h
5b. 视野外敌人 + 孤身 → 不推；有队友在侧 → 允许推； ✅ T29i/i2
5c. 门控通过但目标点在走廊外 → 放弃推进； ✅ T29j
5d. 未接线：同一"该推"场景下 `decide()` 仍返回 `dps_lane_push`。 ✅ T29n
6. `shadow` 模式：最终动作与 `off` **逐字节一致**，但多一条 shadow 事件； ✅ P2（已接线+真机两跑）
7. 塔坐标抖动 → 目标被 `TARGET_HOLD_S` 保持（不来回切）。 ✅ P2

**真机（L3）· 观测结果（两次都已做）** —— 真机 USB `9f352c1f`，`--capture screencap`：

① **只读 90s**（`--no-action --push shadow`，开局附近 / hp 0.66）：
- 902 帧 / 154 拍，其中 **125 拍有 push 评估**。**会推 0 拍**。
- 挡住：`_alone` 115、`_hp` 10。该局决策：`support_dps_lane` 90 / `grid_best` 38 / `no_target` 15 / `dps_lane_push` 6。

② **一整局 420s**（`--action --push shadow`，15058 帧 / **638 拍** / 213 次动作）：
- **566 拍有 push 评估**（72 拍走不到移动块），**会推 0 拍**。
- 挡住：`_hp` **379**（hp 中位数 **0.426**）/ `_gank` 98 / `_escaping` 72 / `_alone` 17。
- 决策分布：`support_dps_lane` 231 / `grid_best` 183 / **`dead_waiting` 66** / `dps_lane_push` 61 / `disengage_retreat` 10。
- 该局 `dead_waiting` 66 拍（≈ 约 1~2 次阵亡）。**注：shadow 不接管移动，所以这些是 m2 平时就有的表现**，
  不是推进引入的；但也没有同期 baseline 可比，故"死亡数不上升"这条**尚不能判定**。

**⚠️ 结论（P3 的头号输入）：推进在这两次真机里 0 次触发，现在的闸门等于"永不推"。**
两个不同场景各暴露一个主因，说明不是偶发：
1. 该 agent 平时 hp 中位数只有 **0.43**（它只在 hp<0.25 / 资源低时才回家），而判据1 要求 **hp ≥ 0.55**
   → `_hp` 一个闸就吃掉 67% 的帧；
2. 小地图蓝点常年 0~1 个，判据2 要求 **≥2 或身边有队友** → 空旷时被 `_alone` 全挡（只读那局 115/125）。
- 另外 F3 **不是**形同虚设：`_gank` 触发 98 次（一个只读局 0 次、整局 98 次，说明它挑的是"对面都不在视野"的真空期）。
- **所以 P3 的第一步不是"切 `on`"**，而是先松阈值（`PUSH_SELF_HP_MIN` / `PUSH_MIN_ALLY` / `PUSH_ALLY_NEAR`）
  再跑一局 shadow 看"会推比例"是否到了一个合理区间；`on` 要等那时候再定。要放多松，是策略取舍（推得早 vs 送得多），
  需要用户拍板。

③ **方案 A 后复跑一整局 420s**（`PUSH_SELF_HP_MIN 0.55→0.45`、`PUSH_MIN_ALLY 2→1`、`PUSH_ALLY_NEAR 0.10→0.15`）：
- 会推仍是 0 拍。瓶颈后移：`_hp` 379→270；新暴露 `_gank` 411、`_outnumbered` 241、`_no_tower` 170。
- ✅ **顺手拿到 P0-F1 的第一次实机正面证据**：`avoid_turret` **80** + `escape_turret` **30** = **110 拍塔相关决策**
  （改之前这条链是死代码，永远 0）；`[重锚]` 也触发 2 次。**"被塔打不撤"这个真实缺陷在实机上被修掉了。**
- ⚠️ **但 ②③ 记录的"闸门分布"后来被证实不可信**（见下 ⑤ 的测量 bug）：`cooldowns["push_info"]` 跨拍存活，
  没走到移动块的拍会把**上一拍**的评估贴到这一拍上（实测：标着 `_alone` 的拍却记着蓝点 2~4 个）。
  所以"`_gank` 是头号闸"这个当时结论**有一部分是伪影**。

④ **B′+B″ 复跑（`PUSH_BLIND_GANK_S=8.0` 持续判据、`PUSH_MAX_ENEMY_VISIBLE=2` 绝对上限）**：
- 陈旧口径下：`_gank` 411→**172**、`_outnumbered` 241→**0**（两道闸都按预期松开）。
- 但两次的 hp 分布差很多（中位 0.557 vs 0.391）→ **局间方差把对照污染了**，不能直接归因。

⑤ **修掉陈旧 bug 后重跑（干净口径，C 局 420s）**：
- **1359 拍**里 **只有 235 拍真的走到了 push 评估**（**1124 拍 push=None** = 那一拍没做 push 评估：
  多数是根本没到移动块，少数是"到了移动块但目标已被 `support_dps_lane_red` 等分支占掉"）。
- 235 拍里 **会推 0**；闸门：`_hp` 142 ｜ `_alone` 69 ｜ `_no_wave` 17 ｜ `_no_tower` 5 ｜ `_gank` **2**。
- **修正后的结论**：
  1. **B′/B″ 确实有效**：干净口径下 `_gank` 只剩 2 次、`_outnumbered` 0 次 —— 它们不再是瓶颈。
  2. **真正的墙是"机会窗口太少"**：只有 **17%** 的拍（235/1359）能走到移动块，其余都在打架/技能/逃生/
     回城/阵亡/追敌。推进只被允许在 `target is None` 的兜底分支里抢机会，而该局 **53%** 的拍是
     `support_dps_lane`（跟着射手路队友）—— 推进几乎永远排不上。
  3. 在那 235 次机会里，`_hp`(142) + `_alone`(69) 仍挡掉 90%。
  - 上限估计：就算把 hp 与 alone 两道闸全拆掉，最多也只有 211 拍能走到后面几道闸（≤16%），
    真正通过的只会更少 → **继续单点放宽的收益已经很小，且越放越接近"无条件推线"**。

**测量 bug（值得记一笔）**：`push_info` 这类"跨拍存活的状态"必须在**每拍开头清空**，
否则"没产生新值的拍"会把旧值伪装成这一拍的数据。已改为：`--push != off` 时每拍先置 `None`
（off 仍然零足迹），并加单测 T30j 钉住。

**⚠️ 结论（以 ⑤ 的干净口径为准；上面 ②③ 那段"闸门结构"判断有一半是伪影）**
1. **两道头闸已被 B′/B″ 拆掉**：干净口径下 `_gank` 只剩 2 次、`_outnumbered` 0 次。
2. **真正的墙是"机会窗口太少"**：82% 的拍（1124/1359）根本到不了移动块，且该局 54% 的拍是
   `support_dps_lane` —— 推进只被允许在 `target is None` 的兜底分支抢机会，几乎永远排不上。
3. 剩下 235 次机会里 `_hp`(142) + `_alone`(69) 挡掉 90%。**单点放宽的收益天花板 ≈16%**，
   而且越放越接近"无条件推线"（送的风险主要由 F1 塔撤 + 血线 + 逃生窗口兜）。

**下一轮可选项（B′/B″ 已完成；下面都还没做，等拍板）**
| 选项 | 改什么 | 解决的问题 | 代价/风险 |
|---|---|---|---|
| **E 提升推进优先级** | 让推进不止在 `target is None` 兜底里抢（细化"支援点"→"推着线走"） | **真正的墙**：82% 拍到不了移动块 / 53% 是 `support_dps_lane` | 高：动到动作优先级结构，要单独设计 + 回归 → **✅ 已立设计：[ARCH_PUSH_PRIORITY.md](ARCH_PUSH_PRIORITY.md)**（评估面 235→~683 拍；含 `solve()` 二层覆盖的量化与 E-P0/P1/P2 分阶段） |
| **F 再松 hp/alone** | hp 0.45→0.35；`alone` 改成"有任一蓝点即可"甚至不要 | 235 次机会里约能救回 90 拍 | 中高：低血也压线、孤身也压线 → 送的风险明显上升 |
| **C 判据4 降级** | 走廊内没敌塔时不再直接挡，允许退化为"跟我方兵线" | `_no_tower`（长局外塔掉光） | 中：目标点只由兵线决定 |
| **G 接受现状 / 停在这里** | 不动 | —— | 推进≈永不触发；但护栏的**副产品已经赚到了**（F1 塔撤实机生效、F2/F3 感知补全、`behavior.jsonl` 修好） |

**真机（L3）· 复跑（`--action` 一整局，用于 P3 调参后对照）**
- 命令：`venv\Scripts\python.exe scripts\m2_agent_v2.py --capture screencap --serial <序列号> --seconds 420 --action --push shadow`
  （MuMu 走默认 desktop 采集；真机加 `--capture screencap --serial <序列号>`）
  影子模式**不接管移动**，所以这局和平时一样打，只是每拍多记录一条 push 评估。
- 收数据后统计（`temp/live_annot/behavior.jsonl`，`push` 字段为 null 的拍 = 该拍走不到移动块，
  比如在打架/逃生/回城）：
  ```python
  import json, collections
  c, n, has = collections.Counter(), 0, 0
  for L in open("temp/live_annot/behavior.jsonl", encoding="utf-8"):
      r = json.loads(L); p = r.get("push"); n += 1
      if p: has += 1; c[p["reason"] if p["ok"] else p["why"]] += 1
  print(f"总拍 {n} / 有 push 评估 {has}", c.most_common())
  ```
- 指标（**先用代理量**，因为塔血/经济读不到）：
  - ① "在敌塔圈内的时长占比"（推进有效性的代理量，用 `towers_v7.enemy` + 自身估计位置算）；
  - ② 死亡数**不上升**（硬指标）；
  - ③ 回城被打断次数不上升；
  - ④ `dps_lane_push` 的触发占比下降（说明真的接管了）；
  - ⑤ `push_blocked_*` 的分布是否合理（例如 `_no_wave` 占大头 = 闸门太紧，`_gank` 为 0 = F3 从没触发）。
- 达标才切 `on`；另外 `0.06 / 0.09 / 0.10` 这几个半径也要用这局数据回看是否要调。

---

## 9. 分阶段（每阶段可独立回退）

| 阶段 | 内容 | 状态 | 回退 |
|---|---|---|---|
| **P0** | F1 修塔威胁（用分阵营塔）+ F2 注入 v7 数据 + F3 视野外粗判据 | ✅ **已落地**（离线 127/127；实际获得真机间接证据，见 §8③） | 各自独立，纯增益 |
| **P1** | `push_ok` / `push_target` 纯函数 + 单测（**不接线**） | ✅ **已落地**（T29a~n 共 17 项；当时阶段离线 144/144） | 删函数即可 |
| **P2** | `--push shadow` 接线 + 一局真机数据 + 指标对照 | ✅ 接线已落地（T30a~i；离线 154/154）+ ✅ **真机两跑已做**：只读 90s（125 拍评估）+ **整局 420s `--action`（638 拍 / 566 拍评估 / 213 次动作）**，两次 **会推 0 拍** —— 结论见 §8 | `--push off` |
| **P3** | 按数据调阈值 → 切 `on` | ⚠️ **A→B′+B″ 都试过**：四局 shadow（+1 局口径修正复跑）**会推恒 0**。△ 但干净口径下已定位真因：**82% 的拍到不了移动块**（推进只在 `target is None` 兜底抢机会，而 54% 的拍是 `support_dps_lane`），剩下 235 次机会里 `_hp`142+`_alone`69 挡 90%。两道头闸（F3/人数）已被 B′/B″ 拆掉（`_gank`→2、`_outnumbered`→0）。**下一步候选 E（提优先级）/F（再松 hp+alone）/C/G 已列在 §8，等拍板** | `--push off` |

---

## 10. 风险与不做的事

- **航位推算漂移**：推进方向依赖 `self_est`；已有塔重锚纠偏（`TOWER_SNAP_MAX_ERR=0.25`、同塔冷却 6s），
  但误差仍在 → 目标点取"最近且走廊内"双重约束，且推进是**持续推杆 + 定时重发**，对漂移不敏感。
- **小地图塔坐标可能漏检/误检**：一律要求"最近 + 走廊内 + 非基地建筑"三重过滤；塔坐标跳变由 `TARGET_HOLD_S` 吸收。
- **低帧率（曾降至 2.8fps）**：`MAPMOVE` 时长 350-900ms，与帧率耦合不深；但方向纠偏变慢 → 保留定时重发。
- **不做**：越塔强杀、分推换线、基于经济差的决策、Gank 路线规划（都属更高层，且部分依赖读不到的读数）。
