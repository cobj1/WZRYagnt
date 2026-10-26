# -*- coding: utf-8 -*-
"""laya ONNX 的 OpenVINO 后端: 在 NPU / Arc iGPU 上跑打分器(laya-fusion-plan §6.2)。

## 为什么是"替换 session"而不是重写推理

laya 的 `ONNXAgent._infer_batch`(onnx_agent.py:736) 只通过一个接口取结果:

    ort_outs = self.session.run(["logits", "act_logits"], ort_inputs)
    logits, act_logits = ort_outs[0], ort_outs[1]

`ort_inputs` 是 numpy dict(input_ids / attention_mask / marker_pos / marker_mask / qtype),
之后的 softmax、按选项数切片、温度校准全在 numpy 侧由 laya 完成。因此只要给
`agent.session` 换一个带 `run()` 的对象, tokenization / prompt 构造 / 解码 / 校准
全部复用, 零重写。

## 静态形状

NPU 不支持动态形状(预览特性), 且官方 ONNX 的 `qtype` 输入会引入 83 个动态端口,
必须先冻结 `qtype`(见 `scripts/export_laya_ov.py`)再导出固定形状的 IR:
  input_ids/attention_mask [1, max_len], marker_pos/marker_mask [1, max_opts]

本 shim 负责把任意 (B, L, K) 的输入 pad/truncate 到编译时的静态形状:
  - B > 1: 逐行推理(我们的 scorer 走 system_one, 常态 B=1)
  - L < max_len: 用 pad_token_id 补齐, attention_mask 置 0
  - K < max_opts: marker_pos 补 0, marker_mask 置 0
    (laya 自身的约定就是"被 mask 的槽位记 -1e4, 解码时切掉", 所以补槽不影响有效选项)

## 实测(Intel Core Ultra 7 255H, L=512/K=16 最坏情况)

  CPU 876.5ms | NPU 403.4ms | **Arc 140T iGPU 92.5ms**
"""
import time

import numpy as np

_DEFAULT_IR = "runs/laya/laya_ov/laya_qfreeze.xml"


