# ARCH · 执行通道（控制层）—— adb / ESP32 HID 桥 与通道选择策略

> 硬件接线与固件细节见 [ESP32_HID_HARDWARE.md](ESP32_HID_HARDWARE.md)；感知/UI 层见
> [ARCH_UI_QUERY_LAYER.md](ARCH_UI_QUERY_LAYER.md)；移动决策见 [ARCH_MOVE_PUSH.md](ARCH_MOVE_PUSH.md)。
> 本文只覆盖**执行（注入）**这一条轴。

---

## 0. 结论摘要

1. **两条执行通道都已实现**，且接口逐方法对齐，可整体替换（`ActionExecutor(executor=...)`）。
2. **执行是互斥的**：一次解析只产出一个 executor，不存在"两条通道同时下发"。
   感知（`--capture`）与执行（`--backend`）是**两个独立参数**，可自由组合。
3. **默认 `auto` = 优先板子、探测不通回落 adb**，且"处处没有板子地址时**零探测**"——
   与旧版 `--backend adb` 行为一致，既有 MuMu 工作流零影响。
4. **验证等级：L1 通过（47 项断言）／L2 不适用／L3 未做。**
   即 ESP32 通道目前只验证到**链路 + 协议原语**层面，**没有留下"整 agent 经 ESP32 后端跑通一局"的痕迹**。
5. 未决项：采集路径写死 `"adb"`（`wzry/capture/adb.py`）；`--board-size` 与固件不一致会
   **静默偏移**；鼠标固件做不到多点；板子供电/复位两个硬件坑。

---

## 1. 现状（读代码结论）

### 1.1 资产清单

| 文件 | 角色 | 关键锚点 |
|---|---|---|
| `firmware/esp32_hid_touch/esp32_hid_touch.ino` | **主用固件**（717 行）。自定义 `USBHIDDevice` → 对手机表现为**多点触摸屏 Digitizer + 键盘**，绝对坐标 | `TOUCH_MAX_CONTACTS 5`（L74）、`BRIDGE_PORT 7300`（L62）、`USB_SETTLE_MS 1500`（L81）、`releaseAllTouches()`、`hello <reset> <uptime> <W>x<H>` |
| `firmware/esp32_hid_bridge/esp32_hid_bridge.ino` | **退路固件**（489 行）。普通相对指针鼠标，**单点** | `home/move/press/release/click/scroll` |
| `firmware/flash.ps1` | arduino-cli 编译烧写（90 行），FQBN 板级选项已固化 | `esp32:esp32:XIAO_ESP32S3` / `USBMode=default` / `UploadMode=cdc` |
| `wzry/control/esp32_hid.py` | 电脑端客户端：`BoardLink`（TCP 行协议）+ `Esp32HidExecutor` | 动作命令读超时 3s；**仅 `sendall` 失败才重发**（非幂等动作防重放） |
| `wzry/control/executor.py` | `AdbExecutor`：常驻 server + `adb -s <serial> shell input ...` | `get_device_size()` 读 `dumpsys window displays` 的 `cur=WxH`；v14.2 起每次操作前重读 |
| `wzry/adb_util.py` | adb 可执行文件定位 | `resolve_adb()`：`WZRY_ADB` > PATH > MuMu 路径 |
| `wzry/capture/adb.py` | 感知：`adb exec-out screencap`（裸 RGBA 解析） | 见 §5.2 缺陷 |
| `wzry/control/channel.py` | **通道选择策略**（v16.1） | `resolve_channel()` / `probe_board()` / `save_board_cfg()` |
| `wzry/action/executor_v2.py` | 上层动作原语（摇杆/技能/攻击） | `self.ex = executor or AdbExecutor()`（L28） |
| `scripts/m2_agent_v2.py` | 生产入口 | `--capture`（L1731）、`--backend`（L1823）、`resolve_channel(...)`（L2042） |
| `scripts/test_esp32_bridge.py` | 协议级离线测试（29 项） | 用 `FakeLink` 钉死命令串，与固件 `handleLine` 同表 |
| `scripts/test_channel_select.py` | 通道选择离线测试（18 项） | 锁死不变量，见 §4 |

### 1.2 接口契约

`Esp32HidExecutor` 与 `AdbExecutor` 提供**同名同语义**的
`tap / swipe / key / tap_win / swipe_win / get_device_size / set_window_size /
window_to_device / close`，且动作事件流字段一致（`t/type/x/y/source/latency_ms`）。
因此替换执行器时，上层感知、决策、状态机、录制**一行都不用改**。

`Esp32HidExecutor` 额外提供多点原语 `touch_down/touch_move/touch_up`（仅 `mode="touch"`），
这是"左手摇杆按住 + 右手技能同按"的实现基础 —— 单点鼠标固件做不到。

---

## 2. 两条通道的关系

### 2.1 执行（写）轴：互斥

三条代码证据：

