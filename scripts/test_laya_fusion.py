# -*- coding: utf-8 -*-
"""laya-fusion C1/C2 离线单测: mock scorer, 不需要 laya 包 / GPU / 模拟器。

运行: venv/Scripts/python.exe -X utf8 scripts/test_laya_fusion.py
覆盖:
  T1  _grid_best 不再 NameError (v14.0 bugfix: state->camp)
  T2  solve 基线: 无 scorer 时确定性且返回 dict
  T3  ensemble: uniform 概率 = 与基线完全一致(不变式); 重排单元测试: 原始 r 保留
  T4  shadow: 落盘记录含 labels/chosen/probs; 缓存 TTL 内不重复推理
  T5  未安装 laya: 优雅降级(reorder 恒 False, 错误计数, solve 不受影响)
  T6  reorder 抛异常时 optimizer 兜底回规则排序
  T7  v14.1 幻影敌人过滤: camp_src=default / 低置信英雄框不进 e_scr(防空放技能)
  T8  v14.2 map_move 改走摇杆(点小地图真机无效): 世界 Δ -> 摇杆方向, 按时长节流, 航位推算
  T9  v14.2 m2.decide() 用 conf/camp_src 过滤幻影敌人(真实运行时链路)
  T10 v14.2 B.2 英雄阵营 = 框上方血条颜色(绿=友/红=敌)
"""
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.decision import optimizer                       # noqa: E402
from wzry.decision.laya_scorer import (LayaScorer,        # noqa: E402
                                       build_state_text, candidate_label)

_PASS = []
_FAIL = []


def check(name, cond, extra=""):
    """记录断言结果但不中断，跑完再用退出码报告。

    早期版本是 fail-fast（首条失败即 SystemExit），导致 T21b–T21f 之类
    后续用例根本没机会执行，失败面被掩盖。改为收集式：所有用例都跑，
    末尾统一打印 FAILED 并 exit(1)（CI 语义不变）。
    """
    if cond:
        _PASS.append(name)
        print(f"PASS {name}")
    else:
        _FAIL.append(name)
        print(f"FAIL {name} {extra}")


def make_state(reds=(0.70, 0.75), blues=((0.55, 0.50),), enemy_scr=((0.60, 0.52),)):
    units = ([{"cls": "enemy_hero", "screen": [float(x), float(y)]}
              for x, y in enemy_scr]
             + [{"cls": "enemy_minion", "screen": [0.62, 0.58]}])
    return {
        "minimap": {"found": True,
                    "dots": {"red": [list(reds)], "blue": [list(b) for b in blues],
                             "green": [[0.50, 0.50]]}},
        "units": units,
        "ui": {"hp": 0.9},
    }


FALLBACK = {"type": "map_move", "nx": 0.5, "ny": 0.5, "reason": "rule_follow"}


class FakeLaya(LayaScorer):
    """不经 laya 包: 直接给固定概率, 可控可断言。"""

    def __init__(self, probs_fn, **kw):
        kw.setdefault("shadow_path", Path(tempfile.mkdtemp()) / "shadow.jsonl")
        super().__init__(**kw)
        self._probs_fn = probs_fn
        self.predict_calls = []

    def _predict(self, state_text, labels):
        self.predict_calls.append((state_text, tuple(labels)))
        return self._probs_fn(labels)


def top2_bias(labels):
    """把全部概率压到规则分第 2 名的标签上(reorder 单测用)。"""
    out = [0.02] * len(labels)
    out[1] = 0.98
    return out


