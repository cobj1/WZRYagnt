# ESP32 HID 桥：硬件接线与上电检查

> 面向 `firmware/esp32_hid_touch` / `firmware/esp32_hid_bridge` 的硬件侧说明。
> 软件侧接口见 `wzry/control/esp32_hid.py` 的模块 docstring。

---

## 0. 先说结论：这套东西项目里已经实现了

不要重写。现有资产如下：

| 文件 | 角色 |
|---|---|
| `firmware/esp32_hid_touch/esp32_hid_touch.ino` | **主用固件**。自定义 `USBHIDDevice`，对手机表现为**多点触摸屏（Digitizer）+ 键盘**。支持 5 个并发触点，绝对坐标。 |
| `firmware/esp32_hid_bridge/esp32_hid_bridge.ino` | 退路固件。普通相对指针鼠标，单点，做不到"摇杆按住 + 技能同时按"。 |
| `firmware/flash.ps1` | arduino-cli 编译烧写脚本，已固化正确的 FQBN 板级选项。 |
| `wzry/control/esp32_hid.py` | 电脑端 `Esp32HidExecutor`，接口与 `AdbExecutor` 对齐，可整体替换。 |
| `scripts/test_esp32_bridge.py` | 桥的联调测试。 |

**缺的只有硬件接线文档** —— 也就是本文件。

### 已验证的可用基线（2026-10-15）

| 项 | 实测值 |
|---|---|
| 板子 | Seeed XIAO ESP32S3，固件 `esp32_hid_touch` |
| 板子 IP | `192.168.0.107`，端口 `7300`（2.4G SSID `wc`） |
| 固件自报 | `hello poweron <uptime> 3200x1440` |
| 指令往返延迟 | **19–22 ms**（WiFi，`ping` / `t_up` 实测） |
| 手机 | `192.168.0.103`，走**无线 adb**（`5555`），USB 口让给板子 |
| 屏幕分辨率 | `3200x1440`（横屏，对应王者对局方向） |

即：**链路已经完全打通，接线方案已被验证。** 本文档是对该配置的固化说明。

---

## 1. 接线：一句话

> **手机 —— Type-C 数据线 —— 板子的 USB-C 口。就这一步。**

原因是你的板子是 **Seeed XIAO ESP32S3**（由 `flash.ps1` 的 FQBN `esp32:esp32:XIAO_ESP32S3` 确定），它：

- **只有一个 USB-C 口**，因此不存在"该插 USB 口还是 UART 口"的选择问题；
- 该口**直连 ESP32-S3 芯片的原生 USB（GPIO19 = D- / GPIO20 = D+）**，板上**没有 CP2102/CH340 之类的 UART 桥片**；
- 所以它既能被电脑当串口烧录（`flash.ps1` 用 `UploadMode=cdc`，靠原生 USB CDC 下载），也能被手机当 HID 设备。

> 这一点与大多数 ESP32-S3 DevKit 不同：DevKitC 那类板子有 **两个** Type-C 口，标 `UART` 的口上焊了桥片，接手机只会被识别成串口设备。**XIAO 没有这个坑。**

---

## 2. Type-C 直连能不能直接通？取决于 CC 引脚

USB 链路必须**有且只有一个主机（Host）**。手机和 ESP32 出厂默认都是设备（Device）。

Type-C 的角色由 CC（Configuration Channel）上的电阻决定：

- **主机端（DFP）**：CC 上呈 **Rp**（上拉 / 电流源），并负责输出 VBUS 5V
- **设备端（UFP）**：CC 上呈 **5.1kΩ 下拉（Rd）**

手机 USB-C 口通常是 **DRP（双角色）**：检测到对端是 Rd 时，会主动切到 Host 并输出 5V。

### 两种接法

| 接法 | 说明 | 建议 |
|---|---|---|
| **手机 → C-to-C 数据线 → 板子** | 手机检测到板端 Rd，切 Host 并供电。现代 Android 手机接 C-to-C 的 U 盘能识别，说明该路径成立。 | 先试这个，最省事 |
| **手机 → Type-C OTG 转接头 → 数据线 → 板子** | 转接头内含 Rp，**强制**手机进入 Host，不依赖手机的 DRP 行为。 | 直连不认时用这个 |

### 线材要求

必须是**带 D+/D- 的数据线**。很多随附线是纯充电线（CC 还在但数据对不完整），会导致枚举失败。
判断方法：用这根线接电脑，能否识别到设备。

### 什么时候需要反过来（ESP32 当 Host）

