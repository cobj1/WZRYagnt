/*
 * esp32_hid_touch —— ESP32-S3 USB HID 桥（touch 版固件：多点触摸屏 Digitizer）
 *
 * 角色
 *   板子插在手机的 USB 口上，对手机表现为「USB 触摸屏（多点 absolute digitizer）+ 键盘」；
 *   同时板子连 Wi-Fi，电脑作为客户端连到板子的 TCP 服务端（默认 7300），逐行发命令，
 *   板子回 "ok" 或 "err <reason>"。板子本身不含任何决策逻辑。
 *
 *   对应电脑端：wzry/control/esp32_hid.py 的 Esp32HidExecutor(mode="touch")
 *
 * 为什么需要这一版
 *   mouse 版固件只有一个相对指针：做不到「左手摇杆按住 + 右手技能同时按」（单触点硬约束）。
 *   触摸屏 Digitizer 是多触点绝对坐标，`t_down/t_move/t_up` 可让多个手指同时按在不同位置，
 *   这才是真机能打王者荣耀的前提。
 *
 * 硬件
 *   ESP32-S3（必须是 S3，普通 ESP32 没有原生 USB）。板级定义按 XIAO ESP32S3。
 *   板级选项（arduino-cli 的 FQBN 后缀，缺一不可）：
 *     USBMode=default      必须：USB-OTG (TinyUSB)，否则没有 HID
 *     CDCOnBoot=default    注意这个名字是反的，default = Enabled：保留 USB CDC，
 *                          才能用 1200bps 触摸自动进下载模式；顺带能看串口日志
 *     UploadMode=cdc       USB-OTG CDC (TinyUSB)
 *     FlashSize=8M, PartitionScheme=default_8MB
 *
 * 协议（touch 版）
 *   hello <reset_reason> <uptime_ms> <W>x<H>  连接建立后板子主动上报（W/H = 目标屏尺寸）
 *   ping                                -> ok
 *   t_tap <x> <y> <ms>                  -> 单点按下 x,y 保持 ms 后抬起（自动挑空闲触点）
 *   t_swipe <x1> <y1> <x2> <y2> <ms>    -> 单指从起点拖到终点，全程按住 ms（自动挑空闲触点）
 *   t_down <id> <x> <y>                 -> 第 id 个触点按下（多指并发用）
 *   t_move <id> <x> <y>                 -> 第 id 个触点移到绝对坐标
 *   t_up <id>                           -> 第 id 个触点抬起
 *   type <text>                         -> 键盘输入 ASCII
 *   key <combo>                         -> 按键，如 enter / cmd+h / noop
 *   home/move/press/release/click/scroll-> 一律回 err mode is touch（鼠标是另一套固件）
 *
 *   坐标单位：**手机物理像素**（0..TOUCH_SURFACE_W-1 / 0..TOUCH_SURFACE_H-1），
 *   与电脑端 Esp32HidExecutor(device_size=(W,H)) 一致；固件内部把它线性映射到
 *   HID 逻辑范围 0..32767。
 *
 * 已知限制
 *   - 键盘 HID 无法产生 Android 的 HOME(3)/BACK(4)：这两个键在 USB 键盘上不存在，
 *     只能靠点屏幕上的按钮。
 *   - TOUCH_SURFACE_W/H 必须与目标手机分辨率一致（默认 1080x2340），否则触点会整体偏移。
 *     换手机时改这两个常量（或在 secrets.h 里覆盖）后重新编译烧写。
 */

#include <WiFi.h>
#include <esp_system.h>

#include "USB.h"
#include "USBHID.h"
#include "USBHIDKeyboard.h"

#include "secrets.h"

#if !defined(WIFI_SSID) || !defined(WIFI_PASS)
#error "缺少 Wi-Fi 配置：请把 secrets.example.h 复制成 secrets.h 并填写"
#endif

#ifndef BRIDGE_PORT
#define BRIDGE_PORT 7300
#endif

