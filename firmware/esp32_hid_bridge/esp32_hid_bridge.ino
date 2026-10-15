/*
 * esp32_hid_bridge —— ESP32-S3 USB HID 桥（mouse 版固件）
 *
 * 角色
 *   板子插在手机的 USB 口上，对手机表现为「标准 USB 鼠标 + 键盘」；
 *   同时板子连到 Wi-Fi，电脑作为客户端连到板子的 TCP 服务端（默认 7300），
 *   逐行发命令，板子回 "ok" 或 "err <reason>"。板子本身不含任何决策逻辑。
 *
 *   对应电脑端：wzry/control/esp32_hid.py 的 Esp32HidExecutor(mode="mouse")
 *
 * 硬件
 *   ESP32-S3（必须是 S3，普通 ESP32 没有原生 USB）。已在 XIAO ESP32S3 的板级定义下编写。
 *   板级选项（arduino-cli 的 FQBN 后缀，缺一不可）：
 *     USBMode=default      必须：USB-OTG (TinyUSB)，否则没有 HID
 *     CDCOnBoot=default    注意这个名字是反的，default = Enabled：保留 USB CDC，
 *                          才能用 1200bps 触摸自动进下载模式；顺带能看串口日志
 *     UploadMode=cdc       USB-OTG CDC (TinyUSB)
 *     FlashSize=8M, PartitionScheme=default_8MB
 *
 * 协议（mouse 版）
 *   hello <reset_reason> <uptime_ms>   连接建立后板子主动上报
 *   ping                               -> ok
 *   home                               -> 指针顶到左上角 (0,0)（相对鼠标靠大负位移撞边界）
 *   move <dx> <dy>                     -> 相对移动（自动按 ±127 分片、按 ~1ms 节奏发）
 *   press / release / click            -> 左键按下 / 抬起 / 点一下
 *   scroll <v> <h>                     -> 滚轮 / 横向滚轮
 *   type <text>                        -> 键盘输入 ASCII
 *   key <combo>                        -> 按键，如 enter / cmd+h / ctrl+alt+del；noop = 空操作
 *   t_*                                -> 一律回 err mode is mouse（触摸屏是另一套固件）
 *
 * 已知限制
 *   - 单点：普通 HID 鼠标只有一个指针，做不到「左手摇杆按住 + 右手技能同时按」。
 *     真正打王者荣耀需要 HID 触摸屏（多点 digitizer）固件，这是本文件之后的下一步。
 *   - 键盘 HID 无法产生 Android 的 HOME(3)/BACK(4)：这两个键在 USB 键盘上不存在，
 *     只能靠点屏幕上的按钮。
 */

#include <WiFi.h>
#include <esp_system.h>

#include "USB.h"
#include "USBHIDMouse.h"
#include "USBHIDKeyboard.h"

#include "secrets.h"

#if !defined(WIFI_SSID) || !defined(WIFI_PASS)
#error "缺少 Wi-Fi 配置：请把 secrets.example.h 复制成 secrets.h 并填写"
#endif

#ifndef BRIDGE_PORT
#define BRIDGE_PORT 7300
#endif

// ---------- 可调参数 ----------
#define HOME_CHUNKS 32        // home 时重复发 -127 的次数（32*127≈4064px，覆盖到 4K 屏）
#define MOVE_STEP_PX 48       // move 分片的单步最大像素（必须 ≤ 127）
// 不需要额外加延时：USBHID::SendReport 内部会等主机取走上一个报告，天然不会丢包
#define CLICK_HOLD_MS 40      // click 的按下时长
#define KEY_HOLD_MS 30        // 组合键的按住时长
#define CMD_LINE_MAX 200          // 单行命令上限（含结尾 '\0'）
#define WIFI_RETRY_MS 5000UL  // Wi-Fi 重连间隔
#define WIFI_REBOOT_MS 120000UL  // 断网超过此时间则重启板子（看门狗）
#define USB_SETTLE_MS 1500    // 上电后等主机完成 HID 枚举

USBHIDMouse Mouse;
USBHIDKeyboard Keyboard;
WiFiServer server(BRIDGE_PORT);
WiFiClient client;

static char lineBuf[CMD_LINE_MAX];
static size_t lineLen = 0;
static volatile bool usbSuspended = false;  // 手机锁屏时 USB 口会挂起
static uint32_t wifiDownSince = 0;
static uint32_t wifiLastTry = 0;

// ---------- 按键名表 ----------
struct KeyName {
  const char *name;
  uint8_t code;
};

