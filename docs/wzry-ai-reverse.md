# 王者荣耀离线对局 AI 逆向：调研与执行记录

> 目标：搞清楚"王者荣耀离线对局里的人机 AI 是怎么做决策的"，并把可复用的规则/阈值  
> 拿来改进本项目的 `WZRYagnt` 决策层（`wzry/decision/optimizer.py`）。
>
> 本文记录：已完成的逆向路径、关键证据、网上可用工具调研、以及下一步执行方案。
>
> **给后续 AI / 新会话**：直接跳到 **第十三节「实时内存路线与交接说明」** —— 那一节是  
> 自包含的交接文档（环境连接方式、三条路线的状态、精确下一步、踩坑清单、立即行动清单）。

---

## 一、结论速览

1. **游戏不是 UE4，是 Unity IL2CPP**（Unity 2022.3.5f1 / metadata v29）。原计划里的  
   "pak 解包 + AES key + Lua 提取"整条路线**不适用**，没有 `.pak`、没有加密 pak 索引。
2. **离线对局是真的一等公民**：客户端配置里有 `EnableOfflineGame_b: true`、  
   `IsLocalAgent_b`、`IsNeedConnectAIServer_b`，源码树里有整块 `OfflineGameplay/` 模块。  
   这解释了"断网 AI 照跑"。
3. **核心战术配置不随包**。全量 58 个 `.db` 资源容器逐字节检索：只有 `9/9_0.db`  
   带少量 `AILab/ai_config/*` 与 `multi_task_174_yuji.prototxt`；  
   `game_ai_model_config` / `tactics_config_file` / `AiMgr` / `ai_config_rl` / `ssg_config`  
   / `obstacle_config` / `skill_manager_config` / `hero_lane` **全无命中**，数据目录也没有  
   下载缓存。→ 这些由 AILab 训练管线/热更侧产出，**"抄配置"这条路走不通**。
4. **真正的决策逻辑编译进原生引擎 `libGameCore.so`**，是 \*\*规则集（RuleTactics）
   - 神经模型概率选择（GameTacticsManager）\** 的混合体。
5. **已经把这些决策函数的代码地址钉出来了**（见第七节地址表），可以定向反汇编，  
   不需要对整个 43.7MB 的 `.text` 做全量分析。
6. **规则链已完整还原**：`RuleTactics` 走一张全局函数指针表（基址 `0x509c000`），  
   该表在文件里全是 0、由加载器填充；改从 `.rela.dyn` 读 `r_addend` 后，  
   还原出 **141 个具名规则函数**（见第八节）。
7. **两份可执行产物已落地**：`wzry/decision/official_rules.py`（官方常量与谓词）
   - `optimizer.py` 三处接线（见第十、十一节），并已通过冒烟测试。
8. **运行时实时读取已验证可行**（Route C，见**第十三节**）：guest 内存可直读，  
   静态偏移与运行时地址 1:1 换算。第十三节是**给后续 AI 的交接说明**，  
   含三条路线（A/B/C）的状态、卡点、命令与必避的坑。

---

## 二、已完成的逆向路径

### 2.1 环境与取数

| 项    | 值                                                                                                                     |
| ---- | --------------------------------------------------------------------------------------------------------------------- |
| 运行环境 | MuMu Player 12，Android 15，ABI `x86_64`（arm64 应用走翻译）                                                                   |
| adb  | `D:\app\MuMuPlayer\nx_main\adb.exe`，连接 `127.0.0.1:16384`                                                              |
| root | `adb root` 可用（uid=0），可读 `/data/data`                                                                                  |
| 包名   | `com.tencent.tmgp.sgame`                                                                                              |
| APK  | `/data/app/~~mVHdouW8UqoFTOeUr9yNGQ==/com.tencent.tmgp.sgame-RVybgyKqTGOvcl0htOzX6A==/base.apk`（单包 1.748 GB，1567 个条目） |

关键点：**MuMu 实例默认不启动**，需 `MuMuManager.exe control --vmindex 0 launch`，  
其 adb 端口由 `MuMuManager.exe adb -v 0` 返回。

### 2.2 构建形态判定

APK 顶层结构（按体积）：

| 文件                                                     | 体积      | 说明                      |
| ------------------------------------------------------ | ------- | ----------------------- |
| `assets/bin/Data/Package/builtin/9/9_0.db`             | 837 MB  | 资源容器（数据段）               |
| `assets/bin/Data/Package/builtin/0/0_0.db`             | 223 MB  | 资源容器                    |
| `lib/arm64-v8a/libil2cpp.so`                           | 217 MB  | IL2CPP 编译后的 C# 代码       |
| `assets/bin/Data/Package/builtin/3002/3002_0.db`       | 210 MB  | 资源容器                    |
| `lib/arm64-v8a/libGameCore.so`                         | 82 MB   | **原生战斗引擎（AI 所在）**       |
| `assets/bin/Data/Managed/Metadata/global-metadata.dat` | 57.5 MB | IL2CPP 元数据（全部类型/方法/字段名） |
| `lib/arm64-v8a/libpuerts.so`                           | 14 MB   | PuerTS（TypeScript 脚本支持） |

**包内没有任何 C# DLL**（`Managed/` 下只有 `Resources/*.dat` 和元数据），  
所以类名/方法名只能从 `global-metadata.dat` 恢复——这也是下面脚本做法的依据。

### 2.3 从元数据恢复源码树（关键一招）

`global-metadata.dat` 会把每个类型的**定义文件路径**（构建机上的绝对路径）作为字符串保留。  
把它抽出来，等于免费拿到整棵源码树：

```
共 13083 个 .cs 路径，根为：
D:/Agent1/agent/v4/proj/VersionBranch_v121_publish_20260828/Program/GameScripts/
  ├── GameCoreScripts/Shared/GameSystem/...
  ├── BaseScripts/Domestic/GameSystem/OfflineGameplay/...
  ├── SystemScripts/Domestic/GameSystem/...
  └── ...
```

跟 AI/离线相关的文件（节选）：

| 路径                                                                          | 含义                  |
| --------------------------------------------------------------------------- | ------------------- |
| `BaseScripts/Domestic/GameSystem/OfflineGameplay/COfflineGamePlaySystem.cs` | 离线对局系统              |
| `.../OfflineGameplay/OfflineGameMatchData.cs`                               | 离线对局数据（含 `AILevel`） |
| `.../OfflineGameplay/COfflineHeroSelectData.cs`                             | 离线选将                |
| `.../Player/AI/CRobotPlayerProxy.cs`                                        | 机器人玩家代理             |
| `.../Player/AI/FakeAIPlayerInfo.cs`                                         | 伪 AI 玩家信息           |
| `SystemScripts/.../Player/AI/CAIPlayerSystem.cs`                            | AI 玩家系统             |
| `GameCoreScripts/.../Battle/CommanderMode/CCommanderJueWuOpeningSystem.cs`  | 指挥官模式（绝悟）           |
| `SystemScripts/.../TrainEntry/HeroPracticeCourt/CAICommanderSystem.cs`      | 英雄练习场 AI 指挥官        |
| `BaseScripts/Domestic/GRobot/GRobotEngine.cs`                               | GRobot 大厅助手         |

> ⚠️ 防误判：`GameCoreScripts/.../AutoPerformance/BehaviorTree/APAILogic.cs` 是**画质自适应 AI**  
> （内含 KMeans / RandomForest），跟对局无关；`CBattleAIVoice*` 系列是**对局语音播报**，  
> 也不是决策。C# 层只有代理/簿记类，真决策在原生引擎。

### 2.4 离线对局的直接证据

`assets/Config/GameModeConfig_VAI.json`：

```json
{
  "EnableOfflineGame_b": true,
  "IsLocalAgent_b": false,
  "IsNeedConnectAIServer_b": false,
  "ServerAIRequestFreq_i": 2,
  "LobbyOfflineAIVoice": "...",
  "EnableFastRecover_b": false
}
```

运行时日志文件 `files/SGAME_OFFLINE_LOG_FILE_NAME` 反序列化出的 .NET 类型：

```
Assets.Scripts.GameSystem.OfflineGameMatchData+stOffilneLog
字段：gameSeq startTime playTime bWin bSurrender bSurrenderTenMin mapId heroList AILevel
```

→ 离线对局有独立的战绩记录结构，且带 **AILevel（人机难度等级）**。

### 2.5 资源容器（`.db`）结论

- 结构是 `<id>.db`（索引段）+ `<id>_0.db`（数据段）成对出现。
- 头部是自定义格式（8 字节魔数 `01 00 00 02 01 02 03 04` + 若干偏移/尺寸字段），  
  **没有** `UnityFS` / `CAB-` 签名，但内部有 **30029 处** `2022.3.5f1`（Unity 版本串），  
  说明内部是大量 Unity SerializedFile 记录。部分文本资产（TextAsset）内容**明文可见**。
- 全量 58 个容器检索结果：

| 关键字                                                                                                                                                      | 命中                   |
| -------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------- |
| `AILab/ai_config`                                                                                                                                        | 仅 `9/9_0.db`（14 条路径） |
| `multi_task` / `prototxt`                                                                                                                                | 仅 `9/9_0.db`         |
| `game_ai_model` / `tactics_config_file` / `AiMgr` / `ai_config_rl` / `ssg_config` / `obstacle_config` / `skill_manager_config` / `hero_lane` / `weights` | **全部无命中**            |
| 数据目录中下载的配置                                                                                                                                               | 无命中                  |

