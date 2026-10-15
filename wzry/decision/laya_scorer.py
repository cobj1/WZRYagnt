# -*- coding: utf-8 -*-
"""v14.0 可插拔决策打分器 (laya-fusion-plan C1)。

Scorer 协议(optimizer 侧只调这三个, 鸭子类型):
  reorder(scored, f, hp, camp) -> bool   原地排序 scored=[(rule_score, (action, tag))];
                                         True=已按混合分排序; False=调用方按规则分排序
  log_chosen(action) -> None             optimizer.solve 返回前回调, 记录最终选择
  stats() -> dict / stop() -> None

LayaScorer 设计约束(laya-fusion-plan.md §3):
  - 异步线程推理: 主循环只做 缓存查表 + 非阻塞入队, 永不等待
  - top-16 按规则分粗排 + 短标签; 结果按标签元组缓存 <=300ms
  - ensemble 只改排序分 s = r + beta*(p - 1/K); 阈值比较仍用原始 r(optimizer 侧保证)
  - 模型懒加载: 未安装 laya / 加载失败 -> 永远返回 False(等价 off), 异常计数
  - shadow 落盘 runs/laya/shadow.jsonl
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


class LayaScorer:
    def __init__(self, mode="shadow", beta=0.0, model_path=None,
                 top_k=TOP_K, cache_ttl=CACHE_TTL, shadow_path=None):
        assert mode in ("off", "shadow", "ensemble")
        self.mode = mode
        self.beta = float(beta)
        self.model_path = model_path
        self.top_k = int(top_k)
        self.cache_ttl = float(cache_ttl)
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
                        "reorders": 0, "shadow_logged": 0, "predicts": 0}
        self._last_err = ""
        self._last_err_print = 0.0

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
            self._agent = _laya.load(
                str(self.model_path) if self.model_path
                else "convaiinnovations/laya",
                backend="onnx", onnx_path=str(onnx_path))
            self._onnx = True
        else:
            if self.model_path:
                self._router = _laya.Router(
                    models={"english": str(self.model_path)}, default="english")
            else:
                self._router = _laya.Router()
            self._onnx = False
        self._model_ok = True

    # ---- 推理(可被子类/测试覆盖) ----
    def _predict(self, state_text, labels):
        if not self._model_ok:
            raise RuntimeError("laya model not loaded")
        questions = {"action": {"type": "choice", "instructions": _INSTR,
                                "criteria": {lab: lab for lab in labels}}}
        if self._onnx:
            res = self._agent.system_one(state_text, questions)
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
        if self.mode == "off" or self._stopped:
            return False
        try:
            n = len(scored)
            if n == 0:
                return False
            k = min(self.top_k, n)
            idx = sorted(range(n), key=lambda i: -scored[i][0])[:k]
            labels = [candidate_label(scored[i][1]) for i in idx]
            key = tuple(labels)
            now = time.time()
            probs = None
            with self._cache_lock:
                hit = self._cache.get(key)
                if hit is not None and now - hit[0] <= self.cache_ttl:
                    probs = hit[1]
                    self._counts["cache_hits"] += 1
            if probs is None:
                self._submit(key, f, camp, scored, idx, labels)
            else:
                # 缓存命中也建 pending(带 probs): shadow 模式下保证落盘记录带概率
                self._make_pending(key, f, camp, scored, idx, labels, probs)
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

    def _make_pending(self, key, f, camp, scored, idx, labels, probs):
        """建 pending 影子记录(先冲掉上个未 flush 的), 返回 rec。"""
        with self._pending_lock:
            stale = self._pending
            self._pending = None
        if stale is not None:
            self._flush(stale["rec"])          # 上个决策点没等到 log_chosen, 兜底落盘
        laya_top = None
        if probs:
            laya_top = labels[max(range(len(probs)), key=lambda j: probs[j])]
        rec = {"t": round(time.time(), 3), "mode": self.mode, "camp": camp,
               "state": build_state_text(f, camp), "labels": labels,
               "rule_scores": [round(float(scored[i][0]), 4) for i in idx],
               "probs": probs, "laya_top": laya_top, "chosen": None,
               "agree": None, "latency_ms": None}
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
        with self._cache_lock:
            self._cache[key] = (time.time(), probs)
            if len(self._cache) > 64:
                oldest = min(self._cache, key=lambda kk: self._cache[kk][0])
                self._cache.pop(oldest, None)
        with self._pending_lock:
            p = self._pending
            if p is not None and p["key"] == key:
                p["rec"]["probs"] = probs
                if probs:
                    p["rec"]["laya_top"] = labels[max(range(len(probs)),
                                                     key=lambda j: probs[j])]
                    p["rec"]["latency_ms"] = round((time.time() - t0) * 1000, 1)

    def log_chosen(self, action):
        try:
            with self._pending_lock:
                p = self._pending
                self._pending = None
            if p is None:
                return
            rec = p["rec"]
            tag = None
            for a, tg in (p.get("cands") or []):
                if a is action:
                    tag = candidate_label((a, tg))
                    break
            if tag is None:
                tag = str((action or {}).get("reason")
                          or (action or {}).get("type") or "fallback")[:24].lower()
            rec["chosen"] = tag
            if rec.get("probs") and rec.get("laya_top"):
                rec["agree"] = (rec["laya_top"] == tag)
            self._flush(rec)
        except Exception as e:
            self._err(e)

    def _flush(self, rec):
        try:
            self.shadow_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.shadow_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._counts["shadow_logged"] += 1
        except Exception as e:
            self._err(e)

    def _err(self, e):
        self._counts["errors"] += 1
        self._last_err = f"{type(e).__name__}: {e}"
        now = time.time()
        if now - self._last_err_print > 30.0:
            self._last_err_print = now
            print(f"[LayaScorer] err#{self._counts['errors']}: {self._last_err}")

    def stats(self):
        return {"mode": self.mode, "beta": self.beta,
                "model_ok": self._model_ok, "stopped": self._stopped,
                "last_error": self._last_err, **self._counts}
