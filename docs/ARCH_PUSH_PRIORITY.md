# ARCH · 推进优先级改造（方案 E）—— 设计

> 版本：草案 v0.1　日期：2026-10-15
> 父方案：[ARCH_MOVE_PUSH.md](ARCH_MOVE_PUSH.md)（推进判据/目标点/`off|shadow|on` 三档 + 真机四局结论）
> 状态：**E-P0 + E-P1 + E-P2 的工具都已落地**（`off`/`shadow` 行为不变，只有 `--push on` 会接管支援帧；
> 离线回归 **191/191**，新增 T31a~h / T32a~e / T33a~f / T34a~g）；**离线回放 + 两局对照工具已落地**
> （`scripts/replay_push.py`：live 统计 + 阈值扫描 + `--compare` 达标判定 + 自检 0/58 —— 见 §9/§10）；
> ⏳ **真机两局（shadow / on）与接管对照还没跑**（USB 设备曾断开，代码侧已无待办）。
> 本文行号对应当前代码。

---

## 0. 结论摘要

- 父方案卡住的真因不是阈值，而是**机会窗口**：推进只在 `decide()` 移动块的**兜底分支**里抢机会，
  真机一局 1306 拍里只有 **235 拍**能走到那里（18%），而 **54% 的拍是 `support_dps_lane`**（跟着射手路队友）—— 推进跟它**根本不竞争**。
- 方案 E = **让推进也能在"支援"分支竞争**，把"走去队友身上"细化为"**推着线走（队友前方的兵线 / 敌塔塔外站位）**"。
- 数据算出来的收益（同一局回算）：评估面 **235 → ~683 拍（5.0×）**，而且支援帧的闸门通过率**远高于**兜底帧
  （支援帧判据2 通过率 **448/454 = 99%**，兜底帧只有约 30%）—— 因为支援帧"走廊内本来就有队友"。
