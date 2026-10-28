# 规则长尾分诊表（141 条）

> 数据源：`reverse/out/il2cpp/rules_table.tsv`（不重新扫 `.so`）。
> 生成：`D:/app/python/python.exe reverse/gen_rules_triage.py`（以下命令均在 `WZRYagnt/` 下执行）。
> 现状（2026-10-26 实测）：`rules.py` 注册 **40** 条（`evidence=EVID_DISASM` 5 / `EVID_NAME` 34 / 未标 1），
> 唯一地址 **33** 个（旧文案写“已实现 37 条”，与实测不符）；
> 表内可识别的具名函数（去重、去桩）**67** 个，待做表 **52** 条 → 去重并扣掉两次实现后**实际未做 45 条**。
>
> **2026-10-27 实现与分桶**：已实现 **MovePosWhenLinger @0x027ec574** 与
> **MovePosWhenLingerAroundDragon @0x027eb0e4**（均 `evidence=EVID_NAME`）。
> 判据 `IsLinger @0x027fc344` / `CheckLingerAroundDragon @0x027ea87c` 并入
> `rules._is_linger` / `_check_linger_around_dragon`，**不单独注册**——固定顺序命中即短路，
> 单独注册会让后面的动作规则永不执行（与 `_in_spring_box` 同一角色）。→ 真实未做 **45 条**。
>
> 45 条按「对本项目是否有用」分三桶（钟馗**单英雄**）：
> - **A 4 条**：判据已被现有代码消费（#6 #16 #20 #31），无需独立注册；
> - **B 25 条**：英雄/装备专属（守约二技能、孙尚香、鲲鲲传送、达摩卡墙……），本项目**跳过**，换英雄时再回来看；
> - **C 16 条**：通用且未实现——**这才是真实可做的子集**：
>   #2 move_pos_when_skill_no_move / #3 tactics_UseMapSpecialSkill / #7 Process / #11 wait_resurrection_armor /
>   #22 multi_person_cake / #28 AtkUnit / #32 IsAroundDist / #37 AvoidStuckDoor / #38 transhold /
>   #39 GenMovePos / #41 linger_dir_adjust / #42 Process / #44 InitSpecialPlaceStuckWall /
>   #45 move_pos_when_struck_wall / #46 UseMapSpecialSkill / #52 attack_ding。
>
> 实现注意（本轮踩过的）：**加规则前先搜同类**。已有 `r_is_linger_around_dragon`
> （IsLingerAroundDragon @0x027eafb4，语义是“目标为龙+敌近→别开龙”）与本轮的
> `_check_linger_around_dragon`（“我在龙坑打转”）名字极像但语义相反，已改名区分。
>
> 复核口径的两次教训（别再用更宽的口径）：
> - **地址只能从 `Rule(...)` 注册行取**。全文 grep 会把「注释里提到过这个地址」当成已实现：
>   #6 `stay_in_spring @0x027f2a5c` 只在常量注释里出现（`SPRING_BOX_HALF_X` 已提取），
>   **没有注册规则**，所以它仍未实现。
> - **名字必须匹配地址**。#14 `use_chuansong_skill @0x027f3ea4` 与已注册的
>   `UseChuanSongSkill @0x027f3f00` 同名但不同地址，是两条不同的函数。
>   抓名字不抓地址会把它误判为已实现（第一版复核就是这么错的：报了 45，实际 47）。
>
> ⚠️ **候选常量不可直接采信**：`rule_consts.py` 的“候选常量”是在 **420 条指令窗口**内扫
> `cmp/cmn/tbz` 得到的，**不保证属于该规则函数**（相邻函数会被卷进来）。
> 实测 #1 `IgnoreBailishouyueSkill2 @0x27ee08c`：完整反汇编是
> `sub w8, w0, #0x51` / `cmp w8, #0xdb` = **单位 ID 范围 [81,300] 过滤 + 取最近**（随后算 dx/dy），
> 与规则名的字面含义不符 —— 若照名字写“忽略守约二技能”就是错的。
> **每条必须先用 `D:/app/python/python.exe reverse/disasm_one.py <addr> <n>` 看完整指令流再动笔。**
>
> 说明：§8.2 的 141 是 RuleTactics 调度表里的具名规则数；本表按`RuleTactics::` / `rule_` 前缀从 260 行原始注册表里抽，含部分重载与子函数，所以条数会多于 141 —— 以本表为**工作清单**，以 141 为**对账口径**。