只有走 **AOA / ADB over USB** 方案时才需要 ESP32 当主机。那要求板端 CC 改 **Rp 上拉**、
并由 ESP32 自己给 VBUS 供 5V + 过流保护 —— 属于定制硬件，**本项目不走这条路**（走的是
Wi-Fi TCP 受控 + USB HID 输出）。

---

## 3. 为什么是 S3，不是 S2 或经典 ESP32

| 芯片 | 原生 USB OTG | 能否跑本固件 |
|---|---|---|
| **ESP32-S3** | ✅ | ✅ 本项目目标芯片（XIAO ESP32S3） |
| ESP32-S2 | ✅ | 理论可行（有 USB OTG），但**单核**：WiFi 任务与 USB HID 报告发送共享一个核，时序抖动风险明显高于 S3，不建议 |
| ESP32 经典款（WROOM-32 / DevKitC） | ❌ 仅 USB-UART 桥 | ❌ **完全不行**，没有原生 USB，做不出 HID 设备 |

S3 的双核让 WiFi 收发和 USB 报告互不抢占，这是本项目选 S3 的实际理由。

---

## 4. 供电：最容易踩的坑

手机当主机时，VBUS 由手机提供，未做 PD 协商时默认限流 **500mA**（USB 2.0 标准）。

- ESP32-S3 在 **WiFi 发射瞬间**峰值电流可达 **300–350mA**，加上浪涌容易触发手机限流。
- 触发后的典型症状：**板子反复重启**，串口不断打印启动日志，USB 反复枚举。
  固件里有 `WIFI_REBOOT_MS` 看门狗，但那是"Wi-Fi 长期不可用才重启"，掉电重启属于另一类现象。

**处理办法：**

1. 在板子的 **5V 与 GND 之间并联 470µF ~ 1000µF 电解电容** + 0.1µF 陶瓷电容，抑制 WiFi 峰值跌落。
   XIAO ESP32S3 的 5V 引脚在排针上，可直接焊电容。
2. 若仍不稳，改用**外部 5V** 给板子供电，但**不要切断 VBUS 与手机的连接**（手机需要检测到 VBUS 才进 Host）。
   外部 5V 与手机 VBUS 之间用肖特基二极管做"或"逻辑，防止倒灌。
3. **不要串接 PD 诱骗模块** —— 会把 VBUS 拉到 9V/12V，直接烧板子。

**绝对不要**把板子的 5V/VIN 接到手机的 D+/D-，也不要给 3.3V 引脚灌 5V。

---

## 5. 烧录与上电顺序

### 5.1 配置

```powershell
# 首次：从模板生成本机配置
copy firmware\esp32_hid_touch\secrets.example.h firmware\esp32_hid_touch\secrets.h
# 然后编辑 secrets.h：
#   WIFI_SSID / WIFI_PASS   必须是 2.4GHz 频段（ESP32-S3 不支持 5GHz）
#   BRIDGE_PORT             默认 7300，电脑端要一致
#   TOUCH_SURFACE_W / H     必须与目标手机分辨率一致
```

> ⚠️ **本机当前 `secrets.h` 里 `TOUCH_SURFACE_W=3200 / H=1440`（横屏声明，对应王者对局方向）。**
> 换手机或改方向时这两个值必须同步改，否则**触点会整体偏移**——这是最容易误判为"固件有 bug"的问题。
> `secrets.h` 含 Wi-Fi 密码，已在 `.gitignore` 中，勿提交。

### 5.2 烧录（板子接电脑，不接手机）

```powershell
.\firmware\flash.ps1 -Mode touch      # 主用固件
.\firmware\flash.ps1 -Mode mouse      # 退路固件
.\firmware\flash.ps1 -Mode touch -BuildOnly   # 只编译
.\firmware\flash.ps1 -Mode touch -Port COM7   # 指定串口
```

`flash.ps1` 已固化的板级选项（缺一不可）：

```
USBMode=default        USB-OTG (TinyUSB) —— 否则没有 HID
CDCOnBoot=default      注意这个名字是反的：default = Enabled，保留 USB CDC，
                       才能用 1200bps 触摸自动进下载模式
UploadMode=cdc         USB-OTG CDC (TinyUSB)
FlashSize=8M, PartitionScheme=default_8MB
```

### 5.3 ⚠️ 烧完必须物理复位

**这是本项目实测踩过的坑，务必记住：**

`arduino-cli upload` 结束时会打印 `Hard resetting via RTS pin...`，但 XIAO ESP32S3 用的是
**USB-Serial/JTAG**（原生 USB），其 DTR/RTS 是**虚拟的**，走传统 RTS 时序的复位**对芯片无效**。

