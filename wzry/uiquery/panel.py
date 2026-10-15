# -*- coding: utf-8 -*-
"""UI 导航与面板识别层（v15.2）：与主战斗感知**严格分开**的"查询流程"。

为什么单独成层（按用户给的切法）：
  这类操作会**整屏遮挡**（开商店/比分/属性面板时，主感知看到的是面板而不是战场），
  所以它不是"再多几个识别函数"，而是一次**独占的模态操作**：
      打开(点击) → 读面板 → 回填状态层 → 关闭 → 交还屏幕
  本层的核心因此是 **模态锁 + 卡死逃生（超时必关）**，而不是识别精度。

与主链路的接口（单向、可审计）：
  - 执行：复用执行层的 tap（不自己造输入通道）
  - 回填：只产出**统一字段名**（ui.gold / ui.kills / extra.enemy_items ...），
          由主链路的状态层合并；本层不另开数据通道
  - 停手：`PanelNavigator.busy` 为真时，主战斗决策**不得出手**（面板期间的动作都是面板操作）

坐标不写死：从 configs/calibration_absolute.json 的 `ui_taps` 读（未标定 -> 不执行、降级）。
识别不猜：面板是否打开用"中央亮卡片 + 四周暗遮罩"的结构判据（可离线单测）；
         面板内数字用 wzry.vision.digit_reader —— 面板是**大字静态画面**，
         比 HUD 上的小字稳得多（这正好补上"等级/金币/战绩"读不出来那个缺口）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
CALIB_PATH = ROOT / "configs" / "calibration_absolute.json"

PANELS = ("scoreboard", "hero_panel", "shop", "skill_info")
# 本层允许回填的字段白名单（避免把面板读数随意塞进状态层）
# 注：*_gold / *_kills 是**队伍级**数据（来自战绩面板表头），故意不复用 gold/kills ——
#   那两个字段在主链路里是"英雄自己"的金币/击杀，混用会把状态层污染成两套口径。
ALLOWED_FIELDS = ("gold", "level", "kills", "deaths", "assists", "skill_point",
                  "enemy_items", "enemy_level", "hero_items", "hero_attrs",
                  "ally_gold", "enemy_gold", "ally_kills", "enemy_kills")
# 战绩面板的经济格写成 "XX.Yk"（一位小数 + 千后缀），不能按普通数字读（见 read_k_gold）
K_GOLD_FIELDS = ("ally_gold", "enemy_gold")


def load_cfg():
    """读 ui_taps / panel_rois。返回 (taps, rois)；未标定时为空 dict。"""
    try:
        d = json.loads(CALIB_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}, {}
    return d.get("ui_taps") or {}, d.get("panel_rois") or {}


def panel_open_score(frame):
    """面板打开度的结构判据：中央亮卡片 vs 四周暗遮罩。返回 0..1。"""
    try:
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h, w = g.shape[:2]
        cy0, cy1 = int(h * 0.22), int(h * 0.78)
        cx0, cx1 = int(w * 0.22), int(w * 0.78)
        center = float(g[cy0:cy1, cx0:cx1].mean())
        border = float(np.concatenate([
            g[:int(h * 0.12)].ravel(), g[int(h * 0.88):].ravel(),
            g[:, :int(w * 0.12)].ravel(), g[:, int(w * 0.88):].ravel()]).mean())
        return round(max(0.0, (center - border) / 255.0), 4)
    except Exception:
        return 0.0


# 战绩面板（scoreboard）**不是**"中央亮卡片"，而是深蓝暗遮罩，亮卡片判据对它完全失效
#   （真机踩到：以为没打开 -> 超时 -> 逃生"关一次"反而把面板留在屏幕上）。
# 分离度（中央区深蓝占比，实测）：面板 0.815 | 对局 0.045 | 结算页 0.004 | 加载VS页 0.524
#   加载页用亮度门(mean<=85) 挡掉（它是 93.7，暗面板只有 57）。
BOARD_BLUE_MIN = 0.62
BOARD_MEAN_MAX = 85.0
BOARD_BRIGHT_THR = 0.18      # 亮卡片面板默认阈值（与 PanelNavigator.panel_thr 同源）


def is_panel_open(frame, thr=BOARD_BRIGHT_THR):
    """两种面板结构任一命中即算"面板已打开"。

    亮卡片（商店/英雄属性/结算）-> panel_open_score >= thr
    战绩面板（深蓝暗遮罩，整屏遮挡）-> scoreboard_open_score >= BOARD_BLUE_MIN
    """
    return panel_open_score(frame) >= thr or scoreboard_open_score(frame) >= BOARD_BLUE_MIN


def scoreboard_open_score(frame):
    """战绩面板打开度 = 中央区(22%~78%)深蓝像素占比；亮度门不过则返回 0.0。

    为什么单独一条判据：战绩面板**不是**"中央亮卡片"。真机实测它是**深蓝暗遮罩**，
      中央比战场还暗（center-border = +3.3，而战场是 +18）-> panel_open_score 反而变小，
      完全看不见它。踩到的后果：PanelNavigator.run() 以为没打开 -> 超时 -> 逃生"关一次"
      反而把面板留在屏幕上。
    实测分离度（中央区蓝占比）：面板 0.815 | 对局 0.045 | 结算页 0.004 |
      加载VS页 0.524(但 mean 93.7) | 阵亡暗屏 0.43。故 0.62 阈值 + 亮度门(<=85) 足够稳。
    """
    try:
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if float(g.mean()) > BOARD_MEAN_MAX:      # 亮屏 -> 是战场/加载页，不可能是暗面板
            return 0.0
        h, w = frame.shape[:2]
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        sub = hsv[int(h * 0.22):int(h * 0.78), int(w * 0.22):int(w * 0.78)]
        if sub.size == 0:
            return 0.0
        H, S = sub[..., 0].astype(int), sub[..., 1].astype(int)
        return round(float(((H >= 95) & (H <= 135) & (S >= 60)).mean()), 3)
    except Exception:
        return 0.0


def panel_open_norm(frame, bright_thr=BOARD_BRIGHT_THR, board_thr=BOARD_BLUE_MIN):
    """把两种面板结构归一化成**同一个标量**：>=1.0 即"面板打开"。

    亮卡片用 panel_open_score / bright_thr；战绩面板用 scoreboard_open_score / board_thr。
    两者量纲不同（0.18 vs 0.62），归一化后扫描式标定(probe_ui_tap)的"跳变判定"
    才能两种面板共用同一套阈值。
    """
    return round(max(panel_open_score(frame) / float(bright_thr),
                     scoreboard_open_score(frame) / float(board_thr)), 3)


class PanelNavigator:
    """模态 UI 查询器：同一时刻只允许一个面板操作占用屏幕。"""

    def __init__(self, tap=None, taps=None, timeout=3.0, panel_thr=0.18, settle=0.35):
        self.tap = tap                      # callable(x, y) 或 None（未接线时降级）
        self._taps_cfg, self._rois_cfg = load_cfg()
        self.taps = dict(taps or self._taps_cfg or {})
        self.timeout = float(timeout)
        self.panel_thr = float(panel_thr)
        self.settle = float(settle)         # 面板打开动画的落定时间（见 run 里的说明）
        self._busy_with = None
        self.log = []

    # ---------- 模态锁 ----------
    @property
    def busy(self):
        return self._busy_with is not None

    def acquire(self, panel):
        if self._busy_with is not None:
            return False, f"已被 {self._busy_with} 占用（面板会整屏遮挡，不能并发）"
        if panel not in PANELS:
            return False, f"未知面板 {panel}"
        if panel not in self.taps:
            return False, f"ui_taps 未标定 {panel}（降级：不执行）"
        self._busy_with = panel
        return True, "ok"

    def release(self, reason=""):
        prev = self._busy_with
        self._busy_with = None
        self.log.append({"panel": prev, "reason": reason, "t": round(time.time(), 3)})
        return prev

    # ---------- 执行 ----------
    def _tap(self, tap, name):
        fn = tap or self.tap
        xy = self.taps.get(name)
        if fn is None or not xy:
            return False
        try:
            fn(int(xy[0]), int(xy[1]))
            return True
        except Exception:
            return False

    def read(self, panel, frame):
        """面板帧 -> 统一字段 dict。ROI 由 configs 的 panel_rois[panel] 给出；未标定返回 {}。"""
        out = {}
        rois = (self._rois_cfg or {}).get(panel) or {}
        if not rois:
            return out
        try:
            from wzry.vision import digit_reader as _dr
        except Exception:
            return out
        h, w = frame.shape[:2]
        for field, roi in rois.items():
            if field not in ALLOWED_FIELDS:
                continue
            x0, y0, x1, y1 = [int(v) for v in roi]
            x0, y0 = max(0, x0), max(0, y0)
            x1, y1 = min(w, x1), min(h, y1)
            if x1 <= x0 or y1 <= y0:
                continue
            if field in K_GOLD_FIELDS:
                # 经济格 "XX.Yk"：read_number 会读成 3378（丢小数点 + k 当数字），必须走专用解析
                v, conf = _dr.read_k_gold(frame[y0:y1, x0:x1])
            else:
                v, conf = _dr.read_number(frame[y0:y1, x0:x1])
            if v is not None:
                out[field] = v
        return out

    def run(self, panel, grab, tap=None, reader=None):
        """完整查询流程：打开 → 轮询等面板 → 读 → **必定关闭并验证**。

        返回 {"ok","reason","data","opened","closed"}
        - 任何失败路径（未标定/超时/读数失败）都会走到 finally 里的 close（卡死逃生）
        - 调用方在 busy 期间不得下发战斗动作
        """
        ok, why = self.acquire(panel)
        if not ok:
            return {"ok": False, "reason": why, "data": {}, "opened": False, "closed": False}
        res = {"ok": False, "reason": "", "data": {}, "opened": False, "closed": False}
        # 开工前必须确认"屏幕是干净的"：如果屏幕上已经有面板（上一轮没关掉、或玩家自己开着
        #   设置菜单），轮询会**立刻误判成"我刚打开成功"**，然后去读一个根本不对的面板
        #   （真机踩到：设置菜单也是深蓝遮罩，结构分同样 >=1.0）。此时一个点击都不下发。
        if grab:
            f0 = grab()
            if f0 is not None and is_panel_open(f0, self.panel_thr):
                why = "开工前屏幕已被面板占用（非本层打开）-> 拒绝执行，避免误判打开成功"
                self.release(why)
                return {"ok": False, "reason": why, "data": {}, "opened": False, "closed": False}
        try:
            self._tap(tap, panel)
            t0 = time.time()
            frame = None
            while time.time() - t0 < self.timeout:
                frame = grab() if grab else None
                if frame is None:
                    time.sleep(0.05)
                    continue
                if is_panel_open(frame, self.panel_thr):
                    res["opened"] = True
                    break
                time.sleep(0.05)
            if res["opened"] and frame is not None:
                # 真机踩到：刚检测到打开就立刻读/关，都会失手 —— 面板还在播打开动画，
                #   ✕ 还没就位（点了不中），画面也还在动（读出来的是过渡帧）。
                #   先让动画落定，再读、再关。
                time.sleep(self.settle)
                _nf = grab() if grab else None
                if _nf is not None:              # 注意：不能写 `or frame`（numpy 数组真值会报错）
                    frame = _nf
                res["data"] = (reader or self.read)(panel, frame)
                res["ok"] = bool(res["data"])
                res["reason"] = "ok" if res["ok"] else "面板已打开但未读到字段（ROI 未标定？）"
            else:
                res["reason"] = f"超时 {self.timeout}s：未检测到面板打开"
        except Exception as e:
            res["reason"] = f"异常 {type(e).__name__}: {e}"
        finally:
            # 逃生：关面板，并且**验证真的关掉了**（不能只报"我点过了"）
            res["closed"] = self._close_and_verify(grab, tap)
            if not res["closed"]:
                res["reason"] += " | ⚠ 关闭未确认：面板可能还挡着屏幕，主链路不得出手"
            self.release(res["reason"])
        return res

    def _close_and_verify(self, grab, tap, tries=2):
        """点关闭键并**验证**；关不掉就再试一次。返回是否确认已关。

        为什么必须验证：run() 原来在 finally 里直接 `res["closed"] = True` —— 那是
        "我点过了"，不是"关掉了"。真机实测：点完 ✕ 面板仍开着（结构分 1.27），
        而主链路以为屏幕已交还，接下来会对着面板下发战斗动作。
        """
        if not (self.taps or {}).get("close"):
            return False                     # 没标定关闭键 -> 不算关闭（别自欺）
        for _ in range(max(1, int(tries))):
            self._tap(tap, "close")
            time.sleep(self.settle)
            f = grab() if grab else None
            if f is None:
                continue
            if not is_panel_open(f, self.panel_thr):
                return True
        return False


if __name__ == "__main__":
    taps, rois = load_cfg()
    print("ui_taps:", taps)
    print("panel_rois:", {k: list(v) for k, v in (rois or {}).items()})