static const KeyName KEY_TABLE[] = {
    {"enter", KEY_RETURN},        {"return", KEY_RETURN},
    {"esc", KEY_ESC},             {"escape", KEY_ESC},
    {"tab", KEY_TAB},             {"space", KEY_SPACE},
    {"backspace", KEY_BACKSPACE}, {"delete", KEY_DELETE},
    {"del", KEY_DELETE},          {"insert", KEY_INSERT},
    {"pageup", KEY_PAGE_UP},      {"pagedown", KEY_PAGE_DOWN},
    {"up", KEY_UP_ARROW},         {"down", KEY_DOWN_ARROW},
    {"left", KEY_LEFT_ARROW},     {"right", KEY_RIGHT_ARROW},
    {"menu", KEY_MENU},           {"capslock", KEY_CAPS_LOCK},
    {"numlock", KEY_NUM_LOCK},    {"printscreen", KEY_PRINT_SCREEN},
    {"pause", KEY_PAUSE},
    {"f1", KEY_F1},   {"f2", KEY_F2},   {"f3", KEY_F3},   {"f4", KEY_F4},
    {"f5", KEY_F5},   {"f6", KEY_F6},   {"f7", KEY_F7},   {"f8", KEY_F8},
    {"f9", KEY_F9},   {"f10", KEY_F10}, {"f11", KEY_F11}, {"f12", KEY_F12},
    // 修饰键（press/release 内部按 0x80..0x87 当修饰位处理）
    {"ctrl", KEY_LEFT_CTRL},      {"lctrl", KEY_LEFT_CTRL},
    {"rctrl", KEY_RIGHT_CTRL},    {"shift", KEY_LEFT_SHIFT},
    {"lshift", KEY_LEFT_SHIFT},   {"rshift", KEY_RIGHT_SHIFT},
    {"alt", KEY_LEFT_ALT},        {"lalt", KEY_LEFT_ALT},
    {"ralt", KEY_RIGHT_ALT},      {"altgr", KEY_RIGHT_ALT},
    {"cmd", KEY_LEFT_GUI},        {"gui", KEY_LEFT_GUI},
    {"win", KEY_LEFT_GUI},        {"meta", KEY_LEFT_GUI},
    {"super", KEY_LEFT_GUI},      {"rcmd", KEY_RIGHT_GUI},
};

// 键盘 HID 产生不了、需要点屏幕的 Android 导航键（明确回错，不静默错按）
static const char *UNSUPPORTED_KEYS[] = {"home", "back", "recents", "appswitch",
                                         "power", "volumeup", "volume_up",
                                         "volumedown", "volume_down", "mute"};

// ---------- 小工具 ----------
static const char *resetReasonName(esp_reset_reason_t r) {
  switch (r) {
    case ESP_RST_POWERON:  return "poweron";
    case ESP_RST_EXT:      return "ext";
    case ESP_RST_SW:       return "sw";
    case ESP_RST_PANIC:    return "panic";
    case ESP_RST_INT_WDT:  return "int_wdt";
    case ESP_RST_TASK_WDT: return "task_wdt";
    case ESP_RST_WDT:      return "wdt";
    case ESP_RST_DEEPSLEEP:return "deepsleep";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_SDIO:     return "sdio";
    default:               return "unknown";
  }
}

static void reply(const char *s) {
  if (client && client.connected()) {
    client.print(s);
    client.print('\n');
  }
}

static void releaseAllHid() {
  Mouse.release(MOUSE_ALL);
  Keyboard.releaseAll();
}

static void sendHello() {
  char buf[64];
  snprintf(buf, sizeof(buf), "hello %s %lu", resetReasonName(esp_reset_reason()),
           (unsigned long)millis());
  client.print(buf);
  client.print('\n');
}

static void onUsbEvent(void *arg, esp_event_base_t base, int32_t id, void *data) {
  (void)arg;
  (void)base;
  (void)data;
  if (id == ARDUINO_USB_SUSPEND_EVENT) {
    usbSuspended = true;
  } else if (id == ARDUINO_USB_STARTED_EVENT || id == ARDUINO_USB_RESUME_EVENT) {
    usbSuspended = false;
  }
}

static void trimInPlace(char *s) {
  char *p = s;
  while (*p == ' ' || *p == '\t') p++;
  if (p != s) memmove(s, p, strlen(p) + 1);
  size_t n = strlen(s);
  while (n > 0 && (s[n - 1] == ' ' || s[n - 1] == '\t' || s[n - 1] == '\r')) s[--n] = 0;
}

// ---------- 鼠标原语 ----------
static bool busy() {
  if (usbSuspended) {
    reply("err phone usb asleep");
    return true;
  }
  return false;
}

