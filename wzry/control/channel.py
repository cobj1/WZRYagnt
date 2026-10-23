# -*- coding: utf-8 -*-
"""执行通道选择策略（adb / ESP32 HID 桥）。

前置事实（不要误解）
------------------
执行（**注入**）通道是**互斥**的：`--backend` 单选，`ActionExecutor` 只持有一个
executor，两路同时下发会互相抢触点且无从仲裁。感知（`--capture`）与执行无耦合，
不在本模块职责内 —— 本模块只回答一个问题：**这一次该用哪条执行通道。**

优先级（`resolve_channel`）
-------------------------
1. `backend="adb"`    -> 强制 adb，**不探测板子**（零额外延迟）
2. `backend="esp32"`  -> 强制板子；地址缺失直接报错，**不静默回落**（真机调试时
                        静默改成 adb 会让人误判为"板子没生效"，属最难查的一类问题）
3. `backend="auto"`（默认）-> **优先板子**：
     地址来源   `--board-host`  >  环境变量 `WZRY_BOARD_HOST`  >  `configs/board.json`
     有地址+探测成功 -> 板子
     有地址+探测失败 -> 打印原因并回落 adb
     **无地址 -> 直接 adb，不发起任何探测**

安全性质：`auto` 在"从未配置过板子地址"时与旧版 `--backend adb` **行为一致、零额外
延迟**。这是本改动不破坏既有 MuMu 工作流的关键，改动时不要破坏它。

地址持久化
----------
板子走 DHCP，IP 会变。成功连上后把 (host, port, mode, size) 写入
`configs/board.json`，之后 `--backend auto` 不必再敲 `--board-host`。
该文件只含局域网地址，不含 Wi-Fi 密码；已在 .gitignore 中。
"""
import json
import os
import socket
from pathlib import Path

from wzry.control.esp32_hid import BoardLink

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "configs" / "board.json"

DEFAULT_PORT = 7300
DEFAULT_MODE = "touch"
DEFAULT_SIZE = "1080x2340"
# 探测超时必须短：auto 是本机默认路径，探一个不存在的地址不该让启动卡住。
# 板子在同网段且已连 Wi-Fi 时实测往返 19-22ms，0.8s 余量充足。
DEFAULT_PROBE_TIMEOUT = 0.8
ENV_HOST = "WZRY_BOARD_HOST"


class ChannelResolutionError(RuntimeError):
    """通道无法按用户显式要求解析（如 `--backend esp32` 但没给地址）。"""