> **2026-10-28 函数边界复核（C 组）→ 当日晚间已勘误，原结论作废**
>
> 原结论"多数槽位地址在函数中段、需先做函数边界重建"是**工具读偏 0x1000 造成的假象**：
> `disasm_one.py` 早期版本直接把 VA 当文件偏移（`.text` 两者差 0x1000 = 256 条指令）。
> 用 PT_LOAD 正确映射后实测 **52/52 (100%) 条地址精确落在函数序言**，
> 而把地址当文件偏移读时只有 1/52 (1.9%)。
> 证据与重读结果见 `docs/反汇编地址口径勘误-2026-10-28.md`；
> 校验脚本 `reverse/verify_addr_convention.py`；回归守卫 `scripts/test_disasm_addr.py`。
>
> 三条被点名的规则在正确字节下语义都相当具体（不再"不可确认"）：
> - #11 `wait_resurrection_armor @0x27eb7d8`：动作码 **16**，config **913471**，
>   血量比 > **0.5**，浮点门槛 **120.0**，容器 stride **56**。
> - #3 `tactics_UseMapSpecialSkill @0x27d7ce8`：多分支调度器，每支 `状态字段≥1` +
>   虚拟谓词 → 动作码 **3**；状态字段 `+0x14dc` / `+0x14e0` / `+0x1490`。
> - #22 `multi_person_cake @0x27fe7c4`：config **900251**，返回值 ∈ **[300,301]**，时间差 **≥31**。
>
> **仍未新增规则（0 条）**，但阻塞点已两度收敛：
> ① ~~虚拟调用无法解析~~ → **已于同日晚打通**（`reverse/resolve_got.py`：`.rela.dyn` 的
>   `R_AARCH64_RELATIVE` addend 就是目标函数；#11 解出 `TargetFinder`、
>   #3 解出指向 #46 的 `RuleTactics::UseMapSpecialSkill`、#22 解出
>   `[GetTargetVec] target_type=kAnyone`）；
> ② **官方动作码未映射**（16 / 3 与 `official_ai/actions.py` 的 label 体系无对照表）—— 现在这是唯一硬缺口。
> 附：`reverse/dump_rule_table.py` 从重定位重建出 **194 个规则函数 / 100 个带规则名**，
> 本表 52 条地址 **52/52 全命中**（独立第三证据）。
> 正确推进顺序：**动作码对照表 → 逐条落地**。
> 另按 tags 初步判断 3 条不是动作规则，不应注册：#7 `RuleTactics::Process()` 是调度器、
> #32 `IsAroundDist` 是距离判据、#44 `InitSpecialPlaceStuckWall` 是初始化。

工作流（每条 10~20 分钟）：
1. `D:/app/python/python.exe reverse/disasm_one.py <addr> <n> --prologue`
   取判据附近的 `cmp/fcmp/fmov/tbz`，并确认该地址确实是函数入口（`[prologue] YES`）；
2. 用日志串确认分支语义（`rules_table.tsv` 的 tags 列就是该函数引用的日志）；
3. 常量写进 `rules.py` 并标 `evidence="disasm"` + `addr`；
4. 在 `test_official_ai_replica.py` 加一条边界测试（阈值两侧各一例）。

## 一、已实现