static void doHome() {
  if (busy()) return;
  // 相对指针没有绝对定位，只能朝左上角一直推，撞到屏幕边界即停，于是落在 (0,0)。
  for (int i = 0; i < HOME_CHUNKS; i++) {
    Mouse.move(-127, -127);
  }
  reply("ok");
}

static void doMove(int dx, int dy) {
  if (busy()) return;
  int step = MOVE_STEP_PX;
  if (step > 127) step = 127;
  long n = labs(dx) > labs(dy) ? labs(dx) : labs(dy);
  int chunks = (int)((n + step - 1) / step);
  if (chunks == 0) chunks = 1;
  int doneX = 0, doneY = 0;
  for (int i = 1; i <= chunks; i++) {
    int tx = (int)((long)dx * i / chunks);
    int ty = (int)((long)dy * i / chunks);
    Mouse.move((int8_t)constrain(tx - doneX, -127, 127),
               (int8_t)constrain(ty - doneY, -127, 127));
    doneX = tx;
    doneY = ty;
  }
  reply("ok");
}

static void doScroll(int v, int h) {
  if (busy()) return;
  int8_t wheel = (int8_t)constrain(v, -127, 127);
  int8_t pan = (int8_t)constrain(h, -127, 127);
  Mouse.move(0, 0, wheel, pan);
  reply("ok");
}

static void doClick() {
  if (busy()) return;
  Mouse.press(MOUSE_LEFT);
  delay(CLICK_HOLD_MS);
  Mouse.release(MOUSE_LEFT);
  reply("ok");
}

// ---------- 键盘原语 ----------
static bool lookupKey(const char *name, uint8_t *code, bool *isMod) {
  if (name[0] != 0 && name[1] == 0) {  // 单字符：走 ASCII 表
    *code = (uint8_t)name[0];
    *isMod = false;
    return true;
  }
  for (size_t i = 0; i < sizeof(KEY_TABLE) / sizeof(KEY_TABLE[0]); i++) {
    if (strcmp(name, KEY_TABLE[i].name) == 0) {
      *code = KEY_TABLE[i].code;
      *isMod = (KEY_TABLE[i].code >= 0x80 && KEY_TABLE[i].code <= 0x87);
      return true;
    }
  }
  return false;
}

static bool isUnsupportedKey(const char *name) {
  for (size_t i = 0; i < sizeof(UNSUPPORTED_KEYS) / sizeof(UNSUPPORTED_KEYS[0]); i++) {
    if (strcmp(name, UNSUPPORTED_KEYS[i]) == 0) return true;
  }
  return false;
}

static void doKey(char *combo) {
  if (busy()) return;
  if (!strcmp(combo, "noop") || !strcmp(combo, "none")) {  // 空操作（探活/测延迟用）
    reply("ok");
    return;
  }
  uint8_t mods[4];
  int nMods = 0;
  uint8_t mainKey = 0;
  bool hasMain = false;

  char *tok = strtok(combo, "+");
  while (tok != NULL) {
    trimInPlace(tok);
    if (isUnsupportedKey(tok)) {
      reply("err unsupported key (Android HOME/BACK 只能点屏幕)");
      return;
    }
    uint8_t code;
    bool isMod;
    if (!lookupKey(tok, &code, &isMod)) {
      reply("err unknown key");
      return;
    }
    if (isMod && !hasMain && nMods < 4) {
      mods[nMods++] = code;
    } else {
      mainKey = code;
      hasMain = true;
    }
    tok = strtok(NULL, "+");
  }

  for (int i = 0; i < nMods; i++) Keyboard.press(mods[i]);
  if (hasMain) Keyboard.press(mainKey);
  delay(KEY_HOLD_MS);
  Keyboard.releaseAll();  // 一次放开全部，避免卡键
  reply("ok");
}

static void doType(const char *text) {
  if (busy()) return;
  for (const char *p = text; *p; p++) {
    if ((unsigned char)*p > 0x7F) {
      Keyboard.releaseAll();
      reply("err non-ascii");
      return;
    }
  }
  Keyboard.print(text);
  Keyboard.releaseAll();
  reply("ok");
}