// 目标手机分辨率（电脑端 device_size 必须与之一致）
#ifndef TOUCH_SURFACE_W
#define TOUCH_SURFACE_W 1080
#endif
#ifndef TOUCH_SURFACE_H
#define TOUCH_SURFACE_H 2340
#endif

// ---------- 可调参数 ----------
#define TOUCH_MAX_CONTACTS 5      // 并发触点数（dec 里会重复 TOUCH_MAX_CONTACTS 次）
#define TOUCH_REPORT_ID 7         // 触摸输入报告的 Report ID；本板只注册 键盘(1) + 触摸屏
#define TOUCH_FEATURE_REPORT_ID 8 // Contact Count Maximum 特性报告单独一个 ID（照微软样本）
#define SWIPE_STEP_MS 8           // t_swipe 每步之间的间隔（越小越平滑、命令越少）
#define CMD_LINE_MAX 200          // 单行命令上限（含结尾 '\0'）
#define WIFI_RETRY_MS 5000UL      // Wi-Fi 重连间隔
#define WIFI_REBOOT_MS 120000UL   // 断网超过此时间则重启板子（看门狗）
#define USB_SETTLE_MS 1500        // 上电后等主机完成 HID 枚举

// ---------- 多点触摸屏（自定义 HID Digitizer）----------
// arduino-esp32 没有内置多点触摸设备（只有 USBHIDMouse/Keyboard/Gamepad/…），
// 于是照 USBHIDMouse 的做法自己继承 USBHIDDevice：构造函数里 hid.addDevice(this, len)，
// _onGetDescriptor 回一段自写的 Report Descriptor，begin() 里 hid.begin()。
// 报告布局（每个触点 6 字节 = flags1+id1+X2+Y2，整份报告 1 + 6*TOUCH_MAX_CONTACTS = 31 字节）：
//   contact_count(8) | [ tip_switch(1)+in_range(1)+pad(6) | contact_id(8) | X(16) | Y(16) ] * N
struct __attribute__((packed)) TouchFinger {
  uint8_t flags;   // bit0 = tip switch（按下）, bit1 = in range
  uint8_t id;      // contact identifier
  int16_t x;       // 0..32767
  int16_t y;       // 0..32767
};

struct __attribute__((packed)) TouchReport {
  uint8_t count;
  TouchFinger finger[TOUCH_MAX_CONTACTS];
};

// 每个 Finger block 都必须重新声明 usage page：block 内的 X/Y 用的是 Desktop 页，
// 不重设的话第 2 个 block 起 Finger / Tip Switch / Contact Identifier 都会被算到
// Desktop 页上 —— 描述符非法，上位机只会认出 1 个触点（多点静默失效）。
#define TOUCH_FINGER_BLOCK                                                                 \
  HID_USAGE_PAGE ( HID_USAGE_PAGE_DIGITIZER ),                                             \
  HID_USAGE ( HID_USAGE_DIGITIZER_FINGER ),                                                \
  HID_COLLECTION ( HID_COLLECTION_LOGICAL ),                                               \
    HID_USAGE ( HID_USAGE_DIGITIZER_TIP_SWITCH ),                                          \
    HID_USAGE ( HID_USAGE_DIGITIZER_IN_RANGE ),                                            \
    HID_LOGICAL_MIN ( 0 ),                                                                 \
    HID_LOGICAL_MAX ( 1 ),                                                                 \
    HID_REPORT_SIZE ( 1 ),                                                                 \
    HID_REPORT_COUNT ( 2 ),                                                                \
    HID_INPUT ( HID_DATA | HID_VARIABLE | HID_ABSOLUTE ),                                  \
    HID_REPORT_SIZE ( 6 ),                                                                 \
    HID_REPORT_COUNT ( 1 ),                                                                \
    HID_INPUT ( HID_CONSTANT ),                                                            \
    HID_USAGE ( HID_USAGE_DIGITIZER_CONTACT_IDENTIFIER ),                                  \
    HID_LOGICAL_MIN ( 0 ),                                                                 \
    HID_LOGICAL_MAX ( TOUCH_MAX_CONTACTS ),                                                \
    HID_REPORT_SIZE ( 8 ),                                                                 \
    HID_REPORT_COUNT ( 1 ),                                                                \
    HID_INPUT ( HID_DATA | HID_VARIABLE | HID_ABSOLUTE ),                                  \
    HID_USAGE_PAGE ( HID_USAGE_PAGE_DESKTOP ),                                             \
    HID_USAGE ( HID_USAGE_DESKTOP_X ),                                                     \
    HID_USAGE ( HID_USAGE_DESKTOP_Y ),                                                     \
    HID_LOGICAL_MIN ( 0 ),                                                                 \
    HID_LOGICAL_MAX_N ( 0x7FFF, 2 ),                                                       \
    HID_REPORT_SIZE ( 16 ),                                                                \
    HID_REPORT_COUNT ( 2 ),                                                                \
    HID_INPUT ( HID_DATA | HID_VARIABLE | HID_ABSOLUTE ),                                  \
  HID_COLLECTION_END

