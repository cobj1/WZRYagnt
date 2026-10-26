# -*- coding: utf-8 -*-
"""offline_gate 无设备回归：token 生成/校验/过期/串号/版本。"""
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision.offline_gate import (  # noqa: E402
    TOKEN_VERSION, load_token, mint_token, revoke_token, verify_token,
)

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "offline_session.json"
        now = 1_800_000_000.0

        ok, reason = verify_token("123", "127.0.0.1:16384", path, now=now)
        check("missing token rejected", not ok and "missing" in reason, reason)

        data = mint_token(path, pid="123", serial="127.0.0.1:16384",
                          evidence={"clock_diff": 0.0}, now=now)
        check("mint writes file", path.exists() and data["mode"] == "offline")
        check("token readable", (load_token(path) or {}).get("pid") == "123")

        ok, reason = verify_token("123", "127.0.0.1:16384", path, now=now + 10)
        check("valid token accepted", ok and reason == "ok", reason)

        ok, reason = verify_token("999", "127.0.0.1:16384", path, now=now + 10)
        check("pid mismatch rejected", not ok and "pid" in reason, reason)

        ok, reason = verify_token("123", "127.0.0.1:7555", path, now=now + 10)
        check("serial mismatch rejected", not ok and "serial" in reason, reason)

        ok, reason = verify_token("123", "127.0.0.1:16384", path,
                                  now=now + 4 * 3600 + 1)
        check("expired token rejected", not ok and "expired" in reason, reason)

        mint_token(path, pid="123", serial="127.0.0.1:16384",
                   ttl_s=60, now=now + 10_000)
        ok, reason = verify_token("123", "127.0.0.1:16384", path, now=now)
        check("future-dated token rejected",
              not ok and "future" in reason, reason)

        bad = json.loads(path.read_text(encoding="utf-8"))
        bad["version"] = TOKEN_VERSION + 1
        path.write_text(json.dumps(bad), encoding="utf-8")
        ok, reason = verify_token("123", "127.0.0.1:16384", path, now=now + 10_100)
        check("version mismatch rejected", not ok and "version" in reason, reason)

        path.write_text("{not json", encoding="utf-8")
        ok, reason = verify_token("123", "127.0.0.1:16384", path, now=now)
        check("corrupt token rejected", not ok and "unreadable" in reason, reason)

        check("revoke removes token", revoke_token(path) and not path.exists())
        check("revoke twice is safe", revoke_token(path) is False)

        # MemSource.connect() 必须被门禁挡住（不触碰设备：find_pid/refresh_maps 打桩）
        import wzry.vision.offline_gate as gate
        from wzry.vision.mem_source import MemSource
        old_path = gate.DEFAULT_TOKEN_PATH
        gate.DEFAULT_TOKEN_PATH = path
        try:
            src = MemSource(serial="127.0.0.1:16384")
            src.m.find_pid = lambda: "123"
            src.m.refresh_maps = lambda: None
            try:
                src.connect()
                guarded = False
            except RuntimeError as exc:
                guarded = "offline gate" in str(exc)
            check("MemSource refuses without token", guarded)

            mint_token(path, pid="999", serial="127.0.0.1:16384")
            try:
                src.connect()
                guarded = False
            except RuntimeError as exc:
                guarded = "offline gate" in str(exc)
            check("MemSource refuses on pid mismatch", guarded)

            mint_token(path, pid="123", serial="127.0.0.1:16384")
            check("MemSource connects with valid token", src.connect() == "123")
        finally:
            gate.DEFAULT_TOKEN_PATH = old_path

    print()
    if FAILS:
        print(f"{len(FAILS)} failed: {FAILS}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
