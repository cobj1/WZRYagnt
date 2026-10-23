# 规则长尾分诊表（141 条）

> 数据源：`reverse/out/il2cpp/rules_table.tsv`（不重新扫 `.so`）。
> 生成：`D:/app/python/python.exe reverse/gen_rules_triage.py`。
> 现状：已实现 **17** 条；表内可识别的具名函数（去重、去桩）**67** 个，待做 **59** 条。
>
> 说明：§8.2 的 141 是 RuleTactics 调度表里的具名规则数；本表按`RuleTactics::` / `rule_` 前缀从 260 行原始注册表里抽，含部分重载与子函数，所以条数会多于 141 —— 以本表为**工作清单**，以 141 为**对账口径**。

工作流（每条 10~20 分钟）：
1. `D:/app/python/python.exe reverse/rule_consts.py <addr>` 取判据附近的 `cmp/fcmp/fmov/tbz`；
2. 用日志串确认分支语义（`rules_table.tsv` 的 tags 列就是该函数引用的日志）；
3. 常量写进 `rules.py` 并标 `evidence="disasm"` + `addr`；
4. 在 `test_official_ai_replica.py` 加一条边界测试（阈值两侧各一例）。

## 一、已实现

| 规则 | 证据 | 地址 |
| --- | --- | --- |
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

## 二、待做（按指令数升序 = 先做短的）