static const uint8_t TOUCH_REPORT_DESCRIPTOR[] = {
  HID_USAGE_PAGE ( HID_USAGE_PAGE_DIGITIZER ),
  HID_USAGE      ( HID_USAGE_DIGITIZER_TOUCH_SCREEN ),
  HID_COLLECTION ( HID_COLLECTION_APPLICATION ),
    HID_REPORT_ID ( TOUCH_REPORT_ID )
    HID_USAGE ( HID_USAGE_DIGITIZER_CONTACT_COUNT ),
    HID_LOGICAL_MIN ( 0 ),
    HID_LOGICAL_MAX ( TOUCH_MAX_CONTACTS ),
    HID_REPORT_SIZE ( 8 ),
    HID_REPORT_COUNT ( 1 ),
    HID_INPUT ( HID_DATA | HID_VARIABLE | HID_ABSOLUTE ),
    TOUCH_FINGER_BLOCK, TOUCH_FINGER_BLOCK, TOUCH_FINGER_BLOCK,
    TOUCH_FINGER_BLOCK, TOUCH_FINGER_BLOCK,
    // 上位机（Android hid-multitouch / Windows）会 GET_REPORT 来读"最多几个触点"。
    // 缺这一段，触摸栈要么认不出设备，要么 GET_REPORT 被 STALL —— 实测踩过的坑。
    HID_REPORT_ID ( TOUCH_FEATURE_REPORT_ID )
    HID_USAGE_PAGE ( HID_USAGE_PAGE_DIGITIZER ),
    HID_USAGE ( HID_USAGE_DIGITIZER_CONTACT_COUNT_MAXIMUM ),
    HID_LOGICAL_MIN ( 0 ),
    HID_LOGICAL_MAX ( TOUCH_MAX_CONTACTS ),
    HID_REPORT_SIZE ( 8 ),
    HID_REPORT_COUNT ( 1 ),
    HID_FEATURE ( HID_DATA | HID_VARIABLE | HID_ABSOLUTE ),
  HID_COLLECTION_END
};

class USBHIDTouchscreen : public USBHIDDevice {
public:
  USBHIDTouchscreen() : hid() {
    hid.addDevice(this, sizeof(TOUCH_REPORT_DESCRIPTOR));
  }

  void begin(void) { hid.begin(); }

  bool sendReport(const TouchReport &report) {
    return hid.SendReport(TOUCH_REPORT_ID, &report, sizeof(report));
  }

  uint16_t _onGetDescriptor(uint8_t *buffer) override {
    memcpy(buffer, TOUCH_REPORT_DESCRIPTOR, sizeof(TOUCH_REPORT_DESCRIPTOR));
    return sizeof(TOUCH_REPORT_DESCRIPTOR);
  }