包 9 内实际存在的 AILab 路径（14 条）：

```
AILab/ai_config/5v5/tactics/feature/hero_main_job.txt
AILab/ai_config/5v5/tactics/feature/hero_ep_dict.txt
AILab/ai_config/5v5/tactics/feature/meta_info_feature.txt
AILab/ai_config/5v5/common/bush.txt
AILab/ai_config/5v5/common/bush_small_map.txt
AILab/ai_config/5v5/ssg/scr/att_scripts/1.txt  |  2.txt
AILab/ai_config/5v5/speak/config/{passive_local_scene,passive_rsp_mobile,passive_rsp_train,unit_cid}.txt
AILab/ai_config/ai_server/pb2struct/bush.txt
AILab/ai_/5v5/tactics//multi_task_174_yuji.prototxt + .weights
```

已明文恢复的一段配置内容（键值格式）：

```
main_job=AILab/ai_config/5v5/tactics/feature/hero_main_job.txt
ep_type=AILab/ai_config/5v5/tactics/feature/hero_ep_dict.txt
bush_file=AILab/ai_config/ai_server/pb2struct/bush.txt
#main_hero_type=default
#default_main_runtime_id=25
```

### 2.6 原生引擎侧的 AI 结构

`libGameCore.so`（86,426,392 字节）：

- **`.symtab` 已剥离**，仅剩 2498 个 `.dynsym` 动态符号，其中**没有任何 AI 相关函数名**。
- 段布局：`.rodata` @ `0xfbe3a0`（2.5 MB）、`.text` @ `0x2185770`（**43.7 MB**，约 1093 万条指令）。

字符串里暴露的 AI 组织方式：

| 类别     | 名称                                                                                                                                                                                                                                                                                                                                                                                           |
| ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 规则集    | `RuleTactics` 及几十条具名规则：`AvoidKilledByOrgan`、`AvoidKilledBySoldier`、`KeepAwayFromEnemyTower`、`GotoTargetPosInSpring`、`GotoTargetPosWhenFirstBorn`、`ReturnCityBetween2TowerInEmyHigh`、`UseChuanSongSkill`、`StayWithMonster`、`StayWithMonsterUsingSkill`、`Use10v10LaneRule`、`CarryBombNearFrd`、`FrdFzDonotTakeBloodPack`、`ProcessLingerInSpring`、`IsInEnemyHoverArea`、`LeaveCake`、`PaoDuRule` … |
| 战术选管   | `GameTacticsManager`、`GameTacticsMaster`、`CommonTactics`、`RuleTactics`、`TacticsRetreat`、`TacticsAttackMonster`、`TacticsMultiTaskTwoHandAction`                                                                                                                                                                                                                                               |
| 目标选择   | `AITargetSearcher::GetNearestEnemy`                                                                                                                                                                                                                                                                                                                                                          |
| 节点式 AI | `AIFallbackNode`、`AIPeriodTriggerNode`、`AIBlueprintEventHandler`、`AIEventCollector`                                                                                                                                                                                                                                                                                                          |
| 官方试炼场  | `AITrialManager`、`AITrialHuicheng`（回城）、`AITrialGuaji`（挂机）、`AITrialFeedTower`（送塔）、`AITrialBeKilledByHeroInBush`（草丛被杀）、`AITrialEuipment`                                                                                                                                                                                                                                                       |
| 模型     | Caffe 风格 `multi_task_*.prototxt` + `.weights`（每英雄一个，如 `174_yuji` 虞姬）                                                                                                                                                                                                                                                                                                                         |
| 配置     | `AILab/ai_config/...`、`ai_config_rl/5v5/game_ai_model_config.txt`                                                                                                                                                                                                                                                                                                                            |

算法线索（日志串直接透露）：

```
[SelectTactics] max prob:%f, min prob score:%f, select:%d
[SelectTactics] model result size: %d
[CurrentTacticsCheck] tactics_size:%d select_tactics:%d rank:%d grid_tactics_state:%d
                     arrived_strategy_target:%d enemy_hero_min_dis:%d
[FinalSelectTactics] rank:%d grid_tactics_state:%d arrived_strategy_target:%d enemy_hero_min_dis:%d
[Last Tactics Execute] tactics size:%d, select tactics:%d
```

→ 结构判断：**规则产生候选战术 → 模型给每个战术打分（概率）→ 按概率/分数选一个执行**，  
是"规则 + 学习模型"的混合系统（与绝悟纯 RL 不同，但同源：都用 `max prob` 选择）。

---

### 2.7 取包与解包：完整命令 + 已解出的内容清单

#### A. 取包（复现命令）

```powershell
$adb = "D:\app\MuMuPlayer\nx_main\adb.exe"
$S   = "127.0.0.1:16384"
$rev = "d:\work\aiKings\WZRYagnt\reverse"

# 1) 找 APK 真实路径（分片包会返回多个；本包是单包 base.apk）
& $adb -s $S shell pm path com.tencent.tmgp.sgame

# 2) 列出 APK 内全部条目（1567 条），存成本地清单
& $adb -s $S shell "unzip -l <base.apk 路径>" | Out-File -Encoding utf8 "$rev\apk_list.txt"

# 3) 只抽需要的条目：设备端 unzip + adb pull（避免拉整包 1.75GB）
& $adb -s $S shell "cd /data/local/tmp && unzip -o '<base.apk>' \
   'lib/arm64-v8a/libil2cpp.so' 'lib/arm64-v8a/libGameCore.so' \
   'assets/bin/Data/Managed/Metadata/global-metadata.dat'"
& $adb -s $S pull /data/local/tmp/... "$rev\out\il2cpp"
```

要点：**APK 就是普通 zip**（没有 AES 加密的 `.pak`，也不是 UE4），  
设备自带 `unzip / zipinfo / xxd / dd`，所以全程"设备端解压 → 只 pull 需要的文件"。  
整包 1.748 GB / 1567 条，**不要 pull 整包**。

#### B. 已解出的内容清单（全部落在 `reverse\out\`）

