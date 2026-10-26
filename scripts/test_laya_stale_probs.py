# -*- coding: utf-8 -*-
"""Laya 融合 v14.4：滞后一拍概率复用（stale_probs_ttl）单测。

背景（实测）
    异步 worker 的推理结果要到**下一次**决策点才可用；而 `cache_ttl=0.30s` 小于
    2Hz 的决策间隔（0.5s），且状态 key 每帧唯一 →
    `probs` 恒为 None → ensemble 模式**一次都不会重排**（300 帧实测 reorders=0）。
    `stale_probs_ttl > 0` 时，把同一组候选标签的上一拍分布复用给本拍（限时），
    融合才真正生效。默认 0 = 关闭，保持旧行为。

运行：venv/Scripts/python.exe -X utf8 scripts/test_laya_stale_probs.py
"""
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.decision import optimizer                       # noqa: E402
from wzry.decision.laya_scorer import LayaScorer          # noqa: E402

_PASS, _FAIL = [], []


def check(name, cond, extra=""):
    if cond:
        _PASS.append(name)
        print(f"PASS {name}")
    else:
        _FAIL.append(name)
        print(f"FAIL {name} {extra}")


def make_state(hp=0.9):
    units = [{"cls": "enemy_hero", "screen": [0.60, 0.52]},
             {"cls": "enemy_minion", "screen": [0.62, 0.58]}]
    return {
        "minimap": {"found": True,
                    "dots": {"red": [[0.70, 0.75]], "blue": [[0.55, 0.50]],
                             "green": [[0.50, 0.50]]}},
        "units": units,
        "ui": {"hp": hp},
    }


class FakeLaya(LayaScorer):
    def __init__(self, probs_fn, **kw):
        kw.setdefault("shadow_path", Path(tempfile.mkdtemp()) / "shadow.jsonl")
        super().__init__(**kw)
        self._probs_fn = probs_fn
        self.calls = 0

    def _predict(self, state_text, labels):
        # `predicts += 1` 原本写在 LayaScorer._predict 里；本 Fake 把它整体替掉后
        #   计数器就不再自增，导致测试里的 `wait_predict()` 空等满超时。
        #   而 `stale_probs_ttl` 按墙钟计时，空等 3s 就会正好把窗口跨过去 ——
        #   T2 在 run_tests 里失败、单跑又时过时不过，就是这个边界造成的。
        self._counts["predicts"] += 1
        self.calls += 1
        return self._probs_fn(labels)


def bias_second(labels):
    out = [0.02] * len(labels)
    out[1] = 0.98
    return out


def make_scored(n=6):
    return [(float(i), ({"type": "map_move", "nx": 0.1 + 0.01 * i, "ny": 0.3,
                         "reason": f"cand{i}"}, f"cand{i}")) for i in range(n)]


def wait_predict(sc, at_least=1, timeout=3.0):
    t0 = time.time()
    while sc.stats().get("predicts", 0) < at_least and time.time() - t0 < timeout:
        time.sleep(0.05)


def wait_model(sc, timeout=120.0):
    """等 worker 把模型加载完。

    本套测试用 `FakeLaya._predict` 替掉了推理，但 `start()` 仍会真的加载 laya 模型
    （首次要拉 4 个文件、~5s 起步，冷缓存更久）。原先只等 3s —— 模型未就绪时
    `_submit` 排进去的活没人消费，表现为 `submits=1 / predicts=0 / model_ok=False`，
    在 `run_tests.py` 里连跑多个套件时必现。这里先等 model_ok 再发任务。
    """
    t0 = time.time()
    while not sc.stats().get("model_ok") and time.time() - t0 < timeout:
        time.sleep(0.2)
    ok = bool(sc.stats().get("model_ok"))
    if not ok:
        print(f"[warn] 模型 {timeout:.0f}s 内未就绪：{sc.stats().get('last_error') or 'unknown'}")
    return ok