| 规则 | 证据 | 地址 |
| --- | --- | --- |
| CheckUseZhiliao | name | 0x027e14ec |
| CheckUseZhongjie | name | 0x027e126c |
| UseChengji | name | 0x027e0ba0 |
| UseHanbingChengji | name | 0x027e09b0 |
| UseChuanSongSkill | name | 0x027f3f00 |
| IsNowEPTooLow | name | 0x027df1e8 |
| CheckReturnCity | disasm | 0x027df394 |
| KeepAwayFromEnemyTower | disasm | 0x027dbd98 |
| IsInDangerousPlace | disasm | 0x027db2ac |
| GotoTargetPosWhenFirstBorn | disasm | 0x027dd40c |
| FrdFzDonotTakeBloodPack | name | 0x027d9030 |
| AvoidKilledByOrgan | name | 0x027f39b4 |
| SituationAssessmentDangerous | name | - |
| AtkSoldierInFrdHigh | name | 0x027e9a3c |
| ProcessLingerInSpring | name | 0x027d61c0 |
| SelectCake | name | 0x027df040 |
| LeaveCake | name | 0x027dcc5c |
| UseMultiPersonCake | name | - |
| StayWithMonster | disasm | 0x027e5438 |
| IsStuckWall | name | 0x027e766c |
| LongTimeMoveStopRule | name | 0x027f32ec |
| LongTimeOnlyAtk | name | - |
| NotargetOptimizeProcess | name | 0x027f02a8 |
| IsInEnemyHoverArea | name | 0x027dfa28 |
| StayWithMonsterUsingSkill | name | 0x027e620c |
| QuitMovePosWhenArrive | name | 0x027f2d2c |
| IsLingerAroundDragon | name | 0x027eafb4 |
| ReturnCityBetween2TowerInEmyHigh | name | 0x027f24b0 |
| GotoTargetPosInSpring | name | 0x027dd190 |
| MoveStopInSpringWhenRevival | name | 0x027f2b98 |
| MovePosWhenLongTimeOnlyAtk | name | 0x027eb200 |
| DangerousPlaceRetreat | name | 0x027de1dc |
| StopShanxianUseInUncorrectTimeing | name | 0x02558f0c |
| OptimizePosSkillWhenNotarget | name | 0x027f0000 |
| OptimizeDirSkillWhenNotarget | name | 0x027f02a8 |
| IsInSavePlace | name | 0x027db060 |
| GetHeroSpeedUpSkill | name | 0x027de90c |

## 二、待做（按指令数升序 = 先做短的）

> 注：表中 #5 `move_pos_when_linger_around_dragon` 与 #18 `move_pos_when_linger`
> 已于 2026-10-27 实现（见 §一 与 CHAIN）；为保留原编号未从表内删除。