| 产物                                                | 位置                                             | 体积                                                                                                             | 说明 / 结论                                                                                                                                                              |
| ------------------------------------------------- | ---------------------------------------------- | -------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `apk_list.txt`                                    | `reverse\`                                     | 1567 行                                                                                                         | APK 全条目清单（顶层结构分析依据）                                                                                                                                                  |
| `libil2cpp.so`                                    | `out\il2cpp\lib\arm64-v8a\`                    | 217.1 MB                                                                                                       | C# 侧代码（IL2CPP）                                                                                                                                                       |
| `libGameCore.so`                                  | 同上                                             | 82.4 MB                                                                                                        | **原生战斗引擎：对局 AI 就在这**                                                                                                                                                 |
| `global-metadata.dat`                             | `out\il2cpp\assets\bin\Data\Managed\Metadata\` | 57.5 MB                                                                                                        | 全部类型/方法/字段名                                                                                                                                                          |
| `libgrobot.so`                                    | `out\apk\rev\lib\arm64-v8a\`                   | 18.6 KB                                                                                                        | 机器人 SDK 的 JNI 壳（**不是**对局 AI）                                                                                                                                         |
| `GRobotSDK_NBPackage.zip`                         | `out\apk\rev\assets\`                          | 472.2 KB                                                                                                       | 大厅 AI 助手资源包                                                                                                                                                          |
| `out\grobot\`（解压后）                                | —                                              | `index.android.js` 489KB、`vendor.android.js` 212KB、`module_config.json`、`vendor-manifest.json`、87 张 `v4-*.png` | **GRobot = 游戏内 AI 对话助手（React Native）**，与对局 AI 无关                                                                                                                     |
| `assets\Config\*`                                 | `out\apk\rev\assets\Config\`                   | 各 0.1–10 KB                                                                                                    | `GameModeConfig.json` / `_VAI.json` / `GameModeConfigConstraint.json` / `LivecastConfig.json` / `OfflineRenderSetting.config` / `Version*.txt` / `so_load_order.txt` |
| `assets\LevelTemplate\*`                          | `out\apk\rev\assets\LevelTemplate\`            | 1–36 KB                                                                                                        | 关卡模板：`Level.lv`、`Design\Design.json`、`Blueprint\*.gl`                                                                                                                |
| 资源容器 `.db`                                        | `out\db\builtin\`、`out\db2\`                   | 见下 C 节                                                                                                         | 索引段 + 数据段成对                                                                                                                                                          |
| `SGAME_OFFLINE_LOG_FILE_NAME`（头部）                 | 设备 `files\`                                    | 256 KB                                                                                                         | 反序列化出 `OfflineGameMatchData` 字段名（含 **`AILevel`**）                                                                                                                    |
| `OfflineHerpOpSetting.bytes`                      | 设备 `files\`                                    | 8 B                                                                                                            | `08 00 00 00 00 00 00 00`                                                                                                                                            |
| `runtime_GameModeConfig.json`、`custom_config.dat` | `out\cfg\`                                     | 2.2 KB / 529 B                                                                                                 | 运行时配置副本（与 APK 内 **MD5 一致**）；服务端下发配置                                                                                                                                  |
| `maps_<pid>.txt`                                  | `out\mem\`                                     | 7384 行                                                                                                         | 游戏进程内存映射（Route C 的依据）                                                                                                                                                |

#### C. 资源容器 `.db` 的解包结论（**重要：没有写成功的解包器**）

- **形态**：`<id>.db`（索引段）+ `<id>_0.db`（数据段）成对；头部 8 字节魔数  
  `01 00 00 02 01 02 03 04` + 若干偏移/尺寸字段，**没有 `UnityFS` / `CAB-` 签名**。
- **内部**：约 **3 万个 Unity SerializedFile**（版本串 `2022.3.5f1` 在 `9_0.db` 里出现  
  **30,029** 次），部分 TextAsset 内容**明文可见**——已恢复出这样一段：
  ```
  main_job=AILab/ai_config/5v5/tactics/feature/hero_main_job.txt
  ep_type=AILab/ai_config/5v5/tactics/feature/hero_ep_dict.txt
  bush_file=AILab/ai_config/ai_server/pb2struct/bush.txt
  #main_hero_type=default
  #default_main_runtime_id=25
  ```
- **已拉下来的容器**：`9.db + 9_0.db`(857 MB)、`0_0.db`(223.6 MB)、`3002_0.db`(209.6 MB)、  
  `9900_0.db`(87.5 MB)、`320010(+_0)`、`305000000145(+_0)`、`305000000071(+_0)`、  
  `5100(+_0)`、`99(+_0)`；设备上还把**全部 58 个 `.db`** 解到 `/data/local/tmp/db3`（1.3 GB）  
  做过逐字节 grep。
- **结论**：核心 AI 配置（`game_ai_model_config` / `tactics_config_file` / `AiMgr` /  
  `ai_config_rl` / `ssg_config` / `obstacle_config` / `skill_manager_config` / `hero_lane` /  
  `weights`）**不在任何容器里**，数据目录也没有下载缓存 → 由 AILab 训练管线/热更侧产出。
- **未完成**：`.db` 的**分段器没有实现**（需按 Unity SerializedFile 头切分内部对象），  
  UnityPy / AssetStudio 因此也没被真正跑通。因为"核心配置不在包内"已确认，这条线**主动中止**。  
  若后续确实要取包内那 14 个 `AILab/ai_config/*` 与 `multi_task_174_yuji.prototxt`，  
  才需要补一个分段器（按 `2022.3.5f1` 头切段 → 交给 UnityPy）。


## 三、网上可用工具调研

### 3.1 资源/容器解包类

| 工具 | 能力 | 对本项目 |
| --- | --- | --- |
| **UnityPy**（Python） | 读 AssetBundle / SerializedFile，批量提取 TextAsset/贴图/音频；支持 `UnityCFS` 解密、修复 AssetRipper 的 Unreadable 资源 | ✅ 首选。容器解析我可以自写索引 + 交给 UnityPy 读 SerializedFile |
| **AssetStudio**（Perfare） | 浏览/提取/导出 assets 与 assetbundles，内存直读 | ⚠️ 需先切出合法的 SerializedFile/AB 段 |
| **AssetRipper** | Unity 3.0+ 全版本，GUI，转回原生 Unity 工程 | ⚠️ 同上，且更重 |
| **UABE**（Unity AssetBundle Extractor） | 单资源级查看/替换 | ⚠️ 同上 |
| **WebExtract + Binary2Text**（Unity 官方 `Editor/Data/Tools`） | 拆 AssetBundle、把二进制 SerializedFile 转文本（需打进 TypeTree） | ⚠️ 需 TypeTree |

### 3.2 IL2CPP 元数据类

| 工具 | 能力 | 对本项目 |
| --- | --- | --- |
| **Il2CppDumper**（Perfare，Windows exe） | 由 `libil2cpp.so` + `global-metadata.dat` 恢复全部类型/方法/字段与地址；生成 IDA/Ghidra/Binary Ninja 脚本 | ✅ **必用**。支持 Unity 5.3–2022.2、metadata v29 |
| **Il2CppInspector / Il2CppInspectorRedux**（LukeFZ） | Il2CppDumper 失败时的替代；输出更丰富的 C++ 头 + Ghidra/IDA 脚本；**不是反编译器**，给的是结构与函数地址 | ✅ 备选/互补 |
| **il2cpp_dumper**（Rust crate `il2cpp_dumper`） | 并行生成 DummyDLL / `il2cpp.h` / `script.json` | ⚠️ 可选 |
| **Zygisk-Il2CppDumper** | 运行时 hook dump（元数据被加密时用） | ❌ 不需要，我们的元数据是明文 |

> 注意：这些工具解决的是 **`libil2cpp.so`（C# 侧）**。而王者的对局 AI 在
> **`libGameCore.so`（原生，非 IL2CPP）**，所以它们**不是关键路径**，只用于把
> `CAIPlayerSystem` / `CRobotPlayerProxy` 等 C# 代理层看清楚。

### 3.3 原生二进制（ARM64）分析与辅助

| 工具 | 能力 | 本项目可用性 |
| --- | --- | --- |
| **Ghidra**（NSA，免费） | ARM64 反汇编 + 反编译，脚本化；可直接定位任意地址 | ✅ **主力**。把第七节地址表喂进去做定向分析 |
| **IDA Pro**（商业） | 最强反编译器（Hex-Rays），ARM64 支持好 | ✅ 有授权则更省事 |
| **Binary Ninja**（商业） | 反编译 + 脚本；`WARP`/`SigKit` 用已知函数库识别未知函数 | ✅ 若已有授权 |
| **radare2 / rizin** | 开源，CLI + 脚本，headless 友好 | ⚠️ 适合批处理 |
| **REA**（`morluto/rea`，你给的 `Opiuz/REA` 是其镜像） | Agent 外壳，把 Hopper/Ghidra 包成 MCP 工具 | ❌ **Windows 原生二进制分析不可用**（要求 macOS 12+/Linux；Windows Ghidra 支持标注为 experimental/unavailable） |
| **kong-re**（PyPI） | 面向 stripped 二进制的自动化管线：函数分诊 → 调用图 → LLM 引导反编译恢复类型/符号 → 回写 Ghidra 工程 | ✅ 值得一试，正好匹配"strip + 无符号"场景 |
| **GhidrAssist**（Ghidra 插件） | 把 Ghidra 接到 LLM（OpenAI/Anthropic/本地 Ollama），对选中函数做上下文分析 | ✅ 轻量、见效快 |
| **IDA llm-explainer**（Hex-Rays 插件） | 单函数/批量 LLM 解释，自动重命名 | ✅ 有 IDA 时 |
| **LLM4Decompile / CHISEL**（论文级） | 用 LLM 反编译 / 迭代恢复源码（编译反馈 + fuzz 差分） | ⚠️ 研究性质，可作参考思路 |
| **castone/angr 等自写脚本** | 定向反汇编、字符串 xref、伪代码还原 | ✅ 本项目已用（见第五节脚本） |

### 3.4 明确不适用

| 工具 | 原因 |
| --- | --- |
| **ASC / droidasc** | 只处理 **DEX/Java 层**。王者 AI 不在 DEX 里，用它只能翻出登录 SDK、反作弊（`libtersafe`/`libTPCore`）胶水 |
| **REA** | Windows 无法跑原生二进制分析；且它只是 Hopper/Ghidra 的外壳，直接装 Ghidra 效果相同 |
| **Lua 反编译工具**（luatool/unluac 等） | 待验证：源码树里有 `Pandora/Slua` 绑定，但容器里未见 `.lua` 文件，暂不作为路径 |

---

## 四、推荐技术栈与执行顺序

```
① 容器/资源侧（可选，收益有限）
   自写 .db 分段器 → UnityPy 提取 TextAsset
   → 目标：包 9 里那 14 个 ai_config + multi_task_174_yuji.prototxt/weights
   → 预期：只有 feature 表，没有核心战术表（已确认未随包）

② 引擎侧（主线，收益最高）
   已有：xref 地址表（第七节）
   → Ghidra（或 capstone 自写脚本）定向反汇编 5 个函数簇
   → 目标：RuleTactics 规则条件、选战术打分公式、阈值、锁敌准则
   → **已完成**：规则集与锁敌（capstone 自写脚本路线，见第八/九节）；选战术打分待做

③ C# 代理层（辅助，低成本）
   Il2CppDumper 跑出 dump.cs
   → 看 CAIPlayerSystem / CRobotPlayerProxy / OfflineGameMatchData 的字段与方法
   → 目标：确认 AI 等级(AILevel)如何影响参数、离线对局如何创建

④ 运行时验证（可选）
   root + 内存 dump：进入离线对局后，在游戏进程内存里搜已加载的
   ai_config / 模型输出，直接读运行期真实参数
```

---

## 五、本项目已产出的脚本与数据

目录：`d:\work\aiKings\WZRYagnt\reverse\`

| 脚本 | 作用 |
| --- | --- |
| `scan_metadata.py` | 从 `global-metadata.dat` 抽全部字符串 → `metadata_strings.txt`（62.3 万唯一符号） |
| `extract_src_paths.py` | 从符号里抽 `.cs` 源码路径 → `src_paths.txt`（13083 条） |
| `so_strings.py` | 任意二进制抽字符串 → `*.strings.txt` |
| `elf_sections.py` / `elf_symbols.py` | 列 ELF 段表 / 导出 `.dynsym` |
| `find_bytes.py` / `search_bin.py` | 二进制关键字检索（含偏移） |
| `ctx_dump.py` | 转储指定偏移的十六进制 + 可打印串 |
| `find_bundles.py` | 扫 UnityFS 签名 |
| `scan_ai_paths.py` / `scan_containers.py` | 枚举容器内 AILab 路径 / 批量扫描关键字 |
| `probe_asm.py` | 打印指定偏移处的 ARM64 指令（用于校验解码器行为） |
| `xref_strings.py` | **ARM64 字符串交叉引用定位器**（ADRP+ADD 链还原 → 反查日志串引用点） |
| `func_dump.py` | 函数级反汇编 + 注解（入口由 `bl` 调用图反推；标注字符串与调用目标） |
| `targets_ai.txt` | AI 关键字面量清单 |
| `dump_ptrtable.py` | 直接读 `.data` 里的函数指针表（**结果是全 0**，见第八节） |
| `dump_reloc_ptrs.py` | **从 `.rela.dyn` 还原运行时指针表**（读 `r_addend`，关键突破） |
| `scan_rules.py` | 批量扫规则函数 → 规则名/地址/常量/浮点表 |
| `tactics_rules.py` | 按"引用点代码区间"筛出 RuleTactics 规则链 → 具名规则表 |
| `elf_sections.py` / `elf_symbols.py` | 列 ELF 段表 / 导出 `.dynsym` |

主要数据产物：`reverse\out\il2cpp\` 下
`global-metadata.dat` / `libil2cpp.so` / `libGameCore.so` /
`metadata_strings.txt` / `src_paths.txt` / `gamecore.strings.txt` /
`gamecore.dynsym.txt` / `gamecore.xref.txt`；
`reverse\out\apk\rev\` 下 `GRobotSDK_NBPackage.zip` 与 `Config\*`；
`reverse\out\grobot\` 下 `index.android.js` / `vendor.android.js`；
资源容器在 `reverse\out\db\`、`reverse\out\db2\`。

本轮新增产物：`reloc_509c000.txt`（259 条规则地址）、`reloc_5142000.txt`、
`reloc_508f000.txt`、`rules_table.tsv`、`tactics_rules_rt.tsv`（**141 条具名规则**）、
`func_ruletactics.txt`、`func_getnearestenemy.txt`、`deep_getnearestenemy.txt`、
`deep_search_exec.txt`、`rule_gototargetpos_firstborn.txt`、`rule_checkreturncity.txt`、
`rule_staywithmonster.txt`。

### 踩坑记录（复用必读）

- **capstone 默认遇到无法解码的字节会终止整个反汇编**。第一次扫全 `.text` 只得到 4 个
  `adrp`（真实值 737,092 个），加 `md.skipdata = True` 才正常。
- Python 在 Windows 写文件会把 `\n` 转成 `\r\n`，导致 ripgrep 的 `$` 锚点失效；
  写索引文件时应显式 `newline="\n"`。
- capstone 的 `adrp` 操作数**直接给绝对页地址**（如 `#0x4b43000`），不是相对立即数，
  计算目标地址时不要再叠加 PC。
- MuMu 默认不启动实例；`adb devices` 空不代表环境坏了。
- **`.data` 里的函数指针表在文件中全为 0**，值由加载器在启动时写入。
  直接 dump 只会得到一串零（不是脚本错）；必须改读 `.rela.dyn` 中
  `R_AARCH64_RELATIVE`(1027) 的 `r_addend`。
- **`0x509c000` 是混合注册表**（被 591 个槽位引用，混着 JS 引擎/测试框架/`AITrial`）。
  不按"引用点所在代码区间"过滤，得到的就是噪声，不是规则链。

---

## 六、核心结论（面向本项目决策层）

1. 不要指望"抄官方配置表"——核心战术表与模型**不随包、不落盘**。
2. 官方对局 AI 是**规则集 + 模型概率选择**，这与本项目现有
   `wzry/decision/optimizer.py`（价值场密采样 + 候选枚举 + argmax）
   以及 `hok-decision-ai`（束搜索 + 线性价值 + 硬规则掩码）的架构**同源但更细**。
3. 最值得抄的是**规则清单本身**（`RuleTactics` 的几十条具名规则就是"该做什么"的目录），
   以及**选战术的打分维度**（`grid_tactics_state` / `arrived_strategy_target` /
   `enemy_hero_min_dis` 这三项是官方日志里明确打印的判据）。
4. 合规提醒：包内存在 TP/ACE 反作弊（`libtersafe`、`libTPCore`、`libtersafe` 等）。
   **只做静态只读分析，不要改客户端、不要注入线上对局**，否则有封号风险。

---

## 七、AI 决策函数地址表（`xref_strings.py` 产出）

由 `xref_strings.py` 全量扫描（1093 万条指令 / 737,092 条 `adrp`）得到：

| 命中串 | 引用点（虚拟地址） | 归属 |
| --- | --- | --- |
| `[GetTacticsConfigList]` | `0x2557304` | 取战术配置表 |
| `[Last Tactics Execute]` | `0x2556d74` | 上一帧执行结果 |
| `[CurrentTacticsCheck]` | `0x25565ac` | **当前战术校验（打分维度在此打印）** |
| `[FinalSelectTactics]` | `0x25571cc` | **最终选择** |
| `[SelectTactics]` | `0x2665c40`、`0x2756f08` | **战术概率选择（max prob 在此）** |
| `[InitHeroTacticsInfo]` | `0x2663690`、`0x2754930` | 英雄战术初始化 |
| `RuleTactics->Process` | `0x27d1614` | **规则集主入口** |
| `RuleTactics::ProcessLingerInSpring` | `0x27d6498` | 泉水滞留规则 |
| `RuleTactics::KeepAwayFromEnemyTower` | `0x27dbd98` | 远离敌塔规则 |
| `AITargetSearcher::GetNearestEnemy` | `0x34e037c`、`0x3acfa48` | **锁敌** |
| `AITrialHuicheng` | `0x250f1a4` | 官方回城试炼 |
| `AITrialGuaji` | `0x4952948` | 官方挂机试炼 |
| `AITrialFeedTower` | `0x24df098` | 官方送塔试炼 |
| `AILab/ai_config` | `0x2c303cc` | **配置加载（可验证"读哪些路径"）** |

建议的啃读顺序：`0x27d1614`（规则主流程）→ `0x34e037c`（锁敌）→
`0x25565ac`/`0x25571cc`/`0x2665c40`（选战术与打分）。
**进度**：前两项已完成（结果见第八、九节）；选战术三项仍待做。

---

## 八、规则表还原：从 `.rela.dyn` 拿到 141 条具名规则

### 8.1 规则不是硬编码分支，而是查表调用

`RuleTactics` 按固定顺序调用一张全局函数指针表：

```
adrp x8, #0x509c000
ldr  x8, [x8, #0x9f0]        ; 取第 n 条规则的函数指针
mov  x0, x19 ; mov x1, x20 ; mov x2, x21   ; 统一传 (ctx, arg1, arg2)
blr  x8                      ; 调用该规则
```

但这张表在**文件里全是 0**——由加载器启动时填充。所以第一步用
`dump_ptrtable.py` 直接读 `.data` 得到的是一串零（**这是正常的，不是脚本写错**）。

### 8.2 正确姿势：读重定位项的 `r_addend`

`.rela.dyn`（本项目 678,517 项）里，`R_AARCH64_RELATIVE`(1027) 的语义是
"把 `base + r_addend` 写进 `r_offset`"——**目标地址就存在 `r_addend` 里**，
因此不需要运行游戏即可读出。`dump_reloc_ptrs.py` 按
`r_offset ∈ [基址, 基址+count×8)` 过滤后直接输出每条槽位指向的函数地址。

### 8.3 必须避开的坑：`0x509c000` 是混合注册表

该基址被全引擎 **591 个槽位**引用，里面混着 JS 引擎、测试框架、`AITrial*` 等条目。
**必须按"引用点所在代码区间"过滤**（本项目取 `0x27d0000–0x27dc000` 只保留
RuleTactics 自己的调用），过滤后得到 **172 个槽位 / 141 个具名规则函数**。

### 8.4 规则链（141 条，按用途分组）

| 用途 | 规则（括号内为入口地址，未注者见 `.tsv`） |
| --- | --- |
| 回城/泉水 | `GotoTargetPosInSpring` / `GotoTargetPosWhenFirstBorn`(0x27dd40c) / `CheckReturnCity`(0x27df394) / `ReturnCityInHaiYueSkill3Alone` / `ReturnCityBetween2TowerInEmyHigh` / `IsAttackSoldierReturnCity` / `MoveStopInSpringWhenRevival` / `stay_in_spring` / `ProcessLingerInSpring`(0x27d6498) |
| 危险规避 | `IsInDangerousPlace`(0x27db2ac) / `DangerousPlaceRetreat` / `AvoidKilledByOrgan` / `IsInEnemyHoverArea` / `AvoidNezhaSkill3` / `RemoveShouyueEyes` / `AvoidZhenjiBingZhu` / `AvoidZhuBaJieSkill3` / `CarryBombNearFrd` / `AttackDayuDing` / `AvoidStuckDoor` / `AvoidDasimingStuckWall` / `AvoidFeiStuckWall` / `RuleStuckWallV3` / `IsStuckWall` |
| 草丛/传送/卡位 | `PushHeroOutofBush` / `NotMoveWhenUsingPortal` / `PushHeroOutofPortal` / `AttackHeroInPortal` / `TransportHold` / `UseChuansongSkill`(+`Juexing`/`V102`/`V112DaYuChuanSong`) / `UseKongkongerTeleport` / `MoveIfLongtimeStopIngrass` / `IsLongtimeStopIngrass` |
| 资源/兵线/中立 | `FrdFzDonotTakeBloodPack` / `LeaveCake` / `SelectCake` / `UseMultiPersonCake` / `StayWithMonster`(0x27e5438) / `StayWithMonsterUsingSkill` / **`game_ai_offline::RuleTactics::IsLingerAroundDragon`** / `AtkSoldierInFrdHigh` / `UseZhanjudianRule` / `Use10v10LaneRule` |
| 技能决策 | `CheckAkeUseSkill3` / `UseHanbingChengji` / `UseChengji` / `CheckUseZhongjie` / `CheckUseZhiliao` / `UseMapSpecialSkill` / `UsingSanguoEquipSkill` / `UseSunquanSkill3` / `UseWangWeiSkill3` / `UseLubandashiSkill4HookEscape` / `UseV113Xuebao` / `RideStateAction` / `CancelAilinSkill3` |
| 态势/其他 | `SituationAssessmentDangerous` / `Prepare` / `InitParaInt` / `SelectTargetInRange` / `SetActionReturnCity` / `TacticsMultiTaskTwoHandAction` / `NotargetOptimizeProcess` / `LongTimeMoveStopRule` / `LongTimeOnlyAtk` / `WaitResurrectionArmor` |

完整表（含每条地址、指令数、日志标签、常量）见 `reverse/out/il2cpp/tactics_rules_rt.tsv`。

> 旁证：`game_ai_offline::` 命名空间的存在，再次确认离线 AI 是官方一等公民。

### 8.5 RuleTactics 主流程的调用顺序

调度器位于 `0x27d0e14–0x27d1310`，按固定顺序取槽位调用：

```
0x9f0, 0xb30, 0xb70, 0xbd0, 0xad0, 0x9b0, 0x9d0, 0x9e0, 0x9c0, 0xa10, 0xa20,
0xc20, 0xa00, 0xae0, 0xaf0, 0xa30, 0xf20, 0x4a0, 0xa60, 0x4b0, 0xb00, 0xb40,
0xbc0, 0x910, 0x9a0, 0xb50, 0xb60, [0xd40 条件], 0xe40, 0xe60, 0xdf0, 0xe00,
0xe10, 0xda0, 0xde0, 0xe20, 0xe80, 0xdb0, 0xdd0, 0xdc0, 0xe30, 0xe50, 0xe70
```


返回值按位判读（`tbz w0,#0` / `and w0,#1`），结果写回 ctx 的
`+0x1040` / `+0x1044` / `+0x1700` / `+0x1780`。

---

## 九、定点反汇编结果

### 9.1 规则案例：`GotoTargetPosWhenFirstBorn`（`0x27dd40c`）

```
cmp w8, #0x1c3 (451)   → 超时中止（日志 "after 30s, stop"）
参数 == -1             → "no target pos, stop"
当前动作 != 2          → "not move action, stop"
最近敌距 < 0x12d (301) → "enemy hero around, stop"
在泉水内               → "in spring, rest"
帧 > start+duration    → "timeout, stop"
```

**推理链**：日志写 30s、立即数是 451 帧 → 451/30 ≈ 15.03，故推测该计时器按
**15fps** 递增。这是推断，不是官方文档。

### 9.2 规则案例：`CheckReturnCity`（`0x27df394`）

判定分支（名字即日志标签原文）：

| 分支 | 含义 |
| --- | --- |
| `FORCE_RETURN_CITY`（cur_hp/max_hp） | 血量强制回城 |
| `FORCE_RETURN_CITY`（cur_ep/max_ep） | 蓝量强制回城 |
| `GANK_TIME_NOT_RETURN_CITY` | gank 时段不回 |
| `REVIVE_DIS_NOT_RETURN_CITY` | 复活倒计时内不回 |
| `STRATEGY_RETURN_CITY` / `rule_strategy_return_city` | 策略性回城 |
| `not return, fuhuojia usable` | 复活甲可用则不回 |

比较立即数：`0x46(70)`、`0x12d(301)`、`0x20f(527)`；动作编号记录为 `1.5 / 2.5 / 3`。

> **301 在两条不同规则里复用** → 是官方共享的"危险半径"常量。

### 9.3 锁敌：三段结构

| 段 | 地址 | 说明 |
| --- | --- | --- |
| 外壳 | `0x34e037c` | 打日志 `AITargetSearcher::GetNearestEnemy` → 用 setter 灌 ~10 个过滤参数 → 转发 |
| **真身** | `0x34de91c` | 由 `0x5142000+0x690` 重定位解出；签名 = 多过滤器检索（寄存器 6 个 + 栈上 9 个标志） |
| 执行 | `0x2593820` | 实际检索；由 `0x508f000+0x300` 解出 |

过滤器 setter（表 `0x508f000`）：

| 表项 | 地址 | 作用 |
| --- | --- | --- |
| `+0x250` | `0x2593104` | 设整型过滤参数 |
| `+0x260` | `0x25931d8` | 设整型过滤参数 |
| `+0x2d0` | `0x25936c0` | 设布尔过滤参数 |
| `+0x300` | `0x2593820` | 提交/执行检索 |
| `+0x310` | `0x25938b8` | 收尾 |

反汇编件：`reverse/out/il2cpp/deep_getnearestenemy.txt`、`deep_search_exec.txt`。

### 9.4 已识别的 ctx 状态偏移

从规则函数的内存访问归纳（ctx 基址随函数而异，以下是规则内引用到的偏移）：

`0x900` `0xf94` `0x1040` `0x1044` `0x115c` `0x12f0` `0x12fc` `0x1300`
`0x15e0` `0x15e4` `0x1700` `0x1780` `0x1b34`

---

## 十、已落地的代码改造

### 10.1 新增 `wzry/decision/official_rules.py`

把还原出的常量与谓词集中管理，每条注明汇编证据：

| 常量 | 值 | 证据 |
| --- | --- | --- |
| `FIRST_BORN_TIMEOUT_FRAMES` | 451 | `0x27dd40c` `cmp w8,#0x1c3` |
| `HERO_ABORT_DIST` | 301 | `0x12d` "enemy hero around, stop" |
| `DANGER_DIST` | 301 | IsInDangerousPlace / CheckReturnCity 共用 |
| `DANGER_DIST_FAR` | 527 | `0x20f` |
| `RETURN_CITY_FRAME_GATE` | 70 | `0x46` |
| `RETURN_CITY_HP_RATE` | 0.60 | **占位**：官方为配置表驱动；取 0.60 与既有 optimizer 一致 |
| `RETURN_CITY_EP_RATE` | None | **占位**：阈值未知，为 None 时该分支不触发 |
| `MAP_UNITS` | 1000 | **工程近似**，可用环境变量 `WZRY_OFFICIAL_SCALE` 覆盖 |

谓词：`return_city_decision()` / `first_born_gate()` / `is_in_dangerous_place()` / `norm()`。

### 10.2 `optimizer.py` 三处接线

| 位置 | 改动 |
| --- | --- |
| 顶部导入 | `from wzry.decision import official_rules as _official` |
| `_grid_best` | 新增官方**危险半径**惩罚项：`V -= w_risk * 0.6 * (d_red < norm(301))` |
| `gen_candidates` §8 | 撤退逻辑换成官方 `CheckReturnCity` 具名分支（替换掉原 `if False else` 遗留三元） |

### 10.3 验证

`py_compile` 通过；冒烟测试输出：

```
candidates: 6
return tags: ['retreat_low_hp_FORCE_RETURN_CITY_HP']
norm(301) = 0.301
first_born(35s) -> (False, 'after_30s')
danger(0.2) -> (True, 'danger_radius')
solve -> none / stand
```

### 10.4 三个必须知道的设计约束

1. **不改生存行为**：`RETURN_CITY_HP_RATE = 0.60` 与原 `hp < 0.6` 完全一致。
   官方血量阈值是配置表驱动、非立即数，硬套会平白削弱容错。
2. **标定前缀**：`configs/action_value.json` 里是 `retreat_low_hp`（前缀匹配），
   换标签会让历史标定**静默回退到 default**，因此最终标签取
   `retreat_low_hp_FORCE_RETURN_CITY_HP`（保前缀 + 带官方分支名）。
3. **`MAP_UNITS` 是假设**：`norm(301)=0.301` 与既有 `0.32/0.40/0.45` 同量级，
   但这是**一致性信号，不是证据**；需实测校准后用 `WZRY_OFFICIAL_SCALE` 固化。

### 10.5 操作层命令类型 → ADB/HID 执行层（2026-10-20）

把 `global-metadata.dat` 里的 25 种 `COMMAND_TYPE_*` 落到
`wzry/action/operate_commands.py`，执行通道仍是 ADB / ESP32 HID，**不做进程内调用**：

- **支持**：`MoveDir` / `MovePos` / `MoveStop`、`AttackCommon` / `AttackActor`、
  `PosSkill` / `DirSkill` / `ObjSkill` / `ChargeSkill`、`None`。
- **未复刻**（显式抛 `UnsupportedOperateCommand` 并给替代方案）：`AttackToPos`
  （触摸层没有 A 地板）、`BuyEquip`/`SellEquip`、`LearnSkill`、`UseJointSkill`、
  `Signal*`、镜头/瞄准子状态。
- **接入**：`apply_action` 接受 `{"type":"operate","cmd":"MoveDir",...}`，也接受
  `{"type":"MoveDir",...}`；`update_cooldowns` 同步技能/攻击冷却。
- **测试**：`scripts/test_operate_commands.py`（37 项）。

---

## 十一、改造清单

### ✅ 已接入（本次）

| 官方规则 | 落点 |
| --- | --- |
| `CheckReturnCity` 7 分支骨架 | `official_rules.return_city_decision` + `gen_candidates` |
| `IsInDangerousPlace` 危险半径 | `_grid_best` 惩罚项 |
| 共享常量 301 / 527 / 70 / 451 | `official_rules` 统一出口 |

### ⏳ 代码已留接口，缺数据源

| 官方规则 | 缺什么 | 接上即生效 |
| --- | --- | --- |
| `FORCE_RETURN_CITY_EP` | 蓝量 `ep` 字段 | 传 `ep=` —— ✅ `+0x1C4/+0x1C8` 已定位（`mem_source` 已接）；绝对值仍需与面板对账 |
| `not return, fuhuojia usable` | 复活甲/装备识别 | 传 `revive_armor_usable=` —— **仍未打通**（装备栏未定位） |
| `GANK_TIME_NOT_RETURN_CITY` | 局内时间/时段 | ✅ **已可接**：`ui.match_clock`（内存直读，秒；`--mem` 开启后写入） |
| `GotoTargetPosWhenFirstBorn` | `state["t"]` + 目标点 | ✅ `ui.match_clock` 可作精确 `elapsed_s`；目标点已有 |
| `IsInDangerousPlace` 小兵/朝向项 | 小兵距离、`y_action` | ⏳ 小兵位置仍缺（actor 的 slot12 对非英雄为 null） |
| `StayWithMonster`（黑白名单 + hp 门槛） | **野怪检测**（`units` 无 monster 类） | ⚠️ **未打通**：`extra.mem_monsters` 恒空（`CID_MONSTER` 为空）；`16100/16101` 是红方高地塔，不是野怪（§11.9） |

> v16.3 已落地：`wzry/vision/mem_battle.py` + `scripts/m2_agent_v2.py --mem`。
> 同时修正了金币口径：`+0x200` 是**总经济**，可用金币在 `+0x15D0`
> （`mem_source.read_hero_states` 已改，`ui.gold` 现在与 HUD 逐值吻合）。

### ⛔ 不建议跟（仅作参考）

- **英雄专属规则**：`UseLubandashiSkill4HookEscape`、`UseWangWeiSkill3`、`UseSunquanSkill3`、
  `UseV113Xuebao`、`CheckAkeUseSkill3` 等——绑定具体英雄技能，本项目当前是单英雄逻辑。
- **特殊模式**：`CarryBombNearFrd`（炸弹）、`RideStateAction`（骑乘）、
  `Use10v10LaneRule`、`UseZhanjudianRule`。

---

## 十二、参考链接（工具调研来源）

- Il2CppDumper：<https://github.com/Perfare/Il2CppDumper>
- Il2CppInspectorRedux：<https://github.com/LukeFZ/Il2CppInspectorRedux>
- il2cpp_dumper（Rust）：<https://crates.io/crates/il2cpp_dumper>
- AssetStudio：<https://github.com/Perfare/AssetStudio>
- AssetRipper：<https://github.com/AssetRipper/AssetRipper>
- UnityPy：<https://pypi.org/project/UnityPy/>
- Unity 官方资源分析（WebExtract / Binary2Text）：<https://docs.unity3d.com/Manual/assets-analyze-built-assets.html>
- kong-re（LLM 引导的 Ghidra 自动化管线）：<https://pypi.org/project/kong-re/>
- GhidrAssist（Ghidra + LLM）：<https://github.com/cyberkaida/GhidrAssist>
- IDA llm-explainer：<https://plugins.hex-rays.com/pgarba/ida-llm-explainer/llm-explainer>
- LLM4Decompile：<https://arxiv.org/pdf/2403.05286>
- Binary Ninja WARP（已知函数识别）：<https://binary.ninja/2025/08/22/warp.html>
- REA（Reverse Engineer Anything，macOS/Linux）：<https://github.com/morluto/rea>
- Droid ASC（DEX 层，本任务不适用）：<https://github.com/MG1937/ASC>

---

## 十三、实时内存路线（Route A/B/C）与交接说明

> 本节是**给后续 AI / 后续会话的交接文档**。目标：不走纯视觉层，直接从游戏进程里
> 实时取出对局状态（单位位置、血量、技能冷却等）。
>
> 脚本目录：`d:\work\aiKings\WZRYagnt\reverse\`；数据产物：`reverse\out\`。

### 13.1 三条路线与当前状态

| 路线 | 做法 | 状态 |
| --- | --- | --- |
| **C 直接读进程内存** | root 读 `/proc/<pid>/mem`，用 ELF 偏移换算运行时地址 | ✅ **已验证可行**（读任意偏移 OK，只差"状态对象在哪"） |
| **A 官方共享内存** | 打开 `BattleDataShareMemPrefix_s` 让游戏把对局数据写进共享内存 | ❌ 受阻（注册表被剥离 + 翻译层改写指针）；**配置改动已证明不会被回退** |
| **B 扫 libGameCore `.data`** | 在原生引擎的全局指针里找状态对象 | ⏳ 进行中（第一轮诊断完成，见 13.5） |

### 13.2 Route C：已验证 ✅

**原理**：MuMu（x86_64 宿主 + ARM 翻译）把 guest 的 arm64 库**原样映射**进游戏进程，
并保留 ELF 段文件偏移：

```
400036a00000-400038b85000 r--p 00000000  .../lib/arm64/libGameCore.so   (段1 文件偏移 0)
400038b85000-40003b543000 r--p 02184000  .../lib/arm64/libGameCore.so   (段2 文件偏移 0x2184000)
40003b543000-40003b9f8000 r--p 04b41000  .../lib/arm64/libGameCore.so
40003b9f8000-40003bc6f000 rw-p 04ff5000  .../lib/arm64/libGameCore.so   (.data/.bss)
```

**换算公式**：
```
runtime = 段起始地址 + (目标文件偏移 − 该段文件偏移)
文件偏移 = 本地 ELF 段表给出的 vaddr→off 映射（见 ElfLayout）
```

**证据（三条自检 + 一次交叉验证）**：

```
0x1165dd8 -> 0x400037b65dd8  OK  '[SelectTactics] model result size: %d'
0x114c10a -> 0x400037b4c10a  OK  'tactics_config_file'
0x1159e44 -> 0x400037b59e44  OK  'RuleTactics->Process->LingerDirAdjust'
live slot[0] = 0x4000391acc9c  vs  静态 r_addend = 0x027acc9c  （精确吻合）
```

**工具**：`reverse/livemem.py`

```python
from livemem import LiveMem
m = LiveMem(); m.find_pid(); m.refresh_maps()
m.resolve("libGameCore.so", 0x27d1614)     # vaddr -> 运行时地址
m.read(addr, size)                          # 任意读
m.read_so("libGameCore.so", vaddr, size)    # 按本地偏移读
m.u64s(addr, n) / m.u32s(addr, n) / m.cstr(so, vaddr)
```

**两个必须遵守的实现约束**：
1. **必须用 `adb exec-out`** 读内存。`adb shell` 会分配 pty，对二进制流做 CR/LF 转换，数据会坏。
2. `dd if=/proc/<pid>/mem bs=4096 skip=<页号> count=<页数>`（`/proc/pid/mem` 支持 lseek，skip 是 O(1)）；
   非页对齐地址在本地切片。一次调用约 60~150ms，够 10Hz 用。

### 13.3 Route A：受阻 ❌（不要再从头试 dumper）

**已确认存在的官方通道**（都在 `global-metadata.dat` 里）：

```
C# API ： EnableSharedMemory / GetOrCreateSharedMemory / ShareMem
配置键 ： BattleSyncShareMemMapFile_s        BattleSyncShareMem_s
         BattleDataShareMemMapFilePrefix_s  BattleDataShareMemPrefix_s
         IsSharedMemReplay_b                UseExternalCapture_b
实现文件： GameCoreScripts/.../GameLogic/GameData/BattleData/CLevelContext.cs
```

**出厂全关，且运行时副本是 APK 资源的逐字节副本**：
- `files/Config/GameModeConfig.json` 与 APK 内 `assets/Config/GameModeConfig.json`
  **MD5 完全一致**（`948CEF4C47A6BCA8AFF46ADA294A48E9`）
- 手工改成 `wzryshm` 后重启，**改动不被回退** → 是持久副本，不是每次重抽
- 游戏把 `base.apk` mmap 进进程（`67b42000` / `682e8000` 两段）→ 资源走 APK

**两个专业 dumper 都失败在同一处**：

| 工具 | 关键输出 |
| --- | --- |
| Il2CppDumper 6.7.46 | `Metadata Version: 29` → `Searching...` → `CodeRegistration : 0`、`MetadataRegistration : 0`、`No symbol is detected` |
| Il2CppInspectorRedux 2026.2 | `No matches in symbol table` → `No matches via data heuristics` → **`No matches via code heuristics`** |

**但 `.text` 没被加密**：在 `.text +0x1000 / +0x400000 / +0x2000000 / +0x9000000` 四点采样
均为合法 ARM64（IL2CPP 桩、浮点运算、adrp/ldr）。→ **不是加壳，是生成代码里的
`g_CodeRegistration`/`g_MetadataRegistration` 被定向剥离**（专门废掉 dumper 的加固）。

**运行时启发式补救也没收敛**：`find_registration.py` 按
`{(i32 count, pad, u64 ptr)}` 扫 live 可写段，10 个候选**全是误判**
（连续整数被读成结构体：`count0=267→6000→10004`、`345→346→347…`）。
更麻烦的是发现 **MuMu 翻译层会把宿主地址写进 `.data`**：
```
+0x000: 0x0000400029dd927c   → guest arm64 段
+0x008: 0x0000724fae535ea0   → x86_64/Android 段
```
→ "按静态结构特征扫内存"在 MuMu 上**不可靠**。

**若要走通 A**，只有两条：① 换 **arm64 真机**（指针空间干净，dumper 大概率直接成功）；
② 在真机上用运行时调用枚举 `il2cpp_*`。

> ⚠️ 曾犯错误并已撤回：用 `atime` 判断"游戏没读外部配置"。**对照实验证明该文件系统
> 不更新 atime**（自己 `cat` 完仍显示 2020-01-01），Android `/sdcard` 是 FUSE + noatime。
> 不要再用这个方法。

### 13.4 Route B：进行中 ⏳


**第一轮诊断已完成**（假设被证伪，记录在此避免重复）：

`.data`（25.8 万槽位）的指针落点分布：

| 槽位数 | 指向 | 解读 |
| --- | --- | --- |
| 195,221 | libGameCore 自身 | 静态重定位：虚表 / 函数指针 / 规则表 |
| **2,568** | `0x40003bc6f000..0x40003ca00000`（紧随最后一个段，= **.bss**） | 运行时全局对象 ← **目标** |
| 274 | `[anon:dalvik-main space]` | 宿主 ART |
| 1 | `libc.so` | — |
| 1,241 | 未映射 | 待解释 |

对那 2568 个目标抽 300 个样本回读首 8 字节 → **0 个带虚表**。
→ 结论：**"首字段是虚表"这个判据在这里不成立**，它们更像 POD 结构 / 数组池。

**下一步（精确动作）**：

1. 重启游戏（见 13.7 命令），拿到新 pid。
2. **内容分类**：对 2568 个目标各 dump 前 128 字节，按特征归类：
   - 连续指针（全部落在映射段内）→ 容器 / 索引表
   - 连续数值 → 状态数据
   - 混合 → 结构体
3. **解释 1241 个"未映射"**：若它们聚在一片连续地址，说明 maps 解析还漏了区域
   （注意：匿名段可能只有 5 个字段，`livemem` 已修复此点）。
4. **用 ctx 已知偏移做指纹匹配**（来自第九节的反汇编）：
   同一个对象里应同时出现
   `+0x900` 是指针、`+0x1040`/`+0x1044` 是 0/1 标志、`+0x12fc`/`+0x1300` 是两个阈值。
   命中即为战术上下文（RuleTactics ctx）。

**另一个可选方向**：`0x509c000` 的规则表已能在运行时读到真实函数地址（13.2 已证），
可以从**调用点反推**——搜索 `.text` 里 `blr` 到某规则函数的代码，其 `x0/x19` 来源即 ctx。

### 13.5 新增脚本（本节相关）

| 脚本 | 作用 |
| --- | --- |
| `livemem.py` | **内存读取层**：ELF 偏移 → 运行时地址 → 直读。（Route C 的基础设施） |
| `peek.py` | 查看 live 地址内容，按 `(u32 count, pad, u64 ptr)` 对解析 |
| `hist_gc_data.py` | 统计 `.data` 所有 u64 的落点分布（不做假设） |
| `scan_gc_objects.py` | 按"guest 匿名堆区"过滤（**已知判据不成立，保留作对照**） |
| `probe_bss_objects.py` | 探 `.bss` 目标是否有虚表（首轮结果 0/300） |
| `find_registration.py` | 运行时启发式找 IL2CPP 注册表（**已证伪，勿再跑**） |

### 13.6 踩坑清单（复用必读）

- **capstone 默认遇不可解码字节会终止整个反汇编**。必须 `md.skipdata = True`，
  否则扫全 `.text` 只会得到 4 个 `adrp`（真实 737,092 个）。
- capstone 的 `adrp` 操作数**直接给绝对页地址**（如 `#0x4b43000`），不要再叠加 PC。
- **`adb shell` 会破坏二进制流**（pty 做 CR/LF 转换）；读内存/取二进制一律 `adb exec-out`。
- Python 在 Windows 写文件会把 `\n` 转成 `\r\n`，导致 ripgrep `$` 锚点失效 → 显式 `newline="\n"`。
- **`/proc/pid/maps` 的匿名段常只有 5 个字段（无路径名）**；解析时若要求 ≥6 字段会把
  guest 堆/全局区整段丢掉（曾导致 3809 个假"未映射"）。
- `.data` 里的函数指针表**在文件中全为 0**，值由加载器写入；要读 `.rela.dyn` 的
  `R_AARCH64_RELATIVE`(1027) 的 `r_addend`。
- `0x509c000` 是**混合注册表**（被 591 个槽位引用，混着 JS 引擎/测试框架/`AITrial`）；
  必须按"引用点所在代码区间"过滤才是 RuleTactics 的规则链。
- PowerShell 调用 shell 的雷区：多行远程命令会被拆行 → 用单行 `;` 串联；
  `sed` 里的引号会被 PowerShell 吃掉 → 改用"本地改好再 push"；
  **`$pid` 是只读内置变量**，别拿来存进程号。
- IL2CPP 的类名/方法名**只在 `global-metadata.dat`**，`.so` 里没有明文，不能用
  `.so` 字符串搜证"某方法是否实现"。

### 13.7 环境与连接（复现步骤）

```powershell
# 1) MuMu 实例默认不启动；adb 端口由 MuMuManager 返回
$mm = "D:\app\MuMuPlayer\nx_main\MuMuManager.exe"
& $mm control --vmindex 0 launch
& $mm adb -v 0                       # -> {"adb_host":"127.0.0.1","adb_port":16384}

# 2) 连接 + root
$adb = "D:\app\MuMuPlayer\nx_main\adb.exe"
$S = "127.0.0.1:16384"
& $adb connect $S ; & $adb -s $S root ; & $adb -s $S shell id   # 期望 uid=0

# 3) 启动游戏并取 pid
& $adb -s $S shell "monkey -p com.tencent.tmgp.sgame -c android.intent.category.LAUNCHER 1"
& $adb -s $S shell "pidof com.tencent.tmgp.sgame"
```

| 项 | 值 |
| --- | --- |
| 包名 / ABI | `com.tencent.tmgp.sgame` / 宿主 `x86_64`（游戏 arm64 走翻译） |
| APK | `/data/app/~~mVHdouW8UqoFTOeUr9yNGQ==/com.tencent.tmgp.sgame-RVybgyKqTGOvcl0htOzX6A==/base.apk` |
| 本地 IL2CPP 件 | `reverse\out\il2cpp\lib\arm64-v8a\{libil2cpp.so,libGameCore.so}`、`...\Managed\Metadata\global-metadata.dat` |
| IL2CPP 工具 | `reverse\tools\Il2CppDumper\`、`reverse\tools\Il2CppInspectorRedux\` |
| **.NET 10 运行时** | 已装（用户级）：`C:\Users\obj\AppData\Local\Microsoft\dotnet`；跑 Redux 需先设 `$env:DOTNET_ROOT` 指向它 |

> 📦 取包与解包的完整命令、以及**已解出的内容清单（含各自体积与结论）**见 **2.7 节**。

### 13.8 待清理项（交接前请处理或明确保留）

1. **游戏配置仍为 patch 状态**（`BattleDataShareMemPrefix_s = "wzryshm"`）。
   还原：
   ```
   adb shell "cp /data/local/tmp/GameModeConfig.json.bak /sdcard/Android/data/com.tencent.tmgp.sgame/files/Config/GameModeConfig.json"
   ```
2. **.NET 10 / ASP.NET Core 10 用户级安装**（为跑 Redux）：删除
   `C:\Users\obj\AppData\Local\Microsoft\dotnet` 即可回滚。
3. `/data/local/tmp` 下留有若干临时件（`GameModeConfig.json.bak`、`custom_config.dat`、`rev*`）。

### 13.9 立即行动清单（给接手者）

```
[ ] 1. 拉起 MuMu + 连接 + adb root（见 13.7）
[ ] 2. 启动游戏，记 pid
[ ] 3. 跑 hist_gc_data.py 确认 .data 落点分布仍如上（基线）
[ ] 4. 对 2568 个 .bss 目标做内容分类（13.4 第 2 步）
[ ] 5. 解释 1241 个未映射指针（13.4 第 3 步）
[ ] 6. 用 +0x900/+0x1040/+0x1044/+0x12fc/+0x1300 指纹匹配战术 ctx（13.4 第 4 步）
[ ] 7. 命中后：验证字段随对局变化（血量/位置/冷却应随时间改变）→ 落地到决策层
[ ] 8. 视需要：从规则函数调用点反推 ctx 来源（13.4 的备选方向）
```

> **合规提醒**：包里存在 TP/ACE 反作弊（`libtersafe`、`libTPCore` 等）。本路线只做
> **只读内存分析**，不改客户端、不注入、不写内存。**切勿**把读取器接到线上真人对局。

### 13.10 单位列表已打通（2026-10-20 真人对局实测）

**从全局到 actor 管理器**（`0x3cfff84` 调用点，槽位 `0x5226a90` →
`r_addend 0x45aab64`）：

```
A = *(libGameCore 0x4ff4520)
B = *(A)
T = *(B)
CActorManager* = *(T + 0x388)
```

**CActorManager**：`+0x30` 数组、元素步长 `0x18`（首 qword=actor*）、
`+0x4c`/`+0x6c` 计数、`+0x70` 选择子；`actor->vtable[0x98]` 为 UpdateLogic。

**Actor 头**（10 个英雄全部吻合）：

| 偏移 | 含义 |
| --- | --- |
| `+0x28` | 配置 ID（英雄 ID / 高地塔 1696–1699、16100/16101 / 塔 92/95；小兵/野怪 cid 未定位） |
| `+0x30` | 类型：0=英雄，2=小兵/塔/野怪，1=其他 |
| `+0x34` | 阵营：1=蓝，2=红 |

实测双方阵容：`106(蓝) 107(红) 126(蓝) 141(红) 163(蓝) 168(红) 189(蓝) 133(红) 173(蓝) 117(红)`。

**实时状态已定位（训练营实测）**：

```
actor + 0x230 -> pos 组件：+0x128 = x, +0x12c = y, +0x130 = z（有符号 int32）
actor + 0x1d0 -> attr 组件：+0xc0 = 最大 HP, +0xc8 = 当前 HP
```

- 小乔向右移动：`x -44034 → -35077`，`z` 不变；红 163 位置 `(55757,57652)`
  正好等于红色出生点。
- 满血 `max == cur`；死亡 `cur = 0`、`max` 不变。
- **注意**：`actor+0x1d0/+0x230` 的指针会随重生/换组件变化，每帧必须重读；
  旧指针会读到已释放内存，出现 `hp > hp_max` 的脏值。
- 世界坐标有符号：蓝方为负、红方为正，量级 ±60000。
- **小兵/野怪**没有同构的位置组件（slot12 为 null），血量仍可读；建筑坐标已由 `+0x278` 出生点补齐
  （3 秒采样 0/34 变化），小兵/野怪位置仍需结合 lane 路径或逻辑单元数组，后续再做。

已落地：
- `wzry/vision/mem_source.py`：单位的 **配置 ID / 类型 / 阵营 / 世界坐标 / 当前与最大 HP**；
  快路径 4 次 adb 往返/帧，**250–330ms（3–4Hz）**，首帧慢约 2s。
- `scripts/mem_live_pipeline.py`：内存 → `optimizer.solve` 的旁路验证脚本。
- `MemSource.to_game_state(hero_id)`：输出 `GameState`（英雄的 screen = 小地图归一化 x,z，
  以及 `minimap.dots.red/blue/green`；cls 按自己阵营动态判定）。
- world→minimap 近似标定：`x∈[-58700,58100]`、`z∈[-55700,60000]`；
  蓝/红出生点分别映射到 `(0.02,0.98)` / `(0.98,0.02)`。
- 新开一局实测：10 个英雄全部读到，快帧约 **280ms**，106 在出生点时
  `screen=(0.02,0.98)`，与截图小地图泉水一致。

**小兵/野怪位置仍缺**：对一只小兵全部 15 个 slot 连续采样，只有计时器在变，
没有世界坐标；建筑坐标已由 `+0x278` 补齐（见 §11.9）。
完整记录见 `reverse/out/mem/ACTOR_FINDINGS.md`。

### 13.11 技能剩余 CD / 召唤师技能 / 回城（2026-10-20 深夜，已打通）

英雄状态记录（0x2AD8）在 `+0x19D4` 起有 7 个同构按钮槽，stride `0x78`：

| 槽 | ID | 含义 |
| --- | --- | --- |
| 1-3 | 英雄技能 ID（小乔 10610/10620/10630） | 1/2/大招 |
| 4 | 0 | 空槽 |
| 5 | 90003 | 恢复（60s） |
| 6 | 80xxx（闪现=80115） | 召唤师技能（120s） |
| 7 | 90005 | 回城（**无 CD**，只有可用位） |

槽内：`+0x08` id、`+0x0C` 等级（0=未学）、`+0x10` 状态位域（**bit0=可用**）、
`+0x14` **剩余 CD 毫秒**、`+0x18` 总 CD 毫秒。

- 实测：大招 `0→32558/35000`，屏幕 35→31 同步；1技能 `0→4472/5000`；
  闪现 `0→119076/120000`；恢复 `0→59076/60000`；回城 `state 1→0` 约 6s 后回 1。
- **施法通道**：局内 `input tap` / `input swipe` 不触发施法，必须
  `input motionevent DOWN/MOVE/UP`（§14.5 的"点按不进冷却"结论作废）。
- **定位坑**：`memscan herostate` 依赖记录内坐标，移动后会漂移导致 0 命中；
  优先用全英雄数组（`idbag` → `read_hero_states()`），herostate 仅兜底。
- **代码**：`mem_source._parse_self_state` 输出 `skills`/`slots`/`summoner`/`heal`/`recall`；
  `to_game_state` 写 `ui.self_skill_cd`（left_s/total_s）、`ui.self_summoner_cd`、
  `ui.self_recall_ready`；`MemBattle.patch_state` 写
  `ui.skill_cd/ratio/ready`、`ui.summoner_id/cd/ratio/ready`、`ui.heal_cd/ready`、
  `ui.recall_ready`，以及 `extra.mem_summoner/mem_heal/mem_recall`。
- 全英雄（敌我 10 人）的同一组槽都能从数组里读到，含敌方召唤师技能 CD。

### 13.12 全英雄基础属性 + 成长（2026-10-21，已打通）

actor 的 slot0 组件（`actor+0x110`）`+0x460` 起有 22×i32 的**基础属性块**
（slot1/slot4 有同内容副本），成长量 ×1e-4：

```
攻速 基础/成长、HP 基础/成长、HP回复 基础/成长、
MP 基础/成长、MP回复 基础/成长、AD 基础/成长、护甲 基础/成长、魔抗 基础/成长
```

- 公式 `基础 + 成长×(等级-1)`：106 Lv2 HP 3200+205=3405（实时 3405）；
  Lv6 MP 640+45.7143×5≈868.6（旧施法实测 868）——逐英雄对账通过。
- 实时 HP/MP 与公式的差额 = **装备加成**（例：117 +1306 HP、118 +400 MP）。
- 代码：`MemSource.read_hero_base_stats()` / `stats_at_level()`；
  输出 `extra.hero_stats`（`to_game_state`）与 `extra.mem_hero_stats`（`MemBattle`）。
- 仍未打通：装备提供的 **AD/护甲/魔抗/攻速** 实时值（装备栏结构未定位）。


---

## 十四、AI 的数据面（2026-10-20 追加）

本文前面的章节解决的是「AI 怎么决策（规则链 / 锁敌 / 打分）」。
「AI 读哪些数据、怎么把数据喂给模型」单独成文：

👉 **`docs/wzry-ai-data-inputs.md`**

要点（避免重复踩坑）：

1. 二进制里有**两套**特征抽取器，别混：`feature::Vec*`（对局 AI）与
   `feature_aivoice::Vec*`（AI 语音/陪玩）。键名逐字相同，后者带一堆
   `stop_atk_for_player` 类开关。
2. 数据流水线：`feature_extract_interval` 抽样 → `view_distance` 裁剪 + 按距离取前 N
   → 按类别拼定长向量（兵线/建筑/野怪/血包/英雄10人+自己）→ `normType` 归一化
   → 序列模型（tensorflow/caffe）→ `SetTacticsResult(prob, rank, cmd_idx)` → 规则覆盖。
3. 动作空间是**「双手动作」= 目标类型 × 技能槽位**（`AtkHero/AtkSoldier/AtkTower/AtkCrystal/
   AtkMonster` × `AtkSkill1..4`），不是高层战术名。高层由规则 + `tactics_list` 概率决定。
4. **建筑是 22 个具名槽位**（friend/enemy × crystal/spring/{mid,top,bottom}×{high, norm_1st, norm_2nd}），
   对应最多 24 个单位槽；官方按语义索引取塔，不遍历识别 —— 这条最值得抄。
5. 技能 CD / 装备 / buff 在 **AI 侧结构**（`SkillInfo{usable,cd}`、`BuffMarkState`），
   不在 actor 属性块 —— 印证了我们"在 actor/attr 里扫不到 CD"的结论。
6. 难度是参数化的：`model_id / predict_freq / delay_frame / delay_buy_time / god_vision`，
   且按 `ally_money / enemy_money`、离泉水距离做**动态调档**。

新脚本：`reverse/ai_scan.py`（全量数据流扫描）、`reverse/ai_inputs.py`（间接调用归因）、
`reverse/find_node_registry.py`。产物：`out/il2cpp/gc.{strrefs,icalls,slots}.tsv`。