// ---------- 行分发 ----------
static void handleLine(char *line) {
  trimInPlace(line);
  if (line[0] == 0) return;  // 空行不回复

  char *sp = strchr(line, ' ');
  const char *cmd = line;
  char *rest = NULL;
  if (sp != NULL) {
    *sp = 0;
    rest = sp + 1;
    while (*rest == ' ') rest++;
  }

  if (!strcmp(cmd, "ping")) {
    reply("ok");
    return;
  }
  if (!strcmp(cmd, "home")) {
    doHome();
    return;
  }
  if (!strcmp(cmd, "move")) {
    int dx, dy;
    if (rest == NULL || sscanf(rest, "%d %d", &dx, &dy) != 2) {
      reply("err bad args");
      return;
    }
    doMove(dx, dy);
    return;
  }
  if (!strcmp(cmd, "scroll")) {
    int v, h;
    if (rest == NULL || sscanf(rest, "%d %d", &v, &h) != 2) {
      reply("err bad args");
      return;
    }
    doScroll(v, h);
    return;
  }
  if (!strcmp(cmd, "press")) {
    if (!busy()) {
      Mouse.press(MOUSE_LEFT);
      reply("ok");
    }
    return;
  }
  if (!strcmp(cmd, "release")) {
    Mouse.release(MOUSE_LEFT);
    reply("ok");
    return;
  }
  if (!strcmp(cmd, "click")) {
    doClick();
    return;
  }
  if (!strcmp(cmd, "key")) {
    if (rest == NULL) {
      reply("err bad args");
      return;
    }
    doKey(rest);
    return;
  }
  if (!strcmp(cmd, "type")) {
    if (rest == NULL) {
      reply("err bad args");
      return;
    }
    doType(rest);
    return;
  }
  if (cmd[0] == 't' && cmd[1] == '_') {
    reply("err mode is mouse");
    return;
  }
  reply("err unknown cmd");
}

// ---------- 服务循环 ----------
static void initUsb() {
  // 先挂事件回调再启动协议栈，避免漏掉 USB_STARTED
  USB.onEvent(ARDUINO_USB_SUSPEND_EVENT, onUsbEvent);
  USB.onEvent(ARDUINO_USB_STARTED_EVENT, onUsbEvent);
  USB.onEvent(ARDUINO_USB_RESUME_EVENT, onUsbEvent);
  Mouse.begin();
  Keyboard.begin();
  USB.begin();
}

static void serviceUsb() {
  static bool lastOk = true;
  bool ok = (bool)USB;
  if (lastOk && !ok) {
    releaseAllHid();  // 未枚举成功/被拔掉时，保持"什么都没按"
    usbSuspended = true;
  } else if (!lastOk && ok) {
    usbSuspended = false;
  }
  lastOk = ok;
}

static void serviceWifi() {
  static bool announced = false;
  if (WiFi.status() == WL_CONNECTED) {
    wifiDownSince = 0;
    if (!announced) {
      announced = true;
      Serial.printf("[bridge] Wi-Fi 已连接，本机 IP %s，端口 %d\n",
                    WiFi.localIP().toString().c_str(), BRIDGE_PORT);
    }
    return;
  }
  announced = false;
  if (wifiDownSince == 0) wifiDownSince = millis();
  if (millis() - wifiDownSince > WIFI_REBOOT_MS) {
    Serial.println("[bridge] Wi-Fi 长期不可用，重启板子");
    Serial.flush();
    ESP.restart();
  }
  if (millis() - wifiLastTry >= WIFI_RETRY_MS) {
    wifiLastTry = millis();
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASS);
  }
}

static void serviceClient() {
  static bool dropping = false;
  if (!client || !client.connected()) {
    if (client) {
      releaseAllHid();  // 链路断了必须松开，否则手机上会卡住按下的键
      client.stop();
    }
    WiFiClient incoming = server.accept();
    if (incoming) {
      client = incoming;
      client.setNoDelay(true);
      lineLen = 0;
      dropping = false;
      releaseAllHid();
      sendHello();
    }
    return;
  }

  while (client.available()) {
    int c = client.read();
    if (c < 0) break;
    if (c == '\n') {
      if (!dropping) {
        lineBuf[lineLen] = 0;
        handleLine(lineBuf);
      }
      lineLen = 0;
      dropping = false;
      continue;
    }
    if (c == '\r') continue;
    if (dropping) continue;
    if (lineLen + 1 < CMD_LINE_MAX) {
      lineBuf[lineLen++] = (char)c;
    } else {
      lineLen = 0;
      dropping = true;
      reply("err line too long");
    }
  }
}

void setup() {
  Serial.begin(115200);
  initUsb();
  delay(USB_SETTLE_MS);  // 等主机把 HID 枚举完

  WiFi.mode(WIFI_STA);
  WiFi.setHostname("esp32-hid-bridge");
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  server.begin();
  server.setNoDelay(true);
  Serial.printf("[bridge] 启动，等待 Wi-Fi；端口 %d\n", BRIDGE_PORT);
}

void loop() {
  serviceUsb();
  serviceWifi();
  serviceClient();
}