| # | slot | 地址 | 指令数 | 规则名/首日志串 | 候选常量（前 8） |
| --- | --- | --- | --- | --- | --- |
| 1 | [0398] | 0x27ee08c | 26 | IgnoreBailishouyueSkill2 | 196 / 40 / 4054 / 3277 |
| 2 | [0352] | 0x27ea7b0 | 51 | move_pos_when_skill_no_move | 32 / 1424 / 1436 / 1064 / 1076 / 2632 / 3937 / 13 |
| 3 | [0168] | 0x27d7ce8 | 53 | tactics_UseMapSpecialSkill | 224 / 2872 / 3333 / 31 / 3526 / 37 |
| 4 | [0450] | 0x27f39b4 | 70 | avoid_killed_by_organ | 112 / 7456 / 32 / 31 / 29 / 12 / 4320 / 150 |
| 5 | [0360] | 0x27eb0e4 | 71 | move_pos_when_linger_around_dragon | 64 / 2872 / 2816 / 39 / 1064 / 176 / 24 / 13 |
| 6 | [0438] | 0x27f2a5c | 79 | stay_in_spring | 151 / 7378697629483820646 / 7385 / 31 / 625 / 624 / 13 / 2372 |
| 7 | [0142] | 0x27d26dc | 80 | Process | 112 / 49 / 1568 / 65 / 1659 / 2685 |
| 8 | [0332] | 0x27e8ca4 | 86 | use_V102_chuansong_skill | 144 / 1801 / 58658 / 28 / 2632 / 3101 / 29 / 548 |
| 9 | [0432] | 0x27f234c | 89 | ReturnCityInHaiYueSkill3Alone | 54464 / 1008 / 46 / 2632 / 1629 / 29 / 77 / 3016 |
| 10 | [0420] | 0x27f02a8 | 98 | optimize_skill_when_notarget | 80 / 308 / 21 / 20 / 32 / 2872 / 2087 / 33 |
| 11 | [0364] | 0x27eb7d8 | 101 | wait_resurrection_armor | 64 / 2872 / 719 / 28 / 308 / 60 / 32 / 28087 |
| 12 | [0440] | 0x27f2b98 | 101 | move_stop_in_spring_when_revival | 308 / 60495 / 13 / 63 / 34 / 6908 / 1040 / 16 |
| 13 | [0484] | 0x27f9b4c | 108 | UseKongkongerTeleport | 550 / 300 / 15 / 308 / 32 / 50 / 26705 / 140737488355328 |
| 14 | [0452] | 0x27f3ea4 | 118 | use_chuansong_skill | 160 / 7385 / 308 / 400 / 58658 / 58661 / 56 / 300 |
| 15 | [0488] | 0x27faa08 | 128 | sunquan_skill3 | 112 / 151 / 2260 / 24 / 56 / 257 / 308 / 20834 |
| 16 | [0496] | 0x27fc344 | 136 | IsLinger | 533 / 308 / 8778 / 15 / 30 / 2632 / 14 / 536 |
| 17 | [0444] | 0x27f30c0 | 139 | UseZhanjudianRule | 96 / 21 / 224 / 16960 / 308 / 16 / 176 / 160 |
| 18 | [0376] | 0x27ec574 | 145 | move_pos_when_linger | 151 / 2872 / 3164 / 25 / 308 / 220 / 120 / 449 |
| 19 | [0436] | 0x27f280c | 148 | Use10v10LaneRule | 224 / -16000 / 32000 / 3755 / -22585 / -62000 / 62000 / 22585 |
| 20 | [0354] | 0x27ea87c | 154 | CheckLingerAroundDragon | 128 / 140 / 542 / 308 / 61375 / 1008 / 149 / 32 |
| 21 | [0316] | 0x27e73d0 | 160 | AttackHeroInPortal | 308 / 58108 / 10500 / -46124 / 10501 / 4140 / 151 / 3689348814741910323 |
| 22 | [0510] | 0x27fe7c4 | 160 | multi_person_cake | 308 / 48283 / 1456 / 32 / 301 / 1468 / 300 / 31 |
| 23 | [0506] | 0x27fdd7c | 166 | AvoidDasimingStuckWall | 144 / 517 / 3744 / 3141 / 20 / 27756 / 40 / 16 |
| 24 | [0500] | 0x27fcd7c | 170 | avoid_v113_xuebao | 128 / 20 / 40 / 32 / 15 / 2632 / 22 / 4076 |
| 25 | [0508] | 0x27fe510 | 171 | AvoidFeiStuckWall | 96 / 542 / 3744 / 3141 / 20 / 27756 / 40 / 64 |
| 26 | [0304] | 0x27e5ed8 | 188 | laye_keep | 80 / 15 / 308 / 3844 / 32 / 344 / 16 / 548 |
| 27 | [0464] | 0x27f6690 | 190 | use_chuansong_skill_juexing | 288 / 64 / 80 / 769 / 768 / 104 / 14740 / 28087 |
| 28 | [0336] | 0x27e9154 | 193 | AtkUnit | 128 / 1380 / 3661 / 12 / 7664 / 131 / 11 / 3155 |
| 29 | [0314] | 0x27e70a8 | 202 | PushHeroOutofPortal | 176 / 24 / 308 / 58108 / 10500 / -46124 / 28 / 335609856 |
| 30 | [0498] | 0x27fc6b0 | 203 | UseV113Xuebao | 160 / 75 / 308 / 25404 / 1040 / 10000 / 28 / 20 |
| 31 | [0494] | 0x27fc210 | 213 | IsLinger | 308 / 32 / 3263 / 517 / 1825 / 14 / 665 / 533 |
| 32 | [0356] | 0x27eac44 | 220 | IsAroundDist | 128 / -6148914691236517206 / 43691 / 5848 / 18446744073709551614 / 12 / 24 / 35747 |
| 33 | [0236] | 0x27df394 | 221 | strategy_return_city | 80 / 1659 / 1118 / 3145 / 308 / 2372 / 2240 / 70 |
| 34 | [0414] | 0x27ef3c4 | 228 | CarryBombNearFrd | 240 / 41705 / 11 / 63 / 33 / 45 / 100 / 65 |
| 35 | [0442] | 0x27f2d2c | 228 | avoid_killed_by_organ | 20160 / 13 / 26 / 2632 / 2089 / 25 / 308 / 368 |
| 36 | [0492] | 0x27fbde0 | 268 | UseLubandashiSkill4HookEscape | 208 / 525 / 512 / 31 / 308 / 32 / 1008 / 536870904 |
| 37 | [0502] | 0x27fd0f8 | 281 | AvoidStuckDoor | 192 / 88 / 32 / 48 / 24 / 506 / 30 / 21 |
| 38 | [0468] | 0x27f7360 | 325 | transhold | 128 / 180 / 149 / 2632 / 3399 / 14 / 1659 / 917 |
| 39 | [0380] | 0x27ecc5c | 335 | GenMovePos | 192 / 575 / 354 / 2362 / 576 / 308 / 64 / 92 |
| 40 | [0480] | 0x27f8f78 | 369 | AvoidZhenjiBingZhu | 144 / 300 / 18725 / 62554 / 56 / 1008 / 188 / 32 |
| 41 | [0138] | 0x27c003c | 400 | linger_dir_adjust | 464 / 2372 / 1040 / 2444 / 2764 / 2664 / 2684 / 2396 |
| 42 | [0140] | 0x27cee04 | 400 | Process | 416 / 49 / 1568 / 65 / 308 / 1008 / 1659 / 2685 |
| 43 | [0150] | 0x27d5a90 | 400 | use_V112_dayu_chuansong | 176 / 188 / 1501 / 56928 / 202 / 16 / 1273 / 2632 |
| 44 | [0166] | 0x27d73e0 | 400 | InitSpecialPlaceStuckWall | 272 / 28 / 961 / 280 / 152 / 288 / 57 / 16 |
| 45 | [0320] | 0x27e7b1c | 400 | move_pos_when_struck_wall | 128 / 2872 / 41 / 30 / 151 / 542 / 548 / 18725 |
| 46 | [0404] | 0x27ee324 | 400 | UseMapSpecialSkill | 1408 / 14373 / 14304 / 14314 / 14364 / 3121 / 21 / 308 |
| 47 | [0416] | 0x27ef8b0 | 400 | GetBombRetreatPos | 256 / 2372 / 23 / 10000 / 41705 / 11 / 1008 / 45 |
| 48 | [0460] | 0x27f5430 | 400 | RideStateAction | 160 / 7385 / 3486 / 3355 / 13 / 1040 / 51711 / 2304 |
| 49 | [0474] | 0x27f8528 | 400 | yzj_skill1_stop_charge | 128 / 6764 / 541 / 506 / 534 / 548 / 542 / 15 |
| 50 | [0486] | 0x27f9cfc | 400 | UsingSanguoEquipSkill | 304 / 59642 / 43691 / 43690 / 21844 / 27 / 15 / 308 |
| 51 | [0490] | 0x27fac24 | 400 | UseWangWeiSkill3 | 176 / 138 / 7164 / 257 / 13832 / 31 / 354 / 3126 |
| 52 | [0504] | 0x27fd6a4 | 400 | attack_ding | 144 / 34079 / 1000 / 63 / 37 / 28 / 50 / 56928 |