1. `--backend` 是**单选**：`choices=["auto", "adb", "esp32"]`（`m2_agent_v2.py:1823`）。
2. 执行器实例化是 `if/else`，只产出一个 `ex`（`m2_agent_v2.py:2043–2075`）。
3. `ActionExecutor` 只持有**一个** executor：`self.ex = executor or AdbExecutor()`（`executor_v2.py:28`）。

语义上也必须互斥：两路同时注入会互相抢触点，且上层**无从仲裁**哪一路的动作生效。

### 2.2 感知（读）轴：与执行完全解耦

`--capture` 是独立参数（`m2_agent_v2.py:1731`：`desktop / screencap / scrcpy`），与 `--backend`
**零耦合**：采集实例化不引用 `args.backend`，ESP32 执行器实例化也不引用 `args.capture`。

由此得到一条重要推论：**选板子会反过来约束怎么取帧** —— 板子占住手机 USB 口（它要当 HID
设备插着），有线 adb 就不可用，感知只能改走**无线 adb（5555）或 scrcpy**。

### 2.3 修正记录：勿把"感知可分离"读成"执行可并行"

评估过程中一度把上面两条总结成"**真机实际是混合配置，不是二选一**"，并进一步称
"这条混合链路正是已记录的真机基线配置"。**回查后否定该表述**，理由三条：

- 文档 §9 原话只是"是**可行的**混合配置"——**可行 ≠ 已采用**；
- 仓库内 `--backend esp32` 的**启动命令记录为 0 条**（`*.md` / `*.bat` / `*.ps1` / `*.json` 全查过）；
- `data/action_events.jsonl` 尾部事件带 `"display": [null, null]` 字段，这是 `AdbExecutor._log()`
  **独有**的格式（`Esp32HidExecutor` 的事件不含该键）→ 说明实际跑的一直是 ADB 通道。

**正确表述**：执行轴**二选一**；"可以混合"只成立在"读轴用 ADB、写轴用 HID"这个组合上。
本文以回查结果为准。

---

## 3. 通道选择策略（v16.1）

参数表、典型用法、离线自测命令见 [ESP32_HID_HARDWARE.md §10](ESP32_HID_HARDWARE.md)。
这里只记**设计意图**与**不变量**。

### 3.1 为什么是"策略"而不是"两个开关"

因为执行互斥（§2.1），所以需要回答的不是"开哪几条"，而是**"这一次该用哪条"**。
`resolve_channel()`（`wzry/control/channel.py:104`）就是这个问题唯一入口：
输入 `backend` + 地址来源，输出一个决策字典（`channel/host/port/mode/size/source/reason/hello`）。

### 3.2 地址与参数优先级

- **板子地址**：`--board-host` > 环境变量 `WZRY_BOARD_HOST` > `configs/board.json`
- **分辨率 / 模式**：CLI > `configs/board.json` > 默认（`1080x2340` / `touch`）

板子走 DHCP，IP 会变，所以**连成功才落盘** `configs/board.json`（该文件只含局域网地址，
无凭据，已进 `.gitignore`）。之后 `--backend auto` 不必再敲 `--board-host`。

### 3.3 三条不变量（改动时不要破坏）

1. **`auto` 在处处没有板子地址时零探测。** 直接选 adb，不发起任何 socket 连接 ——
   与旧版 `--backend adb` 行为一致、启动零额外延迟。这是既有 MuMu 工作流不受影响的前提。
2. **强制即强制。** `--backend esp32` 拿不到地址或探测不通时**明确报错退出**（`ChannelResolutionError`
   → `SystemExit`），**绝不静默回落 adb** —— 否则真机调试会误判为"板子没生效"。
3. **探测成功才落盘。** 必须连上并收到 `hello` 才写配置，避免把错地址持久化。

### 3.4 摇杆下发方式：持久触点（v16.2，`--stick-hold`）

**问题。** 移动通道（`map_move`，已从"点小地图"改为"摇杆推杆"）原先一律走 `t_swipe`：
按下 → 插值 `ms/8` 个报告 → **松手**。三个后果：

1. **松手即停** —— 王者荣耀松开摇杆英雄就停止移动，于是移动变成"推一段 → 停 → 再推"。
   代码里原本已有补救意图：`m2_agent_v2.py` 的 `stick_move`（注释 v2.26"决策间隔里不让
   摇杆松手"），但它同样调 `ex.move()` → `t_swipe` → **仍然松手**，只能缩短静止时间。
2. **执行线程被占 200–1000 ms/条**（`t_swipe` 要按住 `ms` 才回 `ok`；adb 实测 1004 ms）。
   配合队列深度 ≤1 的背压，动作带宽上限被压到 **≈1 Hz**。
3. **每次 56 个报告**（450 ms ÷ `SWIPE_STEP_MS 8`）纯属浪费。

**方案。** 用固件早已提供、但上层从未调用的持久触点原语：

