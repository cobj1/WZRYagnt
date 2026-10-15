// secrets.example.h —— 复制成同目录下的 secrets.h 再填写。
// secrets.h 是本机私密配置（Wi-Fi 密码），不要提交到版本库。
#pragma once

// 必须是 2.4GHz 频段的 Wi-Fi：ESP32-S3 不支持 5GHz
#define WIFI_SSID "your-2g-ssid"
#define WIFI_PASS "your-wifi-password"

// 板子监听的 TCP 端口；电脑端要一致：Esp32HidExecutor(host=..., port=BRIDGE_PORT)
#define BRIDGE_PORT 7300
