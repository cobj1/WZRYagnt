# -*- coding: utf-8 -*-
"""v14.2 可插拔决策打分器 (laya-fusion-plan C1)。

Scorer 协议(optimizer 侧只调这三个, 鸭子类型):
  reorder(scored, f, hp, camp) -> bool   原地排序 scored=[(rule_score, (action, tag))];
                                         True=已按混合分排序; False=调用方按规则分排序
  log_chosen(action) -> None             optimizer.solve 返回前回调, 记录最终选择
  stats() -> dict / stop() -> None
可选协议(主循环接):
  set_context(session_id=..., frame_id=..., model_sha=...)  运行期上下文, 写进每条记录
  log_executed(action) -> None           执行层实际下发的动作(经 guards/节流后)

LayaScorer 设计约束(laya-fusion-plan.md §3):
  - 异步线程推理: 主循环只做 缓存查表 + 非阻塞入队, 永不等待
  - top-16 按规则分粗排 + 短标签; 结果按 (状态粗分箱, 标签元组) 缓存 <=300ms
  - ensemble 只改排序分 s = r + beta*(p - 1/K); 阈值比较仍用原始 r(optimizer 侧保证)
  - 模型懒加载: 未安装 laya / 加载失败 -> 永远返回 False(等价 off), 异常计数
  - shadow 落盘 runs/laya/shadow.jsonl
  - 标签必须唯一: 重复标签会让选项数 < K 且概率被复用到不同动作 -> 计数 + fail-open
  - latency_ms 在预测与缓存命中两条路径都落盘(P1 门槛 p95<80ms 依赖它)
  - 运行期熔断: 异常数 >= max_errors 或标签重复 >= max_label_dups -> 本局永久降级 off
  - 推理后端可选: backend="openvino" 时把 laya ONNX 的 session 换成 OpenVINO,
    跑在 Arc 核显/NPU 上(方案 §6.2; 本机无独显, CPU 876ms / NPU 403ms / 核显 92.5ms)
"""
import json
import math
import queue
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHADOW_DIR = ROOT / "runs" / "laya"
TOP_K = 16
CACHE_TTL = 0.30
MAX_ERRORS = 20          # v14.2 熔断: 累计异常数上限(0=不熔断)
MAX_LABEL_DUPS = 5       # v14.2 熔断: 标签重复次数上限(0=不熔断)
# v14.3 OpenVINO 后端的默认序列长度。实测本项目的状态模板 + 16 选项只需 274 token
#   (state 99 + 问题/选项 175), 取 320 留余量。IR 必须按同一长度导出。
#   实测 Arc 140T 核显: L=512 -> 92.5ms, L=320 -> 50.7ms(FFN 主导, 近似随 L 线性)。
OV_MAX_LEN = 320

_INSTR = ("Choose the best option for the hero's next action. "
          "Prefer options that enable kills with allies nearby, avoid enemy "
          "clusters and turret zones when low HP, and stay on the objective path.")


def candidate_label(cand):
    """(action, tag) -> 短标签(<=24 字符 ASCII), 供 Laya choice criteria 用。"""
    try:
        tag = None
        if isinstance(cand, (tuple, list)) and len(cand) > 1:
            tag = cand[1]
        obj = tag if tag is not None else (cand[0] if isinstance(cand, (tuple, list)) else cand)
        if isinstance(obj, dict):
            obj = obj.get("reason") or obj.get("type") or "none"
        s = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in str(obj))
        return (s[:24] or "none").lower()
    except Exception:
        return "none"


def build_state_text(f, camp="blue"):
    """_feat 特征 -> 压缩英文状态文本(约 350 字符, 远小于 512 token 上限)。"""
    try:
        gx, gy = f["g"]

        def pts(v, k=4):
            return " ".join(f"{float(p[0]):.2f},{float(p[1]):.2f}"
                            for p in (v or [])[:k]) or "none"

        d_near = min([math.hypot(gx - p[0], gy - p[1]) for p in (f.get("reds") or [])],
                     default=9.9)
        return ("MOBA hero positioning. "
                f"camp={camp} hp={float(f.get('hp', 1.0)):.2f} self=({gx:.2f},{gy:.2f}) "
                f"enemies_map={int(f.get('n_red', 0))} allies_map={int(f.get('n_blue', 0))} "
                f"enemy_heroes_screen={int(f.get('n_enemy', 0))} "
                f"enemy_turrets={int(f.get('n_turret', 0))} "
                f"nearest_enemy_d={d_near:.2f} turret_d={float(f.get('d_turret', 1.0)):.2f} "
                f"in_turret_zone={int(bool(f.get('in_turret_zone')))}. "
                f"enemy_pos={pts(f.get('reds'))}; ally_pos={pts(f.get('blues'))}.")
    except Exception:
        return "MOBA hero positioning. state unavailable."