| # | slot | 地址 | 指令数 | 规则名/首日志串 | 候选常量（前 8） |
| --- | --- | --- | --- | --- | --- |
| 1 | [0398] | 0x27ee08c | 26 | IgnoreBailishouyueSkill2 | 196 / 40 / 4054 / 3277 |
| 2 | [0352] | 0x27ea7b0 | 51 | move_pos_when_skill_no_move | 32 / 1424 / 1436 / 1064 / 1076 / 2632 / 3937 / 13 |
| 3 | [0168] | 0x27d7ce8 | 53 | tactics_UseMapSpecialSkill | 224 / 2872 / 3333 / 31 / 3526 / 37 |
| 4 | [0358] | 0x27eafb4 | 66 | IsLingerAroundDragon | 43691 / 3205 / 129 / 10 / 505 |
| 5 | [0450] | 0x27f39b4 | 70 | avoid_killed_by_organ | 112 / 7456 / 32 / 31 / 29 / 12 / 4320 / 150 |
| 6 | [0360] | 0x27eb0e4 | 71 | move_pos_when_linger_around_dragon | 64 / 2872 / 2816 / 39 / 1064 / 176 / 24 / 13 |
| 7 | [0438] | 0x27f2a5c | 79 | stay_in_spring | 151 / 7378697629483820646 / 7385 / 31 / 625 / 624 / 13 / 2372 |
| 8 | [0142] | 0x27d26dc | 80 | Process | 112 / 49 / 1568 / 65 / 1659 / 2685 |
| 9 | [0332] | 0x27e8ca4 | 86 | use_V102_chuansong_skill | 144 / 1801 / 58658 / 28 / 2632 / 3101 / 29 / 548 |
| 10 | [0432] | 0x27f234c | 89 | ReturnCityInHaiYueSkill3Alone | 54464 / 1008 / 46 / 2632 / 1629 / 29 / 77 / 3016 |
| 11 | [0420] | 0x27f02a8 | 98 | optimize_skill_when_notarget | 80 / 308 / 21 / 20 / 32 / 2872 / 2087 / 33 |
| 12 | [0364] | 0x27eb7d8 | 101 | wait_resurrection_armor | 64 / 2872 / 719 / 28 / 308 / 60 / 32 / 28087 |
| 13 | [0440] | 0x27f2b98 | 101 | move_stop_in_spring_when_revival | 308 / 60495 / 13 / 63 / 34 / 6908 / 1040 / 16 |
| 14 | [0484] | 0x27f9b4c | 108 | UseKongkongerTeleport | 550 / 300 / 15 / 308 / 32 / 50 / 26705 / 140737488355328 |
| 15 | [0452] | 0x27f3ea4 | 118 | use_chuansong_skill | 160 / 7385 / 308 / 400 / 58658 / 58661 / 56 / 300 |
| 16 | [0198] | 0x27dd190 | 127 | GotoTargetPosInSpring | 308 / 32 / 1008 / 976 / 301 / 2848 / 1065 / 2220 |
| 17 | [0488] | 0x27faa08 | 128 | sunquan_skill3 | 112 / 151 / 2260 / 24 / 56 / 257 / 308 / 20834 |
| 18 | [0496] | 0x27fc344 | 136 | IsLinger | 533 / 308 / 8778 / 15 / 30 / 2632 / 14 / 536 |
| 19 | [0444] | 0x27f30c0 | 139 | UseZhanjudianRule | 96 / 21 / 224 / 16960 / 308 / 16 / 176 / 160 |
| 20 | [0376] | 0x27ec574 | 145 | move_pos_when_linger | 151 / 2872 / 3164 / 25 / 308 / 220 / 120 / 449 |
| 21 | [0306] | 0x27e620c | 147 | StayWithMonsterUsingSkill | 80 / 460 / 2329 / 672 / 3828 / 3800 / 3808 / 3172 |
| 22 | [0436] | 0x27f280c | 148 | Use10v10LaneRule | 224 / -16000 / 32000 / 3755 / -22585 / -62000 / 62000 / 22585 |
| 23 | [0354] | 0x27ea87c | 154 | CheckLingerAroundDragon | 128 / 140 / 542 / 308 / 61375 / 1008 / 149 / 32 |
| 24 | [0316] | 0x27e73d0 | 160 | AttackHeroInPortal | 308 / 58108 / 10500 / -46124 / 10501 / 4140 / 151 / 3689348814741910323 |
| 25 | [0510] | 0x27fe7c4 | 160 | multi_person_cake | 308 / 48283 / 1456 / 32 / 301 / 1468 / 300 / 31 |
| 26 | [0506] | 0x27fdd7c | 166 | AvoidDasimingStuckWall | 144 / 517 / 3744 / 3141 / 20 / 27756 / 40 / 16 |
| 27 | [0434] | 0x27f24b0 | 168 | ReturnCityBetween2TowerInEmyHigh | 128 / 61375 / 150 / 308 / 380 / 1008 / 32 / 24 |
| 28 | [0418] | 0x27f0000 | 170 | OptimizePosSkillWhenNotarget | 144 / 308 / 21 / 16 / 32 / 16959 / 1329 / 2924 |
| 29 | [0500] | 0x27fcd7c | 170 | avoid_v113_xuebao | 128 / 20 / 40 / 32 / 15 / 2632 / 22 / 4076 |
| 30 | [0508] | 0x27fe510 | 171 | AvoidFeiStuckWall | 96 / 542 / 3744 / 3141 / 20 / 27756 / 40 / 64 |
| 31 | [0304] | 0x27e5ed8 | 188 | laye_keep | 80 / 15 / 308 / 3844 / 32 / 344 / 16 / 548 |
| 32 | [0464] | 0x27f6690 | 190 | use_chuansong_skill_juexing | 288 / 64 / 80 / 769 / 768 / 104 / 14740 / 28087 |
| 33 | [0336] | 0x27e9154 | 193 | AtkUnit | 128 / 1380 / 3661 / 12 / 7664 / 131 / 11 / 3155 |
| 34 | [0314] | 0x27e70a8 | 202 | PushHeroOutofPortal | 176 / 24 / 308 / 58108 / 10500 / -46124 / 28 / 335609856 |
| 35 | [0498] | 0x27fc6b0 | 203 | UseV113Xuebao | 160 / 75 / 308 / 25404 / 1040 / 10000 / 28 / 20 |
| 36 | [0494] | 0x27fc210 | 213 | IsLinger | 308 / 32 / 3263 / 517 / 1825 / 14 / 665 / 533 |
| 37 | [0356] | 0x27eac44 | 220 | IsAroundDist | 128 / -6148914691236517206 / 43691 / 5848 / 18446744073709551614 / 12 / 24 / 35747 |
| 38 | [0236] | 0x27df394 | 221 | strategy_return_city | 80 / 1659 / 1118 / 3145 / 308 / 2372 / 2240 / 70 |
| 39 | [0362] | 0x27eb200 | 227 | MovePosWhenLongTimeOnlyAtk | 112 / 40 / 75 / 505 / 548 / 14 / 2633 / 30066 |
| 40 | [0414] | 0x27ef3c4 | 228 | CarryBombNearFrd | 240 / 41705 / 11 / 63 / 33 / 45 / 100 / 65 |
| 41 | [0442] | 0x27f2d2c | 228 | avoid_killed_by_organ | 20160 / 13 / 26 / 2632 / 2089 / 25 / 308 / 368 |
| 42 | [0242] | 0x27dfa28 | 247 | IsInEnemyHoverArea | 176 / 308 / 359 / 2422 / 32 / 24 / -6148914691236517206 / 43691 |
| 43 | [0492] | 0x27fbde0 | 268 | UseLubandashiSkill4HookEscape | 208 / 525 / 512 / 31 / 308 / 32 / 1008 / 536870904 |
| 44 | [0502] | 0x27fd0f8 | 281 | AvoidStuckDoor | 192 / 88 / 32 / 48 / 24 / 506 / 30 / 21 |
| 45 | [0468] | 0x27f7360 | 325 | transhold | 128 / 180 / 149 / 2632 / 3399 / 14 / 1659 / 917 |
| 46 | [0380] | 0x27ecc5c | 335 | GenMovePos | 192 / 575 / 354 / 2362 / 576 / 308 / 64 / 92 |
| 47 | [0480] | 0x27f8f78 | 369 | AvoidZhenjiBingZhu | 144 / 300 / 18725 / 62554 / 56 / 1008 / 188 / 32 |
| 48 | [0138] | 0x27c003c | 400 | linger_dir_adjust | 464 / 2372 / 1040 / 2444 / 2764 / 2664 / 2684 / 2396 |
| 49 | [0140] | 0x27cee04 | 400 | Process | 416 / 49 / 1568 / 65 / 308 / 1008 / 1659 / 2685 |
| 50 | [0150] | 0x27d5a90 | 400 | use_V112_dayu_chuansong | 176 / 188 / 1501 / 56928 / 202 / 16 / 1273 / 2632 |
| 51 | [0166] | 0x27d73e0 | 400 | InitSpecialPlaceStuckWall | 272 / 28 / 961 / 280 / 152 / 288 / 57 / 16 |
| 52 | [0320] | 0x27e7b1c | 400 | move_pos_when_struck_wall | 128 / 2872 / 41 / 30 / 151 / 542 / 548 / 18725 |
| 53 | [0404] | 0x27ee324 | 400 | UseMapSpecialSkill | 1408 / 14373 / 14304 / 14314 / 14364 / 3121 / 21 / 308 |
| 54 | [0416] | 0x27ef8b0 | 400 | GetBombRetreatPos | 256 / 2372 / 23 / 10000 / 41705 / 11 / 1008 / 45 |
| 55 | [0460] | 0x27f5430 | 400 | RideStateAction | 160 / 7385 / 3486 / 3355 / 13 / 1040 / 51711 / 2304 |
| 56 | [0474] | 0x27f8528 | 400 | yzj_skill1_stop_charge | 128 / 6764 / 541 / 506 / 534 / 548 / 542 / 15 |
| 57 | [0486] | 0x27f9cfc | 400 | UsingSanguoEquipSkill | 304 / 59642 / 43691 / 43690 / 21844 / 27 / 15 / 308 |
| 58 | [0490] | 0x27fac24 | 400 | UseWangWeiSkill3 | 176 / 138 / 7164 / 257 / 13832 / 31 / 354 / 3126 |
| 59 | [0504] | 0x27fd6a4 | 400 | attack_ding | 144 / 34079 / 1000 / 63 / 37 / 28 / 50 / 56928 |
