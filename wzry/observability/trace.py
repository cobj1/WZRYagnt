# -*- coding: utf-8 -*-
"""决策轨迹落盘：每帧一行 JSONL，供回放与离线复盘。

不替代 data/matches 的状态录制，只补决策来源/耗时/任务意图这三类
原本只在 stdout 里、无法按帧检索的信息。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class DecisionTrace:
    def __init__(self, path: str | Path | None = None, flush_every: int = 20) -> None:
        self.path = Path(path) if path else None
        self.flush_every = max(1, int(flush_every))
        self._fp = None
        self._pending = 0
        self.written = 0

    @property
    def active(self) -> bool:
        return self._fp is not None

    def open(self) -> None:
        if self._fp is not None or self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = open(self.path, "a", encoding="utf-8")

    def write(self, payload: dict[str, Any]) -> None:
        if self._fp is None or not payload:
            return
        self._fp.write(json.dumps(payload, ensure_ascii=False,
                                  separators=(",", ":")) + "\n")
        self._pending += 1
        self.written += 1
        if self._pending >= self.flush_every:
            self._fp.flush()
            self._pending = 0

    def close(self) -> None:
        if self._fp is not None:
            try:
                self._fp.flush()
            finally:
                self._fp.close()
                self._fp = None
