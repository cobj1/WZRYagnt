# -*- coding: utf-8 -*-
"""离线会话门禁 CLI：验证当前对局确实是离线训练局，并签发读取 token。

为什么要做：项目只在离线训练局读取内存。此前这条约束只写在文档里，
任何脚本（包括误跑的在线对局）都能直接连 MemSource。本脚本把约束变成
可执行的门禁：验证通过才写 runtime/offline_session.json，
MemSource.connect() 缺 token 直接拒绝。

验证方法（不读内存，只截图 + 点击）：
  1. 打开对局内设置面板；
  2. 检测面板顶部黄字横幅「当前处于单人训练相关模式中…游戏已暂停」
     （离线实测 ~975 个黄色像素，在线同位置为 0）；
  3. 隔 2.5s 截两帧，比较顶部时钟区域：离线会暂停 → 时钟冻结；
     在线不暂停 → 时钟继续走 → 拒绝签发。
  4. 人工确认（默认交互式）保证当前确实是对局内而不是大厅。

用法（需用带 cv2 的 venv）：
  venv/Scripts/python.exe run_offline.py verify            # 交互确认 + 自动验证
  venv/Scripts/python.exe run_offline.py verify --yes      # 跳过人工确认
  venv/Scripts/python.exe run_offline.py status            # 查看 token 是否有效
  venv/Scripts/python.exe run_offline.py revoke            # 立即作废

注意：`--manual` 只跳过"时钟冻结"自动检查，仍需人工确认，用于离线 UI 异常时兜底；
签发的 method 会如实记为 manual。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from wzry.adb_util import resolve_adb  # noqa: E402
from wzry.vision.offline_gate import (  # noqa: E402
    DEFAULT_TOKEN_PATH, load_token, mint_token, revoke_token, verify_token,
)

PKG = "com.tencent.tmgp.sgame"
GEAR = (1730, 40)              # 1920x1080 对局内设置齿轮
CLOCK_ROI = (860, 0, 1060, 60)  # 顶部计时器（1920x1080 参考坐标）
# 设置面板顶部黄字「当前处于单人训练相关模式中…游戏已暂停」所在区域。
# 在线对局的同位置没有黄字（实测 0 像素），是比"时钟冻结"更直接的离线判据。
BANNER_ROI = (700, 40, 1270, 80)
PANEL_WAIT_S = 1.2
PAUSE_GAP_S = 2.5
MAX_CLOCK_DIFF = 4.0           # 冻结判定：实测暂停约 1.8、在线走动约 34
MIN_BANNER_PX = 300            # 黄字像素数下限（实测离线约 975，在线 0）
MIN_CLOCK_STD = 5.0            # 时钟区域必须有内容，防止被面板盖住后误判
RUNTIME = ROOT / "runtime"


def _adb(adb: str, serial: str, args) -> subprocess.CompletedProcess:
    return subprocess.run([adb, "-s", serial] + list(args),
                          capture_output=True, timeout=20)


def _pidof(adb: str, serial: str) -> Optional[str]:
    try:
        out = _adb(adb, serial, ["exec-out", f"pidof {PKG}"])
    except Exception:
        return None
    txt = out.stdout.decode("utf-8", "replace").strip()
    return txt.split()[0] if txt else None


def _tap(adb: str, serial: str, x: int, y: int) -> None:
    _adb(adb, serial, ["shell", "input", "tap", str(x), str(y)])


def _back(adb: str, serial: str) -> None:
    _adb(adb, serial, ["shell", "input", "keyevent", "4"])


def _grab(serial: str, gap_s: float = 0.0) -> "object":
    from wzry.capture.screencap_capture import ScreencapCapture
    cap = ScreencapCapture(interval=0.05, serial=serial).start()
    try:
        frame, _ = cap.wait_frame(timeout=6.0)
        if frame is None:
            raise RuntimeError("截图失败（检查 MuMu / adb 连接）")
        if gap_s > 0:
            time.sleep(gap_s)
            frame2, _ = cap.wait_frame(timeout=6.0)
            if frame2 is None:
                raise RuntimeError("第二帧截图失败")
            return frame.copy(), frame2.copy()
        return frame.copy()
    finally:
        cap.stop()


def _roi(frame):
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = CLOCK_ROI
    return frame[int(y0 * h / 1080):int(y1 * h / 1080),
                 int(x0 * w / 1920):int(x1 * w / 1920)]


def _mean_abs_diff(a, b) -> float:
    import numpy as np
    ga = a.astype(np.int16)
    gb = b.astype(np.int16)
    return float(np.abs(ga - gb).mean())


def _banner_pixels(frame) -> int:
    """设置面板顶部黄字像素数（离线训练横幅）。"""
    import numpy as np
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = BANNER_ROI
    roi = frame[int(y0 * h / 1080):int(y1 * h / 1080),
                int(x0 * w / 1920):int(x1 * w / 1920)]
    b, g, r = roi[:, :, 0], roi[:, :, 1], roi[:, :, 2]
    mask = (r > 170) & (g > 140) & (b < 110)
    return int(np.count_nonzero(mask))


def _save_png(frame, path: Path) -> None:
    import cv2
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), frame)


def _confirm(serial: str, adb: str) -> bool:
    shot = RUNTIME / f"offline_confirm_{datetime.now():%Y%m%d_%H%M%S}.png"
    try:
        _save_png(_grab(serial), shot)
        print(f"当前画面已保存: {shot}")
    except Exception as exc:
        print(f"[warn] 预览截图失败: {exc}")
    print("请确认：当前是【离线训练/离线5V5】的对局内画面（不是大厅、不是在线对局）。")
    ans = input("输入 offline 继续，其它任意键取消: ").strip().lower()
    return ans in ("offline", "y", "yes")


def verify(a) -> int:
    adb = resolve_adb()
    pid = _pidof(adb, a.serial)
    if not pid:
        print("未找到游戏进程，先启动游戏并进入离线对局。")
        return 2
    print(f"serial={a.serial} pid={pid}")

    if not a.yes and not _confirm(a.serial, adb):
        print("已取消，未签发 token。")
        return 2

    evidence = {"pid": pid, "serial": a.serial}
    method = "manual" if a.manual else "paused_clock"
    if not a.manual:
        before = _grab(a.serial)
        banner_px = 0
        for _attempt in range(2):
            _tap(adb, a.serial, *GEAR)
            time.sleep(PANEL_WAIT_S)
            probe = _grab(a.serial)
            banner_px = _banner_pixels(probe)
            if banner_px >= MIN_BANNER_PX:
                break
        if banner_px < MIN_BANNER_PX:
            _back(adb, a.serial)
            print(f"未检测到离线训练横幅（黄字 {banner_px}px < {MIN_BANNER_PX}）"
                  " → 不是离线训练局 → 拒绝签发。")
            return 3

        f1, f2 = _grab(a.serial, gap_s=PAUSE_GAP_S)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        p1 = RUNTIME / f"offline_verify_{ts}_a.png"
        p2 = RUNTIME / f"offline_verify_{ts}_b.png"
        _save_png(f1, p1)
        _save_png(f2, p2)
        _back(adb, a.serial)

        panel_diff = _mean_abs_diff(before, f1)
        clock_a, clock_b = _roi(f1), _roi(f2)
        std = float(clock_a.std())
        diff = _mean_abs_diff(clock_a, clock_b)
        evidence.update({
            "screenshots": [str(p1), str(p2)],
            "banner_px": banner_px,
            "panel_diff": round(panel_diff, 3),
            "clock_diff": round(diff, 3),
            "clock_std": round(std, 3),
            "gap_s": PAUSE_GAP_S,
        })
        print(f"离线横幅={banner_px}px 设置面板差异={panel_diff:.2f} "
              f"时钟差异={diff:.2f} 时钟std={std:.2f}")
        if std < MIN_CLOCK_STD:
            print("顶部时钟区域为空（可能被面板遮挡）→ 拒绝。")
            return 3
        if diff > MAX_CLOCK_DIFF:
            print("对局没有暂停：时钟仍在走 → 拒绝签发。")
            return 3
        print("离线训练横幅 + 对局暂停（时钟冻结），校验通过。")

    data = mint_token(DEFAULT_TOKEN_PATH, pid=pid, serial=a.serial,
                      method=method, evidence=evidence,
                      ttl_s=a.ttl)
    print(f"token 已签发: {DEFAULT_TOKEN_PATH}")
    print(f"  pid={data['pid']} 有效期至="
          f"{datetime.fromtimestamp(data['expires_at']):%Y-%m-%d %H:%M:%S}")
    return 0


def status(a) -> int:
    data = load_token(DEFAULT_TOKEN_PATH)
    if data is None:
        print(f"没有 token（{DEFAULT_TOKEN_PATH}）。")
        return 1
    print(f"token: pid={data.get('pid')} serial={data.get('serial')} "
          f"mode={data.get('mode')} method={data.get('method')}")
    print(f"  有效期至={datetime.fromtimestamp(data.get('expires_at', 0)):%Y-%m-%d %H:%M:%S}")
    adb = resolve_adb()
    pid = _pidof(adb, a.serial)
    if not pid:
        print("游戏未运行；token 只有在 pid 匹配时才会被接受。")
        return 1
    ok, reason = verify_token(pid, a.serial, DEFAULT_TOKEN_PATH)
    print(f"当前 pid={pid} 校验: {'通过' if ok else '拒绝'} ({reason})")
    return 0 if ok else 1


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="离线会话门禁")
    ap.add_argument("command", choices=["verify", "status", "revoke"])
    ap.add_argument("--serial", default="127.0.0.1:16384")
    ap.add_argument("--ttl", type=float, default=45 * 60,
                    help="token 有效期（秒），默认 45 分钟（单局）")
    ap.add_argument("--yes", action="store_true", help="跳过人工确认")
    ap.add_argument("--manual", action="store_true",
                    help="跳过时钟冻结检查，仅人工确认后签发")
    a = ap.parse_args()
    if a.command == "verify":
        return verify(a)
    if a.command == "status":
        return status(a)
    print("token 已作废。" if revoke_token(DEFAULT_TOKEN_PATH) else "没有 token。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
