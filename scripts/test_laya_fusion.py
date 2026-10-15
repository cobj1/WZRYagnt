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


def check(name, cond, extra=""):
    if cond:
        _PASS.append(name)
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {extra}")
        raise SystemExit(1)


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

    print(f"ALL PASS ({len(_PASS)})")


if __name__ == "__main__":
    main()