```
t_down 0 <cx> <cy>     # 抢住触点 0 当摇杆
t_move 0 <x> <y>       # 每次决策只改朝向；1 条命令、1 个报告、≈20 ms
t_up 0                 # 真要停才松手
```

固件本就是为此设计的：`t_tap` / `t_swipe` 用 `freeContact()` **自动挑空闲触点**，
`.ino` 注释明写"上层把触点 0 当摇杆持续按住时，固定用触点 0 会直接打断摇杆"。

**实现要点**

- `ActionExecutor.move_hold()` / `release_stick()` / `stick_held`（`executor_v2.py`）。
- **能力判定必须同时看 `mode`**：`Esp32HidExecutor` 在 mouse 模式下也有 `touch_down`
  方法，但调用会抛 `RuntimeError`。只看 `hasattr` 会误判 → 判据是
  `mode == "touch"` **且** 有 `touch_down`。
- **不支持的执行器自动回退**：adb / mouse 固件一切摇杆调用仍走原 `swipe`，
  `stop()` 也仍是原实现 → **行为与改造前逐字节一致**。
- `stop()` 在持久模式下是**真空操作**（未按住不发任何命令），避免白发一条 `swipe`。
- `t_move` 失败时标记为未按住，下次调用重新 `t_down`，避免"以为按住其实没按"。
- 回城类动作（`recall` / `restore` / `summoner`）执行前**先松手** —— 回城读条会被移动打断。
- 主循环按设计不直接碰 `ex`，所以"对局阶段切换"与"退出"的松手都经队列由 worker 执行
  （新增 `stick_release` 动作类型）。

**验收点（可观测）**：`--stick-hold on` 后，退出汇总里的 **`丢弃 N 次` 应显著下降**
（见 §3.5）；`data/action_events.jsonl` 里应出现 `touch` 型事件与 `t_move` 序列，
而不是清一色 `swipe`。

### 3.5 动作丢弃可见性（v16.2）

原实现 `m2_agent_v2.py` 的投递处是 `if _exec_idle.is_set():` —— **没有 else、没有计数、
没有重试**。worker 正忙时做出的决策会被**静默丢弃**，而 `n_actions` 只统计入队成功的，
所以这个丢失在统计上被完全掩盖。主循环周期（≈530–585 ms）与一次 `swipe`（350–900 ms，
adb 实测 1004 ms）同量级 → 碰撞概率高。

现补：`else` 分支累计 `_act_drop`、节流打印（≥5 s 一条）、退出汇总输出
`丢弃 N 次` 与占本应下发动作的比例。**这一项零行为变更**，只是让丢失可见。

### 3.6 "何时停止"的语义（v16.2）

持久摇杆是**物理按住**，所以"没有新指令"不再等于"停下"。围绕这一点补齐两处：

**(a) 航位推算改按实际用时推进（修 §3.4 引入的偏差）。**

`_step` 原本是 `STICK_SPEED × dur/1000`。这在 swipe 模式下**自洽**，因为 `dur` 守卫
强制"两次推杆间隔 ≥ dur"，英雄确实走了 `dur`。持久摇杆去掉该守卫后，间隔变成主循环
周期（≈0.53 s），而 `dist ≥ MAPMOVE_NEAR(0.04)` 又使 `dur` **恒被钳到上限 900 ms**
→ `_step ≈ 0.0162` 而实际 ≈ `0.018 × 0.53 = 0.0095`，**估计前进量约为实际的 1.7 倍**。
`self_est` 被 `_mm_self_est()` 消费在 `m2_agent_v2.py` 的 323/361/410/440/919 行
（接战距离、塔威胁、推进目标、逃生判据），估计跑快了这些判断全部偏乐观。

改为 `_step = STICK_SPEED × min(now − _map_t, MAPMOVE_EL_MAX)`（首次视为 0）；
非 hold 模式仍用 `dur/1000`，口径不变。另外在**松手处**（recall / restore / summoner /
`stick_release` / 复活）都 `pop("_map_t")`，避免把静止时间算成"走过"。

**(b) 到达即停 + 保险丝（见 `wzry/decision/nav_gate.py`）。**

`dist < MAPMOVE_NEAR` 时原实现把 `self_est` 强制重置回**本方基地**再继续推进。这在
连续移动下会变成"从目标往外走"的**过冲**（θ 变成"基地→目标"方向，而英雄已在目标处）。
改为：到达 → **松手停住**；若同一位置连续停推超过 `MAPMOVE_IDLE_MAX`(5 s) 仍没换目标，
说明开环估计很可能已失真 → 返回 `"fuse"`，走回旧的"重锚回基地 + 继续推进"，保留原实现
防"永久僵住"（实测 180 s 只发 19 次 swipe）的兜底，并打印 `fuse_count` 供观测。

**该闸门只在 hold 模式启用**；非 hold 模式不查，保持原行为逐字节一致（便于 A/B 归因）。

