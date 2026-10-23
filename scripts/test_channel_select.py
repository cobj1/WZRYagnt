# -*- coding: utf-8 -*-
"""执行通道选择策略离线测试（不需要板子、不需要模拟器）。

要守住的核心性质
----------------
1. **执行互斥**：一次解析只产出一个通道（不存在"两个都开"的返回值）。
2. **不破坏既有工作流**：`auto` 在"处处都没有板子地址"时**不发起任何探测**、
   直接选 adb —— 与旧版 `--backend adb` 行为一致（零额外启动延迟）。
3. **强制不静默**：`--backend esp32` 拿不到地址或探测不通时**明确报错**，
   绝不偷偷回落 adb（真机调试里静默换通道属最难查的一类问题）。
4. **地址/参数优先级**：CLI > 环境变量 > configs/board.json > 默认。

运行: venv/Scripts/python.exe -X utf8 scripts/test_channel_select.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.control import channel as ch                       # noqa: E402

_PASS = []


def check(name, cond, extra=""):
    if cond:
        _PASS.append(name)
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {extra}")
        raise SystemExit(1)


_CALLS = {"probe": 0}


def _probe_ok(host, port=ch.DEFAULT_PORT, timeout=ch.DEFAULT_PROBE_TIMEOUT):
    _CALLS["probe"] += 1
    return "hello poweron 1234 3200x1440", None


def _probe_fail(host, port=ch.DEFAULT_PORT, timeout=ch.DEFAULT_PROBE_TIMEOUT):
    _CALLS["probe"] += 1
    return None, "TimeoutError: timed out"


def _quiet(*a, **k):
    pass


def main():
    tmp = Path(tempfile.mkdtemp(prefix="chan_"))
    ch.CONFIG_PATH = tmp / "board.json"          # 隔离：绝不写项目里的真实配置
    orig = {k: getattr(ch, k) for k in ("probe_board", "load_board_cfg")}
    os.environ.pop(ch.ENV_HOST, None)

    def reset():
        _CALLS["probe"] = 0
        os.environ.pop(ch.ENV_HOST, None)

    # ---------- C1 强制 adb：不探测 ----------
    reset()
    ch.probe_board = _probe_ok
    ch.load_board_cfg = lambda: {"host": "10.0.0.9"}     # 即使存了板子也不该被探
    d = ch.resolve_channel("adb", log=_quiet)
    check("C1 强制adb->adb且不探测", d["channel"] == "adb" and _CALLS["probe"] == 0,
          f"{d} probe={_CALLS['probe']}")

    # ---------- C2 强制 esp32 无地址 -> 报错（不回落） ----------
    reset()
    ch.load_board_cfg = lambda: {}
    try:
        ch.resolve_channel("esp32", log=_quiet)
        check("C2 强制esp32无地址->报错", False, "未抛异常")
    except ch.ChannelResolutionError as exc:
        check("C2 强制esp32无地址->报错", "--board-host" in str(exc), str(exc))
    check("C2b 未发起探测", _CALLS["probe"] == 0, f"probe={_CALLS['probe']}")

    # ---------- C3 强制 esp32 有地址且通 -> esp32 ----------
    reset()
    ch.probe_board = _probe_ok
    d = ch.resolve_channel("esp32", board_host="10.0.0.9", log=_quiet)
    check("C3 强制esp32+探测通过->esp32",
          d["channel"] == "esp32" and d["source"] == "cli" and d["host"] == "10.0.0.9", f"{d}")

    # ---------- C4 强制 esp32 但不通 -> 报错（不静默回落） ----------
    reset()
    ch.probe_board = _probe_fail
    try:
        ch.resolve_channel("esp32", board_host="10.0.0.9", log=_quiet)
        check("C4 强制esp32不通->报错不回落", False, "未抛异常")
    except ch.ChannelResolutionError as exc:
        check("C4 强制esp32不通->报错不回落", "esp32" in str(exc), str(exc))

    # ---------- C5 关键性质：auto 无地址 -> adb 且零探测 ----------
    reset()
    ch.load_board_cfg = lambda: {}
    ch.probe_board = _probe_ok
    d = ch.resolve_channel("auto", log=_quiet)
    check("C5 auto无地址->adb且零探测",
          d["channel"] == "adb" and _CALLS["probe"] == 0 and d["host"] is None,
          f"{d} probe={_CALLS['probe']}")

    # ---------- C6 auto + 环境变量 + 通 -> esp32 ----------
    reset()
    os.environ[ch.ENV_HOST] = "10.0.0.7"
    d = ch.resolve_channel("auto", log=_quiet)
    check("C6 auto+env+通过->esp32",
          d["channel"] == "esp32" and d["source"] == "env" and d["host"] == "10.0.0.7", f"{d}")

    # ---------- C7 auto + 环境变量 + 不通 -> 回落 adb ----------
    reset()
    os.environ[ch.ENV_HOST] = "10.0.0.7"
    ch.probe_board = _probe_fail
    d = ch.resolve_channel("auto", log=_quiet)
    check("C7 auto探测失败->回落adb",
          d["channel"] == "adb" and "回落" in d["reason"], f"{d}")

    # ---------- C8 auto + 持久化配置 + 通 -> esp32 ----------
    reset()
    ch.probe_board = _probe_ok
    ch.load_board_cfg = lambda: {"host": "192.168.0.107", "port": 7300,
                                 "mode": "touch", "size": "3200x1440"}
    d = ch.resolve_channel("auto", log=_quiet)
    check("C8 auto+config+通过->esp32",
          d["channel"] == "esp32" and d["source"] == "config", f"{d}")

    # ---------- C9 优先级 CLI > env > config ----------
    reset()
    os.environ[ch.ENV_HOST] = "10.0.0.7"
    ch.load_board_cfg = lambda: {"host": "10.0.0.9", "size": "3200x1440"}
    d = ch.resolve_channel("auto", board_host="10.0.0.1", log=_quiet)
    check("C9a CLI优先于env", d["source"] == "cli" and d["host"] == "10.0.0.1", f"{d}")
    reset()
    os.environ[ch.ENV_HOST] = "10.0.0.7"
    d = ch.resolve_channel("auto", log=_quiet)
    check("C9b env优先于config", d["source"] == "env" and d["host"] == "10.0.0.7", f"{d}")

    # ---------- C10 size/mode: CLI > config > 默认 ----------
    reset()
    ch.load_board_cfg = lambda: {"host": "10.0.0.9", "mode": "mouse", "size": "3200x1440"}
    d = ch.resolve_channel("auto", log=_quiet)
    check("C10a 未给CLI->用config的size/mode",
          d["size"] == "3200x1440" and d["mode"] == "mouse", f"{d}")
    d = ch.resolve_channel("auto", board_size="1080x1920", board_mode="touch", log=_quiet)
    check("C10b CLI覆盖config",
          d["size"] == "1080x1920" and d["mode"] == "touch", f"{d}")
    ch.load_board_cfg = lambda: {"host": "10.0.0.9"}
    d = ch.resolve_channel("auto", log=_quiet)
    check("C10c 都无->默认值",
          d["size"] == ch.DEFAULT_SIZE and d["mode"] == ch.DEFAULT_MODE, f"{d}")

    # ---------- C11 返回值形状：channel 只能是 adb/esp32（互斥，无"两个"） ----------
    reset()
    ch.load_board_cfg = lambda: {}
    for b in ("adb", "esp32"):
        kw = {"board_host": "10.0.0.9"} if b == "esp32" else {}
        d = ch.resolve_channel(b, log=_quiet, **kw)
        check(f"C11 backend={b} 只产出单个通道",
              d["channel"] in ("adb", "esp32"), f"{d}")

    # ---------- C12 save/load 往返（临时文件，不碰真实配置） ----------
    reset()
    ch.load_board_cfg = orig["load_board_cfg"]
    ok = ch.save_board_cfg("192.168.0.107", 7300, "touch", "3200x1440")
    cfg = ch.load_board_cfg()
    check("C12 save/load往返",
          ok and cfg.get("host") == "192.168.0.107" and cfg.get("size") == "3200x1440",
          f"ok={ok} cfg={cfg}")

    # ---------- C13 未知 backend -> 报错 ----------
    reset()
    ch.load_board_cfg = lambda: {}
    try:
        ch.resolve_channel("bogus", log=_quiet)
        check("C13 未知backend->报错", False, "未抛异常")
    except ch.ChannelResolutionError:
        check("C13 未知backend->报错", True)

    ch.probe_board, ch.load_board_cfg = orig["probe_board"], orig["load_board_cfg"]
    print(f"\n全部通过: {len(_PASS)} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