后果：烧写四个区段全部 `Hash of data verified` 成功，但芯片**仍停在 ROM 下载模式**
（USB 描述符 PID 仍是 `1001`），串口无输出，局域网 7300 端口无响应 —— 看起来像"烧坏了"，实际只是没启动。

**解决：烧完后按一下板上的 RESET 键，或拔插一次 USB。** 之后才能看到 `[bridge] touch 版启动…`。

### 5.4 上电顺序

1. 板子先接电脑烧录，**物理复位**，串口 115200 观察日志，确认打印 `[bridge] touch 版启动，屏幕 3200x1440`。
2. 拔掉电脑，**接手机**（经数据线 / OTG 转接头）。
3. 固件里有 `USB_SETTLE_MS = 1500`，上电后会等主机完成 HID 枚举，这是正常的。
4. 板子连上 Wi-Fi 后，串口打印 `[bridge] Wi-Fi 已连接，本机 IP <x.x.x.x>，端口 7300`。
5. 电脑连板子：

```powershell
python -m wzry.control.esp32_hid --host <板子IP> ping
```

> 注：`wzry/control/esp32_hid.py` 的自测 CLI **没有 `--device-size` 参数**（只有
> `--host/--port/--mode/--event-log` + 子命令），`device_size` 只在生产路径
> `m2_agent_v2.py` 里传入。自测时坐标以固件自报的 `3200x1440` 为准。

---

## 6. 协议速查（touch 版，端口 7300）

板子**不含任何决策逻辑**，逐行收命令，回 `ok` 或 `err <reason>`。
连接建立时板子**主动上报**一行：

```
hello <reset_reason> <uptime_ms> <W>x<H>
```

| 命令 | 说明 |
|---|---|
| `ping` | 探活 → `ok` |
| `t_tap <x> <y> <ms>` | 单点按下 x,y 保持 ms 后抬起，**自动挑空闲触点** |
| `t_swipe <x1> <y1> <x2> <y2> <ms>` | 单指从起点拖到终点，全程按住 ms，**自动挑空闲触点** |
| `t_down <id> <x> <y>` | 第 id 个触点按下（多指并发用，id ∈ 0..4） |
| `t_move <id> <x> <y>` | 第 id 个触点移到绝对坐标 |
| `t_up <id>` | 第 id 个触点抬起 |
| `type <text>` | 键盘输入，**仅 ASCII** |
| `key <combo>` | 按键，如 `enter` / `cmd+h` / `noop` |
| `stat` | 只读诊断 → `ok cmds=N drops=N down=N up=Ns`（`drops`=HID 报告发送失败次数） |
| `home` `move` `press` `release` `click` `scroll` | 一律回 `err mode is touch`（属另一套固件） |

`t_tap` / `t_swipe` 的 `ms` 上限为 `SWIPE_MS_MAX`（3000），超出按上限执行。

**坐标单位是手机物理像素**（`0..W-1` / `0..H-1`），固件内部线性映射到 HID 逻辑范围 `0..32767`。

### 多点并发怎么用

左手摇杆 + 右手技能同时按，靠**固定触点 id**：

```
t_down 0 300 1200     # 触点 0 按住摇杆
t_down 1 2500 1000    # 触点 1 按技能
t_up 1                # 松开技能，摇杆不受影响
t_move 0 350 1200     # 摇杆继续拖
```

固件里 `t_tap` / `t_swipe` 会**自动挑空闲触点**，不会抢占上层正按住的那个（固定用触点 0 会直接打断摇杆）。

---

## 7. 手机侧验证

```bash
# 确认设备被识别为触摸屏
adb shell dumpsys input | grep -i -A3 touch

# 观察原始事件（eventN 按上一步的输出替换）
adb shell getevent -lt /dev/input/eventN
```

**最直观的方法**：设置 → 开发者选项 → **显示指针位置**（或"指针位置"），打开后手指/触点位置会实时画在屏幕上。
用 `t_tap` 打一个点，看画的十字是否落在预期位置 —— 这一步能立刻暴露"坐标整体偏移"问题。

---

## 8. 排错