**(c) 阵亡期间必须真松手。** `{"type":"none"}` 不参与入队（`m2:3021`），所以旧模式下
"无指令=停下"在持久摇杆里不成立 —— 触点仍按着，英雄会沿旧方向继续走。多数场景这是
期望行为（AI 本就该连续走位），但**阵亡**是例外：不松手会让复活瞬间先沿旧方向走约
一拍。现在在"刚阵亡"那一拍投递一次 `stick_release`，并在复活时清 `_map_t` / `_map_gate`。

**(d) 边走边打（多点并发）。** 这是 hold 模式相对 swipe 的另一处实质收益：

- 固件 `t_tap` / `t_swipe` 用 `freeContact()` **自动挑空闲触点**，摇杆固定占触点 0 →
  攻击/技能落在 1..4，**不打断摇杆**；HID 报告是 5 触点**全量快照**（`rep.count` +
  每个 finger 的 `id/flags/x/y`），所以手机看到的是两个触点**同时**按下，不是先后。
- 坐标也不冲突：摇杆活动区（帧空间 1280×720）约 `x 110..282, y 488..660`；攻击键
  `(1182,617)`、攻击小兵 `(1067,654)`、最近技能键 `(963,634)` —— 与摇杆区**相距 ≥681 px**。
- `apply_action` 只在 `recall` / `restore` / `summoner` 前松手（回城读条会被移动打断）；
  `attack` / `skill` **不松手**。

**两处曾经的漏改（已修，`test_apply_action.py` 钉死）：**

1. `"move"` 型动作仍走 swipe。`decide()` 对 `STICK_REASONS`（`chase_enemy` /
   `chase_enemy_bar` / `follow_ally` / `escape_turret` / `escape_turret_hold`）会直接返回
   `{"type":"move", theta}`，而该分支之前没跟着改成 hold → **追击/逃生这些最需要连续移动的
   场景反而每次松手**，且 worker 被占 450 ms，其间决策出的攻击/技能会被丢弃。
2. hold 模式下 `stick_move` **续推未关**。续推本是为对抗 swipe 的"松手即停"，按住不放
   已自带该语义；继续发只是白占执行槽 —— 队列深度 ≤1，**每一条续推就挤掉一次真正的
   攻击/技能机会**。现已在 hold 模式跳过（非 hold 模式保留原续推）。

### 3.7 验收点汇总（可观测）

| 观测项 | 期望 |
|---|---|
| 退出汇总 `丢弃 N 次` | 持久摇杆后显著下降（§3.5） |
| 退出汇总 `未知类型 N 次` | 应为 **0**；大于 0 = 有动作 `type` 拼错被静默忽略（§3.8g），按清单核对 |
| `data/action_events.jsonl` | 出现 `t_move` 序列而非清一色 `swipe` |
| 屏幕移动观感 | 连续不停顿（原"推一段→停"消失） |
| `[map_move] 到达闸门保险丝` 打印 | **不应频繁出现**；频繁 = 开环估计失真严重，该去补真实位置反馈而非继续调参 |

### 3.8 输入格式（接口参考）

驱动执行层有三层"输入"，**坐标空间各不相同**，这是最容易出错的地方。

#### (a) 命令行：怎么启动驱动

```powershell
# 最小可用（只感知不驱动）：不传 --action
python scripts/m2_agent_v2.py

# 驱动真机（板子优先, 不通回落 adb）
python scripts/m2_agent_v2.py --action --backend auto `
    --board-host 192.168.0.107 --board-size 3200x1440 `
    --capture screencap --serial 192.168.0.103:5555 --stick-hold on --seconds 300
```

| 参数 | 作用 |
|---|---|
| `--action` | **总开关**。不传 = 只感知/决策不驱动；`--no-action` 显式关闭 |
| `--backend auto\|adb\|esp32` | 执行通道（§3） |
| `--capture desktop\|screencap\|scrcpy` | 感知通道，与 `--backend` 解耦（§2.2） |
| `--stick-hold on\|off` | 摇杆持久触点（§3.4），默认 on |
| `--board-host/--board-port/--board-mode/--board-size` | 板子地址与屏幕尺寸（`ESP32_HID_HARDWARE.md §10`） |

#### (b) 动作 dict：决策层 → 执行层的接口

`apply_action(ex, action, cooldowns)`（`scripts/m2_agent_v2.py`）。`action` 为 dict，
按 `type` 分支：