def main():
    st = make_state()

    # T1 bugfix: _grid_best 有红点时不再 NameError
    f = optimizer._feat(st, 0.9)
    gx, gy, gv = optimizer._grid_best(f, "blue")
    check("T1_grid_best_no_nameerror",
          all(isinstance(v, float) for v in (gx, gy, gv)) and 0.02 <= gx <= 0.98,
          f"got {gx},{gy},{gv}")

    # T2 基线: 无 scorer, 两次结果一致且是 dict
    optimizer.set_scorer(None)
    a1 = optimizer.solve(st, dict(FALLBACK), 0.9, "blue")
    a2 = optimizer.solve(st, dict(FALLBACK), 0.9, "blue")
    check("T2_baseline_deterministic", a1 == a2 and isinstance(a1, dict),
          f"{a1} vs {a2}")

    # T3a ensemble 单元: 首调只提交(异步), 结果回填后重调才按混合分重排, 且原始 r 保留
    sc = FakeLaya(top2_bias, mode="ensemble", beta=10.0)
    scored = [(float(i), (({"type": "map_move", "nx": 0.1 + 0.01 * i, "ny": 0.3,
                            "reason": f"cand{i}"}, f"cand{i}"))) for i in range(10)]
    before = [s for s, _ in scored]
    did1 = sc.reorder(scored, f, 0.9, "blue")
    check("T3a1_first_call_submits_only",
          did1 is False and [s for s, _ in scored] == before,
          f"did1={did1}")
    job = sc._q.get_nowait()
    sc._run_job(*job)                                     # 同步模拟 worker 推理完成
    did2 = sc.reorder(scored, f, 0.9, "blue")
    rank2_score = sorted(before, reverse=True)[1]         # 规则分第 2 名
    check("T3a2_reorder_applied", did2 is True and scored[0][0] == rank2_score,
          f"did2={did2} top={scored[0][0]} (expect rank2 by rule score={rank2_score})")
    check("T3b_rule_scores_preserved",
          sorted(s for s, _ in scored) == sorted(before),
          "原始分应原样保留(仅顺序变化)")

    # T3c ensemble 不变式: uniform 概率 -> 与基线完全一致
    uni = FakeLaya(lambda labs: [1.0 / len(labs)] * len(labs),
                   mode="ensemble", beta=1.0)
    optimizer.set_scorer(uni)
    a3 = optimizer.solve(st, dict(FALLBACK), 0.9, "blue")
    check("T3c_uniform_equals_baseline", a3 == a1, f"{a3} vs {a1}")

    # T4 shadow: 记录 labels/chosen/probs; 缓存 TTL 内只推理一次
    sh_path = Path(tempfile.mkdtemp()) / "shadow.jsonl"
    sh = FakeLaya(lambda labs: [1.0 / len(labs)] * len(labs),
                  mode="shadow", beta=0.0, shadow_path=sh_path)
    optimizer.set_scorer(sh)
    optimizer.solve(st, dict(FALLBACK), 0.9, "blue")      # 提交 job(无 worker, 队列滞留)
    job = sh._q.get_nowait()                              # 手动同步执行推理
    sh._run_job(*job)
    optimizer.solve(st, dict(FALLBACK), 0.9, "blue")      # 命中缓存 -> 不再提交
    recs = [json.loads(x) for x in sh_path.read_text(encoding="utf-8").splitlines() if x]
    check("T4a_shadow_record",
          len(recs) >= 1 and recs[-1]["chosen"] and recs[-1]["labels"]
          and recs[-1]["probs"] is not None,
          f"recs={recs[-1] if recs else None}")
    check("T4b_cache_no_second_predict", len(sh.predict_calls) == 1,
          f"predict_calls={len(sh.predict_calls)}")
    check("T4c_state_text_compact",
          len(build_state_text(f)) < 2000 and candidate_label((FALLBACK, "rule_follow")) == "rule_follow")

    # T5 laya 已安装时验证真实加载, 未安装时优雅降级。
    # 注: 本机 laya ONNX(int8) 会话创建实测约 22s(runs/laya/laya.int8.onnx, 442MB),
    # 故等待预算放到 90s; 未安装 laya 时通常几秒内即报错走降级分支。
    real = LayaScorer(mode="shadow", beta=0.0,
                      shadow_path=Path(tempfile.mkdtemp()) / "shadow.jsonl")
    optimizer.set_scorer(real)
    real.start()
    a5 = optimizer.solve(st, dict(FALLBACK), 0.9, "blue")
    for _ in range(1800):                                 # 等 worker 加载/报错(最多 90s)
        if real.stats()["errors"] >= 1 or real.stats()["model_ok"]:
            break
        time.sleep(0.05)
    check("T5_no_laya_degrades",
          isinstance(a5, dict) and (real.stats()["errors"] >= 1
                                    or real.stats()["model_ok"]),
          f"stats={real.stats()}")
    real.stop()

    # T6 reorder 抛异常 -> optimizer 兜底规则排序
    class Boom:
        def reorder(self, *a, **k):
            raise RuntimeError("boom")

        def log_chosen(self, *a, **k):
            raise RuntimeError("boom")

    optimizer.set_scorer(Boom())
    a6 = optimizer.solve(st, dict(FALLBACK), 0.9, "blue")
    check("T6_boom_scorer_fallback", isinstance(a6, dict), f"{a6}")

    optimizer.set_scorer(None)

    # T7 v14.1 幻影敌人过滤: camp_src="default"(颜色判据不显著被保守判敌) 或
    #    低置信的英雄框不进 e_scr -> 不再产出 skill1/skill2/attack(防空放技能)
    st_ph = make_state(enemy_scr=((0.60, 0.52),))
    for u in st_ph["units"]:
        if u["cls"] == "enemy_hero":
            u["camp_src"], u["conf"] = "default", 0.9
    f_ph = optimizer._feat(st_ph, 0.9)
    check("T7a_phantom_hero_filtered", f_ph["n_enemy"] == 0 and f_ph["e_scr"] == [],
          f"e_scr={f_ph['e_scr']}")
    st_ok = make_state(enemy_scr=((0.60, 0.52),))
    for u in st_ok["units"]:
        if u["cls"] == "enemy_hero":
            u["camp_src"], u["conf"] = "color", 0.8
    f_ok = optimizer._feat(st_ok, 0.9)
    check("T7b_trusted_hero_kept", f_ok["n_enemy"] == 1 and len(f_ok["e_scr"]) == 1, f"{f_ok}")
    check("T7c_trust_rule",
          optimizer.enemy_hero_trusted({"conf": 0.40, "camp_src": "color"}) is False
          and optimizer.enemy_hero_trusted({"camp_src": "default", "conf": 0.99}) is False
          and optimizer.enemy_hero_trusted({"conf": 0.9, "camp_src": "color"}) is True
          and optimizer.enemy_hero_trusted({}) is True)   # 非检测器来源: 兼容旧行为

    # T8 v14.1 map_move 节流: 同一目标点在窗口内不重复下发(原为每帧重发 -> 寻路被
    #    反复重置, 英雄原地打转); 目标点显著变化则立即重发
    sys.path.insert(0, str(ROOT / "scripts"))
    import m2_agent_v2                                     # noqa: E402

    class FakeEx:
        # v15.3: m2 的"持久触点"(--stick-hold) 特性会读 ex.hold_enabled；
        #   测试替身必须镜像执行器接口，否则 T8a 抛 AttributeError（与视觉层无关）。
        hold_enabled = False

        def __init__(self):
            self.taps = []
            self.moves = []

        def tap(self, x, y, source=""):
            self.taps.append((x, y))

        def move(self, theta, r=1.0, duration=300):
            self.moves.append((theta, r, duration))

    # v14.2 map_move **不再点小地图**(实测真机点小地图不移动英雄), 改为摇杆推杆:
    #     世界 Δ -> theta; 同长度推杆按 duration 节流, 且航位推算 self_est 前进
    fx, cd = FakeEx(), {}
    mv = {"type": "map_move", "nx": 0.66, "ny": 0.66, "reason": "t"}
    m2_agent_v2.apply_action(fx, dict(mv), cd)
    check("T8a_map_move_uses_stick",
          len(fx.taps) == 0 and len(fx.moves) == 1 and fx.moves[0][0] > 0,
          f"taps={fx.taps} moves={fx.moves}")
    se1 = cd["self_est"]
    m2_agent_v2.apply_action(fx, dict(mv), cd)
    check("T8b_stick_not_overlapped", len(fx.moves) == 1, f"moves={len(fx.moves)}")
    cd["_map_t"] -= 1.0                                   # 模拟上一次推杆已结束
    m2_agent_v2.apply_action(fx, dict(mv), cd)
    se2 = cd["self_est"]
    check("T8c_stick_reissued_self_advances",
          len(fx.moves) == 2 and se2[0] > se1[0] and se2[1] < se1[1],
          f"moves={len(fx.moves)} se1={se1} se2={se2}")
    # T8d v14.2 防"到点即停": 航位推算一旦估计位置到点就再也不推 -> 英雄站死不动
    #     (实测真机 180s 只发 19 次 swipe、4 次阵亡、0 击杀)。到点必须重锚继续推进。
    fx2, cd2 = FakeEx(), {}
    for _ in range(40):
        m2_agent_v2.apply_action(fx2, dict(mv), cd2)
        cd2["_map_t"] -= 1.0
    check("T8d_no_stall_at_target", len(fx2.moves) >= 35, f"moves={len(fx2.moves)}")

    # T9 v14.2 真实运行时链路: m2.decide() 直接用 unit 的 conf/camp_src 过滤敌英。
    #     (solve 的 v2.73 白名单会原样放行规则层已定的 skill/attack, 所以幻影敌人
    #      过滤若只写在 optimizer._feat 里对实机技能释放没有任何作用 —— 必须打在 decide。)
    def _mk(camp_src, conf):
        return {"t": 100.0, "screen_size": [1280.0, 720.0],
                "units": [{"cls": "enemy_hero", "screen": [0.60, 0.52, 0.10, 0.15],
                           "conf": conf, "camp_src": camp_src}],
                "minimap": {"found": False, "dots": {}}, "ui": {"hp": 0.9}, "extra": {}}

    a_ph = m2_agent_v2.decide(_mk("default", 0.90), {})
    check("T9a_phantom_no_skill",
          a_ph.get("type") not in ("skill", "attack", "combo"), f"{a_ph}")
    a_lc = m2_agent_v2.decide(_mk("color", 0.40), {})
    check("T9b_lowconf_no_skill",
          a_lc.get("type") not in ("skill", "attack", "combo"), f"{a_lc}")
    a_ok = m2_agent_v2.decide(_mk("color", 0.80), {})
    check("T9c_trusted_hero_skill",
          a_ok.get("type") == "skill" and a_ok.get("id") == 2, f"{a_ok}")

    # T10 v14.2 B.2 英雄阵营判据 = 框上方血条颜色(绿=友/红=敌), 不再用框内蓝/红占比
    import numpy as np                                          # noqa: E402
    import cv2                                                  # noqa: E402
    from wzry.vision.hok_yolov5 import HokYoloV5                 # noqa: E402
    mk = object.__new__(HokYoloV5)      # 跳过 __init__(不加载 onnx), 只测 map_class
    box = (500.0, 300.0, 600.0, 420.0)  # bh=120 -> 血条 ROI = y 285..293, x 515..585

    def _mkframe(bar_bgr):
        f = np.full((720, 1280, 3), 60, np.uint8)
        if bar_bgr is not None:
            cv2.rectangle(f, (515, 285), (585, 293), bar_bgr, -1)
        return f

    d_g = mk.map_class("hero", _mkframe((0, 255, 0)), box, 0.9)
    check("T10a_hero_green_bar_ally",
          d_g["cls"] == "ally_hero" and d_g["camp_src"] == "bar", f"{d_g}")
    d_r = mk.map_class("hero", _mkframe((0, 0, 255)), box, 0.9)
    check("T10b_hero_red_bar_enemy",
          d_r["cls"] == "enemy_hero" and d_r["camp_src"] == "bar", f"{d_r}")
    d_n = mk.map_class("hero", _mkframe(None), box, 0.9)
    check("T10c_hero_no_bar_default",
          d_n["cls"] == "enemy_hero" and d_n["camp_src"] == "default", f"{d_n}")
    # T10d 草地/植被(宽绿区但覆盖率低)不得被当成绿血条 -> 否则敌人站草丛会被误判友军
    f_g = np.full((720, 1280, 3), 60, np.uint8)
    cv2.rectangle(f_g, (515, 285), (545, 293), (0, 255, 0), -1)   # 仅覆盖 ROI 宽度 43%
    d_gr = mk.map_class("hero", f_g, box, 0.9)
    check("T10d_grass_not_ally",
          d_gr["cls"] == "enemy_hero" and d_gr["camp_src"] == "default", f"{d_gr}")
    # T10e/f/g v14.2 蓝条=队友(WZRY: 自己绿/队友蓝/敌红), 但必须"红优先"且只在交战圈内认蓝
    b_near = (500.0, 300.0, 600.0, 420.0)      # 框中心距屏幕中心 ~0.07 -> 圈内
    f_b = np.full((720, 1280, 3), 60, np.uint8)
    cv2.rectangle(f_b, (515, 285), (585, 293), (255, 0, 0), -1)          # 蓝条
    d_b = mk.map_class("hero", f_b, b_near, 0.9)
    check("T10e_blue_bar_near_ally",
          d_b["cls"] == "ally_hero" and d_b["camp_src"] == "bar"
          and d_b["bar_color"] == "blue", f"{d_b}")
    b_far = (0.0, 600.0, 100.0, 720.0)         # 框中心距屏幕中心 ~0.52 -> 圈外, 保守留 default
    f_bf = np.full((720, 1280, 3), 60, np.uint8)
    cv2.rectangle(f_bf, (15, 585), (85, 593), (255, 0, 0), -1)
    d_bf = mk.map_class("hero", f_bf, b_far, 0.9)
    check("T10f_blue_bar_far_default",
          d_bf["cls"] == "enemy_hero" and d_bf["camp_src"] == "default"
          and d_bf["bar_color"] == "blue_far", f"{d_bf}")
    f_mix = np.full((720, 1280, 3), 60, np.uint8)
    cv2.rectangle(f_mix, (515, 262), (585, 270), (255, 0, 0), -1)        # 蓝(覆盖率 1.0)
    cv2.rectangle(f_mix, (515, 285), (571, 293), (0, 0, 255), -1)        # 红(覆盖率 0.8)
    d_mix = mk.map_class("hero", f_mix, b_near, 0.9)
    check("T10g_red_priority_over_blue",
          d_mix["cls"] == "enemy_hero" and d_mix["camp_src"] == "bar"
          and d_mix["bar_color"] == "red", f"{d_mix}")

    # T11 v14.3 战斗决策(用户批准): ①不占优不接战 ②回城必须无近敌
    #     (实测 180s: 3 死 0 英雄击杀、recall_interrupted×2)
    def _st(hp, n_enemy):
        us = [{"cls": "enemy_hero", "screen": [0.60, 0.52, 0.10, 0.15],
               "conf": 0.8, "camp_src": "color"} for _ in range(n_enemy)]
        return {"t": 100.0, "screen_size": [1280.0, 720.0], "units": us,
                "minimap": {"found": False, "dots": {}}, "ui": {"hp": hp}, "extra": {}}

    a_low = m2_agent_v2.decide(_st(0.40, 1), {})
    check("T11a_low_hp_no_engage",
          a_low.get("type") == "map_move" and a_low.get("reason") == "disengage_retreat",
          f"{a_low}")
    a_many = m2_agent_v2.decide(_st(0.90, 3), {})
    check("T11b_outnumbered_no_engage",
          a_many.get("type") == "map_move"
          and a_many.get("reason") == "disengage_retreat", f"{a_many}")
    a_ok2 = m2_agent_v2.decide(_st(0.90, 1), {})
    check("T11c_advantage_engages",
          a_ok2.get("type") == "skill" and a_ok2.get("id") == 2, f"{a_ok2}")
    a_cl = m2_agent_v2.decide(_st(0.20, 1), {})
    check("T11d_recall_blocked_by_near_enemy", a_cl.get("type") != "recall", f"{a_cl}")
    a_rec = m2_agent_v2.decide(_st(0.20, 0), {})
    check("T11e_recall_when_clear", a_rec.get("type") == "recall", f"{a_rec}")

    optimizer.set_scorer(None)

    # T12 v14.2 修复: 红点全在走廊外时 _grid_best 不得崩(原先对空数组 np.min -> ValueError,
    #     异常在 gen_candidates 之前 -> 规则优化器与 Laya 同时被静默旁路)
    st_out = make_state(reds=(0.18, 0.20))
    f_out = optimizer._feat(st_out, 0.9)
    try:
        gx_o, gy_o, gv_o = optimizer._grid_best(f_out, "blue")
        t12_ok = all(isinstance(v, float) for v in (gx_o, gy_o, gv_o))
        t12_err = ""
    except Exception as e:                                # noqa: BLE001
        t12_ok, t12_err = False, f"{type(e).__name__}: {e}"
    check("T12a_grid_best_empty_corridor_no_crash", t12_ok, t12_err)
    a_out = optimizer.solve(st_out, dict(FALLBACK), 0.9, "blue")
    check("T12b_solve_survives_empty_corridor", isinstance(a_out, dict), f"{a_out}")

    # T13 v14.2 修复: 阈值闸门用重排前固定的 r_max, 不得被打分器的排序翻转。
    #     构造 gv < r_max 的状态(基线走候选分支), 再用"只反转顺序、r 全不变"的打分器:
    #     旧实现下 scored[0][0] 变成 r_min, gv >= r_min 成立 -> 闸门翻成 grid_best。
    st_g = make_state(reds=(0.50, 0.35), enemy_scr=((0.52, 0.52),))
    f_g = optimizer._feat(st_g, 0.9)
    _, _, gv_g = optimizer._grid_best(f_g, "blue")
    cands_g = optimizer.gen_candidates(f_g)
    rs_g = sorted((optimizer.evaluate(c, f_g, "blue") + 0.35 * optimizer._forecast(c, f_g)
                   for c in cands_g), reverse=True)
    check("T13a_fixture_gv_below_rmax", gv_g < rs_g[0], f"gv={gv_g} rmax={rs_g[0]}")

    class ReverseScorer:
        """只把顺序反转(不改任何规则分) -> 模拟打分器把低分候选提到首位。"""

        def reorder(self, scored, f, hp, camp):
            scored.sort(key=lambda x: x[0])
            return True

        def log_chosen(self, action):
            pass

    optimizer.set_scorer(None)
    base_g = optimizer.solve(st_g, dict(FALLBACK), 0.9, "blue")
    optimizer.set_scorer(ReverseScorer())
    rev_g = optimizer.solve(st_g, dict(FALLBACK), 0.9, "blue")
    optimizer.set_scorer(None)
    check("T13b_gate_not_order_dependent",
          base_g.get("reason") != "grid_best" and rev_g.get("reason") != "grid_best",
          f"base={base_g.get('reason')} rev={rev_g.get('reason')} gv={gv_g} rmax={rs_g[0]}")

    # T14 v14.2 标签唯一化: 屏内 2 敌原先产出两个同名 kite_retreat_pressure
    st_2e = make_state(reds=(0.70, 0.75), enemy_scr=((0.60, 0.52), (0.58, 0.55)))
    f_2e = optimizer._feat(st_2e, 0.9)
    labs_2e = [candidate_label(c) for c in optimizer.gen_candidates(f_2e)]
    check("T14a_gen_candidates_labels_unique",
          len(set(labs_2e)) == len(labs_2e),
          f"n={len(labs_2e)} uniq={len(set(labs_2e))}")

    sc_dup = FakeLaya(lambda labs: [1.0 / len(labs)] * len(labs),
                      mode="ensemble", beta=1.0)
    dup = [(1.0, ({"type": "map_move", "nx": 0.1, "ny": 0.1, "reason": "same"}, "same")),
           (0.9, ({"type": "map_move", "nx": 0.2, "ny": 0.2, "reason": "same"}, "same"))]
    did_dup = sc_dup.reorder(dup, f_2e, 0.9, "blue")
    check("T14b_dup_label_fails_open",
          did_dup is False and sc_dup.stats()["label_dups"] == 1,
          f"did={did_dup} stats={sc_dup.stats()}")
    sc_dup.stop()

    # T15 v14.2 latency_ms 两条路径都落盘(P1 门槛 p95<80ms 依赖; 原实现只在 probs 非空时写)
    p15 = Path(tempfile.mkdtemp()) / "shadow.jsonl"
    sh15 = FakeLaya(lambda labs: [1.0 / len(labs)] * len(labs),
                    mode="shadow", beta=0.0, shadow_path=p15, cache_ttl=60.0)
    optimizer.set_scorer(sh15)
    optimizer.solve(st, dict(FALLBACK), 0.9, "blue")       # 提交 -> pending#1
    sh15._run_job(*sh15._q.get_nowait())                   # 同步推理 -> 预测路径写延迟
    optimizer.solve(st, dict(FALLBACK), 0.9, "blue")       # 缓存命中 -> 落盘#1, 建 pending#2
    optimizer.solve(st, dict(FALLBACK), 0.9, "blue")       # 缓存命中 -> 落盘#2, 建 pending#3
    sh15._flush_pending()                                  # 收尾落盘#3
    recs15 = [json.loads(x) for x in p15.read_text(encoding="utf-8").splitlines() if x]
    srcs15 = [r.get("latency_src") for r in recs15]
    check("T15_latency_logged_all_paths",
          len(recs15) >= 2 and all(r.get("latency_ms") is not None for r in recs15)
          and "predict" in srcs15 and "cache" in srcs15,
          f"n={len(recs15)} srcs={srcs15} lats={[r.get('latency_ms') for r in recs15]}")
    check("T15b_latency_counter", sh15.stats()["latency_logged"] == len(recs15),
          f"{sh15.stats()}")
    optimizer.set_scorer(None)

    # T16 v14.2 缓存键含状态粗分箱: 标签集合相同但血量不同不得复用概率
    p16 = Path(tempfile.mkdtemp()) / "shadow.jsonl"
    sh16 = FakeLaya(lambda labs: [1.0 / len(labs)] * len(labs),
                    mode="shadow", beta=0.0, shadow_path=p16, cache_ttl=60.0)
    scored16 = [(float(i), ({"type": "map_move", "nx": 0.1 + 0.01 * i, "ny": 0.3,
                             "reason": f"c{i}"}, f"c{i}")) for i in range(10)]
    f_hi = dict(optimizer._feat(st, 0.9))
    f_lo = dict(f_hi)
    f_lo["hp"] = 0.2
    sh16.reorder(scored16, f_hi, 0.9, "blue")
    sh16._run_job(*sh16._q.get_nowait())
    n_before = len(sh16.predict_calls)
    sh16.reorder(scored16, f_lo, 0.2, "blue")              # 仅血量不同 -> 必须 miss
    check("T16_cache_key_includes_state",
          n_before == 1 and sh16.stats()["cache_hits"] == 0,
          f"predicts={n_before} cache_hits={sh16.stats()['cache_hits']}")
    sh16.stop()

    # T17 v14.2 shadow 补 session_id/frame_id/executed(执行层实际下发动作)
    p17 = Path(tempfile.mkdtemp()) / "shadow.jsonl"
    sh17 = FakeLaya(lambda labs: [1.0 / len(labs)] * len(labs),
                    mode="shadow", beta=0.0, shadow_path=p17, cache_ttl=60.0)
    sh17.set_context(session_id="20261015_000000_000000", frame_id=7)
    optimizer.set_scorer(sh17)
    optimizer.solve(st, dict(FALLBACK), 0.9, "blue")
    sh17.log_executed({"type": "map_move", "reason": "guard_rewrote"})
    optimizer.solve(st, dict(FALLBACK), 0.9, "blue")       # 触发落盘
    sh17._flush_pending()
    recs17 = [json.loads(x) for x in p17.read_text(encoding="utf-8").splitlines() if x]
    hit17 = [r for r in recs17 if r.get("executed") == "guard_rewrote"]
    check("T17_context_and_executed",
          len(hit17) == 1 and hit17[0].get("frame_id") == 7
          and hit17[0].get("session_id") == "20261015_000000_000000"
          and hit17[0].get("chosen") is not None,
          f"recs={recs17}")
    optimizer.set_scorer(None)
    sh17.stop()

    # T18 v14.2 本期范围硬边界: solve 不输出技能/攻击/连招/回城/恢复
    st_sk = make_state(reds=(0.70, 0.75), enemy_scr=((0.52, 0.52),))
    out_sk = optimizer.solve(st_sk, {"type": "map_move", "nx": 0.3, "ny": 0.3,
                                     "reason": "rule_move"}, 0.9, "blue")
    check("T18_solve_no_tactical_types",
          out_sk.get("type") in ("map_move", "none"), f"{out_sk}")

    # T19 v14.2 运行期熔断: 异常数 / 标签重复 超阈值 -> 本局降级 off
    sh19 = FakeLaya(lambda labs: [1.0 / len(labs)] * len(labs), mode="shadow",
                    beta=0.0, shadow_path=Path(tempfile.mkdtemp()) / "s.jsonl",
                    max_errors=2)

    def _boom(_s, _l):
        raise RuntimeError("boom")

    sh19._predict = _boom
    did19 = []
    for it in range(3):
        # 每轮用不同标签, 避免命中缓存(失败的预测也会被缓存 0.3s -> 不重复报错)
        cs = [(float(j), ({"type": "map_move", "nx": 0.1 + 0.01 * j, "ny": 0.3,
                           "reason": f"d{it}_{j}"}, f"d{it}_{j}")) for j in range(6)]
        did19.append(sh19.reorder(cs, f, 0.9, "blue"))
        try:
            sh19._run_job(*sh19._q.get_nowait())
        except Exception:                                   # noqa: BLE001
            pass
    st19 = sh19.stats()
    check("T19a_breaker_trips_on_errors",
          st19["tripped"] and st19["errors"] >= 2 and did19[-1] is False
          and "errors" in st19["trip_reason"],
          f"did={did19} stats={st19}")
    sh19.stop()

    sh19b = FakeLaya(lambda labs: [1.0 / len(labs)] * len(labs), mode="ensemble",
                     beta=1.0, shadow_path=Path(tempfile.mkdtemp()) / "s.jsonl",
                     max_label_dups=2)
    dup19 = [(1.0, ({"type": "map_move", "nx": 0.1, "ny": 0.1, "reason": "same"}, "same")),
             (0.9, ({"type": "map_move", "nx": 0.2, "ny": 0.2, "reason": "same"}, "same"))]
    d19 = [sh19b.reorder(dup19, f, 0.9, "blue") for _ in range(3)]
    st19b = sh19b.stats()
    check("T19b_breaker_trips_on_label_dups",
          st19b["tripped"] and st19b["label_dups"] == 2 and d19[-1] is False
          and "label_dups" in st19b["trip_reason"],
          f"d={d19} stats={st19b}")
    sh19b.stop()

    # T20 v14.2 提交节流: min_submit_interval 内不重复提交(模型吞吐 < 决策频率时降频)
    sh20 = FakeLaya(lambda labs: [1.0 / len(labs)] * len(labs), mode="shadow",
                    beta=0.0, shadow_path=Path(tempfile.mkdtemp()) / "s.jsonl",
                    min_submit_interval=30.0)
    cs_a = [(float(j), ({"type": "map_move", "nx": 0.1 + 0.01 * j, "ny": 0.3,
                         "reason": f"a{j}"}, f"a{j}")) for j in range(6)]
    cs_b = [(float(j), ({"type": "map_move", "nx": 0.2 + 0.01 * j, "ny": 0.4,
                         "reason": f"b{j}"}, f"b{j}")) for j in range(6)]
    sh20.reorder(cs_a, f, 0.9, "blue")
    sh20.reorder(cs_b, f, 0.9, "blue")
    st20 = sh20.stats()
    check("T20_submit_throttled",
          st20["submits"] == 1 and st20["throttled"] == 1,
          f"submits={st20['submits']} throttled={st20['throttled']}")
    sh20.stop()

    # T21 v14.3 OpenVINO 后端(方案 §6.2): IR 解析 + onnxruntime 兼容接口
    # T21a 用 tempdir 造 IR 文件断言“按 max_len 的解析优先级”，不依赖仓库里是否导出了产物。
    # （旧版直接比较 runs/laya 下的真实路径字符串：产物缺失时反而返回首选候选 → 假通过；
    #   产物存在时三个候选落到同一文件 → 假失败。）
    from wzry.decision import laya_scorer as _ls
    sc320 = LayaScorer(mode="shadow", beta=0.0, backend="openvino", ov_max_len=320)
    sc512 = LayaScorer(mode="shadow", beta=0.0, backend="openvino", ov_max_len=512)
    _real_shadow_dir = _ls.SHADOW_DIR
    _ov_root = Path(tempfile.mkdtemp())
    _ls.SHADOW_DIR = _ov_root
    try:
        missing_320 = sc320._resolve_ov_ir()
        check("T21a1_ov_ir_missing_returns_preferred_candidate",
              missing_320 == _ov_root / "laya_ov" / "laya_qfreeze_320.xml"
              and not missing_320.exists(),
              f"320->{missing_320}")

        (_ov_root / "laya_ov").mkdir(parents=True, exist_ok=True)
        (_ov_root / "laya_ov" / "laya_qfreeze.xml").write_text("<net/>", encoding="utf-8")
        fb320, fb512 = sc320._resolve_ov_ir(), sc512._resolve_ov_ir()
        check("T21a2_ov_ir_falls_back_to_shared_ir",
              fb320 == fb512 == _ov_root / "laya_ov" / "laya_qfreeze.xml",
              f"320->{fb320} | 512->{fb512}")

        (_ov_root / "laya_ov" / "laya_qfreeze_320.xml").write_text("<net/>", encoding="utf-8")
        check("T21a3_ov_ir_prefers_max_len_specific_file",
              sc320._resolve_ov_ir() == _ov_root / "laya_ov" / "laya_qfreeze_320.xml"
              and sc512._resolve_ov_ir() == _ov_root / "laya_ov" / "laya_qfreeze.xml",
              f"320->{sc320._resolve_ov_ir()} | 512->{sc512._resolve_ov_ir()}")

        (_ov_root / "laya_ov512").mkdir(parents=True, exist_ok=True)
        (_ov_root / "laya_ov512" / "laya_qfreeze.xml").write_text("<net/>", encoding="utf-8")
        check("T21a4_ov_ir_prefers_dedicated_dir_over_shared",
              sc512._resolve_ov_ir() == _ov_root / "laya_ov512" / "laya_qfreeze.xml",
              f"512->{sc512._resolve_ov_ir()}")
    finally:
        _ls.SHADOW_DIR = _real_shadow_dir

    # 后续 T21b–T21f 走真实产物；没有就 SKIP（这才是“环境缺件”，不是断言失败）
    ir320, ir512 = sc320._resolve_ov_ir(), sc512._resolve_ov_ir()
    print(f"INFO T21 real IR: 320->{ir320.exists()} | 512->{ir512.exists()}")

    try:
        from wzry.decision.laya_ov import OVSession
        _have_ov = ir320.exists()
    except Exception:                                       # noqa: BLE001
        _have_ov = False

    if not _have_ov:
        print("SKIP T21b-e (无 openvino 或 IR 不存在; 先跑 scripts/export_laya_ov.py)")
    else:
        sess = OVSession(ir_path=ir320, device="GPU", max_len=320, max_opts=16,
                         pad_token_id=0)
        feed = {"input_ids": np.zeros((1, 300), dtype=np.int64),
                "attention_mask": np.ones((1, 300), dtype=np.int64),
                "marker_pos": np.arange(1, 5, dtype=np.int64).reshape(1, 4),
                "marker_mask": np.ones((1, 4), dtype=bool)}
        out = sess.run(["logits", "act_logits"], feed)
        check("T21b_ov_session_run_shapes",
              len(out) == 2 and out[0].shape == (1, 4) and out[1].shape[0] == 1,
              f"logits={out[0].shape} act={out[1].shape}")
        check("T21c_ov_session_pads_without_truncating", sess.truncated == 0,
              f"truncated={sess.truncated}")

        # 超过 IR 上限的输入 -> 截断并计数(正常路径由 laya 按 max_len 先截断)。
        # 长度按 IR 实际静态形状取：旧版硬编码 400，在 L=512 的产线上不会触发截断。
        over_len = sess.max_len + 20
        sess.run(["logits", "act_logits"],
                 dict(feed, input_ids=np.zeros((1, over_len), dtype=np.int64),
                      attention_mask=np.ones((1, over_len), dtype=np.int64)))
        check("T21d_ov_session_truncation_counted", sess.truncated == 1,
              f"truncated={sess.truncated} max_len={sess.max_len}")

        # 选项数超编译上限 -> 明确报错(而不是静默出错)
        try:
            sess.run(["logits", "act_logits"],
                     dict(feed, marker_pos=np.arange(1, 21, dtype=np.int64).reshape(1, 20),
                          marker_mask=np.ones((1, 20), dtype=bool)))
            _over = False
        except ValueError:
            _over = True
        check("T21e_ov_session_opts_over_limit_raises", _over)

        # 多行(B>1)逐行推理, 结果按行拼接
        feed_b = dict(feed,
                      input_ids=np.zeros((2, 300), dtype=np.int64),
                      attention_mask=np.ones((2, 300), dtype=np.int64),
                      marker_pos=np.tile(np.arange(1, 5, dtype=np.int64).reshape(1, 4), (2, 1)),
                      marker_mask=np.ones((2, 4), dtype=bool))
        out_b = sess.run(["logits", "act_logits"], feed_b)
        check("T21f_ov_session_batch_rows",
              out_b[0].shape == (2, 4) and out_b[1].shape[0] == 2,
              f"logits={out_b[0].shape} act={out_b[1].shape}")

    # T22 异步旁路（G1 40ms 预算的前提）：慢模型不得阻塞决策热路径
    #   实测 GPU 单次推理 92ms，若串进临界路径会把 p95 推到 111ms（超预算 2.8 倍）。
    #   这里用 200ms 的假模型放大特征：reorder 必须立即返回，结果走缓存下一拍生效。
    class SlowLaya(FakeLaya):
        def _predict(self, state_text, labels):
            time.sleep(0.20)
            return [1.0 / len(labels)] * len(labels)

    slow = SlowLaya(lambda labs: [1.0 / len(labs)] * len(labs),
                    mode="ensemble", beta=1.0)
    slow.start()
    try:
        scored_s = [(float(i), ({"type": "map_move", "nx": 0.1 + 0.01 * i,
                                 "ny": 0.3, "reason": f"c{i}"}, f"c{i}"))
                    for i in range(10)]
        t0 = time.perf_counter()
        for _ in range(20):
            slow.reorder(scored_s, f, 0.9, "blue")
        dt = (time.perf_counter() - t0) * 1000
        check("T22_slow_model_does_not_block",
              dt < 100.0, f"20 次 reorder 耗时 {dt:.1f}ms（模型单次 200ms）")
        time.sleep(0.30)                                  # 让 worker 至少跑完一次
        st_slow = slow.stats()
        check("T22b_slow_model_async_stats",
              st_slow.get("submits", 0) >= 1 and st_slow.get("errors", 0) == 0,
              f"{st_slow}")
    finally:
        slow.stop()

    if _FAIL:
        print(f"\nFAILED ({len(_FAIL)}/{len(_PASS) + len(_FAIL)}): " + ", ".join(_FAIL))
        raise SystemExit(1)
    print(f"ALL PASS ({len(_PASS)})")


if __name__ == "__main__":
    main()
