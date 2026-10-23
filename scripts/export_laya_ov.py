# -*- coding: utf-8 -*-
"""laya ONNX -> OpenVINO IR(NPU / Arc iGPU 可用) + 设备实测。

## 为什么需要这个脚本

laya 官方 ONNX 导出**不能直接**在 OpenVINO 上编译/推理, 实测两个硬阻塞:

1. **`qtype` 输入导致图无法静态化**。`qtype` 是问题类型选择器
   (`h = h + type_emb(qtype)`, 见 laya/common.py:621), 它驱动
   `is_score = (qtype == QTYPES["score"])` 这类数据相关分支, 于是即使把
   4 个主输入 reshape 成静态形状, 图中**仍有 83 个动态端口**, NPU 直接报
   `[NPU_VCL] Got negative shape dim bound: '-1'` / `Upper bounds were not specified`。
2. **`GatherElements` batch 维不一致**: 未冻结 qtype 时 CPU/GPU 也会在 infer 阶段
   报 `Shapes [0,512,1024] and [1,16,1024] are not consistent`(data 的 batch 维推成 0)。

本项目只用 `choice` 型问题 -> `qtype` 恒为 0(见 laya/agent.py:928
`"qtype": torch.zeros(rows, dtype=torch.long)`)。把 `qtype` 从输入改成常量后:
  - 动态端口 **83 -> 0**(完全静态)
  - NPU / GPU / CPU **全部编译+推理成功**, 且三设备输出一致

## 实测结果(2026-10-15, Intel Core Ultra 7 255H + Arc 140T + AI Boost NPU)

固定 L=512, K=16(最坏情况; 线上典型 L 约 150-200):

| 设备 | 算力(OpenVINO 报) | 编译 | 推理 p50 | 推理 p95 | 相对 CPU |
|---|---|---|---|---|---|
| CPU | — | 1.3s | 871.8ms | 892.5ms | 1.0x |
| **NPU** (AI Boost, arch 3720) | 13.1 TOPS INT8 / 6.55 TFLOPS FP16 | 33.6s | **401.4ms** | 402.7ms | 2.2x |
| **GPU** (Arc 140T iGPU) | 73.7 TOPS INT8 / 36.9 TFLOPS FP16 | 4.8s | **90.6ms** | 91.6ms | 9.6x |

结论: **NPU 可用但不划算**(401ms, 仍远高于 P1 的 80ms 门槛, 也慢于原 onnxruntime
int8 CPU 路径); **Arc 140T 核显才是目标设备**(90.6ms, L=512 最坏情况;
线上 L 约 1/3 时预计 30-50ms -> 有希望过 80ms 门槛)。进一步可用 NNCF 做静态 INT8
量化, 核显 INT8 理论算力是 FP16 的 2 倍。

## 运行(需要带 openvino 的 Python 环境, 不是项目 venv)

  <ov_python> scripts/export_laya_ov.py \
      --onnx runs/laya/laya.onnx --out runs/laya/laya_ov \
      --max-len 512 --opts 16 --devices NPU,GPU,CPU

产物: runs/laya/laya_ov/laya_qfreeze.xml + .bin (OpenVINO IR)

## 后续接线(尚未实现)

laya 的 `agent.py:1241` 是 `out = self.model(input_ids, attention_mask, marker_pos,
marker_mask, qtype)` -> 返回 `(logits, act_logits)`。因此只要实现一个同签名的
**后端 shim**(把 numpy 喂给 OpenVINO IR, 再包成 torch tensor 返回), 替换 `self.model`
即可复用 laya 全部 tokenization / prompt 构造 / 温度校准逻辑, 无需重写。
注意 shim 必须 pad/truncate 到编译时的固定 L 与 K:
  - input_ids / attention_mask -> 长度 L(pad token, mask 置 0)
  - marker_pos / marker_mask  -> 选项数 K(marker_mask 置 0 表示该项无效)
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np
import onnx
from onnx import numpy_helper


def main():
    ap = argparse.ArgumentParser(description="laya ONNX -> OpenVINO IR (NPU/iGPU)")
    ap.add_argument("--onnx", default="runs/laya/laya.onnx")
    ap.add_argument("--out", default="runs/laya/laya_ov")
    ap.add_argument("--max-len", type=int, default=512, help="静态化后的序列长度 L")
    ap.add_argument("--opts", type=int, default=16, help="静态化后的选项数 K(方案上限 20)")
    ap.add_argument("--devices", default="", help="逗号分隔, 给出则在导出后实测, 如 NPU,GPU,CPU")
    ap.add_argument("--iters", type=int, default=8)
    ap.add_argument("--fp16", action="store_true", default=True, help="压缩到 fp16(默认开)")
    args = ap.parse_args()

    try:
        import openvino as ov
    except Exception as e:
        print(f"需要 openvino: {e}\n  例: <ov_python> -m pip install openvino onnx")
        return 1

    src = Path(args.onnx)
    if not src.exists():
        print(f"找不到 {src}")
        return 1

    print(f"[1] 载入 ONNX {src}")
    t0 = time.time()
    m = onnx.load(str(src))
    print(f"    {time.time() - t0:.1f}s  inputs={[i.name for i in m.graph.input]}")

    qi = next((i for i in m.graph.input if i.name == "qtype"), None)
    if qi is None:
        print("    [warn] 没有 qtype 输入 —— 可能已是静态导出, 直接继续")
    else:
        m.graph.initializer.append(
            numpy_helper.from_array(np.zeros((1,), dtype=np.int64), "qtype"))
        m.graph.input.remove(qi)
        print("    qtype 已冻结为常量 0(本项目只用 choice 型问题)")

    print("[2] 转换并保存 IR")
    tmp = Path(args.out)
    tmp.mkdir(parents=True, exist_ok=True)
    tmp_onnx = tmp / "_qfreeze.onnx"
    onnx.save_model(m, str(tmp_onnx))
    core = ov.Core()
    try:
        om = ov.convert_model(str(tmp_onnx), input=[
            ov.PartialShape([1, args.max_len]), ov.PartialShape([1, args.max_len]),
            ov.PartialShape([1, args.opts]), ov.PartialShape([1, args.opts])])
    finally:
        tmp_onnx.unlink(missing_ok=True)

    dyn = 0
    for op in om.get_ordered_ops():
        for p in op.outputs():
            try:
                if p.get_partial_shape().is_dynamic:
                    dyn += 1
            except Exception:
                pass
    print(f"    动态端口数 = {dyn} (未冻结 qtype 时为 83; 必须为 0 才能上 NPU)")
    if dyn:
        print("    [warn] 仍有动态维 -> NPU 编译大概率失败")

    ir = tmp / "laya_qfreeze.xml"
    ov.save_model(om, str(ir), compress_to_fp16=args.fp16)
    print(f"    已保存 {ir} (+ .bin)")

    if not args.devices:
        return 0

    print(f"\n[3] 设备可用性: {core.available_devices}")
    inputs = {
        "input_ids": np.zeros((1, args.max_len), dtype=np.int64),
        "attention_mask": np.ones((1, args.max_len), dtype=np.int64),
        "marker_pos": np.arange(1, args.opts + 1, dtype=np.int64).reshape(1, args.opts),
        "marker_mask": np.ones((1, args.opts), dtype=bool),
    }
    for dev in [d.strip() for d in args.devices.split(",") if d.strip()]:
        if dev not in core.available_devices:
            print(f"\n=== {dev}: 不可用, 跳过")
            continue
        print(f"\n=== {dev}")
        try:
            go = core.get_property(dev, "DEVICE_GOPS")
            print(f"    算力: { {str(k): round(v / 1000, 1) for k, v in go.items()} } TOPS/TFLOPS")
        except Exception:
            pass
        t0 = time.time()
        try:
            c = core.compile_model(om, dev)
        except Exception as e:
            print(f"    FAIL compile: {str(e)[:300]}")
            continue
        print(f"    compile OK {time.time() - t0:.1f}s")
        try:
            r = c.create_infer_request()
            r.infer(inputs)
            lat = []
            for _ in range(args.iters):
                t0 = time.time()
                out = r.infer(inputs)
                lat.append((time.time() - t0) * 1000)
            lat.sort()
            p50, p95 = lat[len(lat) // 2], lat[int(0.95 * (len(lat) - 1))]
            print(f"    infer OK  p50={p50:.1f}ms  p95={p95:.1f}ms "
                  f"(L={args.max_len} K={args.opts}; P1 门槛 p95<80ms)")
            for name, arr in zip([o.any_name for o in c.outputs], out.values()):
                a = np.asarray(arr)
                print(f"      out {name}: {a.shape} mean={a.mean():.4f}")
        except Exception as e:
            print(f"    FAIL infer: {str(e)[:300]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