| `type` | 必填 | 可选 | 语义 |
|---|---|---|---|
| `map_move` | `nx`, `ny` ∈ [0,1]（**小地图归一化**） | `reason` | 主移动通道：执行器用 `self_est` 现算 θ 再推杆 |
| `move` | `theta`（弧度，0=右、逆时针） | `r`∈[0,1]（默认 0.8）、`duration_ms` | 直接给摇杆方向（追击/逃生用） |
| `skill` | `id` ∈ {1,2,3} | `mode` ∈ {`tap`,`cancel`}（默认 `tap`） | ⚠️ `drag` 不可用，见 (e) |
| `attack` | — | `priority` ∈ {`free`,`minion`,`tower`} | 点攻击键 |
| `summoner` / `recall` / `restore` | — | — | 执行前**先松摇杆**（回城读条会被移动打断） |
| `combo` | `actions`: list[dict] | — | 递归调用，步间隔 0.12 s |
| `none` | — | `reason` | **不入队**（`m2:3021`），纯占位 |
| `operate` / 官方命令名 | `cmd`（`operate` 包装时必填） | 命令参数 | 操作层 `COMMAND_TYPE_*` 复刻，见下 |

**操作层命令（v16.3）**：`wzry/action/operate_commands.py` 把游戏 `COMMAND_TYPE_*`
映射到 ADB/HID。两种写法等价：

```json
{"type":"operate","cmd":"MoveDir","theta":0.5,"r":0.8}
{"type":"MoveDir","theta":0.5,"r":0.8}
```

| 命令 | 参数 | 触摸实现 |
|---|---|---|
| `MoveDir` | `theta`,`r`,`duration_ms`,`hold` | 持久摇杆；无多点回退 swipe（`duration_ms` 仅回退时生效） |
| `MovePos` | `nx`,`ny` + `origin` 或 `state.self_est` | 只算方向；不做航位推算/到达闸门，状态化移动仍用 `map_move` |
| `MoveStop` | — | 松摇杆 |
| `AttackCommon` | `priority` | 攻击键（free/minion/tower） |
| `AttackActor` | `target`（帧坐标）/ `priority` | 点选目标；无 target 回退攻击键 |
| `PosSkill` | `id`,`target`,`duration` | 技能按钮拖到位置 |
| `DirSkill` | `id`,`theta` 或 `target`,`aim_radius` | 技能按钮拖向方向 |
| `ObjSkill` | `id`,`[target]` | 有目标拖动，否则点按智能施法 |
| `ChargeSkill` | `id`,`hold_ms` | 按住技能按钮后松开（多点用非摇杆触点） |
| `None` | — | no-op |

未复刻命令（`AttackToPos`、`BuyEquip`/`SellEquip`、`LearnSkill`、`UseJointSkill`、
`Signal*`、镜头/瞄准子状态等）会抛 `UnsupportedOperateCommand` 并给出替代方案，
**不会静默伪装成功**。测试：`scripts/test_operate_commands.py`。

`cooldowns` 是状态 dict，**传 `{}` 即可跑**（`camp` 默认 `blue`，`self_est` 缺失时回本方基地）。
真实运行时的关键键：`self_est`（航位推算位置）、`camp`、`_map_t`、`_map_gate`、`_act_drop`。

#### (c) TCP 行协议：直接驱板子（绕开 agent）

```
ping                                  -> ok
stat                                  -> ok cmds=N drops=N down=N up=Ns
t_tap <x> <y> <ms>
t_swipe <x1> <y1> <x2> <y2> <ms>
t_down <id> <x> <y> | t_move <id> <x> <y> | t_up <id>     # id ∈ 0..4
key <combo> | type <text(ASCII)>
```

- **坐标是手机物理像素**（`0..W-1` / `0..H-1`），**不是**帧坐标
- 一行一条命令，`\n` 结尾，逐条回 `ok` / `err <reason>`
- 摇杆固定占触点 0；`t_tap` / `t_swipe` **自动挑空闲触点**（1..4）→ 边走边打不互相打断
- 自测 CLI：`python -m wzry.control.esp32_hid --host <IP> tap 600 320`
  （该 CLI **无 `--device-size`**，坐标按设备像素原样下发）

#### (d) 三个坐标空间 —— 别混

| 空间 | 谁在用 | 例子 |
|---|---|---|
| 小地图归一化 0–1 | 仅 `map_move.nx/ny` | `{nx: 0.9, ny: 0.1}` |
| **帧空间 1280×720** | 校准表全部点位、`ActionExecutor` 内部 | 摇杆中心 `(196,574)`、攻击 `(1182,617)` |
| 设备物理像素 | TCP 命令、`Esp32HidExecutor` 出口 | `t_tap 2955 1234 50` |

转换链：`map_move{nx,ny}` → 算 θ → `_stick_point` 得帧坐标 → `_to_dev`（真机 ×2.5/×2.0）
→ 设备像素 → 命令行。**实测样例**：帧 `(1182,617)` → `t_tap 2955 1234 50`
（`1182×2.5=2955`、`617×2.0=1234`）。

#### (e) 驱动方式（v16.2：三种入口）

