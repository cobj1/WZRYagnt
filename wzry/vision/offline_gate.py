# -*- coding: utf-8 -*-
"""离线会话门禁：把"只在离线训练局读内存"从约定变成代码约束。

流程：
  1. 进入离线训练局后运行 `python run_offline.py verify`
     （打开设置面板 → 隔 2.5s 截两帧 → 顶部时钟不动 = 对局已暂停 = 离线）；
  2. 校验通过后写 `runtime/offline_session.json`（pid/serial/有效期/证据）；
  3. `MemSource.connect()` 每次校验该 token，缺失或过期直接拒绝读取。

token 只是本地工程约束，不涉及任何对抗检测的手段。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TOKEN_PATH = ROOT / "runtime" / "offline_session.json"
TOKEN_VERSION = 1
# 单局有效：换局（再来一局）/重开必须重签，避免 pid 不变时跨对局复用
DEFAULT_TTL_S = 45 * 60.0
MAX_CLOCK_SKEW_S = 300.0


def mint_token(path: Optional[Path], pid: Any, serial: str,
               method: str = "paused_clock",
               evidence: Optional[dict] = None,
               ttl_s: float = DEFAULT_TTL_S,
               now: Optional[float] = None) -> dict:
    """写入离线会话 token，返回 token 内容。"""
    path = Path(path) if path else DEFAULT_TOKEN_PATH
    now = time.time() if now is None else float(now)
    data = {
        "version": TOKEN_VERSION,
        "mode": "offline",
        "serial": str(serial),
        "pid": str(pid),
        "created_at": now,
        "expires_at": now + float(ttl_s),
        "method": method,
        "evidence": evidence or {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return data


def load_token(path: Optional[Path] = None) -> Optional[dict]:
    path = Path(path) if path else DEFAULT_TOKEN_PATH
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def verify_token(pid: Any, serial: str, path: Optional[Path] = None,
                 now: Optional[float] = None) -> Tuple[bool, str]:
    """校验 token；返回 (ok, reason)。reason 同时用作打印信息。"""
    now = time.time() if now is None else float(now)
    path = Path(path) if path else DEFAULT_TOKEN_PATH
    if not path.exists():
        return False, f"missing token ({path})"
    data = load_token(path)
    if not isinstance(data, dict):
        return False, "token unreadable"
    if data.get("version") != TOKEN_VERSION:
        return False, f"token version {data.get('version')} != {TOKEN_VERSION}"
    if data.get("mode") != "offline":
        return False, f"token mode {data.get('mode')} != offline"
    if str(data.get("serial")) != str(serial):
        return False, f"token serial {data.get('serial')} != {serial}"
    if str(data.get("pid")) != str(pid):
        return False, f"token pid {data.get('pid')} != {pid} (对局/进程已变)"
    created = float(data.get("created_at") or 0)
    expires = float(data.get("expires_at") or 0)
    if created > now + MAX_CLOCK_SKEW_S:
        return False, "token created_at is in the future"
    if expires <= now:
        return False, f"token expired ({int(now - expires)}s ago)"
    return True, "ok"


def revoke_token(path: Optional[Path] = None) -> bool:
    path = Path(path) if path else DEFAULT_TOKEN_PATH
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
