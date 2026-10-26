# -*- coding: utf-8 -*-
"""推理部署清单与自检（MODEL-02）。

清单只描述"部署了什么、用哪个后端、预算多少"，不复制模型源码。
自检分两级：
  - 默认：文件存在 + 依赖模块可导入 + sha256；
  - `--load`：真正加载 ONNX / OpenVINO IR（CPU 会话 / IR 解析），确认可部署。

LayaScorer 仍是运行时选择后端的唯一入口（auto/onnx/openvino/torch）；本模块
只回答"这台机器现在能不能按清单加载模型"，供启动前 gate 与 CI 使用。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "configs" / "inference.json"
REQUIRED = {"primary", "primary_backend", "fallback", "backends", "budgets_ms"}


class ManifestError(ValueError):
    pass


def load_manifest(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else DEFAULT_MANIFEST
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ManifestError("推理清单顶层必须是对象")
    missing = REQUIRED - set(raw)
    if missing:
        raise ManifestError(f"缺少字段: {sorted(missing)}")
    if raw["primary_backend"] not in raw["backends"]:
        raise ManifestError("primary_backend 必须指向 backends 中的后端")
    for name, cfg in raw["backends"].items():
        if not isinstance(cfg, dict) or not cfg.get("model"):
            raise ManifestError(f"后端 {name} 缺少 model 路径")
    return raw


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _module_ok(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _load_onnx(path: Path) -> tuple[bool, str]:
    import onnxruntime as ort
    t0 = time.perf_counter()
    sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    ms = (time.perf_counter() - t0) * 1000.0
    inputs = [i.name for i in sess.get_inputs()]
    outputs = [o.name for o in sess.get_outputs()]
    return True, f"{ms:.0f}ms inputs={inputs[:4]} outputs={outputs[:4]}"


def _load_openvino(path: Path) -> tuple[bool, str]:
    from openvino import Core
    t0 = time.perf_counter()
    core = Core()
    model = core.read_model(str(path))
    ms = (time.perf_counter() - t0) * 1000.0
    return True, f"{ms:.0f}ms inputs={len(model.inputs)} outputs={len(model.outputs)}"


LOADERS = {"onnx": _load_onnx, "openvino": _load_openvino}


def inspect(manifest: dict[str, Any], root: str | Path = ROOT,
            load: bool = False) -> dict[str, Any]:
    root = Path(root)
    report: dict[str, Any] = {"primary": manifest["primary"], "backends": {}}
    for name, cfg in manifest["backends"].items():
        path = root / cfg["model"]
        entry: dict[str, Any] = {
            "model": str(path),
            "exists": path.exists(),
            "module": cfg.get("module"),
            "module_ok": _module_ok(cfg["module"]) if cfg.get("module") else None,
            "loaded": None,
            "detail": "",
        }
        if path.exists():
            entry["bytes"] = path.stat().st_size
            entry["sha256"] = sha256(path)
        if not path.exists():
            entry["status"] = "missing"
        elif entry["module_ok"] is False:
            entry["status"] = "module_missing"
        elif load:
            loader = LOADERS.get(name)
            if loader is None:
                entry["status"] = "no_loader"
            else:
                try:
                    ok, detail = loader(path)
                    entry["loaded"], entry["detail"] = ok, detail
                    entry["status"] = "ready" if ok else "load_failed"
                except Exception as exc:
                    entry["status"] = "load_failed"
                    entry["detail"] = f"{type(exc).__name__}: {exc}"
        else:
            entry["status"] = "ready"
        report["backends"][name] = entry
    report["primary_ready"] = (
        report["backends"][manifest["primary_backend"]]["status"] == "ready")
    report["budgets_ms"] = manifest["budgets_ms"]
    return report