| 方式 | 怎么用 | 适用 |
|---|---|---|
| **agent 自驱**（默认） | `--action` | 正常跑；`decide()` → 队列 → worker |
| **外部/文件驱动** | `--drive-stdin`，从 stdin 逐行读 JSON 动作 | 外部程序实时驱动；或**直接回放** `data/matches/*/actions.jsonl` |
| **裸协议** | 直接对板子发 `t_*` 命令（见 (c)） | 验证通路/坐标，不启动 agent |

`--drive-stdin` 的节奏：行内 `delay_ms`（毫秒）优先；否则用行内 `t`（绝对秒）与**
前一条的差值**，差值 clamp 到 `[0, --drive-pace-max(默认5s)]`。所以
`data/matches/*/actions.jsonl`（schema = 动作 dict + `t`）可以**原样**喂进去：

```powershell
# 回放一局已录制的决策序列（Windows PowerShell）
Get-Content data\matches\<会话>\actions.jsonl |
    python scripts\m2_agent_v2.py --drive-stdin --backend esp32 `
        --board-host 192.168.0.107 --board-size 3200x1440 --capture screencap
```

> 实测：该管道在本机可用（109 行 → 109 条解析成功）。当前 `actions.jsonl` 全为纯 ASCII
> （`reason` 是英文 slug），故不受 PowerShell 5.1 默认编码影响；**若将来 `reason` 出现中文，
> 建议显式加 `-Encoding UTF8`**（`Get-Content` 在 PS 5.1 默认按 ANSI 读，会乱码；
> 由于 `apply_action` 不使用 `reason`，通常无害，但极端情况下可能破坏 JSON 引号）。
> 驱动结束会打印 `共下发 N 条动作, 跳过 M 行` 并回显首条被跳过的行 —— 管道编码错了能立刻看见。

开启后**主循环自己的动作下发会整块关闭**（避免两路抢深度=1 的队列槽位），感知/学习/
录制照常；`stick_move` 续推与阵亡松手也不再代劳（摇杆归外部控制）。stdin 结束即收尾退出。

> ⚠️ 这是**动作序列驱动**，不是逐字节复现：决策层状态（`self_est` / 冷却）不重建，
> 所以同一条动作在回放时的落点与原始运行不会完全一致。
>
> ⚠️ **`--drive-stdin` 尚未端到端执行验证。** 已验证的是：参数注册（`--help`）、
> 纯逻辑单测（解析 + 主循环本体，38 项）、以及队列/背压接线的代码审查。**未验证**的是
> "真的开一次它并且动作落到手机上" —— 因为本机没有可用的采集设备，主循环起不来。
> 首次真机使用时请先看 `共下发 N 条动作, 跳过 M 行` 是否符合预期。

#### (f) 两个曾经的缺口（v16.2 已修）

1. **`skill` 的 `drag` 模式经 `apply_action` 用不了** → 已修：`apply_action` 现在透传
   `target` 与 `duration`。
   ⚠️ **`target` 必须是帧坐标（1280×720），与技能按钮同空间，不是设备像素** ——
   `skill_cast` 的旧 docstring 写"设备像素"是错的：起点取自校准表（帧空间），而底层
   `swipe` 会对**两端一起**做 frame→device 缩放，传设备像素会被二次缩放。docstring 已改。
   实测：帧 `(963,634)` → 帧 `(900,400)` 下发为 `t_swipe 2407 1268 2250 800 200`。
2. **没有"从文件/外部实时喂动作"的入口** → 已修：见 (e) 的 `--drive-stdin`。
   （`scripts/replay_push.py` 仍是**离线回放评估**，复跑 `push_ok`/`_push_eval`，不驱动手机。）

#### (g) 决策层输出契约（生成侧）

决策层要喂给执行层的，就是 (b) 的那个 action dict。**最低要求**：`dict` 且含
`type: str`。除此之外执行层不校验任何东西 —— 所以下面这些**静默失败**必须知道：

| 写错了会怎样 | 后果 |
|---|---|
| `type` 拼错 / 不在支持列表 | 不执行任何动作。v16.2 起**计数 + 5s 节流打印告警**，退出汇总输出 `未知类型 N 次`（原先完全静默、不打印不计数） |
| `move` 漏了 `theta` | `theta` 默认 **0.0 = 摇杆推向右** → 英雄往右走（不是不动！） |
| `skill.id` ∉ {1,2,3} | `skill_cast` 抛 `ValueError` → 被 worker 吞成 `[exec] 异常(跳过)`，只打印 |
| `skill.mode="drag"` 但无 `target` | 同上，抛错被吞 |
| `type="none"` | **永不执行**（也不入队），只作占位 |
| `map_move` 的 `nx/ny` 超出 [0,1] | 不报错 —— 它只被用来**算方向**，所以能跑，但不合约定 |

**决策层需要维护的状态**（`cooldowns`，`apply_action` 会**读写**）：
`self_est`（自己在小地图的估计位置，缺失则回本方基地）、`camp`（`blue`/`red`）、
以及执行层自己写回的 `_map_t` / `_map_gate` / `_act_drop`。

**当前 `decide()` 实际产出的类型**（`m2_agent_v2.py`）：

`map_move`（`nx`,`ny`,`reason`）｜`move`（`theta`,`r`,`duration_ms`,`reason`，仅
`STICK_REASONS`：chase_enemy / chase_enemy_bar / follow_ally / escape_turret /
escape_turret_hold）｜`skill`（`id`,`mode`，「**当前恒为 `tap`**」）｜`attack`
（`priority`，「**当前恒为 `free`**」）｜`combo`（`actions`，内含 `summoner`/`skill` 等嵌套动作）｜
`recall` / `restore`（`reason`）｜`none`（`reason`）。
→ 也就是说 `skill.mode` 的 `drag`/`cancel` 与 `attack.priority` 的 `minion`/`tower`
**执行层已支持、决策层尚未使用**。

**动作在下发前还可能被改写**（决策层不是唯一写入者）：`optimizer.solve()` 重排/替换移动候选
→ `--hunt` 时 `hunt_red_dot` 覆盖 → 被敌方塔锁定时 `under_tower_fire` 覆盖 →
阵亡时改成 `none`。所以 `data/matches/*/actions.jsonl` 记的是**最终下发**的动作。

---

## 4. 验证等级与证据

沿用本项目的三层口径（同 [ARCH_UI_QUERY_LAYER.md §9](ARCH_UI_QUERY_LAYER.md)）：

| 等级 | 做什么 | 本次结果 |
|---|---|---|
| L1 离线单测 | `test_channel_select.py`：强制/回落/优先级/互斥形状/落盘往返/未知值 | **18/18 通过** |
| L1 离线单测 | `test_stick_hold.py`：持久摇杆能力判定/回退/命令串/坐标缩放/失败恢复 | **21/21 通过** |
| L1 离线单测 | `test_apply_action.py`：边走边打／三条移动入口都走 hold／非 hold 保持原行为／到达即停／回城先松手／skill drag 透传／未知 type 告警与节流 | **33/33 通过** |
| L1 离线单测 | `test_drive_stdin.py`：驱动输入解析／节奏优先级／clamp／脏输入跳过／驱动主循环本体（背压、提前收尾） | **38/38 通过** |
| L1 离线单测 | `test_arrival_gate.py`：到达即停／保险丝／重新计时／边界 | **31/31 通过** |
| L1 离线单测 | `test_esp32_bridge.py`（回归）：命令串与固件解析表逐字对齐 | **29/29 ALL PASS** |
| L1 离线单测 | `test_new_modules.py`（回归）：感知/决策/推进全量 | **191/191 通过** |
| L1 编译验证 | `arduino-cli compile --fqbn esp32:esp32:XIAO_ESP32S3:…`（touch 固件） | ✅ 943,207 B (28%) / RAM 20% |
| L2 历史真机帧 | —— | **不适用**（本轴不依赖画面） |
| L3 实机全流程 | 整 agent 经 ESP32 后端跑完一局 | ❌ **未做**（无运行留痕，见 §5.1） |

链路层实测（2026-10-15，见 `ESP32_HID_HARDWARE.md`）：板子 `192.168.0.107:7300`，
固件自报 `hello poweron <uptime> 3200x1440`，指令往返 **19–22 ms**。
`auto` 的决策路径另经手工实测 4 条：无配置→adb（零探测）／强制 esp32 无地址→报错 exit 2／
强制 adb→adb／给离线地址→约 0.8s 后回落 adb 并打印原因。

---

## 5. 已知限制与未决项（按优先级）

1. **ESP32 通道缺 L3（阻塞"可用"结论）。** 只验证到链路 + 协议原语；没有"整 agent 经
   `--backend esp32` 跑通一局"的证据。要解锁需真机跑一次并留痕（`data/action_events.jsonl`
   中应出现不含 `display` 键的事件，可据此区分通道）。
   **v16.2 的持久摇杆同属此列**：离线命令串与回退路径已测（21/21），但"游戏是否接受
   `t_down` 在摇杆中心 + `t_move` 改朝向"**只能在真机上确认**。真机首跑建议
   `--stick-hold off` 与 `on` 各跑一段做 A/B（其余参数保持一致）。
2. **`wzry/capture/adb.py:41/55` 写死 `"adb"`**，未走 `wzry/adb_util.resolve_adb()`。
   本机 PATH 无 adb，真机取帧会**静默失败**。仅影响采集，不影响 ESP32 注入路径；属低成本技术债。
3. **`--board-size` 与固件 `TOUCH_SURFACE_W/H` 不一致 → 触点整体静默偏移（不报错）。**
   `Esp32HidExecutor.connect()` 只在尺寸不符时打印 warning，不阻断。真机是 `3200x1440`。
4. **鼠标固件做不到多点。** `mode="mouse"` 只能串行（走→松手→放技能），
   无法"摇杆按住 + 技能同按"。真机必须用 `touch` 固件。
5. **配置可能过期。** 板子 IP 变了而 `configs/board.json` 未更新时，`auto` 会多花一个
   `--board-probe-timeout`（默认 0.8s）再回落；要零延迟就 `--backend adb`。
6. **ESP32 只注入、不读屏。** 必须另配感知通道；且选板子会占掉 USB 口（§2.2）。
7. **中文文本不可行。** HID 键盘只传 Usage ID，无输入法上下文；`type` 仅 ASCII。
   中文需走 `adb shell input text` 或剪贴板 + `key cmd+v`。
8. **`Home` / `Back` 键产不出。** 普通 USB 键盘 HID 无法产生 Android `HOME(3)`/`BACK(4)`，
   只能点屏幕上的按钮（固件会回 `err unsupported key`）。
9. **半开连接不释放触点（已识别，本轮有意未改）。** `serviceClient()` 只判断
   `client.connected()`，未开 TCP keepalive；PC 端**静默消失**（休眠 / 断网 / 进程被 kill
   而没发 FIN）时该判断仍为 true → `releaseAllTouches()` 不触发 → **手机上手指卡住**。
   §6 表里"断链会自动松手"只对**正常断开**成立，此处更正。
   *为什么不直接加"空闲超时"*：agent 完全可能合法地长时间不发命令（选人界面 / 暂停），
   一个猜测性的空闲阈值会在对局中途**误断健康连接** —— 属于"可能破坏功能"的改动，而本机
   又无法验证。建议的做法是成对实现：电脑端加 5 s 心跳（`ping`）+ 固件端 30 s 无字节即
   丢弃并 `releaseAllTouches()`，两者一起上才不会误伤。
10. **持久摇杆的航位推算口径未变。** `map_move` 仍按 `dur`（`dist/STICK_SPEED`，350–900 ms）
    名义值推进 `self_est`；持久模式下英雄实际是**连续**移动的，故该估计可能**略微低估**，
    偏向"继续推进"而非提前停下 —— 由 v14.3 塔重锚（`_snap_self_est_to_tower`）纠偏。
    若要精确，应改为按实际 elapsed 推进，但这会与"只改一处以便归因"冲突，故留待 L3 之后再定。
11. **`ActionExecutor.sequence()` 是死代码。** 它被定义在 `if __name__ == "__main__":` 块内，
    类上不可见（`hasattr(ActionExecutor, "sequence") == False`，已实测）。目前无人调用，
    但连招编排若有使用会静默失效。属历史遗留，未在本轮改动。

---

## 6. 真机教训清单（踩坑 → 现行做法）

| 现象 | 根因 | 现行做法 |
|---|---|---|
| 烧写四区段全部 `verified`，但串口无输出、7300 无响应，像"烧坏了" | XIAO 用原生 USB-Serial/JTAG，DTR/RTS 是虚拟的，传统 RTS 复位**无效** → 芯片仍停在 ROM 下载模式 | **烧完按一下 RESET 或拔插 USB**（`ESP32_HID_HARDWARE.md §5.3`） |
| 触点位置正常但整体偏移 | `TOUCH_SURFACE_W/H` 与手机分辨率不一致 | 改 `secrets.h` 重烧；`--board-size` 必须给对（`ESP32_HID_HARDWARE.md §5.1`） |
| 板子反复重启、串口一直打启动日志 | 手机 VBUS 未做 PD 协商默认限流 500mA，ESP32-S3 WiFi 发射峰值 300–350mA | 5V/GND 并 470µF~1000µF + 0.1µF 电容；必要时外部 5V 经肖特基二极管"或"逻辑 |
| 插上手机毫无反应 | 用了纯充电线；或手机未进 Host | 换**带 D+/D-** 的数据线；加 Type-C OTG 转接头 |
| 命令回 `err phone usb asleep` | 手机锁屏，USB 口挂起 | 点亮屏幕。这是正常保护，不是故障 |
| 链路断开后手机上手指卡住 | **正常断开**时固件会自动 `releaseAllTouches()`；但 PC 端**静默消失**（休眠/断网/被 kill）属半开连接，`connected()` 仍为 true → 不会松手 | 目前无解（见 §5.9，需成对加心跳+超时）。临时对策：暂停前手动断开电脑侧 Wi-Fi，或重启板子 |
| 上层以为下发成功、实际被执行层拒绝 | 两端 `MAX_CONTACTS` 不一致（`cid` 越界） | `esp32_hid.py` 的 `MAX_CONTACTS` 与固件 `TOUCH_MAX_CONTACTS` 必须同为 5（`test_esp32_bridge.py E7` 已钉死） |
| 电脑连不上 7300 | IP 错 / 不同网段 / 防火墙 | 看串口打印的 IP；确认同一 Wi-Fi；关防火墙试 |