| 现象 | 根因 | 处理 |
|---|---|---|
| 插上手机毫无反应 | 用了充电线；或手机没进 Host 模式 | 换数据线；加 Type-C OTG 转接头 |
| 手机提示"USB 设备不受支持" | 板子没跑起来 / USB Mode 配错 | 确认 FQBN 里 `USBMode=default`；看串口日志 |
| 板子反复重启 | 手机 VBUS 限流导致掉电 | 加 470µF+ 电容；或外部 5V + 二极管或逻辑 |
| 命令回 `err phone usb asleep` | 手机锁屏，USB 口挂起 | 点亮屏幕。这是正常保护，不是故障 |
| 命令回 `err mode is touch` | 烧的是 touch 固件，却发了鼠标命令 | 电脑端 `mode="touch"`；或改烧 mouse 固件 |
| 命令回 `err unsupported key` | 发了 `home` / `back` / 音量键 | USB 键盘 HID 产生不了 Android 的 HOME(3)/BACK(4)，只能点屏幕上的按钮 |
| 命令回 `err non-ascii` | `type` 传了中文 | HID 键盘只传 Usage ID，没有输入法上下文。中文走 `adb shell input text`，或写剪贴板后 `key cmd+v` |
| 命令回 `err no free contact` | 5 个触点全被占住 | 检查是否有触点忘了 `t_up` |
| **触点整体偏移** | `TOUCH_SURFACE_W/H` 与手机分辨率不一致 | 改 `secrets.h` 后重新烧写 |
| 触点位置正常但游戏无响应 | 触点被识别成 hover 而非 touch | 检查描述符的 Tip Switch / In Range 位；用"显示指针位置"确认 |
| 链路断开后手机上手指卡住 | —— | 固件已在断开时自动 `releaseAllTouches()`，无需处理 |
| 电脑连不上 7300 | IP 错 / 不同网段 / 防火墙 | 看串口打印的 IP；确认同一 Wi-Fi；关防火墙试 |

---

## 9. 与 ADB 路线的取舍

| 维度 | ESP32 HID 桥（本项目） | WiFi ADB |
|---|---|---|
| 硬件成本 | XIAO ESP32S3 + 数据线 | 0 |
| 屏幕读取 | ❌ 只能注入，不能读屏 | ✅ 可 `screencap` |
| 需要开 USB 调试 | 否 | 是（首次） |
| 反检测 | 与真实触摸不可区分 | 部分游戏检测 `adb` / 调试状态 |
| 延迟 | 低（HID 直注，无截图开销） | 中（30–80ms，含截图更高） |

`wzry/control/esp32_hid.py` 的 `Esp32HidExecutor` 与 `AdbExecutor` 接口完全对齐，
两者可整体替换 —— 感知走 ADB 拿画面、执行走 HID 注触点，是可行的混合配置。

> ⚠️ 上表说的是**读轴/写轴可分离**（`--capture` 与 `--backend` 是独立参数）。
> **执行（注入）本身是互斥的**：`ActionExecutor` 只持有一个 executor，一次只走一条
> 通道。两路同时下发会互相抢触点，且无从仲裁。别把"感知可以走 ADB"读成
> "两个执行通道能一起开"。

---

## 10. 执行通道选择策略（v16.1）

> 本节是随硬件文档的**速查**。设计意图、执行互斥性论证、验证等级与未决项见
> [ARCH_CONTROL_CHANNEL.md](ARCH_CONTROL_CHANNEL.md)。

### 10.1 一句话

**默认优先板子；板子不在线自动回落 adb；也可以用 `--backend` 强制指定。**

### 10.2 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--backend` | `auto` | `auto`=优先板子（不通回落 adb）/ `adb`=强制 adb / `esp32`=强制板子 |
| `--board-host` | 无 | 板子 IP。`esp32` 下必填；`auto` 下可不填（走环境变量或已存配置） |
| `--board-port` | `7300` | 须与固件 `BRIDGE_PORT` 一致 |
| `--board-mode` | 取配置，否则 `touch` | 须与烧的固件一致 |
| `--board-size` | 取配置，否则 `1080x2340` | **真机 3200x1440 必须给对**，否则触点整体静默偏移 |
| `--board-probe-timeout` | `0.8` | `auto` 探测板子的连接超时（秒） |
| `--stick-hold` | `on` | 摇杆下发方式：`on`=持久触点（`t_down` 按住 + 每次 `t_move` 只改朝向，英雄连续移动）；`off`=回退 `t_swipe`（推一次就松手，占执行线程 200–1000ms）。仅对 `mode=touch` 生效，adb / mouse 固件自动回退 |

板子地址解析优先级：`--board-host` > 环境变量 `WZRY_BOARD_HOST` > `configs/board.json`。

### 10.3 典型用法