def load_board_cfg():
    """读取上次成功连接的板子信息；不存在/损坏时返回 {}（不抛）。"""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_board_cfg(host, port=DEFAULT_PORT, mode=DEFAULT_MODE, size=DEFAULT_SIZE):
    """记录成功连接的板子信息，供下次 auto 复用。写失败不影响主流程。"""
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(
            {"host": str(host), "port": int(port), "mode": str(mode), "size": str(size)},
            ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def probe_board(host, port=DEFAULT_PORT, timeout=DEFAULT_PROBE_TIMEOUT):
    """探活：能连上且按约定上报 hello 才算成功。

    返回 (hello, err)：成功 -> (hello行, None)；失败 -> (None, 原因字符串)。
    注意"连上了但没 hello"也算失败 —— 我们要求确认对面真的是本项目固件，
    否则可能是别的服务占了这个端口。
    """
    lk = BoardLink(host, port=port, timeout=timeout)
    try:
        hello = lk.connect()
    except (OSError, socket.error) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    except Exception as exc:                       # 兜底：探活绝不向上抛
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        lk.close()
    if hello is None:
        return None, "已连接但未上报 hello（可能不是本项目固件）"
    return hello, None


def _pick_size(cli_size, cfg):
    """分辨率取值：CLI > 持久化配置 > 默认。

    顺序重要：`--board-size` 与固件 `TOUCH_SURFACE_W/H` 不一致时触点会**整体
    静默偏移**（不报错），所以宁可沿用上次连成功过的那个值。
    """
    return cli_size or cfg.get("size") or DEFAULT_SIZE


def resolve_channel(backend="auto", board_host=None, board_port=None,
                    board_mode=None, board_size=None,
                    probe_timeout=DEFAULT_PROBE_TIMEOUT, log=print):
    """解析本次执行通道，返回决策字典。

    返回键：channel("adb"|"esp32") / host / port / mode / size / source / reason / hello
    `source` 取值："cli" | "env" | "config"；channel="adb" 时 host 为 None。

    抛 ChannelResolutionError：仅当 backend="esp32" 且拿不到任何地址时。
    """
    cfg = load_board_cfg()
    port = int(board_port or cfg.get("port") or DEFAULT_PORT)
    mode = board_mode or cfg.get("mode") or DEFAULT_MODE
    size = _pick_size(board_size, cfg)

    if backend == "adb":
        return {"channel": "adb", "host": None, "port": port, "mode": mode,
                "size": size, "source": None, "hello": None,
                "reason": "已指定 --backend adb（未探测板子）"}

    host, source = None, None
    if board_host:
        host, source = board_host, "cli"
    elif os.environ.get(ENV_HOST):
        host, source = os.environ[ENV_HOST], "env"
    elif cfg.get("host"):
        host, source = cfg["host"], "config"

    if backend == "esp32":
        if not host:
            raise ChannelResolutionError(
                "--backend esp32 需要板子地址：给 --board-host <板子IP>，"
                f"或设环境变量 {ENV_HOST}，或先成功连过一次以写入 configs/board.json")
        hello, err = probe_board(host, port, probe_timeout)
        if hello is None:
            raise ChannelResolutionError(
                f"--backend esp32 指定了板子但探测不通 {host}:{port}（{err}）"
                "。先确认板子已连 Wi-Fi、固件在跑、端口与 BRIDGE_PORT 一致")
        log(f"[channel] 强制板子，探测通过（来源 {source}）")
        return {"channel": "esp32", "host": host, "port": port, "mode": mode,
                "size": size, "source": source, "hello": hello,
                "reason": f"指定 --backend esp32（来源 {source}）"}

    if backend != "auto":
        raise ChannelResolutionError(f"未知 backend: {backend!r}（应为 auto/adb/esp32）")

    if not host:
        return {"channel": "adb", "host": None, "port": port, "mode": mode,
                "size": size, "source": None, "hello": None,
                "reason": "未配置板子地址，直接用 adb（未探测）"}

    hello, err = probe_board(host, port, probe_timeout)
    if hello is None:
        log(f"[channel] 板子 {host}:{port} 探测失败（{err}）-> 回落 adb")
        return {"channel": "adb", "host": None, "port": port, "mode": mode,
                "size": size, "source": source, "hello": None,
                "reason": f"优先板子但探测失败，已回落 adb（{host}:{port}）"}
    log(f"[channel] 优先板子，探测通过（来源 {source}）")
    return {"channel": "esp32", "host": host, "port": port, "mode": mode,
            "size": size, "source": source, "hello": hello,
            "reason": f"优先板子并探测通过（来源 {source}）"}


def _main():
    """自测/排查 CLI：只打印决策，不执行任何动作。

        python -m wzry.control.channel                 # auto，无地址 -> adb
        python -m wzry.control.channel --backend esp32 # 无地址 -> 明确报错
        python -m wzry.control.channel --board-host 192.168.0.107
        python -m wzry.control.channel --show-config
    """
    import argparse
    ap = argparse.ArgumentParser(description="执行通道选择策略自测（只决策，不动手）")
    ap.add_argument("--backend", default="auto", choices=["auto", "adb", "esp32"])
    ap.add_argument("--board-host", default=None)
    ap.add_argument("--board-port", type=int, default=None)
    ap.add_argument("--board-mode", default=None, choices=["touch", "mouse"])
    ap.add_argument("--board-size", default=None)
    ap.add_argument("--probe-timeout", type=float, default=DEFAULT_PROBE_TIMEOUT)
    ap.add_argument("--show-config", action="store_true", help="打印 configs/board.json")
    a = ap.parse_args()

    if a.show_config:
        print(f"{CONFIG_PATH}: {json.dumps(load_board_cfg(), ensure_ascii=False)}")
        return 0
    try:
        d = resolve_channel(a.backend, a.board_host, a.board_port, a.board_mode,
                            a.board_size, a.probe_timeout)
    except ChannelResolutionError as exc:
        print(f"解析失败: {exc}")
        return 2
    print(f"通道   : {d['channel']}")
    print(f"地址   : {d['host']}:{d['port']} (来源 {d['source']})")
    print(f"mode   : {d['mode']}   size: {d['size']}")
    print(f"hello  : {d['hello']!r}")
    print(f"原因   : {d['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
