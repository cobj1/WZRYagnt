# flash.ps1 —— 编译并烧写 ESP32 HID 桥固件（Windows / PowerShell）
#
# 工具链（本机已装好）：
#   arduino-cli 1.5.1  ->  WZRYagnt\tools\arduino-cli\arduino-cli.exe（tools/ 已 gitignore）
#   esp32 core 3.3.4   ->  %LOCALAPPDATA%\Arduino15
#   代理设置在 arduino-cli.yaml 的 network.proxy（arduino-cli 不吃 HTTP_PROXY 环境变量）
#
# 从零重装的话：
#   下载 arduino-cli 解压到 tools\arduino-cli\
#   arduino-cli config init
#   arduino-cli config add board_manager.additional_urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
#   arduino-cli config set network.proxy http://127.0.0.1:7897      # 需要翻墙时
#   arduino-cli core update-index
#   arduino-cli core install esp32:esp32@3.3.4
#
# 用法：
#   .\flash.ps1              # 烧 mouse 版（自动探测串口）
#   .\flash.ps1 -Mode touch  # 烧 touch 版（多点触摸屏，推荐；对应电脑端 mode="touch"）
#   .\flash.ps1 -Port COM7   # 指定串口
#   .\flash.ps1 -BuildOnly   # 只编译，不烧写
#   .\flash.ps1 -Clean       # 先清编译缓存
#
# 两版固件的命令集不同，电脑端 mode 必须与烧的固件一致：
#   mouse -> esp32_hid_bridge（相对指针，单点，做不到摇杆+技能同时按）
#   touch -> esp32_hid_touch （多点绝对坐标 Digitizer，真机打王者用这个）
param(
  [string]$Port,
  [ValidateSet("touch", "mouse")][string]$Mode = "mouse",
  [switch]$BuildOnly,
  [switch]$Clean
)

$ErrorActionPreference = "Stop"
$Here   = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($Mode -eq "touch") {
  $Sketch = Join-Path $Here "esp32_hid_touch"
} else {
  $Sketch = Join-Path $Here "esp32_hid_bridge"
}

# 板级选项：XIAO ESP32S3
#   USBMode=default    USB-OTG (TinyUSB) —— 必须，否则没有 HID
#   CDCOnBoot=default  这个名字是反的：default = Enabled，保留 USB CDC
#   UploadMode=cdc     USB-OTG CDC (TinyUSB)，可用 1200bps 触摸自动进下载模式
$Fqbn = "esp32:esp32:XIAO_ESP32S3:USBMode=default,CDCOnBoot=default,UploadMode=cdc,FlashSize=8M,PartitionScheme=default_8MB"

# 找 arduino-cli：先 PATH，再仓库内的 tools\arduino-cli
$Cli = (Get-Command arduino-cli -ErrorAction SilentlyContinue).Source
if (-not $Cli) {
  $Local = Join-Path (Split-Path -Parent $Here) "tools\arduino-cli\arduino-cli.exe"
  if (Test-Path $Local) { $Cli = $Local }
}
if (-not $Cli) {
  Write-Error "未找到 arduino-cli；请解压到 tools\arduino-cli\ 或装到 PATH（见本脚本头部注释）"
}
Write-Host "arduino-cli: $Cli" -ForegroundColor DarkGray

$Secrets = Join-Path $Sketch "secrets.h"
if (-not (Test-Path $Secrets)) {
  Copy-Item (Join-Path $Sketch "secrets.example.h") $Secrets
  Write-Host "已生成 $Secrets —— 请填好 Wi-Fi 名/密码后重新运行。" -ForegroundColor Yellow
  exit 1
}
if (Select-String -Path $Secrets -Pattern "your-2g-ssid" -Quiet) {
  if (-not $BuildOnly) {
    Write-Error "secrets.h 里的 WIFI_SSID 还是示例值，先改成你自己的 2.4GHz Wi-Fi"
  }
  Write-Host "注意：secrets.h 还是占位 Wi-Fi，只能编译不能真跑。" -ForegroundColor Yellow
}

Write-Host "编译…" -ForegroundColor Cyan
if ($Clean) { & $Cli compile --clean --fqbn $Fqbn $Sketch } else { & $Cli compile --fqbn $Fqbn $Sketch }
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($BuildOnly) { Write-Host "只编译模式，结束。" -ForegroundColor Green; exit 0 }

if (-not $Port) {
  Write-Host "探测串口…"
  $json = & $Cli board list --format json | ConvertFrom-Json
  $Port = ($json | Where-Object { $_.port.protocol -eq "serial" } |
           Select-Object -First 1).port.address
  if (-not $Port) { Write-Error "没找到串口设备，插上板子或用 -Port 指定" }
  Write-Host "使用串口 $Port"
}

Write-Host "烧写…" -ForegroundColor Cyan
& $Cli upload -p $Port --fqbn $Fqbn $Sketch
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "完成。板子重启后会连 Wi-Fi；用串口日志看它的 IP，然后：" -ForegroundColor Green
Write-Host "  python -m wzry.control.esp32_hid --host <板子IP> ping"