class OVSession:
    """`onnxruntime.InferenceSession` 的 OpenVINO 替身(只实现 laya 用到的 `run`)。

    参数
      ir_path      OpenVINO IR(.xml); 由 scripts/export_laya_ov.py 产出
      device       "GPU" / "NPU" / "CPU" / "AUTO"
      max_len      编译时的固定序列长度
      max_opts     编译时的固定选项数
      pad_token_id 补齐 input_ids 用(从 tokenizer 取)
    """

    def __init__(self, ir_path=_DEFAULT_IR, device="GPU", max_len=512, max_opts=16,
                 pad_token_id=0, num_requests=1):
        import openvino as ov
        self.ov = ov
        self.ir_path = str(ir_path)
        self.device = device
        self.max_len = int(max_len)
        self.max_opts = int(max_opts)
        self.pad_token_id = int(pad_token_id or 0)
        self.core = ov.Core()
        if device not in self.core.available_devices and device != "AUTO":
            raise RuntimeError(
                f"OpenVINO 设备 {device!r} 不可用; 可用: {self.core.available_devices}")
        self.model = self.core.read_model(self.ir_path)
        self.compiled = self.core.compile_model(self.model, device)
        self.req = self.compiled.create_infer_request()
        self._out_names = [o.any_name for o in self.compiled.outputs]
        # 以编译产物为权威修正静态形状：调用方传的 max_len/max_opts 只是“期望值”，
        # 与 IR 不一致时必须按 IR 走，否则 infer 会以
        # "The input tensor size is not equal to the model input type" 崩在第一次推理。
        self.declared_max_len = int(max_len)
        self.declared_max_opts = int(max_opts)
        self.max_len, self.max_opts = self._resolve_static_shapes(max_len, max_opts)
        if self.max_len != self.declared_max_len or self.max_opts != self.declared_max_opts:
            print(f"[OVSession][警告] 编译形状 L={self.max_len}/K={self.max_opts} "
                  f"与调用方声明 L={self.declared_max_len}/K={self.declared_max_opts} 不一致, "
                  f"已按 IR 实际形状运行({self.ir_path})")
        self.calls = 0
        self.truncated = 0      # v14.3 输入超过 max_len 被截断的次数(应由 laya 侧避免)
        self._warned_trunc = False
        # v14.3 归因埋点: 单独记纯 OpenVINO 推理耗时(GPU/CPU 计算本身),
        #   与 LayaScorer 记的 latency_ms(端到端: tokenize + 推理 + 解码校准)相减
        #   即为 Python 侧开销 + 资源抢占等待。真机延迟归因靠这个。
        self._infer_ms = []

    @property
    def infer_ms(self):
        """最近 200 次纯推理耗时的统计: (p50, p95, n)。"""
        xs = sorted(self._infer_ms[-200:])
        if not xs:
            return (None, None, 0)
        return (xs[len(xs) // 2], xs[int(0.95 * (len(xs) - 1))], len(xs))

    def _resolve_static_shapes(self, max_len, max_opts):
        """从编译后的端口读静态形状; 动态维回退到调用方声明值。"""
        def static_dim(port, idx, default):
            if port is None:
                return int(default)
            try:
                dim = port.get_partial_shape()[idx]
                if dim.is_static:
                    return int(dim.get_length())
            except Exception:                                # noqa: BLE001
                pass
            return int(default)

        inputs = {p.any_name: p for p in self.compiled.inputs}
        resolved_len = static_dim(inputs.get("input_ids"), 1, max_len)
        resolved_opts = static_dim(inputs.get("marker_pos"), 1, max_opts)
        return resolved_len, resolved_opts

    # ---- onnxruntime 兼容接口 ----
    def run(self, output_names=None, input_feed=None):
        """签名与 ort.InferenceSession.run 对齐; 返回 [logits, act_logits]。"""
        feed = input_feed or {}
        ids = np.asarray(feed["input_ids"], dtype=np.int64)
        am = np.asarray(feed["attention_mask"], dtype=np.int64)
        mp = np.asarray(feed["marker_pos"], dtype=np.int64)
        mm = np.asarray(feed["marker_mask"]).astype(bool)

        if ids.ndim == 1:
            ids, am, mp, mm = ids[None], am[None], mp[None], mm[None]
        rows, n_opts = ids.shape[0], mp.shape[1]
        if n_opts > self.max_opts:
            raise ValueError(f"选项数 {n_opts} > 编译上限 {self.max_opts}; "
                             f"请用更大的 --opts 重新导出 IR")
        if ids.shape[1] > self.max_len:
            # 正常路径下 laya 已按 max_len 截断(见 LayaScorer._predict 传 max_len),
            # 这里兜底并计数, 免得"静默丢状态文本"这类问题没人发现。
            self.truncated += 1
            if not self._warned_trunc:
                self._warned_trunc = True
                print(f"[OVSession][警告] 输入 {ids.shape[1]} token > IR 上限 "
                      f"{self.max_len}, 已截断(状态文本可能被丢)")
            ids, am = ids[:, :self.max_len], am[:, :self.max_len]

        outs = [self._run_row(ids[i], am[i], mp[i], mm[i], n_opts) for i in range(rows)]
        logits = np.concatenate([o[0] for o in outs], axis=0)[:, :n_opts]
        act = np.concatenate([o[1] for o in outs], axis=0)
        return [logits, act]

    def _run_row(self, ids, am, mp, mm, n_opts):
        L, K = self.max_len, self.max_opts
        x_ids = np.full((1, L), self.pad_token_id, dtype=np.int64)
        x_am = np.zeros((1, L), dtype=np.int64)
        n = min(len(ids), L)
        x_ids[0, :n] = ids[:n]
        x_am[0, :n] = am[:n]
        x_mp = np.zeros((1, K), dtype=np.int64)
        x_mm = np.zeros((1, K), dtype=bool)
        x_mp[0, :n_opts] = mp[:n_opts]
        x_mm[0, :n_opts] = mm[:n_opts]
        t0 = time.perf_counter()
        out = self.req.infer({"input_ids": x_ids, "attention_mask": x_am,
                              "marker_pos": x_mp, "marker_mask": x_mm})
        self._infer_ms.append((time.perf_counter() - t0) * 1000)
        self.calls += 1
        return np.asarray(out["logits"]), np.asarray(out["act_logits"])

    # ---- 其它 onnxruntime 常用属性(被 laya 或调试代码读到时不至于炸) ----
    def get_providers(self):
        return [f"OpenVINOExecutionProvider:{self.device}"]

    def get_inputs(self):
        return [type("I", (), {"name": i.any_name})() for i in self.compiled.inputs]

    def get_outputs(self):
        return [type("O", (), {"name": n})() for n in self._out_names]