st_hi = make_state(0.9)
st_lo = make_state(0.4)          # 换 hp -> 状态桶变化 -> 缓存 key 不同
f_hi = optimizer._feat(st_hi, 0.9)
f_lo = optimizer._feat(st_lo, 0.4)

# ---- T1 默认关闭：跨 key 的第二拍不会重排 ----
sc1 = FakeLaya(bias_second, mode="ensemble", beta=10.0, cache_ttl=0.30)
sc1.start()
wait_predict(sc1, 0, 0.1)
scored1 = make_scored()
did_a = sc1.reorder(scored1, f_hi, 0.9, "blue")
wait_predict(sc1, 1)
time.sleep(0.05)
did_b = sc1.reorder(make_scored(), f_lo, 0.4, "blue")
check("T1_default_off_no_stale_reorder",
      did_a is False and did_b is False and sc1.stats()["stale_hits"] == 0,
      f"a={did_a} b={did_b} stats={sc1.stats()}")
sc1.stop()

# ---- T2 打开后：第二拍用上一拍分布重排 ----
sc2 = FakeLaya(bias_second, mode="ensemble", beta=10.0, cache_ttl=0.30,
               stale_probs_ttl=3.0)
sc2.start()
wait_model(sc2)
wait_predict(sc2, 0, 0.1)
scored2 = make_scored()
d1 = sc2.reorder(scored2, f_hi, 0.9, "blue")
wait_predict(sc2, 1)
time.sleep(0.05)
scored2b = make_scored()
top_before = scored2b[0][1][1]
d2 = sc2.reorder(scored2b, f_lo, 0.4, "blue")
st2 = sc2.stats()
check("T2_stale_probs_enable_reorder",
      d1 is False and d2 is True and st2["stale_hits"] >= 1 and st2["reorders"] >= 1,
      f"d1={d1} d2={d2} stats={st2}")
check("T3_reorder_changes_top_to_laya_pref",
      scored2b[0][1][1] != top_before,
      f"top_before={top_before} top_after={scored2b[0][1][1]}")
check("T4_no_duplicate_labels_after_reorder",
      len({c[1][1] for c in scored2b}) == len(scored2b))
sc2.stop()

# ---- T5 超时后不再复用 ----
sc3 = FakeLaya(bias_second, mode="ensemble", beta=10.0, cache_ttl=0.30,
               stale_probs_ttl=0.2)
sc3.start()
wait_model(sc3)
wait_predict(sc3, 0, 0.1)
sc3.reorder(make_scored(), f_hi, 0.9, "blue")
wait_predict(sc3, 1)
time.sleep(0.45)                  # 超过 stale_probs_ttl
d3 = sc3.reorder(make_scored(), f_lo, 0.4, "blue")
check("T5_stale_expires", d3 is False and sc3.stats()["stale_hits"] == 0,
      f"d3={d3} stats={sc3.stats()}")
sc3.stop()

# ---- T6 标签集合不同 -> 不复用（防止跨动作空间套用概率） ----
sc4 = FakeLaya(bias_second, mode="ensemble", beta=10.0, cache_ttl=0.30,
               stale_probs_ttl=3.0)
sc4.start()
wait_model(sc4)
wait_predict(sc4, 0, 0.1)
sc4.reorder(make_scored(6), f_hi, 0.9, "blue")
wait_predict(sc4, 1)
time.sleep(0.05)
d4 = sc4.reorder(make_scored(4), f_lo, 0.4, "blue")     # 4 个候选 != 6 个
check("T6_different_label_set_not_reused", d4 is False and sc4.stats()["stale_hits"] == 0,
      f"d4={d4} stats={sc4.stats()}")
sc4.stop()

print()
if _FAIL:
    print(f"FAILED ({len(_FAIL)}/{len(_PASS) + len(_FAIL)}): " + ", ".join(_FAIL))
    raise SystemExit(1)
print(f"ALL PASS ({len(_PASS)})")
