# -*- coding: utf-8 -*-
"""把 YOLOv5 检测器导出为 ONNX (P0 感知重建前置步骤)。

背景:
  `runs/detect/hok_yolov5/gh_hok_yolov5s_best.pt` 来自 GitHub `ironmanhcx/-YOLO-`,
  是 YOLOv5(7.0) 格式 —— ultralytics 8.x 拒绝加载(实测: 不可向前兼容)。
  本脚本用 `tools/hok_yolo/yolov5-7.0` 的代码加载后导出 ONNX, 之后推理只依赖
  onnxruntime, 不再需要 yolov5 仓库(与 Laya 的 ONNX 路线一致)。

类别(8 类, id 顺序): hero / soldier / Buff / grass / wild_monster / dragon /
  tower / crystal  (YOLOv5 混淆矩阵里多出的 background 是评估虚拟类, 非模型输出)

运行:
  venv\\Scripts\\python.exe -X utf8 scripts/export_detector_onnx.py
"""
import argparse
import inspect
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
YV5 = ROOT / "tools" / "hok_yolo" / "yolov5-7.0"
DEFAULT_W = ROOT / "runs" / "detect" / "hok_yolov5" / "gh_hok_yolov5s_best.pt"
DEFAULT_OUT = ROOT / "runs" / "detect" / "hok_yolov5" / "hok_yolov5s.onnx"


def _relax_weights_only():
    """较新 torch 对 torch.load 默认 weights_only=True, 会拒绝 YOLOv5 老式 checkpoint。"""
    import torch
    p = inspect.signature(torch.load).parameters.get("weights_only")
    if p is not None and p.default is not False:
        orig = torch.load

        def _load(*a, **k):
            k.setdefault("weights_only", False)
            return orig(*a, **k)

        torch.load = _load
        return True
    return False


def _stub_ipython():
    """yolov5 为 notebook 场景 `import IPython`(仅用于 is_colab/display 展示),
    导出不需要 -> 打桩, 免装 ipython 及其一串依赖。"""
    import types
    if "IPython.display" in sys.modules:
        return
    ip = types.ModuleType("IPython")
    ip.get_ipython = lambda: None
    disp = types.ModuleType("IPython.display")
    disp.display = lambda *a, **k: None
    ip.display = disp
    sys.modules["IPython"] = ip
    sys.modules["IPython.display"] = disp


def main():
    ap = argparse.ArgumentParser(description="export YOLOv5 detector to ONNX")
    ap.add_argument("--weights", default=str(DEFAULT_W))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--opset", type=int, default=12)
    ap.add_argument("--no-verify", action="store_true")
    args = ap.parse_args()

    if not YV5.exists():
        print(f"缺少 yolov5 代码: {YV5}")
        return 1
    sys.path.insert(0, str(YV5))

    import torch
    import torch.nn as nn

    if _relax_weights_only():
        print("[export] 已放宽 torch.load 的 weights_only 默认值(YOLOv5 老式 ckpt)")
    _stub_ipython()

    from models.experimental import attempt_load

    print(f"[export] 加载权重 {Path(args.weights).name} ...")
    t0 = time.time()
    model = attempt_load(args.weights, device=None, inplace=False, fuse=True)
    model.eval()
    # YOLOv5 Detect.forward 依 self.training 分支: 训练模式会多输出分支张量;
    # 显式置 False 确保只走推理分支(等价 model.eval() 但更明确)
    det = model.model[-1]
    det.training = False
    # dynamic=True -> 每次前向重建锚点网格, 才能支持 H/W 动态输入
    # (与 dynamic_axes 里的 height/width 配套; 否则 ONNX 会因网格广播报错)
    det.dynamic = True
    names = model.names
    print(f"[export] 就绪 {time.time()-t0:.0f}s | 类别({len(names)}): {names}")

    class ExportWrap(nn.Module):
        """YOLOv5 Detect 在部分版本会返回 (pred, branches); 只暴露最终预测。"""

        def __init__(self, m):
            super().__init__()
            self.m = m

        def forward(self, x):
            y = self.m(x)
            return y[0] if isinstance(y, (list, tuple)) else y

    wrapped = ExportWrap(model).eval()
    dummy = torch.zeros(1, 3, args.imgsz, args.imgsz)

    print("[export] 干跑一次前向...")
    with torch.no_grad():
        y = wrapped(dummy)
    print(f"[export] 输出形状 {tuple(y.shape)}  (期望 (1, 25200, 5+nc))")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"[export] torch.onnx.export -> {out} ...")
    t0 = time.time()
    with torch.no_grad():
        torch.onnx.export(
            wrapped, dummy, str(out),
            input_names=["images"], output_names=["output"],
            # batch/anchor/height/width 全动态 —— 与 YOLOv5 官方 export.py 一致,
            # 这样推理可走 auto=True 最小矩形填充(不补成正方形, 更准更快)
            dynamic_axes={"images": {0: "batch", 2: "height", 3: "width"},
                          "output": {0: "batch", 1: "anchors"}},
            opset_version=args.opset,
            do_constant_folding=True,
            dynamo=False,   # 走 legacy TorchScript 导出器(较新 torch 的 dynamo 路径需 onnxscript)
        )
    print(f"[export] 完成 {time.time()-t0:.0f}s, 大小 {out.stat().st_size/1e6:.1f}MB")

    # 类别清单一并落盘(下游适配器要用)
    import json
    (out.parent / "hok_yolov5s_classes.json").write_text(
        json.dumps({"names": names, "imgsz": args.imgsz,
                    "source": "github.com/ironmanhcx/-YOLO- (YOLOv5s)",
                    "nc": len(names)}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    if args.no_verify:
        return 0

    # ---- ONNX vs torch 数值一致性 ----
    print("[verify] onnxruntime 一致性校验...")
    import numpy as np
    import onnxruntime as ort
    sess = ort.InferenceSession(str(out), providers=["CPUExecutionProvider"])
    rng = np.random.default_rng(0)
    x = rng.random((1, 3, args.imgsz, args.imgsz), dtype=np.float32)
    with torch.no_grad():
        y_t = wrapped(torch.from_numpy(x)).numpy()
    y_o = sess.run(["output"], {"images": x})[0]
    print(f"[verify] torch {y_t.shape} vs onnx {y_o.shape}")
    diff = float(np.abs(y_t - y_o).max())
    print(f"[verify] max|Δ| = {diff:.6f} -> {'PASS' if diff < 1e-3 else 'FAIL'}")
    return 0 if diff < 1e-3 else 2


if __name__ == "__main__":
    raise SystemExit(main())