```powershell
# 默认：优先板子，板子不在线就用 MuMu adb —— 不用敲任何板子参数
python scripts/m2_agent_v2.py --action

# 板子只连过一次之后，auto 会自动认出来（地址已存 configs/board.json）
python scripts/m2_agent_v2.py --action --board-size 3200x1440

# 强制真机板子（探测不通直接报错，不会偷偷用 adb）
python scripts/m2_agent_v2.py --action --backend esp32 --board-host 192.168.0.107 --board-size 3200x1440

# 强制模拟器 adb（连板子都不探测，零额外延迟）
python scripts/m2_agent_v2.py --action --backend adb

# 只查决策，不执行任何动作（排查"到底用了哪条通道"）
python -m wzry.control.channel --board-host 192.168.0.107
python -m wzry.control.channel --show-config

# 板子只读诊断（报告丢弃计数 / 已处理命令数 / 当前按下的触点数）
python -m wzry.control.esp32_hid --host 192.168.0.107 stat

# 摇杆 A/B 归因：其余参数保持一致, 只切 --stick-hold
python scripts/m2_agent_v2.py --action --backend esp32 --board-host 192.168.0.107 --board-size 3200x1440 --stick-hold off
python scripts/m2_agent_v2.py --action --backend esp32 --board-host 192.168.0.107 --board-size 3200x1440 --stick-hold on
```

### 10.4 三条不变量（改动时不要破坏）

1. **`auto` 在没有任何板子地址时零探测。** 与旧版 `--backend adb` 行为一致、
   启动不增加延迟。这是既有 MuMu 工作流不受影响的前提。
2. **强制即强制。** `--backend esp32` 拿不到地址或探测不通时**明确报错退出**，
   绝不静默回落 adb —— 否则真机调试会误判成"板子没生效"。
3. **探测成功才落盘。** 只有连上并收到 `hello` 才写 `configs/board.json`，
   避免把错地址持久化下来。

离线自测（不需要板子/模拟器）：`venv/Scripts/python.exe -X utf8 scripts/test_channel_select.py`

### 10.5 采集端（与执行通道是两个独立的轴，v16.3）

**执行通道选板子 ≠ 采集通道就对了。** 真机必须同时把采集端配好，否则会出现"动作正常但画面是错的/根本采不到"。

| 参数 | 真机该怎么给 | 不给会怎样 |
|---|---|---|
| `--serial` | 手机序列号，如 `192.168.0.103:5555` | 采集器默认 `127.0.0.1:16384`（MuMu），**静默 0 帧**，只表现为 `wait_frame` 每 2 秒超时一次 |
| `--capture` | **`scrcpy`**（推荐）；`screencap` 不可用于 3200×1440 | `desktop` 抓的是电脑屏幕；`screencap` 单帧实测 **0.35–1.9 秒**，经常超过 3 秒超时 → 0 帧 |
| `--no-annot` | 追帧率时加 | 每拍多写 2 张标注预览 PNG（纯人工看图，不参与决策） |

**为什么 20:9 必须用 scrcpy**

- `screencap` 走的是「手机端 PNG 编码 + Wi-Fi 传 3.7MB」，3200×1440 实测 **1.884 秒/帧**（≈0.5 fps），
  且随画面复杂度在 0.35–1.9 秒间剧烈波动，超时后整轮 0 帧。
- v16.3 之前 `screencap_capture.py` 还把 3200×1440 **强行拉成 1280×720**（宽高比 2.222→1.778，纵向拉伸 1.25×），
  正方形小地图被拉成非方形，`0.336×h` 一类几何判据与模板匹配全部失真且**不报错**。
  现已改为**保持宽高比、长边缩到 1280**（与 scrcpy 的 `--max-size 1280` 对齐）：
  20:9 → `1280×576`，16:9(1920×1080) → `1280×720`（对 MuMu 路径零变化）。
- `scrcpy` 走 H.264 流，交付帧 `1280×576`，几何正确且吞吐稳定。

**实测帧率**（Redmi K60 真机对局，各 3 次 ×10 秒）

| 配置 | 3 次帧数 | 说明 |
|---|---|---|
| `scrcpy --no-annot` | 119 / 111 / 92 | 中位 **111（≈11 fps）** |
| `scrcpy`（标注开） | 90 / 128 / 101 | 中位 **101（≈10 fps）** |
| `screencap --no-annot` | 0 / 0 / 0 | 单帧 1.88 秒，超时后整轮无帧 |

> ⚠️ **`完成: N 帧` 里的 N 不是唯一采集帧数。** `wait_frame` 只要当前帧龄 <1.0 秒就**重复返回同一帧**
> （`screencap_capture.py:116-122`），所以采集慢时 N 会被放大（曾出现 6 秒 182 帧的假象）。
> 判断吞吐要看 `[timing]` 里 `wait_frame` 的均次耗时，或直接单测一次采集的墙钟耗时。


