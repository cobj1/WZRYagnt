# -*- coding: utf-8 -*-
"""安全红线检查：源码不得包含注入/写进程内存/反作弊规避调用；仓库不得跟踪敏感文件。

用法：
    python -X utf8 scripts/validate_safety.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = re.compile(
    r"WriteProcessMemory|CreateRemoteThread|process_vm_writev|"
    r"ptrace\s*\(|frida|xposed|Magisk",
    re.IGNORECASE,
)
SKIP_DIRS = {"venv", "__pycache__", ".git", "temp", "runs", "data"}
SECRET_PATTERNS = re.compile(r"(^|/)(secrets\.h|\.env|board\.json)$", re.IGNORECASE)


def main() -> int:
    errors = []
    for base in (ROOT / "wzry", ROOT / "scripts"):
        for path in base.rglob("*.py"):
            if path.name == "validate_safety.py":
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for no, line in enumerate(lines, 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if FORBIDDEN.search(line):
                    errors.append(f"{path.relative_to(ROOT)}:{no}: 禁止的注入/规避 API")

    try:
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        ).stdout.splitlines()
    except OSError:
        tracked = []
    for rel in tracked:
        if SECRET_PATTERNS.search(rel.replace("\\", "/")):
            errors.append(f"敏感文件被 git 跟踪: {rel}")

    for e in errors:
        print(f"ERROR {e}")
    print(f"security: {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