  // 回 Contact Count Maximum。不实现的话上位机 GET_REPORT 会被 STALL，
  // 而 STALL 会让整个触摸设备不被识别 —— 这是 ESP32 上实测踩过的坑。
  uint16_t _onGetFeature(uint8_t report_id, uint8_t *buffer, uint16_t len) override {
    (void)report_id;  // 对任何特性报告都回同一字节：宁可答非所问，也不能 STALL
    if (len < 1) return 0;
    buffer[0] = TOUCH_MAX_CONTACTS;
    return 1;
  }

private:
  USBHID hid;
};

USBHIDTouchscreen Touch;
USBHIDKeyboard Keyboard;
WiFiServer server(BRIDGE_PORT);
WiFiClient client;

static char lineBuf[CMD_LINE_MAX];
static size_t lineLen = 0;
static volatile bool usbSuspended = false;  // 手机锁屏时 USB 口会挂起
static uint32_t wifiDownSince = 0;
static uint32_t wifiLastTry = 0;

// 触点状态：绝对像素坐标 + 是否按下
static bool touchDown[TOUCH_MAX_CONTACTS];
static uint16_t touchX[TOUCH_MAX_CONTACTS];
static uint16_t touchY[TOUCH_MAX_CONTACTS];

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

static void sendHello() {
  char buf[96];
  snprintf(buf, sizeof(buf), "hello %s %lu %dx%d", resetReasonName(esp_reset_reason()),
           (unsigned long)millis(), TOUCH_SURFACE_W, TOUCH_SURFACE_H);
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

// ---------- 触摸原语 ----------
static bool busy() {
  if (usbSuspended) {
    reply("err phone usb asleep");
    return true;
  }
  return false;
}

// 设备像素 -> HID 逻辑值 [0, 32767]
static uint16_t toLogical(uint16_t v, uint32_t surface) {
  if (surface < 2) return 0;
  uint32_t span = surface - 1;
  uint32_t out = (uint32_t)v * 32767u / span;
  if (out > 32767u) out = 32767u;
  return (uint16_t)out;
}

// 把当前触点状态打包成一个报告发给手机（未按下的触点填 0）
static void flushTouches() {
  TouchReport rep;
  memset(&rep, 0, sizeof(rep));
  uint8_t n = 0;
  for (uint8_t cid = 0; cid < TOUCH_MAX_CONTACTS; cid++) {
    // 槽位下标恒等于 cid：描述符里每个 Finger collection 在报告内是固定偏移，
    // 若把活跃触点压缩塞到前面的槽位，同一触点的位置会随其他触点增减而漂移，
    // 破坏 hid-multitouch 的 slot <-> contact id 对应关系。
    rep.finger[cid].id = cid;
    if (!touchDown[cid]) continue;  // 未按下：flags/x/y 保持 0
    rep.finger[cid].flags = 0x03;   // tip switch + in range
    rep.finger[cid].x = (int16_t)toLogical(touchX[cid], TOUCH_SURFACE_W);
    rep.finger[cid].y = (int16_t)toLogical(touchY[cid], TOUCH_SURFACE_H);
    n++;
  }
  rep.count = n;
  Touch.sendReport(rep);
}

static void releaseAllTouches() {
  bool any = false;
  for (uint8_t i = 0; i < TOUCH_MAX_CONTACTS; i++) {
    if (touchDown[i]) { touchDown[i] = false; any = true; }
  }
  if (any) flushTouches();
  Keyboard.releaseAll();
}

// 找一个空闲触点；全占满返回 0xFF
static uint8_t freeContact() {
  for (uint8_t cid = 0; cid < TOUCH_MAX_CONTACTS; cid++)
    if (!touchDown[cid]) return cid;
  return 0xFF;
}

static void clampToSurface(uint16_t *x, uint16_t *y) {
  if (*x >= TOUCH_SURFACE_W) *x = TOUCH_SURFACE_W - 1;
  if (*y >= TOUCH_SURFACE_H) *y = TOUCH_SURFACE_H - 1;
}

// ---- apply* 只改状态 + 发报告，不回包：返回 nullptr 成功，否则返回要回给电脑端的 err 文本。
//      命令层 doXxx 负责回包，这样「一条命令恰好一行应答」的契约才守得住
//      （t_tap/t_swipe 内部会调 applyTouchDown，若它自己回包就会多吐一行 ok，
//        电脑端每条命令只读一行 → 应答流永久错位一位）。
static const char *applyTouchDown(uint8_t cid, uint16_t x, uint16_t y) {
  if (cid >= TOUCH_MAX_CONTACTS) return "err contact id out of range";
  clampToSurface(&x, &y);
  touchX[cid] = x;
  touchY[cid] = y;
  touchDown[cid] = true;
  flushTouches();
  return NULL;
}

static const char *applyTouchMove(uint8_t cid, uint16_t x, uint16_t y) {
  if (cid >= TOUCH_MAX_CONTACTS) return "err contact id out of range";
  if (!touchDown[cid]) return "err contact not down";
  clampToSurface(&x, &y);
  touchX[cid] = x;
  touchY[cid] = y;
  flushTouches();
  return NULL;
}

static const char *applyTouchUp(uint8_t cid) {
  if (cid >= TOUCH_MAX_CONTACTS) return "err contact id out of range";
  touchDown[cid] = false;
  flushTouches();
  return NULL;
}

static void doTouchDown(uint8_t cid, uint16_t x, uint16_t y) {
  const char *e = applyTouchDown(cid, x, y);
  reply(e ? e : "ok");
}

static void doTouchMove(uint8_t cid, uint16_t x, uint16_t y) {
  const char *e = applyTouchMove(cid, x, y);
  reply(e ? e : "ok");
}

static void doTouchUp(uint8_t cid) {
  const char *e = applyTouchUp(cid);
  reply(e ? e : "ok");
}

// t_tap / t_swipe 自己挑空闲触点，不抢占上层可能正按住的那个触点
// （上层把触点 0 当摇杆持续按住时，固定用触点 0 会直接打断摇杆）。
static void doTouchTap(uint16_t x, uint16_t y, int ms) {
  if (busy()) return;
  if (ms < 1) ms = 1;
  uint8_t cid = freeContact();
  if (cid == 0xFF) { reply("err no free contact"); return; }
  const char *e = applyTouchDown(cid, x, y);
  if (e) { reply(e); return; }
  delay(ms);
  touchDown[cid] = false;
  flushTouches();
  reply("ok");  // 整条 t_tap 只回这一行
}

static void doTouchSwipe(uint16_t x1, uint16_t y1, uint16_t x2, uint16_t y2, int ms) {
  if (busy()) return;
  if (ms < 1) ms = 1;
  uint8_t cid = freeContact();
  if (cid == 0xFF) { reply("err no free contact"); return; }
  int steps = ms / SWIPE_STEP_MS;
  if (steps < 1) steps = 1;
  const char *e = applyTouchDown(cid, x1, y1);
  if (e) { reply(e); return; }
  for (int i = 1; i <= steps; i++) {
    // 用 long 插值：反向滑动（x2 < x1）时若先转成 uint16_t 会回绕成大正数
    long lx = (long)x1 + ((long)x2 - (long)x1) * i / steps;
    long ly = (long)y1 + ((long)y2 - (long)y1) * i / steps;
    uint16_t tx = (uint16_t)(lx < 0 ? 0 : lx);
    uint16_t ty = (uint16_t)(ly < 0 ? 0 : ly);
    clampToSurface(&tx, &ty);
    touchX[cid] = tx;
    touchY[cid] = ty;
    flushTouches();
    delay(ms / steps);
  }
  touchDown[cid] = false;
  flushTouches();
  reply("ok");  // 整条 t_swipe 只回这一行
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
  if (!strcmp(combo, "noop") || !strcmp(combo, "none")) {
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
  delay(30);
  Keyboard.releaseAll();
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
  if (!strcmp(cmd, "t_tap")) {
    int x, y, ms;
    if (rest == NULL || sscanf(rest, "%d %d %d", &x, &y, &ms) != 3) {
      reply("err bad args");
      return;
    }
    doTouchTap((uint16_t)x, (uint16_t)y, ms);
    return;
  }
  if (!strcmp(cmd, "t_swipe")) {
    int x1, y1, x2, y2, ms;
    if (rest == NULL || sscanf(rest, "%d %d %d %d %d", &x1, &y1, &x2, &y2, &ms) != 5) {
      reply("err bad args");
      return;
    }
    doTouchSwipe((uint16_t)x1, (uint16_t)y1, (uint16_t)x2, (uint16_t)y2, ms);
    return;
  }
  if (!strcmp(cmd, "t_down")) {
    int id, x, y;
    if (rest == NULL || sscanf(rest, "%d %d %d", &id, &x, &y) != 3) {
      reply("err bad args");
      return;
    }
    if (busy()) return;
    doTouchDown((uint8_t)id, (uint16_t)x, (uint16_t)y);
    return;
  }
  if (!strcmp(cmd, "t_move")) {
    int id, x, y;
    if (rest == NULL || sscanf(rest, "%d %d %d", &id, &x, &y) != 3) {
      reply("err bad args");
      return;
    }
    if (busy()) return;
    doTouchMove((uint8_t)id, (uint16_t)x, (uint16_t)y);
    return;
  }
  if (!strcmp(cmd, "t_up")) {
    int id;
    if (rest == NULL || sscanf(rest, "%d", &id) != 1) {
      reply("err bad args");
      return;
    }
    doTouchUp((uint8_t)id);
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
  // 鼠标原语属于另一套固件
  if (!strcmp(cmd, "home") || !strcmp(cmd, "move") || !strcmp(cmd, "scroll") ||
      !strcmp(cmd, "press") || !strcmp(cmd, "release") || !strcmp(cmd, "click")) {
    reply("err mode is touch");
    return;
  }
  reply("err unknown cmd");
}

// ---------- 服务循环 ----------
static void initUsb() {
  USB.onEvent(ARDUINO_USB_SUSPEND_EVENT, onUsbEvent);
  USB.onEvent(ARDUINO_USB_STARTED_EVENT, onUsbEvent);
  USB.onEvent(ARDUINO_USB_RESUME_EVENT, onUsbEvent);
  Touch.begin();
  Keyboard.begin();
  USB.begin();
}

static void serviceUsb() {
  static bool lastOk = true;
  bool ok = (bool)USB;
  if (lastOk && !ok) {
    releaseAllTouches();  // 未枚举成功/被拔掉时，保持"什么都没按"
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
      releaseAllTouches();  // 链路断了必须松开，否则手机上会卡住按下的手指
      client.stop();
    }
    WiFiClient incoming = server.accept();
    if (incoming) {
      client = incoming;
      client.setNoDelay(true);
      lineLen = 0;
      dropping = false;
      releaseAllTouches();
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
  memset(touchDown, 0, sizeof(touchDown));
  initUsb();
  delay(USB_SETTLE_MS);  // 等主机把 HID 枚举完

  WiFi.mode(WIFI_STA);
  WiFi.setHostname("esp32-hid-touch");
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  server.begin();
  server.setNoDelay(true);
  Serial.printf("[bridge] touch 版启动，屏幕 %dx%d，等待 Wi-Fi；端口 %d\n",
                TOUCH_SURFACE_W, TOUCH_SURFACE_H, BRIDGE_PORT);
}

void loop() {
  serviceUsb();
  serviceWifi();
  serviceClient();
}