def _state_bucket(f, camp):
    """v14.2 状态粗分箱(供缓存键)。

    原先缓存键只用 16 个短标签 -> 标签集合相同但状态迥异(HP 0.9 vs 0.2、
    最近敌 0.6 vs 0.1)时会命中同一份概率, 属跨状态复用。加入粗状态桶。
    """
    try:
        gx, gy = f["g"]
        d_near = min([math.hypot(gx - p[0], gy - p[1]) for p in (f.get("reds") or [])],
                     default=9.9)
        return (str(camp), int(round(float(f.get("hp", 1.0)) * 5)),
                int(f.get("n_red", 0)), int(f.get("n_blue", 0)),
                int(min(d_near, 0.99) * 5), int(bool(f.get("in_turret_zone"))))
    except Exception:
        return None


def _reason_of(action):
    """动作 -> 短原因串(<=24 字符小写), 与 log_chosen 的兜底口径一致。"""
    try:
        return str((action or {}).get("reason")
                   or (action or {}).get("type") or "none")[:24].lower()
    except Exception:
        return "none"


def _chosen_tag(cands, action):
    """在候选表里按对象身份找回该动作的短标签; 找不到则退回 _reason_of。"""
    try:
        for a, tg in (cands or []):
            if a is action:
                return candidate_label((a, tg))
    except Exception:
        pass
    return _reason_of(action)