- 一条必须写进设计的**二层覆盖**事实：`decide()` 之后还有 `optimizer.solve()`（[m2 2625-2633](file:///d:/work/aiKings/WZRYagnt/scripts/m2_agent_v2.py#L2625-L2633)）。
  **视野里没有敌人时 solve 原样下放 decide 的动作**（[optimizer 369-371](file:///d:/work/aiKings/WZRYagnt/wzry/decision/optimizer.py#L369-L371)），
  有敌人时它可能用 4096 点网格最优 `grid_best` 覆盖（[optimizer 401-403](file:///d:/work/aiKings/WZRYagnt/wzry/decision/optimizer.py#L401-L403)）。
  → 本方案**不改 solve**，但要**观测并如实报告**它覆盖了多少。

---

## 1. 现状：实测的优先级阶梯

真机一局（C 局，1359 拍，`--push shadow`）**最终动作**的分布（post-solve）：

| 最终 reason | 拍数 | 占比 | 层次 |
|---|---|---|---|
| `support_dps_lane` / `_red` | **736** | **54%** | 移动块·支援（**推进不竞争**） |
| `grid_best` | 237 | 17% | **上游 `solve()` 覆盖**（不在 decide 内） |
| `dps_lane_push` | 195 | 14% | 移动块·兜底（推进**已竞争**，且已修 `off` 时零足迹） |
| `disengage_retreat` | 45 | 3% | 接战门控 |
| `support_red0` / `follow_blue*` / `no_target` / 其它 | ~146 | 11% | 支援/跟队/无目标 |

`decide()` 内部的顺序（按代码行序，L=当前行号）：

```
接战/技能(L563-585) → 回城中/连招/低血兜底(L601-640) → 出钩/技能(L711-738)
 → 低血回城/恢复(L769-801) → 塔威胁: hp_drop_escape/escape_turret/avoid_turret/hold(L840-863)
 → 追敌/停止追击(L910-946)
 → 移动块(L977-1044):  support_dps_lane(992) → support_dps_lane_red(1001) → 【推进 2.5】(1002-1024) → dps_lane_push(1030)
 → follow_ally_lowhp(1035) → dps_lane_push(1044) → follow_ally_clearlane/follow_ally_minimap(1050-1056)
 → no_target(1058)
```

**关键结构性事实**：推进那支（2.5）在 `if _pool_blue:` 的 **`else` 里面**（[L993-1024](file:///d:/work/aiKings/WZRYagnt/scripts/m2_agent_v2.py#L993-L1024)）——
即"射手走廊里**没有**蓝点"时才轮到它。所以它与 `support_dps_lane`（走廊里有蓝点 = 真机 54% 的拍）**零交集**。

---

## 2. 目标与非目标

**目标**：让推进在"**本来就要往射手路前方走**"的拍上也能接管移动，从而把评估面从 18% 抬到 ~70%，
且不违背既有的两条用户铁律（支援=射手路 / 射手在哪跟哪）。

**非目标（明确不做）**：
- 不改接战门控（0.60/0.50/0.35 那套）、不改技能/连招/出钩。
- **不改 `optimizer.solve()`**（那是另一层，且带自己的打分器/权重；本轮只观测）。
- 不做越塔、不做分推换线、不依赖经济差。
- 不让推进在 `follow_ally_lowhp`（低血跟队）分支竞争 —— 那一档的血线本身就低于"占优"门槛，去压线是自相矛盾。

**不变量**：
1. 逃生/残血/回城 > 一切（沿用现有前置 return）；
2. 不占优不接战；
3. **仍在射手路走廊内**（不因推进跑到河道/对抗路）；
4. **不把队友甩开**（推进点与支援点距离受限）；
5. `--push off` 时**逐字节不变**（含零足迹：连 `push_info` 都不写）。

---

## 3. 设计（推荐 E1：细化支援点，而不是抢走支援）

### 3.1 规则

在 `support_dps_lane` 分支里，把"目标 = 队友蓝点"细化成：

```
_sup = 走廊内离我最近的蓝点（现有 L990）
若 推进开关 != off 且 push_ok(...) 通过 且 push_target(...) 给出点 pt：
    且 pt 满足三条附带约束:
      ① dist(pt, _sup) <= PUSH_STAY_WITH_ALLY(提案 0.15)     # 别把队友甩开
      ② (pt - est)·edir >= 0 且 (_sup - est)·edir >= 0        # 我和队友都在"朝敌方"前半场
      ③ pt 在走廊内（push_target 已保证）且 dist(pt, 敌塔) >= PUSH_STANDOFF（已保证）
    → target, reason = pt, push_reason        # 推着线走
否则 → 原样去 _sup（reason = "support_dps_lane"）
```

- `edir` = 已有的 `_push_enemy_dir(est, tw, cooldowns)`（自身→最近敌塔/敌方基地）。
- ② 的用意：队友若在我**身后**（我已经比射手更靠前），继续往前推等于把他甩掉 → 不接管（守住"射手在哪跟哪"）。
- ①②③ 都是"更保守"的约束：**不满足就退回原行为**，所以 E 的最坏情况 = 今天的行为。

### 3.2 为什么是"细化"而不是"抢占"

- 抢在 `support_dps_lane` 之前（让推进压过支援）会直接顶到用户铁律"射手在哪跟哪"，风险与解释成本都高。
- 细化后：**去哪条路、朝哪个方向都没变**（仍在发育路、仍朝敌方、仍贴着队友），只是"站位点"从队友脚下换成
  "队友前方一点点的那条兵线 / 塔外站位"。这正是"推线"的定义，且与"跟队友"兼容。

### 3.3 新增常量

| 常量 | 提案 | 说明 |
|---|---|---|
| `PUSH_STAY_WITH_ALLY` | 0.15 | 推进点与"支援队友点"的最大距离（小地图归一化）。超过就放弃接管。 |

> 其余常量沿用父方案（`PUSH_SELF_HP_MIN 0.45` / `PUSH_MIN_ALLY 1` / `PUSH_CORRIDOR_K 0.30` /
> `PUSH_WAVE_NEAR 0.30` / `PUSH_STANDOFF 0.10` / `PUSH_BLIND_GANK_S 8.0` / `PUSH_MAX_ENEMY_VISIBLE 2`）。

---

## 4. 二层覆盖：`decide` 之后还有 `solve()`（不改，但要量）

| 情形 | `solve()` 行为 | 对推进的影响 |
|---|---|---|
| 动作类型是 skill/attack/combo/recall/restore/none | **原样返回**（类型白名单）[optimizer 366-368](file:///d:/work/aiKings/WZRYagnt/wzry/decision/optimizer.py#L366-L368) | 无（这些拍本来也不该推） |
| **视野里没有敌人**（`not f["reds"]`） | **原样下放 decide 的动作** [optimizer 369-371](file:///d:/work/aiKings/WZRYagnt/wzry/decision/optimizer.py#L369-L371) | ✅ 推进目标**能活下来** |
| 视野里有敌人 | 网格最优 `grid_best` 与战术候选比优，可能**覆盖** [optimizer 401-403](file:///d:/work/aiKings/WZRYagnt/wzry/decision/optimizer.py#L401-L403) | ⚠️ 推进目标可能被换成 `grid_best`（这一层的选择不代表推进） |

→ 所以 E 之后，**"最终动作是不是推进"还要减掉被 solve 覆盖的那些拍**。E-P0 的观测必须把这件事量出来，
否则会把"评估面变大"误读成"推进真的生效"。这也是为什么 E 的验收要分"会推(decide) / 实际执行(executed)"两个口径。

---

## 5. 可观测（E-P0 就要有）

扩展现有 `cooldowns["push_info"]`（父方案 P2 已落盘到 `temp/live_annot/behavior.jsonl`），增加两个键：

| 键 | 含义 |
|---|---|
| `scope` | 本拍在哪个分支被评估：`"fallback"`（现在的 2.5） / `"support"`（E 新增） |
| `used` | 本拍最终**是否采用了**推进目标（shadow 恒 False；on 时按实际） |

外加一项对照：把**最终动作 reason**（已有的 `rec["reason"]`，post-solve）与 `used` 一起看 →
`used=True` 但最终 reason 变成 `grid_best` 的拍数 = **被 solve 覆盖的推进数**。

---

## 6. 验收

**离线（加入 `scripts/test_new_modules.py`，纯函数可测）**
1. 走廊内有蓝点 + 推进门通过 + 推进点贴着队友（≤0.15）→ 采用推进点（新 reason）；
2. 队友在我**身后** → 不接管（仍 `support_dps_lane`）；
3. 推进点离队友 > `PUSH_STAY_WITH_ALLY` → 不接管；
4. 推进门不通过 → 不接管（逐条对应现有各 `push_blocked_*`）；
5. `--push off` → 逐字节等于今天（含"不产生 `push_info`"）；
6. `shadow` → 动作与 `off` 逐字节一致，只多 `push_info`（含 `scope="support"`）；
7. `on` → 只有前 4 条都满足才接管。

**真机（L3）· 对照**
- `--push shadow` 一局，统计：`scope="support"` 的评估拍数、`ok` 比例、以及"`used=True` 被 solve 覆盖"的拍数；
- 达标线（提案）：`support` 分支的 `ok` 比例 ≥ 20%，且**死亡数不上升**、`support_dps_lane` 占比下降；
- 达标后才 `on` 一局，再做同样对照。

---

## 7. 分阶段

| 阶段 | 内容 | 行为变化 | 回退 |
|---|---|---|---|
| **E-P0** | 只在 `support` 分支**评估**推进门并写 `push_info.scope="support"` + `e_ok`/`gap`/`used`（**不采用**） | ✅ **代码已落地**：`_push_eval()`（[m2 394-424](file:///d:/work/aiKings/WZRYagnt/scripts/m2_agent_v2.py#L394-L424)）+ 支援分支调用（[m2 1072-1082](file:///d:/work/aiKings/WZRYagnt/scripts/m2_agent_v2.py#L1072-L1082)）；**零**（off 零足迹、shadow 动作不变，T31b/d/h 钉住） | 删这几行 |
| **E-P1** | `on` 时按 §3.1 三条件接管；`shadow` 仍不动 | ✅ **代码已落地**：`if _pem == "on" and _fie.get("e_ok"): target, reason = 推进点`（[m2 1079-1082](file:///d:/work/aiKings/WZRYagnt/scripts/m2_agent_v2.py#L1079-L1082)）；仅 `--push on` 生效（T33a~f 钉住：接管点仍在走廊内/离塔≥STANDOFF/贴合队友≤0.15；队友在身后或太远就不接管） | `--push off` |
| **E-P2** | 真机 shadow→on 对照 + 定 `PUSH_STAY_WITH_ALLY` | ✅ **工具已落地**：m2 `--push-set KEY=VAL`（白名单内临时覆盖调参常量，[m2 118-146](file:///d:/work/aiKings/WZRYagnt/scripts/m2_agent_v2.py#L118-L146)）+ `replay_push.py --compare A B`（两局对照 + §6 达标线判定）；⏳ **真机两局待跑** | `--push off` |

**设备恢复后的三条命令**（一局 shadow 量 `e_ok`；一局 `on` 真接管；再一条出对照+达标判定）：
```
venv\Scripts\python.exe scripts\m2_agent_v2.py --capture screencap --serial <序列号> --seconds 420 --action --push shadow
venv\Scripts\python.exe scripts\m2_agent_v2.py --capture screencap --serial <序列号> --seconds 420 --action --push on
venv\Scripts\python.exe scripts\replay_push.py --compare temp/_run_shadow.jsonl temp/_run_on.jsonl
```
（每局跑完先把 `temp/live_annot/behavior.jsonl` 拷成上面那两个名字，否则下一局会覆盖它。）

口径说明：`--compare` 复算 §6 的四条达标线（`support` ok 比例 ≥20% / `support` 家族占比下降 /
平均 score 不下降 / 死亡代理量不上升）并并排打印全部指标。
⚠️ `label_recalling` / `reason_dead` / `reason_recall` 是**代理量** —— 记录里没有"绝对死亡数/回城中断数"字段。
**阈值/条件的实验先走离线**（`scripts/replay_push.py --set ...`，真机可用 m2 `--push-set` 直接带参数跑），
真机只用来做最终确认 —— 别再为改一个数打一局。

> E-P0 单独就有价值：它用**一局真机**回答"E 到底能抢到多少拍、被什么挡、有多少会被 solve 覆盖"，
> 而**不改变任何行为** —— 这正是父方案卡住时最该先做的一步。

---

## 8. 风险与不做的事

- **被 `solve()` 覆盖**（见 §4）：E 的收益上限受这一层限制；要真正打通，需要单独设计"让 solve 也认推进"（**不在本轮**）。
- **"跟队友"退化**：若 `PUSH_STAY_WITH_ALLY` 放得太松，会变成"一个人往前压" —— 所以它必须是**硬约束**且宁可小而保守。
- **长局没有敌塔**（`_no_tower`）：长局外塔掉光时推进仍拿不到目标点（父方案 §8 的选项 C 未采纳）。
- **`self_est` 漂移**：推进点全依赖航位推算；已有塔重锚纠偏，且推进是持续推杆+定时重发，对漂移不敏感。
- **不做**：不改 solve/接战门控/技能栈；不做分推换线；不做越塔；不用经济差。

---

## 9. 离线回放基础设施（已落地）

`scripts/replay_push.py` —— 用 `behavior.jsonl` 里记录的**真实输入**重跑 `push_ok` / `_push_eval`
（**直接调用 m2 的同一套函数**，不重写任何口径），把"调阈值/改条件"从"打一局 7 分钟"变成**离线扫描**。

- 输入由 `_push_inputs()`（[m2 351-389](file:///d:/work/aiKings/WZRYagnt/scripts/m2_agent_v2.py#L351-L389)）逐拍写进 `push.in`：
  `hp / est / camp / blue / red / towers / waves / esc(布尔) / nscene / sup` —— 只打包判据真正读到的字段。
- **自检**：同常量复跑应与记录**逐拍一致**。首次跑出 **1.7% 不一致**（全部卡在 `hp==0.45` 那一拍），
  根因是我把 hp `round(...,4)` 取了整，而判据是**精确比较** → 改成不取整，复跑 **0/58**。
  → 这条正好说明"自检不能省"：没有它，这个取整会在所有边界拍上静默改变结论。
- 复审时又抓到第二个回放缺陷：`co.update()` **不删旧键** → 上一拍的 `escape_t` 会泄漏到下一拍
  （把下一拍也判成"在逃"）。已改成**每拍重建 cooldowns、只继承 F3 的 `blind_start`**，并加 T32e 钉住。
- 复审量化过一处"策略一致性"风险：`on` 可能在 `hp∈[0.45,0.55)`（低于接战门槛 `_u_eng_hp`）时去压线。
  用真机 C 局回算：该血量带的 support 帧只有 **3/736**（该 agent 的血量是双峰：要么 <0.45 被血门挡住、
  要么 ≥0.55）→ 暴露面可忽略，**不加**额外守卫。
- 覆盖常量时自检**自动跳过**（改了阈值本来就该不同，那种"不一致"不是错误）。
- 端到端夹具自检（58 拍，29 support + 29 fallback）：同常量 → 会推 27（46.6%）；
  `--set PUSH_SELF_HP_MIN=0.30` → 会推 **41（70.7%）**、闸门只剩 `_gank` 17 —— **阈值扫描现在离线可做**。
- 默认输出**两段**：`[live]`（只读记录：评估拍/scope/会推/e_ok/**实际接管 used**）
  + 回放（可扫阈值）。`--no-replay` 只看 live。`--set` 时自检自动跳过。
  **`used=True` 但最终 reason 变了 = 被 `solve()` 覆盖**，这一项下次真机跑完就能直接读到。
- 限制：只有**含 `push.in`** 的记录能回放（即"加了 `push.in` 之后"的 `--push shadow` 跑）；
  历史四局的数据没有这个字段，回放不了（实测会给出手册式提示而不是 traceback）。

---

## 10. E-P2 的两件工具（已落地）

| 工具 | 位置 | 作用 |
|---|---|---|
| `--push-set KEY=VAL`（可重复） | m2 CLI（[m2 118-146](file:///d:/work/aiKings/WZRYagnt/scripts/m2_agent_v2.py#L118-L146)） | 真机确认时临时覆盖 `PUSH_*` / `TURRET_MM_*`（**白名单**，防顺手改任意全局）；**先全部校验再统一生效**（避免半吊子状态）；默认不覆盖 = 零行为变化，启动时打印 `[push-set]` |
| `--compare A B` | `scripts/replay_push.py` | 两局并排 + §6 达标线判定；A=对照局(shadow) B=变更局(on) |

`--compare` 输出的指标：`frames / dur_s / mean_score / eval_n / support_n / support_ok_ratio /
e_ok / used / overridden / push_exec / static_push / support_ratio / label_recalling / reason_dead / reason_recall`。
两条**踩过的坑**（都是"用真数据一跑就现形"）：
- 记录里存在 `t=0` 的脏行 → `max-min` 曾算出 **1.79e9 秒**；现已过滤非时间戳（`t < 1.6e9` 不参与时长）。
- 两局的 `eval_n` 都是 0 → 说明文件来自**加 `push.in` 之前**的跑，会显式提示，避免把"指标全 0"误读成"推进没生效"。