class LayaScorer:
    def __init__(self, mode="shadow", beta=0.0, model_path=None,
                 top_k=TOP_K, cache_ttl=CACHE_TTL, shadow_path=None,
                 max_errors=MAX_ERRORS, max_label_dups=MAX_LABEL_DUPS,
                 min_submit_interval=0.0, stale_probs_ttl=0.0,
                 backend="auto", ov_device="GPU", ov_ir=None,
                 ov_max_len=OV_MAX_LEN, ov_max_opts=None):
        assert mode in ("off", "shadow", "ensemble")
        assert backend in ("auto", "onnx", "torch", "openvino")
        self.mode = mode
        self.beta = float(beta)
        self.model_path = model_path
        self.top_k = int(top_k)
        self.cache_ttl = float(cache_ttl)
        # v14.3 推理后端: auto=沿用原行为(优先 onnxruntime, 无则 Router/torch);
        #   openvino=用 OpenVINO IR 跑在 Arc 核显/NPU 上(见 wzry/decision/laya_ov.py,
        #   方案 §6.2: CPU 876ms / NPU 403ms / 核显 92.5ms @L=512)。
        self.backend = backend
        self.ov_device = ov_device
        self.ov_ir = ov_ir
        self.ov_max_len = int(ov_max_len)
        self.ov_max_opts = int(ov_max_opts) if ov_max_opts else int(top_k)
        # v14.2 提交节流(秒): >0 时两次"提交推理"的最小间隔, 0=不限。
        #   实测 ONNX int8 在 CPU 上单次 326~474ms, 而决策环 10Hz -> 队列(深度1)
        #   会丢掉约 70% 的提交, 徒增主线程开销与无谓日志。按模型实际吞吐限流。
        self.min_submit_interval = float(min_submit_interval)
        self._last_submit_t = 0.0
        # v14.4 「滞后一拍」复用: 异步 worker 的推理结果在**下一次**决策点才有用。
        #   实测 @2Hz（间隔 0.5s）且 cache_ttl=0.3s、状态 key 每帧唯一时,
        #   缓存必然过期 -> probs 恒为 None -> ensemble 模式**一次都不会重排**。
        #   把上一拍的概率按 max(bucket 放宽) 复用给本拍, 才能让融合真正生效。
        #   0 = 关闭(保持旧行为, 默认)。
        self.stale_probs_ttl = float(stale_probs_ttl)
        self._last_probs = None      # (t, labels, probs)
        self._lat_ms = []       # v14.3 端到端耗时采样(最近 200 次), 供延迟归因
        # v14.2 运行期熔断: 异常数 / 标签重复数 超阈值 -> 本局永久降级为 off。
        #   原先 P1 门槛"异常数=0"只能靠人工看日志; 模型加载失败时每拍都会
        #   抛异常并照常走完流程, 没有人会注意到。0 = 关闭该项熔断。
        self.max_errors = int(max_errors)
        self.max_label_dups = int(max_label_dups)
        self._tripped = False
        self._trip_reason = ""
        self.shadow_path = Path(shadow_path) if shadow_path else (
            SHADOW_DIR / "shadow.jsonl")
        self._q = queue.Queue(maxsize=1)
        self._cache = {}
        self._cache_lock = threading.Lock()
        self._pending = None
        self._pending_lock = threading.Lock()
        self._thread = None
        self._stop_flag = False
        self._stopped = False
        self._router = None
        self._agent = None
        self._onnx = False
        self._model_ok = False
        self._counts = {"errors": 0, "submits": 0, "cache_hits": 0,
                        "reorders": 0, "shadow_logged": 0, "predicts": 0,
                        "label_dups": 0, "exec_logged": 0, "latency_logged": 0,
                        "throttled": 0, "stale_hits": 0}
        self._last_err = ""
        self._last_err_print = 0.0
        self._ctx = {}          # v14.2 运行期上下文(session_id/frame_id/model_sha)

    # ---- 运行期上下文 ----
    def set_context(self, **kw):
        """主循环注入上下文; None 值忽略。会写进之后每条 shadow 记录。"""
        try:
            for k, v in kw.items():
                if v is not None:
                    self._ctx[k] = v
        except Exception:
            pass

    # ---- 生命周期 ----
    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._worker, daemon=True,
                                        name="laya-scorer")
        self._thread.start()

    def stop(self):
        self._stop_flag = True
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._flush_pending()      # v14.2: 收尾把最后一个未落盘的决策点写出

    def _worker(self):
        try:
            self._load_model()
        except Exception as e:
            self._err(e)
        while not self._stop_flag:
            try:
                job = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                break
            self._run_job(*job)
        self._stopped = True

    def _load_model(self):
        # 优先 ONNX 后端(P1 预案: CPU 上 int8 比 torch 快 ~3x, 见 scripts/export_laya_onnx.py);
        # 无 onnx 产物或 --laya-model 指定 HF 目录时回退 Router(torch)。
        # 未安装 laya 时在 worker 线程内失败, 不影响主循环。
        import laya as _laya
        onnx_path = None
        if self.model_path:
            mp = Path(self.model_path)
            for name in ("model.int8.onnx", "laya.int8.onnx", "model.onnx",
                         "laya.onnx"):
                p = mp / name
                if p.exists():
                    onnx_path = p
                    break
        else:
            for name in ("laya.int8.onnx", "laya.onnx"):
                p = SHADOW_DIR / name
                if p.exists():
                    onnx_path = p
                    break
        if onnx_path is not None:
            self._agent = self._load_onnx_agent(_laya, onnx_path)
            self._onnx = True
        else:
            if self.model_path:
                self._router = _laya.Router(
                    models={"english": str(self.model_path)}, default="english")
            else:
                self._router = _laya.Router()
            self._onnx = False
        self._model_ok = True
        # v14.2 模型指纹: 让 shadow 记录能追溯到具体权重产物(名+字节数+mtime)。
        try:
            src = onnx_path if onnx_path is not None else (
                self.model_path or "convaiinnovations/laya")
            p = Path(str(src))
            self._ctx["model_sha"] = (f"{p.name}:{p.stat().st_size}:{int(p.stat().st_mtime)}"
                                      if p.is_file() else str(src))
        except Exception:
            pass

    # ---- ONNX 侧加载(可选换 OpenVINO session) ----
    def _resolve_ov_ir(self):
        """定位 OpenVINO IR(.xml)。

        按 max_len 自动匹配, 依次尝试:
          runs/laya/laya_ov/laya_qfreeze_<max_len>.xml
          runs/laya/laya_ov<max_len>/laya_qfreeze.xml
          runs/laya/laya_ov/laya_qfreeze.xml          (兼容导出脚本默认输出)
        都没有时返回首选候选路径(供报错信息用)。
        """
        if self.ov_ir:
            return Path(self.ov_ir)
        cands = [SHADOW_DIR / "laya_ov" / f"laya_qfreeze_{self.ov_max_len}.xml",
                 SHADOW_DIR / f"laya_ov{self.ov_max_len}" / "laya_qfreeze.xml",
                 SHADOW_DIR / "laya_ov" / "laya_qfreeze.xml"]
        for c in cands:
            if c.exists():
                return c
        return cands[0]

    def _load_onnx_agent(self, _laya, onnx_path):
        """构造 laya ONNX agent; backend=openvino 时把 session 换成 OpenVINO。

        laya 的 ONNXAgent 只通过 `self.session.run(["logits","act_logits"], feed)` 取结果,
        所以替换 session 即可复用它的 tokenization / prompt 构造 / 解码 / 温度校准。
        """
        model_id = str(self.model_path) if self.model_path else "convaiinnovations/laya"
        if self.backend != "openvino":
            return _laya.load(model_id, backend="onnx", onnx_path=str(onnx_path))

        ir = self._resolve_ov_ir()
        if not ir.exists():
            raise FileNotFoundError(
                f"OpenVINO IR 不存在: {ir}\n  先跑: <ov_python> scripts/export_laya_ov.py "
                f"--onnx {onnx_path} --out {ir.parent}")
        from wzry.decision.laya_ov import OVSession

        # 构造期先把 ort.InferenceSession 换成不加载模型的占位, 免得白建一个 442MB 的
        # onnxruntime 会话(反正马上会被替换)。作用域限定在这次构造内。
        import onnxruntime as _ort

        class _Stub:
            def run(self, *a, **k):
                raise RuntimeError("stub session: 应在构造后立刻被 OVSession 替换")

        _orig = _ort.InferenceSession
        _ort.InferenceSession = lambda *a, **k: _Stub()
        try:
            agent = _laya.load(model_id, backend="onnx", onnx_path=str(onnx_path))
        finally:
            _ort.InferenceSession = _orig

        pad_id = getattr(getattr(agent, "tok", None), "pad_token_id", 0) or 0
        agent.session = OVSession(ir_path=ir, device=self.ov_device,
                                  max_len=self.ov_max_len, max_opts=self.ov_max_opts,
                                  pad_token_id=pad_id)
        self._ctx["laya_backend"] = f"openvino:{self.ov_device}"
        self._ctx["laya_ir"] = ir.name
        return agent

    # ---- 推理(可被子类/测试覆盖) ----
    def _predict(self, state_text, labels):
        if not self._model_ok:
            raise RuntimeError("laya model not loaded")
        questions = {"action": {"type": "choice", "instructions": _INSTR,
                                "criteria": {lab: lab for lab in labels}}}
        if self._onnx:
            # v14.3 backend=openvino 时把 max_len 传给 laya, 让 laya 自己按 IR 的编译形状
            # 截断并上报 usage.truncated —— 否则超长输入会被 shim 静默截断。
            kw = {"max_len": self.ov_max_len} if self.backend == "openvino" else {}
            res = self._agent.system_one(state_text, questions, **kw)
        else:
            res = self._router.predict(state_text, questions)
        self._counts["predicts"] += 1
        return self._extract_probs(res, labels)

    @staticmethod
    def _extract_probs(res, labels):
        """兼容多种返回形状: probabilities dict / choice+confidence。返回对齐 labels 的列表。"""
        try:
            ans = ((res or {}).get("answers") or {}).get("action") or {}
            for k in ("probabilities", "probs", "distribution", "scores"):
                d = ans.get(k)
                if isinstance(d, dict) and d:
                    out = [float(d.get(lab, 0.0)) for lab in labels]
                    s = sum(out)
                    return [v / s for v in out] if s > 0 else None
            lab = ans.get("choice") or ans.get("label")
            if lab in labels:
                conf = min(1.0, max(0.0, float(ans.get("confidence", 1.0))))
                rest = (1.0 - conf) / max(1, len(labels) - 1)
                return [conf if x == lab else rest for x in labels]
        except Exception:
            return None
        return None

    # ---- 主线程入口(非阻塞) ----
    def reorder(self, scored, f, hp, camp):
        """optimizer.solve 热路径调用。见模块 docstring 的协议约定。"""
        if self.mode == "off" or self._stopped or self._tripped:
            return False
        try:
            n = len(scored)
            if n == 0:
                return False
            k = min(self.top_k, n)
            idx = sorted(range(n), key=lambda i: -scored[i][0])[:k]
            labels = [candidate_label(scored[i][1]) for i in idx]
            # v14.2 标签必须唯一: 重复标签会被 _predict 的 choice 字典折叠 -> Laya 实际
            #   选项数 < len(labels), 而 1/K 仍按 len(labels) 算, 且同一份概率被复用到
            #   两个不同动作。计数并 fail-open(等价 off), 由 optimizer 侧保证唯一性。
            if len(set(labels)) != len(labels):
                self._counts["label_dups"] += 1
                if (self.max_label_dups
                        and self._counts["label_dups"] >= self.max_label_dups):
                    self._trip(f"label_dups>={self.max_label_dups}")
                return False
            key = (_state_bucket(f, camp), tuple(labels))
            now = time.time()
            hit = None
            with self._cache_lock:
                h = self._cache.get(key)
                if h is not None and now - h[0] <= self.cache_ttl:
                    hit = h
                    self._counts["cache_hits"] += 1
            if hit is None and self.stale_probs_ttl > 0.0:
                # 同一组候选标签 + 未超时 -> 用上一拍的分布(滞后一拍融合)
                last = self._last_probs
                if (last is not None and last[1] == tuple(labels)
                        and now - last[0] <= self.stale_probs_ttl):
                    hit = (last[0], last[2], round((now - last[0]) * 1000, 1))
                    self._counts["stale_hits"] += 1
            if hit is None:
                probs = None
                if (self.min_submit_interval > 0.0
                        and now - self._last_submit_t < self.min_submit_interval):
                    self._counts["throttled"] += 1     # 模型吞吐跟不上 -> 主动跳过本次
                    return False
                self._last_submit_t = now
                self._submit(key, f, camp, scored, idx, labels)
            else:
                # 缓存命中也建 pending(带 probs): shadow 模式下保证落盘记录带概率与延迟
                probs, lat = hit[1], hit[2]
                self._make_pending(key, f, camp, scored, idx, labels, probs,
                                   latency_ms=lat, latency_src="cache")
            if self.mode == "shadow" or probs is None or self.beta <= 0:
                return False
            bonus = {lab: self.beta * (p - 1.0 / len(labels))
                     for lab, p in zip(labels, probs)}
            keys = [scored[i][0] + bonus.get(candidate_label(scored[i][1]), 0.0)
                    for i in range(n)]
            order = sorted(range(n), key=lambda i: -keys[i])
            scored[:] = [scored[i] for i in order]
            self._counts["reorders"] += 1
            return True
        except Exception as e:
            self._err(e)
            return False

    def _make_pending(self, key, f, camp, scored, idx, labels, probs,
                      latency_ms=None, latency_src=None):
        """建 pending 影子记录(先冲掉上个未 flush 的), 返回 rec。"""
        with self._pending_lock:
            stale = self._pending
            self._pending = None
        if stale is not None:
            self._flush(stale["rec"])          # 上个决策点没等到 log_executed, 兜底落盘
        laya_top = None
        if probs:
            laya_top = labels[max(range(len(probs)), key=lambda j: probs[j])]
        rec = {"t": round(time.time(), 3), "mode": self.mode, "camp": camp,
               "state": build_state_text(f, camp), "labels": labels,
               "rule_scores": [round(float(scored[i][0]), 4) for i in idx],
               "probs": probs, "laya_top": laya_top, "chosen": None,
               "executed": None, "agree": None,
               "latency_ms": latency_ms, "latency_src": latency_src}
        for k, v in self._ctx.items():      # v14.2 session_id/frame_id/model_sha
            rec.setdefault(k, v)
        with self._pending_lock:
            self._pending = {"key": key, "rec": rec,
                             "cands": [scored[i][1] for i in idx]}
        return rec

    def _submit(self, key, f, camp, scored, idx, labels):
        try:
            rec = self._make_pending(key, f, camp, scored, idx, labels, None)
            try:
                self._q.put_nowait((key, rec["state"], labels))
                self._counts["submits"] += 1
            except queue.Full:
                pass                                # worker 忙 -> 丢弃, 下个决策点再试
        except Exception as e:
            self._err(e)

    def _run_job(self, key, state_text, labels):
        t0 = time.time()
        probs = None
        try:
            probs = self._predict(state_text, labels)
        except Exception as e:
            self._err(e)
        lat = round((time.time() - t0) * 1000, 1)
        self._lat_ms.append(lat)
        if len(self._lat_ms) > 200:
            del self._lat_ms[:-200]
        if probs:
            self._last_probs = (time.time(), tuple(labels), probs)
        with self._cache_lock:
            self._cache[key] = (time.time(), probs, lat)
            if len(self._cache) > 64:
                oldest = min(self._cache, key=lambda kk: self._cache[kk][0])
                self._cache.pop(oldest, None)
        with self._pending_lock:
            p = self._pending
            if p is not None and p["key"] == key:
                rec = p["rec"]
                rec["probs"] = probs
                # v14.2: 无论 probs 是否为 None 都落盘延迟 —— P1 门槛(p95<80ms)依赖它,
                #   原先只在 probs 非空时写, 导致探针日志 latency_ms 全为 null。
                rec["latency_ms"] = lat
                rec["latency_src"] = "predict"
                if probs:
                    rec["laya_top"] = labels[max(range(len(probs)),
                                                 key=lambda j: probs[j])]

    def log_chosen(self, action):
        """记录最终选择。v14.2: 不再立即落盘 —— 等 log_executed 补上执行层动作,
        或由下个决策点 / stop() 兜底落盘。"""
        try:
            with self._pending_lock:
                p = self._pending
            if p is None:
                return
            rec = p["rec"]
            tag = _chosen_tag(p.get("cands"), action)
            rec["chosen"] = tag
            if rec.get("probs") and rec.get("laya_top"):
                rec["agree"] = (rec["laya_top"] == tag)
        except Exception as e:
            self._err(e)

    def log_executed(self, action):
        """记录执行层实际下发的动作(经 apply_guards/节流后)。

        与 chosen 不同即说明决策被安全层改写 —— 这是 A/B 归因必需的字段。
        """
        try:
            with self._pending_lock:
                p = self._pending
            if p is None:
                return
            p["rec"]["executed"] = _reason_of(action)
            self._counts["exec_logged"] += 1
        except Exception as e:
            self._err(e)

    def _flush_pending(self):
        with self._pending_lock:
            p = self._pending
            self._pending = None
        if p is not None:
            self._flush(p["rec"])

    def _flush(self, rec):
        try:
            self.shadow_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.shadow_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._counts["shadow_logged"] += 1
            if rec.get("latency_ms") is not None:
                self._counts["latency_logged"] += 1
        except Exception as e:
            self._err(e)

    def _err(self, e):
        self._counts["errors"] += 1
        self._last_err = f"{type(e).__name__}: {e}"
        now = time.time()
        if now - self._last_err_print > 30.0:
            self._last_err_print = now
            print(f"[LayaScorer] err#{self._counts['errors']}: {self._last_err}")
        if self.max_errors and self._counts["errors"] >= self.max_errors:
            self._trip(f"errors>={self.max_errors}")

    def _trip(self, reason):
        """运行期熔断: 本局永久降级为 off(等价未接打分器), 只触发一次并告警。"""
        if self._tripped:
            return
        self._tripped = True
        self._trip_reason = reason
        print(f"[LayaScorer][熔断] {reason} -> 本局降级为 off(等价原版); "
              f"stats={self._counts}")

    def stats(self):
        s = {"mode": self.mode, "beta": self.beta,
             "model_ok": self._model_ok, "stopped": self._stopped,
             "tripped": self._tripped, "trip_reason": self._trip_reason,
             "last_error": self._last_err, **self._counts}
        # v14.3 OpenVINO 后端: 暴露"输入超 IR 上限被截断"的次数(应为 0)
        sess = getattr(self._agent, "session", None) if self._onnx else None
        if sess is not None and hasattr(sess, "truncated"):
            s["ov_truncated"] = sess.truncated
            s["ov_backend"] = getattr(sess, "device", None)
        # v14.3 延迟归因: 纯推理 vs 端到端, 差值=Python 侧 + 资源抢占等待
        inf = getattr(sess, "infer_ms", None) if sess is not None else None
        if inf:
            p50, p95, n = inf
            s["ov_infer_p50"] = round(p50, 1) if p50 is not None else None
            s["ov_infer_p95"] = round(p95, 1) if p95 is not None else None
            s["ov_infer_n"] = n
        _lat = self._lat_ms[-200:] if getattr(self, "_lat_ms", None) else []
        if _lat:
            xs = sorted(_lat)
            s["e2e_p50"] = round(xs[len(xs) // 2], 1)
            s["e2e_p95"] = round(xs[int(0.95 * (len(xs) - 1))], 1)
        return s
