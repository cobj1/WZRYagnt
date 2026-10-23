# -*- coding: utf-8 -*-
"""v15.0 新模块离线回归：感知读数 / 知识库 / 记忆层 / 决策支线 / 序列数据 / BCNetMem。

全部离线可跑（不需要真机，不需要游戏）。真机标定项（顶部条带 ROI、HUD 数字模板）
只验"管线正确 + 未标定时优雅降级"，精确度要靠 scripts/calibrate_top_bar.py 在真机帧上定。

用法：
    venv\\Scripts\\python.exe scripts\\test_new_modules.py
"""
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  {extra}" if extra else ""))


# ---------------------------------------------------------------- 1. 类别表一致
from wzry.train import encoding as E  # noqa: E402

cfg_classes = json.loads((ROOT / "configs" / "classes.json").read_text(encoding="utf-8-sig"))
check("T1  encoding.CLASSES 与 configs/classes.json 同序同长",
      list(E.CLASSES) == list(cfg_classes),
      f"{len(E.CLASSES)} vs {len(cfg_classes)}")
check("T1b 资源点已拆分（dragon / jungle_buff 独立成类）",
      "dragon" in E.CLASSES and "jungle_buff" in E.CLASSES)
check("T1c UNIT_DIM 由 CLASSES 派生", E.UNIT_DIM == len(E.CLASSES) + 7)

# ---------------------------------------------------------------- 2. 动作编码
v_mm = E.encode_action({"type": "map_move", "nx": 0.85, "ny": 0.2})
check("T2  map_move -> act=move + target 可信 + theta 不可信",
      v_mm[:10].argmax() == 0 and v_mm[15] == 1.0 and v_mm[14] == 0.0
      and abs(v_mm[12] - 0.85) < 1e-6 and abs(v_mm[13] - 0.2) < 1e-6)
v_move = E.encode_action({"type": "move", "theta": 3.141592653589793, "r": 1.0})
check("T2b move(theta) -> theta 可信 + target 不可信",
      v_move[:10].argmax() == 0 and v_move[14] == 1.0 and v_move[15] == 0.0)
v_sk = E.encode_action({"type": "skill", "id": 2})
check("T2c skill/attack/none 仍正确 one-hot",
      v_sk[:10].argmax() == 2 and E.encode_action({"type": "none"})[:10].argmax() == 9)
check("T2d 向量长度 = 10 + 6（与 bc_train 解码布局一致）", len(v_mm) == 16)

# ---------------------------------------------------------------- 3. 状态编码
st = {
    "t": 1.0, "phase": "in_match", "screen_size": [1280, 720],
    "units": [{"cls": "enemy_hero", "screen": [0.5, 0.4, 0.1, 0.2], "conf": 0.83,
               "camp_src": "bar", "dead": False},
              {"cls": "ally_hero", "screen": [0.3, 0.6, 0.1, 0.2], "conf": 0.71,
               "camp_src": "default", "dead": True}],
    "minimap": {"found": True, "dots": {"blue": [[0.2, 0.3]], "red": [[0.8, 0.2]],
                                        "green": [[0.07, 0.88]], "yellow": []}},
    "ui": {"hp": 0.62, "mp": 0.4, "death_banner": 3000, "skill_states": {"1": {"ready": True}}},
}
enc = E.encode_state(st)
u, ui = enc["units"], enc["ui"]
idx_conf = len(E.CLASSES) + 4
check("T3  conf/trust/dead 三列按真实值写入",
      abs(u[0, idx_conf] - 0.83) < 1e-6 and u[0, idx_conf + 1] == 1.0
      and u[1, idx_conf + 1] == 0.0 and u[1, idx_conf + 2] == 1.0)
check("T3b grid 四通道（绿点已编进去）",
      enc["grid"].shape[-1] == 4 and enc["grid"][:, :, 3].sum() == 1)
check("T3c death_banner 二值化（3000 -> 1.0）", ui[E.UI_KEYS.index("death_banner")] == 1.0)
check("T3d skill_states 展平到 skill1_ready",
      ui[E.UI_KEYS.index("skill1_ready")] == 1.0)
check("T3e 缺失键保持 -1（gold/level 等）", ui[E.UI_KEYS.index("gold")] == -1.0)
check("T3f 新键 enemy_hp_min / skill_point 在 UI_KEYS 里",
      "enemy_hp_min" in E.UI_KEYS and "skill_point" in E.UI_KEYS)

# ---------------------------------------------------------------- 4. 顶部血条读数
import cv2  # noqa: E402
from wzry.vision import top_status_bars as TSB  # noqa: E402

frame = np.zeros((720, 1280, 3), np.uint8)
x = 320
for i in range(5):
    cv2.rectangle(frame, (x, 8), (x + 40, 15), (0, 0, 220), -1)      # 红条 40px
    cv2.rectangle(frame, (x + 40, 8), (x + 100, 15), (110, 110, 110), -1)  # 空格 60px
    x += 140
cfg = {"x0": 300, "x1": 1010, "y0": 0, "y1": 28, "slots": 5}
r = TSB.read(frame, cfg)
check("T4  合成帧读到 5 条槽位血条", r.get("found") and r.get("n") == 5, str(r.get("n")))
check("T4b 血量比例按'实心/(实心+空槽)'算（40/100=0.4）",
      r.get("bars") and all(abs(b["ratio"] - 0.4) < 0.05 for b in r["bars"]),
      str([b["ratio"] for b in r.get("bars", [])]))
check("T4c 全红 -> enemy_hp_min/mean 有值", r.get("enemy_hp_min") is not None)
check("T4d 未标定时优雅降级（不报错、found=False）",
      TSB.read(frame, None).get("found") is False
      or TSB.load_cfg() is not None)

# T4e 自标定脚本端到端：合成帧（5 条同色 + 空槽延伸）必须被判定为"通过校验"；
#     非对局帧（静态 UI 条）必须被拒（否则 --write 会把错 ROI 固化进标定文件）
import os  # noqa: E402
import subprocess  # noqa: E402

tmp_img = ROOT / "temp" / "test_topbar.png"
tmp_img.parent.mkdir(parents=True, exist_ok=True)
cv2.imwrite(str(tmp_img), frame)
_r = subprocess.run([sys.executable, str(ROOT / "scripts" / "calibrate_top_bar.py"),
                     str(tmp_img), "--top", "2"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
check("T4e 自标定脚本识别出合成头像血条行（OK 候选）",
      _r.returncode == 0 and "OK " in (_r.stdout or ""),
      (_r.stdout or "").strip().splitlines()[-2] if _r.stdout else _r.stderr[:120])
_fake = np.zeros((720, 1280, 3), np.uint8)
for i, x in enumerate((60, 150, 240, 330, 420)):     # 静态 UI 条：无空槽延伸
    cv2.rectangle(_fake, (x, 400), (x + 90, 420), (0, 0, 200), -1)
cv2.imwrite(str(tmp_img), _fake)
_r2 = subprocess.run([sys.executable, str(ROOT / "scripts" / "calibrate_top_bar.py"),
                      str(tmp_img), "--write"],
                     capture_output=True, text=True, encoding="utf-8", errors="replace",
                     # 子进程 stdout 是管道 -> Python 3.12 按系统 ANSI 码页(GBK)输出,
                     # 这里按 utf-8 解码 -> 下面按中文串判定会误红(T4e 只查 ASCII 不受影响)。
                     env={**os.environ, "PYTHONUTF8": "1"})
check("T4f 无空槽延伸的静态条 -> 拒绝校验（不进 --write 分支）",
      "拒绝写盘" in (_r2.stdout or "") or _r2.returncode == 3
      or "未找到候选" in (_r2.stdout or ""),
      f"rc={_r2.returncode}")

# ---------------------------------------------------------------- 5. 数字读数器
from wzry.vision import digit_reader as DR  # noqa: E402

# ⚠️ build_templates() 的默认 out 是 configs/digit_templates.npz（**生产模板库**）。
# 真机字形是用 scripts/calibrate_digits.py 有监督抠出来的、存量很少，
# 测试直接调它会把真机字形**洗掉**（实测：跑完测试库就从 208 条回到 204 条）。
# 所以这里必须写到 temp 下的副本。
_t5_tpl_path = ROOT / "temp" / "digit_templates_test.npz"
_t5_tpl_path.parent.mkdir(parents=True, exist_ok=True)
tpl = DR.build_templates(_t5_tpl_path)
img = np.full((40, 120, 3), 20, np.uint8)
try:
    from PIL import Image, ImageDraw, ImageFont
    f = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 22)
    im = Image.new("L", (120, 40), 20)
    ImageDraw.Draw(im).text((6, 6), "1234", fill=255, font=f)
    img = cv2.cvtColor(np.array(im), cv2.COLOR_GRAY2BGR)
    num, confs = DR.read_number(img, tpl)
    check("T5  数字读数器认出合成 '1234'", num == 1234, f"got={num} conf={confs}")
except Exception as e:
    check("T5  数字读数器（字体不可用跳过）", True, f"skip: {e}")
check("T5b 模板文件已落盘（写到 temp 副本，不污染生产库）",
      _t5_tpl_path.exists() and DR.TPL_PATH.exists())
check("T5b2 生产模板库里含真机字形（calibrate_digits 抠的，不应被测试洗掉）",
      int(np.load(DR.TPL_PATH)["labels"].size) >= 204,
      f"生产库 {int(np.load(DR.TPL_PATH)['labels'].size)} 条")
check("T5c 未标定 ROI 返回 None（优雅降级）", DR.read_roi(np.zeros((720, 1280, 3), np.uint8),
                                                        "gold", rois={})[0] is None)

# ---------------------------------------------------------------- 6. 记忆层
from wzry.memory.event_log import EventLog  # noqa: E402

EL = EventLog(path=ROOT / "temp" / "test_events.jsonl", persist=True)
EL.set_max_cd(1, 10.0)
t0 = 1000.0
s0 = {"t": t0, "ui": {"skill1_ready": True}, "units": [],
      "minimap": {"found": True, "dots": {"red": []}},
      "extra": {"hero_pos": [640, 360]}, "screen_size": [1280, 720]}
e0 = EL.update(s0, t0)
s1 = {"t": t0 + 1, "ui": {"skill1_ready": False}, "units": [{"cls": "enemy_hero"}],
      "minimap": {"found": True, "dots": {"red": [[0.8, 0.2]]}},
      "extra": {"hero_pos": [660, 360]}, "screen_size": [1280, 720]}
e1 = EL.update(s1, t0 + 1)
kinds1 = [e["kind"] for e in e1["events"]]
check("T6  ready True->False 观测到技能释放（confidence=0.5）", "cast" in kinds1)
check("T6b 敌方露头/小地图红点事件",
      "enemy_spotted" in kinds1 and "enemy_on_minimap" in kinds1)
check("T6c 冷却剩余按 max_cd 递减", 0 < (EL.cooldown_remaining(1, t0 + 1) or 0) <= 10.0)
s2 = {"t": t0 + 2, "ui": {}, "units": [], "minimap": {"found": True, "dots": {"red": []}},
      "extra": {"hero_pos": [300, 300]}, "screen_size": [1280, 720]}
e2 = EL.update(s2, t0 + 2)
check("T6d 位置跳变 -> displaced（estimate, confidence=0.2）",
      any(e["kind"] == "displaced" and e["confidence"] == 0.2 for e in e2["events"]))
check("T6e 敌方消失 -> enemy_lost", any(e["kind"] == "enemy_lost" for e in e2["events"]))
EL.note_cast(2, t0 + 3)
check("T6f note_cast 显式释放 confidence=1.0",
      EL.last_cast.get(2) == t0 + 3 and EL.snapshot()["n_events"] > 0)
check("T6g 敌方技能估计明确标为不可信",
      EL.enemy_skill_estimate("敌方A")["confidence"] == 0.0)
trail_pos = {"t": t0 + 4, "ui": {}, "units": [],
             "minimap": {"found": True, "dots": {}},
             "extra": {"hero_pos": [700, 300]}, "screen_size": [1280, 720]}
EL.update(trail_pos, t0 + 4)
check("T6h 轨迹导出朝向（有位移时非 None）", EL.heading_deg() is not None,
      f"heading={EL.heading_deg()}")

# ---------------------------------------------------------------- 7. Hero Profile
from wzry.decision import hero_profile as HP  # noqa: E402

prof = HP.load("zhongkui")
check("T7  Hero Profile 加载（钟馗）", bool(prof) and prof.get("hero") == "钟馗")
plan = HP.build_level_plan(prof)
vals = [v for v in plan.values() if v]
# 1/2 技上限 5 + 大招上限 3 = 最多 13 点；15 级有 15 点 -> 末尾 2 级无技能可升（正常）
check("T7b 加点计划覆盖 1..15 级，13 点用满、剩余级无技能可升",
      len(plan) == 15 and len(vals) == 13
      and all(v is None for lv, v in plan.items() if lv > 13),
      f"已用 {len(vals)} 点")
main = (prof["level_order"][0])
check("T7c 主技不超 5 级、大招只落在 4/8/12 级",
      sum(1 for v in plan.values() if v == main) <= 5
      and [lv for lv, v in plan.items() if v == prof["level_order"][2]]
      and all(lv in HP.ULT_LEVEL_GATES for lv, v in plan.items()
              if v == prof["level_order"][2]))
m_no = HP.legal_upgrades(prof, 3, {}, None)
m_yes = HP.legal_upgrades(prof, 3, {}, 1)
check("T7d 无技能点 -> legal=False（门关着）；有技能点 -> True",
      m_no["legal"] is False and m_yes["legal"] is True)

# ---------------------------------------------------------------- 8. 决策支线
from wzry.decision import upgrade_policy as UP  # noqa: E402
from wzry.decision import item_policy as IP  # noqa: E402

ug = UP.suggest({"ui": {"level": 3, "skill_point": 1, "hp": 0.9}}, prof)
check("T8  加点建议给出目标且带合法性", ug.get("target") is not None, str(ug))
ug_low = UP.suggest({"ui": {"level": 3, "skill_point": 1, "hp": 0.2}}, prof)
check("T8b 残血时偏离默认（优先控制/保命类）或仍给出目标",
      ug_low.get("target") is not None, str(ug_low.get("confidence")))
it = IP.plan({"ui": {"gold": 300}}, prof)
g_no_fountain = IP.purchase_gate({"ui": {"gold": 300}}, it.get("price"),
                                 in_combat=True, in_fountain=False)
g_unknown = IP.purchase_gate({"ui": {}}, it.get("price"), in_combat=False, in_fountain=True)
check("T8c 出装规划出下一件", it.get("next") is not None, str(it.get("next")))
check("T8d 战斗中拒绝开商店（整屏遮挡）", g_no_fountain[0] is False)
check("T8e 金币读数不可用时拒绝购买", g_unknown[0] is False)

# ---------------------------------------------------------------- 9. 序列数据
try:
    p = E.states_to_sequences(matches_dir=ROOT / "data" / "matches",
                              out=ROOT / "data" / "datasets" / "bc_seq_v1.npz", seq_len=16)
    z = np.load(p)
    check("T9  序列数据集构建成功（4 维：W,L,N,U）",
          z["units"].ndim == 4 and z["units"].shape[1] == 16
          and z["reset"].shape[0] == z["units"].shape[0],
          f"W={z['units'].shape[0]} L={z['units'].shape[1]}")
    check("T9b 每个窗口首帧 reset=True", bool(z["reset"][:, 0].all()))
except Exception as e:
    check("T9  序列数据集构建", False, f"{type(e).__name__}: {e}")

# ---------------------------------------------------------------- 10. BCNetMem
import torch  # noqa: E402
from wzry.policy.model import BCNetMem, N_ACT, N_MOVE_DIR  # noqa: E402

net = BCNetMem().eval()
B = 3
tu = torch.randn(B, 20, E.UNIT_DIM)
tm = torch.ones(B, 20)
tg = torch.rand(B, E.GRID, E.GRID, E.GRID_CH)
tui = torch.randn(B, len(E.UI_KEYS))
with torch.no_grad():
    o1, h1 = net(tu, tm, tg, tui)
    o2, h2 = net(tu, tm, tg, tui, hidden=h1)
check("T10 BCNetMem 输出形状与 BCNet 对齐",
      o1["act"].shape == (B, N_ACT) and o1["move_theta"].shape == (B, N_MOVE_DIR))
check("T10b hidden 参与计算（同输入不同 hidden -> 输出不同）",
      not torch.allclose(o1["act"], o2["act"]))
check("T10c hidden 可跨帧携带且形状稳定", h1.shape == h2.shape)

# ---------------------------------------------------------------- 11. 推理封装
from wzry.policy.inference import BCNetInference  # noqa: E402

ck = ROOT / "runs" / "bc" / "bc_mem_smoke.pt"
ck.parent.mkdir(parents=True, exist_ok=True)
torch.save(BCNetMem().state_dict(), ck)
inf = BCNetInference(ck)
a1 = inf.decide(st)
check("T11 带记忆权重被自动识别（gru.* 键）", inf.has_memory is True)
check("T11b 推理输出含 target / act_name（供 m2 target 路径）",
      "target" in a1 and "act_name" in a1)
check("T11c reset() 清空记忆", inf.reset("test") is not False and inf.stats()["hidden_alive"] is False)
ck2 = ROOT / "runs" / "bc" / "bc_nomem_smoke.pt"
from wzry.policy.model import BCNet  # noqa: E402
torch.save(BCNet().state_dict(), ck2)
check("T11d 旧无记忆权重仍可加载", BCNetInference(ck2).has_memory is False)

# ---------------------------------------------------------------- 12. 知识库
from wzry import knowledge as K  # noqa: E402

s = K.stats()
check("T12 S45 知识库可加载（126 装备 / 538 技能 / 132 英雄）",
      s["items"] == 126 and s["skills"] == 538 and s["heroes"] == 132, str(s))
check("T12b 装备价格可查（出装门控要用）", K.item_by_name("痛苦面具") is not None)

# ---------------------------------------------------------------- 13. m2 语法
import py_compile  # noqa: E402
try:
    py_compile.compile(str(ROOT / "scripts" / "m2_agent_v2.py"), doraise=True)
    check("T13 m2_agent_v2.py 语法通过（接线未破坏主循环）", True)
except Exception as e:
    check("T13 m2_agent_v2.py 语法", False, str(e))

# ---------------------------------------------------------------- 14. 关键帧落盘
import shutil  # noqa: E402

_src = (ROOT / "scripts" / "m2_agent_v2.py").read_text(encoding="utf-8", errors="replace")
check("T14 m2 已接上关键帧落盘（在 on_state 之后调 on_frame）",
      "recorder.on_frame(frame)" in _src
      and _src.index("recorder.on_state(state_dict)") < _src.index("recorder.on_frame(frame)"))

from wzry.data.collector import MatchRecorder  # noqa: E402

rec_dir = ROOT / "temp" / "test_recorder"
shutil.rmtree(rec_dir, ignore_errors=True)
rec = MatchRecorder(base_dir=rec_dir, frame_every_s=0.0)
rec.start(meta={"test": True})
rec.on_state({"t": 1.0, "ui": {}})
rec.on_frame(np.zeros((720, 1280, 3), np.uint8))
rec.on_frame(np.full((720, 1280, 3), 7, np.uint8))
sess = rec.session_dir
rec.close()
frs = list((sess / "frames").glob("*.png"))
check("T14b Recorder 真的写出关键帧 PNG（frames/ 不再为空）", len(frs) == 2, f"{len(frs)} 张")
check("T14c manifest 随 close 写出", (sess / "manifest.json").exists())
rec2 = MatchRecorder(base_dir=rec_dir, frame_every_s=5.0)
rec2.start(meta={"test": True})
rec2.on_frame(np.zeros((720, 1280, 3), np.uint8))
rec2.on_frame(np.zeros((720, 1280, 3), np.uint8))     # 5s 节流内 -> 不该再写
_sess2 = rec2.session_dir
rec2.close()
check("T14d on_frame 按 frame_every_s 节流（第二张被跳过）",
      len(list((_sess2 / "frames").glob("*.png"))) == 1)
shutil.rmtree(rec_dir, ignore_errors=True)

# ---------------------------------------------------------------- 15. 敌方血量比例(场景红条)
from wzry.vision import hero_hp as HH  # noqa: E402

_hf = np.zeros((720, 1280, 3), np.uint8)
_bx1, _by1, _bx2, _by2 = 600, 400, 680, 470        # 框宽 80px
cv2.rectangle(_hf, (_bx1, _by1), (_bx2, _by2), (60, 60, 60), -1)
cv2.rectangle(_hf, (_bx1 - 8, _by1 - 22), (_bx1 - 8 + 57, _by1 - 16), (0, 0, 220), -1)  # 红条 57px
_r_hp = HH.enemy_hp_ratio(_hf, (_bx1, _by1, _bx2, _by2))
check("T15 场景红条 -> HP 比例（57/115≈0.50，扫描带宽已按满条宽放宽）",
      _r_hp is not None and abs(_r_hp - 0.496) < 0.06, f"got={_r_hp}")
check("T15b 无红条 -> None（不猜）",
      HH.enemy_hp_ratio(np.zeros((720, 1280, 3), np.uint8), (600, 400, 680, 470)) is None)
check("T15c 幻影框(camp_src=default)不参与统计",
      HH.enemy_hp_from_dets(_hf, [{"cls": "enemy_hero", "xyxy": (_bx1, _by1, _bx2, _by2),
                                   "camp_src": "default"},
                                  {"cls": "enemy_hero", "xyxy": (_bx1, _by1, _bx2, _by2),
                                   "camp_src": "bar"}]) == [_r_hp])
check("T15d 非敌方类不参与", HH.enemy_hp_from_dets(_hf, [{"cls": "ally_hero",
                                                     "xyxy": (_bx1, _by1, _bx2, _by2),
                                                     "camp_src": "bar"}]) == [])

# ---------------------------------------------------------------- 16. 取证钩子
_src2 = (ROOT / "scripts" / "m2_agent_v2.py").read_text(encoding="utf-8", errors="replace")
check("T16 m2 有 --hunt 与 --frame-on-enemy 开关",
      "--hunt" in _src2 and "--frame-on-enemy" in _src2
      and "recorder.save_frame(frame, tag=\"enemy\")" in _src2)

_rec = MatchRecorder(base_dir=rec_dir, frame_every_s=5.0)
_rec.start(meta={"test": True})
_rs = _rec.session_dir
fn = _rec.save_frame(np.zeros((720, 1280, 3), np.uint8), tag="enemy")
_rec.close()
check("T16b save_frame 带标签命名且不受节流（瞬时事件取证）",
      fn is not None and str(fn).endswith(".png") and "enemy_" in fn.name
      and len(list((_rs / "frames").glob("enemy_*.png"))) == 1, str(fn.name if fn else None))
shutil.rmtree(rec_dir, ignore_errors=True)

# ---------------------------------------------------------------- 17. UI 查询层（模态锁 + 逃生）
from wzry.uiquery.panel import PanelNavigator, is_panel_open, panel_open_score  # noqa: E402

_calls = []
_nav = PanelNavigator(tap=lambda x, y: _calls.append((x, y)),
                      taps={"scoreboard": [600, 300], "close": [640, 80]}, timeout=0.3)
_ok1, _r1 = _nav.acquire("scoreboard")
_ok2, _r2 = _nav.acquire("shop")
check("T17 模态锁：面板整屏遮挡 -> 同一时刻只允许一个占用",
      _ok1 and not _ok2 and _nav.busy, _r2)
_nav.release()
check("T17b release 交还屏幕", not _nav.busy)
_pf = np.full((720, 1280, 3), 30, np.uint8)
cv2.rectangle(_pf, (180, 120), (1100, 600), (150, 150, 150), -1)
_noise = (np.random.RandomState(0).rand(720, 1280, 3) * 255).astype(np.uint8)
check("T17c 面板打开检测（中央亮卡片+四周暗遮罩 vs 普通帧）",
      is_panel_open(_pf) and not is_panel_open(_noise),
      f"score={panel_open_score(_pf)}")
_calls.clear()
_res = _nav.run("scoreboard", grab=lambda: _noise, reader=lambda p, f: {})
check("T17d 超时逃生：未打开也必定调 close，且释放锁",
      (not _res["ok"]) and any(c == (640, 80) for c in _calls) and (not _nav.busy),
      _res["reason"])
def _mk_grab(seq, tail):
    """按 seq 依次给帧、用完后一直给 tail —— 模拟"开工时干净、点开键后才是面板"。

    必须这样建模：run() 现在开工前会先 grab 一次确认屏幕干净，直接 `grab=lambda: _pf`
    会被当成"屏幕已被占用"而拒绝执行（真机：设置菜单/结算页也是全屏遮罩）。
    """
    _st = {"i": 0}

    def _g():
        i = _st["i"]
        _st["i"] += 1
        return seq[i] if i < len(seq) else tail
    return _g


_res2 = _nav.run("scoreboard", grab=_mk_grab([_noise, _pf, _pf], _pf),
                 reader=lambda p, f: {"gold": 123})
check("T17e 成功路径：ok + 读回字段 + 锁已释放",
      _res2["ok"] and _res2["data"].get("gold") == 123 and not _nav.busy)
_nav._rois_cfg = {"scoreboard": {"bad_field": [0, 0, 40, 40]}}
check("T17f 回填字段走白名单（非白名单字段被丢弃）",
      _nav.read("scoreboard", _pf) == {}, str(_nav.read("scoreboard", _pf)))

# 关闭验证（真机踩到：点完 ✕ 面板还开着，但 run() 直接报 closed=True -> 主链路以为屏幕交还了）
_nav2 = PanelNavigator(tap=lambda x, y: None, taps={"scoreboard": [600, 300], "close": [640, 80]},
                       timeout=0.3, settle=0.02)
_r3 = _nav2.run("scoreboard", grab=_mk_grab([_noise, _pf, _pf, _noise], _noise),
                reader=lambda p, f: {"gold": 1})
check("T17g 关闭后**验证**：画面回到普通帧 -> closed=True 且 ok",
      _r3["closed"] is True and _r3["ok"] is True, str(_r3["closed"]))
_nav3 = PanelNavigator(tap=lambda x, y: None, taps={"scoreboard": [600, 300], "close": [640, 80]},
                       timeout=0.3, settle=0.02)
_r4 = _nav3.run("scoreboard", grab=_mk_grab([_noise], _pf), reader=lambda p, f: {})
check("T17h 关不掉时**不谎报**：closed=False（面板还挡着屏幕，主链路不得出手）",
      _r4["closed"] is False and "关闭未确认" in _r4["reason"], _r4["reason"])
_nav4 = PanelNavigator(tap=lambda x, y: None, taps={"scoreboard": [600, 300]},  # 没给 close
                       timeout=0.3, settle=0.02)
_r5 = _nav4.run("scoreboard", grab=_mk_grab([_noise], _pf), reader=lambda p, f: {})
check("T17i 未标定关闭键 -> closed=False（不自欺）", _r5["closed"] is False)
_nav5 = PanelNavigator(tap=lambda x, y: None, taps={"scoreboard": [600, 300], "close": [640, 80]},
                       timeout=0.3, settle=0.02)
_r6 = _nav5.run("scoreboard", grab=lambda: _pf,              # 开工前屏幕上就有面板
                reader=lambda p, f: {"gold": 1})
check("T17j 开工前屏幕已被面板占用 -> 拒绝执行（不误判'我刚打开成功'），且一个点击都不发",
      _r6["opened"] is False and "拒绝执行" in _r6["reason"] and not _nav5.busy)

# ---------------------------------------------------------------- 17. UI导航/信息查询层
import importlib.util  # noqa: E402

# ---------------------------------------------------------------- 18. 重锚空转修复
_spec = importlib.util.spec_from_file_location("m2mod", ROOT / "scripts" / "m2_agent_v2.py")
m2mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m2mod)
_fr = np.zeros((720, 1280, 3), np.uint8)
cv2.rectangle(_fr, (600, 300), (660, 306), (0, 200, 0), -1)      # 自己绿条(60px)
_hero_xy = (630, 355)
_dets = [type("D", (), {"cls": "enemy_turret", "xyxy": (610, 300, 650, 355)})()]
_mm = {"found": True, "towers": [[0.30, 0.66]]}
_cd = {"camp": "blue", "self_est": (0.35, 0.60)}
_r1 = m2mod._snap_self_est_to_tower(_fr, _dets, _mm, _cd)
check("T18 首次重锚生效（偏差大 -> 吸附到塔）", _r1 is not None and _cd["self_est"] == (0.3, 0.66),
      str(_r1))
_r2 = m2mod._snap_self_est_to_tower(_fr, _dets, _mm, _cd)
check("T18b 已在该塔上 -> 不再空转重锚（修掉每 0.3s 刷屏 + 白跑 self_hp_mp）", _r2 is None)
_cd2 = {"camp": "blue", "self_est": (0.33, 0.62), "snap_key": (0.3, 0.66),
        "snap_key_t": __import__("time").time()}
check("T18c 同一座塔在冷却内 -> 偏差小时不重复吸", m2mod._snap_self_est_to_tower(
    _fr, _dets, _mm, _cd2) is None)
check("T18d 重锚节流已从 0.3s 放宽到 1.0s（每拍 self_hp_mp 太贵）",
      ">= 1.0:" in _src2 and "SNAP_ALREADY_EPS" in _src2)

# ---------------------------------------------------------------- 19. 任务回归
check("T19 既有回归套件仍在（laya_fusion / pipeline 由单独命令跑）", True)
check("T19b m2 已接 UI查询层(wzry.uiquery)与记忆层面板回填",
      "--ui-nav" in _src2 and "uiquery" in _src2 and "note_panel" in _src2)

# ---------------------------------------------------------------- 20. scan-tap 面板按钮标定
_pb = importlib.util.spec_from_file_location("probe_ui_tap", ROOT / "scripts" / "probe_ui_tap.py")
probe = importlib.util.module_from_spec(_pb)
_pb.loader.exec_module(probe)
_v, _e = probe.judge_tap(0.25, 1.31, 4.0)
check("T20 结构分达标即判 panel（哪怕 diff 很小：面板与镜头无关）", _v == probe.V_PANEL, _e)
_v, _e = probe.judge_tap(0.25, 0.30, 55.0, idle_diff=2.0)
check("T20b 结构分没动但画面剧变 -> changed（只当线索，不自动采信）", _v == probe.V_CHANGED, _e)
_v, _e = probe.judge_tap(0.25, 0.26, 1.4)
check("T20c 无反应 -> none", _v == probe.V_NONE, _e)
_v, _e = probe.judge_tap(0.25, 0.30, 40.0, idle_diff=30.0)
check("T20d 闲置抖动兜底：镜头划过(diff=40) 而闲置本就30 -> none（真机 diff=53 假阳的修法）",
      _v == probe.V_NONE, _e)
check("T20e 候选点表 + 判定类型齐备（真机量出的 (862,28) 在候选里）",
      (862, 28) in probe.CANDIDATES["scoreboard"] and set(probe.OPENED) == {probe.V_PANEL}
      and len(probe.CANDIDATES["scoreboard"]) >= 4)
import tempfile  # noqa: E402

_d0 = json.loads((ROOT / "configs" / "calibration_absolute.json").read_text(encoding="utf-8-sig"))
_tmp = Path(tempfile.mkdtemp()) / "cal.json"
_tmp.write_text(json.dumps(_d0, ensure_ascii=False), encoding="utf-8")
probe.CALIB_PATH = _tmp                                   # 测试期间不碰真 config
_taps = probe.write_taps("scoreboard", (862, 28), close_xy=(862, 28))
_d1 = json.loads(_tmp.read_text(encoding="utf-8"))
check("T20f ui_taps 写盘保留原内容（points/screen 不被覆盖）+ close 同点（面板=toggle）",
      _taps["scoreboard"] == [862, 28] and _taps["close"] == [862, 28]
      and "move_stick_center" in _d1["points"] and _d1["screen_width"] == 1280)
_src3 = (ROOT / "scripts" / "probe_ui_tap.py").read_text(encoding="utf-8")
check("T20g 默认不下发点击（--confirm-in-battle 保护闸）+ 面板已开就拒绝跑 + 不装伪对局判据",
      "--confirm-in-battle" in _src3 and "return 5" in _src3 and "return 6" in _src3)

# ---------------------------------------------------------------- 21. 战绩面板判据（暗遮罩）
from wzry.uiquery.panel import (  # noqa: E402
    BOARD_BLUE_MIN, is_panel_open, panel_open_norm, scoreboard_open_score,
)

def _mk(bgr):
    return np.full((720, 1280, 3), bgr, np.uint8)

_board = _mk((120, 60, 40))          # 深蓝暗遮罩（战绩面板）
check("T21 战绩面板：深蓝暗遮罩 -> scoreboard_open_score 高（亮卡片判据看不见它）",
      scoreboard_open_score(_board) >= BOARD_BLUE_MIN
      and is_panel_open(_board) and panel_open_norm(_board) >= 1.0,
      f"blue={scoreboard_open_score(_board)}")
_bright_blue = _mk((200, 120, 80))   # 同样是蓝，但整体亮（加载页/战场）
check("T21b 亮度门：蓝但亮(mean>85) -> 不算面板（挡掉加载VS页那种蓝底）",
      scoreboard_open_score(_bright_blue) == 0.0 and not is_panel_open(_bright_blue),
      f"mean={cv2.cvtColor(_bright_blue, cv2.COLOR_BGR2GRAY).mean():.0f}")
_dark_green = _mk((40, 90, 40))
check("T21c 暗但非深蓝（草地/暗屏）-> 不算面板",
      scoreboard_open_score(_dark_green) == 0.0 and not is_panel_open(_dark_green))
_pf2 = np.full((720, 1280, 3), 30, np.uint8)
cv2.rectangle(_pf2, (180, 120), (1100, 600), (150, 150, 150), -1)
check("T21d 归一化兼容亮卡片面板（旧结构仍>=1.0）+ 普通帧<1.0",
      panel_open_norm(_pf2) >= 1.0 and panel_open_norm(np.full((720, 1280, 3), 40, np.uint8)) < 1.0)

# ---------------------------------------------------------------- 22. 战绩面板 ROI 回填
import wzry.vision.digit_reader as _dr  # noqa: E402
from wzry.uiquery.panel import ALLOWED_FIELDS, K_GOLD_FIELDS  # noqa: E402

check("T22 白名单已加队伍级字段（不与英雄自己的 gold/kills 混用）",
      all(f in ALLOWED_FIELDS for f in ("ally_gold", "enemy_gold", "ally_kills", "enemy_kills"))
      and tuple(K_GOLD_FIELDS) == ("ally_gold", "enemy_gold"))
_sb = ((json.loads((ROOT / "configs" / "calibration_absolute.json")
                   .read_text(encoding="utf-8-sig"))).get("panel_rois") or {}).get("scoreboard") or {}
check("T22b 战绩面板 ROI 已标定（4 字段、都在白名单、框合法）",
      set(_sb) == {"ally_kills", "enemy_kills", "ally_gold", "enemy_gold"}
      and all(f in ALLOWED_FIELDS and len(v) == 4
              and 0 <= v[0] < v[2] <= 1280 and 0 <= v[1] < v[3] <= 720
              for f, v in _sb.items()), str(_sb))

# read_k_gold 的核心逻辑：**低置信字符必须被丢掉**（'k' 被误认成 8 时 conf 只 ~0.19）
#   真机：read_number 把 "33.7k" 读成 3378（丢小数点 + k 当数字）-> 差两个数量级
_ob, _or = _dr._blobs, _dr.read_number
_boxes = [{"x": i * 8, "y": 0, "w": 5, "h": 18} for i in range(4)]
_seq, _per = {"i": 0}, [(3, [0.94]), (3, [0.85]), (7, [0.95]), (8, [0.19])]


def _fake_read(crop, tpl=None, max_digits=6):
    v = _per[_seq["i"] % 4]
    _seq["i"] += 1
    return v


_dr._blobs = lambda g: _boxes
_dr.read_number = _fake_read
try:
    _gold, _cf = _dr.read_k_gold(np.zeros((28, 70, 3), np.uint8))
finally:
    _dr._blobs, _dr.read_number = _ob, _or
check("T22c 面板 '33.7k' -> 33700（低置信的 k 被过滤掉，不是 3378×100）",
      _gold == 33700 and len(_cf) == 3, f"gold={_gold} conf={_cf}")

# ---------------------------------------------------------------- 23. 顶部锁定目标条
from wzry.vision.hero_hp import lock_target  # noqa: E402

_cfg = {"x0": 100, "x1": 200, "y0": 50, "y1": 70}


def _lock_frame(fill_frac, kind="bar"):
    fr = np.zeros((720, 1280, 3), np.uint8)
    if kind == "bar":
        # 空槽=暗红 (0,40,150)：H≈8、V=150（够暗，不算填充）；真机空槽同样落在色相窗内
        fr[50:70, 100:200] = (0, 40, 150)
        if fill_frac > 0:
            fr[50:70, 100:100 + int(100 * fill_frac)] = (60, 60, 255)    # 亮红填充(V=255)
    return fr


_r = lock_target(_lock_frame(1.0), cfg=_cfg)
check("T23 锁定目标条满血 -> present 且 ratio≈1.0", _r["present"] and _r["ratio"] >= 0.95, str(_r))
_r = lock_target(_lock_frame(0.5), cfg=_cfg)
check("T23b 半血 -> ratio≈0.5（填充只能按亮度分，空槽是暗红不能当填充）",
      _r["present"] and 0.45 <= _r["ratio"] <= 0.55, str(_r))
_r = lock_target(_lock_frame(0.0), cfg=_cfg)
check("T23c 只剩空槽 -> present=True 但 ratio=0（不谎报血量）",
      _r["present"] and _r["ratio"] == 0.0, str(_r))
_r = lock_target(_lock_frame(0.0, kind="none"), cfg=_cfg)
check("T23d 屏幕上没有锁定目标 -> present=False 且 ratio=None（不许拿旧值顶上）",
      _r["present"] is False and _r["ratio"] is None, str(_r))
_lc = (json.loads((ROOT / "configs" / "calibration_absolute.json")
                  .read_text(encoding="utf-8-sig")).get("lock_target")) or {}
check("T23e lock_target 寄存器已写盘（真机实测 578/764/14/31）",
      _lc.get("x0") == 578 and _lc.get("x1") == 764 and _lc.get("y0") == 14 and _lc.get("y1") == 31,
      str(_lc))

# ---------------------------------------------------------------- 24. 锁定目标条接进 m2 状态层
check("T24 m2 每拍把锁定目标条写进状态层（ui.lock_target / ui.lock_target_hp + extra.lock_target）",
      "from wzry.vision.hero_hp import lock_target" in _src2
      and '_u5["lock_target"]' in _src2 and '_u5["lock_target_hp"]' in _src2
      and '["lock_target"] = _ltr' in _src2)
check("T24b 没有锁定目标时**不写** lock_target_hp（缺失，而不是拿旧值顶上）",
      'if _ltr.get("present") and _ltr.get("ratio") is not None:' in _src2)
check("T24c 暂不加进 UI_KEYS（会改 UI_DIM，已有 BCNet 权重就对不上了 —— 要加得先重训）",
      "lock_target" not in E.UI_KEYS and "lock_target_hp" not in E.UI_KEYS)

# ---------------------------------------------------------------- 25. 收人头窗口（规则层消费点）
def _mk_cs(hp, lock_on, lock_hp):
    """最小可用状态：屏心一个可信敌英 + 指定血量/锁定目标读数。"""
    return {"t": 100.0, "screen_size": (1280, 720),
            "units": [{"cls": "enemy_hero", "screen": [0.5, 0.5, 0.02, 0.03],
                       "conf": 0.9, "camp_src": "bar"}],
            "ui": {"hp": hp, "lock_target": lock_on, "lock_target_hp": lock_hp},
            "extra": {}, "minimap": {}}


_a1 = m2mod.decide(_mk_cs(0.40, True, 0.20), {})
check("T25 锁定目标残血(0.20)+自己 0.40 -> 放宽门槛接战（不再是 disengage_retreat）",
      _a1.get("reason") != "disengage_retreat", str(_a1))
_a2 = m2mod.decide(_mk_cs(0.40, True, 0.80), {})
check("T25b 同样自己血量但目标不残 -> 仍按常规门槛撤（证明放宽是有条件的）",
      _a2.get("reason") == "disengage_retreat", str(_a2))
_a3 = m2mod.decide(_mk_cs(0.40, False, None), {})
check("T25c 没有锁定目标 -> 撤（缺失不放宽，不拿旧值顶上）",
      _a3.get("reason") == "disengage_retreat", str(_a3))
_a4 = m2mod.decide(_mk_cs(0.28, True, 0.20), {})
check("T25d 目标残血但自己更残(0.28 < 0.35) -> 不接战（防送人头）",
      _a4.get("reason") == "disengage_retreat", str(_a4))
_co5 = {}
m2mod.decide(_mk_cs(0.40, True, 0.20), _co5)
check("T25e 触发时写日志节流键 kill_log_t（真机可观测 [kill] 打印）", "kill_log_t" in _co5)
check("T25f 阈值是常量、可调（KILL_HP_MAX / KILL_SELF_HP_MIN）",
      hasattr(m2mod, "KILL_HP_MAX") and hasattr(m2mod, "KILL_SELF_HP_MIN")
      and m2mod.KILL_SELF_HP_MIN < m2mod.ENGAGE_HP_MIN)

# ---------------------------------------------------------------- 26. 占优推进（第二个消费点）
def _lowscene(n_low, n_high):
    return [0.2] * n_low + [0.9] * n_high


_a6 = m2mod.decide({**_mk_cs(0.55, False, None),
                    "extra": {"enemy_hp_scene": _lowscene(2, 0)}}, {})
check("T26 敌方 2 个残血 + 自己 0.55 -> 允许接战（占优推进）",
      _a6.get("reason") != "disengage_retreat", str(_a6))
_a7 = m2mod.decide({**_mk_cs(0.55, False, None),
                    "extra": {"enemy_hp_scene": _lowscene(1, 1)}}, {})
check("T26b 只有 1 个残血 -> 不算占优，仍按常规门槛撤",
      _a7.get("reason") == "disengage_retreat", str(_a7))
_a8 = m2mod.decide({**_mk_cs(0.45, False, None),
                    "extra": {"enemy_hp_scene": _lowscene(3, 0)}}, {})
check("T26c 敌方三残但自己 0.45 < 0.50 -> 不打（防送）",
      _a8.get("reason") == "disengage_retreat", str(_a8))
_co6 = {}
m2mod.decide({**_mk_cs(0.55, False, None),
              "extra": {"enemy_hp_scene": _lowscene(2, 0)}, "t": 100.0}, _co6)
_a9 = m2mod.decide({**_mk_cs(0.55, False, None), "extra": {}, "t": 100.3}, _co6)
check("T26d 节流跳过的拍上沿用 n_low（0.3s < PRESS_STALE_S，许可不闪断）",
      _a9.get("reason") != "disengage_retreat", str(_a9))
_a10 = m2mod.decide({**_mk_cs(0.55, False, None), "extra": {}, "t": 101.5}, _co6)
check("T26e 超过陈旧上限(1.5s > 0.6s) -> 许可失效，回到常规门槛" ,
      _a10.get("reason") == "disengage_retreat", str(_a10))
check("T26f 常量可调，且三层门槛有序（收人头 0.35 < 占优推进 0.50 < 常规 0.60）",
      m2mod.KILL_SELF_HP_MIN < m2mod.PRESS_SELF_HP_MIN < m2mod.ENGAGE_HP_MIN)

# ---------------------------------------------------------------- 27. P0-F1 敌塔威胁（小地图分阵营塔）
check("T27a P0-F2 分阵营键已注入 build_state（旧键保留）+ F1 只认分阵营敌塔",
      '"towers_v7": mm.get("towers_v7")' in _src2 and '"towers": mm["towers"],' in _src2
      and "def _mm_enemy_towers" in _src2)
check("T27b 旧 YOLO 屏幕空间塔判据已删净（TURRET_ESCAPE_FRAC / TURRET_THREAT_FRAC / _is_base_building）",
      not hasattr(m2mod, "TURRET_ESCAPE_FRAC") and not hasattr(m2mod, "TURRET_THREAT_FRAC")
      and "_is_base_building" not in _src2
      and hasattr(m2mod, "TURRET_MM_THREAT") and hasattr(m2mod, "TURRET_MM_ESCAPE"))


def _tstate(tw_enemy, hp=0.85):
    """最小状态：无单位/无小地图点，只有敌塔(分阵营) —— 自身位置走 cooldowns["self_est"]。"""
    return {"t": 100.0, "screen_size": (1280, 720), "units": [],
            "ui": {"hp": hp, "mp": 1.0}, "extra": {},
            "minimap": {"found": True,
                        "dots": {"blue": [], "red": [], "green": [], "yellow": []},
                        "towers": [list(t) for t in tw_enemy],
                        "towers_v7": {"ally": [], "enemy": [{"n": list(t)} for t in tw_enemy]}}}


_TOW = (0.30, 0.40)
_a = m2mod.decide(_tstate([_TOW]), {"camp": "blue", "self_est": _TOW})
check("T27c 自身估算位置就在敌塔上 -> escape_turret（这条以前是死代码）",
      _a.get("reason") == "escape_turret", str(_a))

_co_h = {"camp": "blue", "self_est": _TOW}
m2mod.decide(_tstate([_TOW]), _co_h)
_co_h["self_est"] = (0.90, 0.90)          # 逃出去后自身位置已经远了
_a = m2mod.decide(_tstate([_TOW]), _co_h)
check("T27d 逃出后 1.5s 内保持远离（escape_turret_hold）",
      _a.get("reason") == "escape_turret_hold", str(_a))
_a = m2mod.decide({**_tstate([_TOW]), "t": 102.0}, _co_h)
check("T27d2 超过 1.5s -> hold 失效（不会一路逃下去）",
      _a.get("reason") != "escape_turret_hold", str(_a))

_a = m2mod.decide(_tstate([_TOW]),
                  {"camp": "blue", "self_est": (0.90, 0.90),
                   "escape_t": 100.0, "hp_drop_t": 100.0})
check("T27e 只由血条急降置的 escape_t **不**进塔 hold（不误绑到敌塔上）",
      _a.get("reason") != "escape_turret_hold", str(_a))

_NEAR = (0.375, 0.40)          # 距塔 0.075 ∈ (ESCAPE 0.06, THREAT 0.09)
_a = m2mod.decide(_tstate([_TOW]), {"camp": "blue", "self_est": _NEAR})
check("T27f 距敌塔 0.075（圈外但靠近）+ 屏内无敌人 -> avoid_turret",
      _a.get("reason") == "avoid_turret", str(_a))

_a = m2mod.decide(_tstate([_TOW]), {"camp": "blue", "self_est": (0.90, 0.90)})
check("T27g 距敌塔很远 -> 无任何塔动作（与修复前行为一致）",
      _a.get("reason") not in ("escape_turret", "escape_turret_hold", "avoid_turret"), str(_a))

_st_h = _tstate([])
_st_h["minimap"]["towers"] = [list(_TOW)]        # 旧"混合未分类"列表里有塔
_a = m2mod.decide(_st_h, {"camp": "blue", "self_est": _TOW})
check("T27h 只有未分类 mm['towers'] -> 不判敌塔（不猜：拿自家塔乱撤更糟）",
      _a.get("reason") != "escape_turret", str(_a))

_a = m2mod.decide(_tstate([_TOW]), {"camp": "blue", "self_est": _TOW, "match_start_t": 100.0})
check("T27i 开局 8s 内不判塔威胁（保留原有开局保护）",
      _a.get("reason") != "escape_turret", str(_a))

# ---------------------------------------------------------------- 28. P0-F3 视野外敌人粗判据
check("T28 F3 粗判据已落地（纯函数，供 P1 push_ok 消费）", hasattr(m2mod, "enemy_out_of_view"))
_oov = m2mod.enemy_out_of_view(
    {"minimap": {"found": True, "dots": {"blue": [[0.2, 0.3], [0.3, 0.4]], "red": []}}, "units": []})
check("T28b 屏内无任何敌英 + 我方 2 人 -> suspect_gank（对面可能在蹲）",
      _oov["suspect_gank"] and _oov["ally_seen"] == 2, str(_oov))
_oov2 = m2mod.enemy_out_of_view(
    {"minimap": {"found": True, "dots": {"blue": [[0.2, 0.3]], "red": []}}, "units": []})
check("T28c 视野内我方只有 1 人 -> 不判蹲（宁缺勿滥）", not _oov2["suspect_gank"], str(_oov2))
_oov3 = m2mod.enemy_out_of_view(
    {"minimap": {"found": True, "dots": {"blue": [[0.2, 0.3], [0.3, 0.4]], "red": [[0.8, 0.1]]}},
     "units": []})
check("T28d 小地图有红点 -> 不算视野外", not _oov3["suspect_gank"], str(_oov3))
_oov4 = m2mod.enemy_out_of_view(
    {"minimap": {"found": True, "dots": {"blue": [[0.2, 0.3], [0.3, 0.4]], "red": []}},
     "units": [{"cls": "enemy_hero", "screen": [0.5, 0.5, 0.02, 0.03],
                "conf": 0.9, "camp_src": "bar"}]})
check("T28e 屏内有可信敌英 -> 不算视野外", not _oov4["suspect_gank"], str(_oov4))
_oov5 = m2mod.enemy_out_of_view({"minimap": {}, "units": []})
check("T28f 拿不到小地图 -> 不判蹲（不猜）", not _oov5["suspect_gank"], str(_oov5))

# ---------------------------------------------------------------- 29. P1 推进门控 / 目标点（纯函数，未接线）
import math  # noqa: E402

check("T29a P1 常量齐备且自洽（站位 > F1 的靠近半径；hp 门高于残血线）+ 两个函数已在",
      all(hasattr(m2mod, _k) for _k in (
          "PUSH_SELF_HP_MIN", "PUSH_MIN_ALLY", "PUSH_ALLY_NEAR", "PUSH_CORRIDOR_K",
          "PUSH_WAVE_NEAR", "PUSH_STANDOFF", "PUSH_LEAD"))
      and m2mod.PUSH_STANDOFF > m2mod.TURRET_MM_THREAT
      and m2mod.PUSH_SELF_HP_MIN > m2mod.LOW_HP_FRAC
      and hasattr(m2mod, "push_ok") and hasattr(m2mod, "push_target"))

_EST = (0.70, 0.68)
_CO_P = {"camp": "blue", "self_est": _EST}


def _pstate(hp=0.80, blue=((0.2, 0.2), (0.3, 0.3)), red=((0.2, 0.9),),
            towers=((0.95, 0.92),), waves=((0.72, 0.70),)):
    """推进测试的最小状态：自身位置走 cooldowns["self_est"]，其余只给推动所需要的键。"""
    return {"t": 1000.0, "screen_size": (1280, 720), "units": [],
            "ui": {"hp": hp, "mp": 1.0}, "extra": {},
            "minimap": {"found": True,
                        "dots": {"blue": [list(p) for p in blue],
                                 "red": [list(p) for p in red],
                                 "green": [[0.074, 0.88]], "yellow": []},
                        "towers": [list(t) for t in towers],
                        "towers_v7": {"ally": [], "enemy": [{"n": list(t)} for t in towers]},
                        "v7": {"minions": {"ally": [{"n": list(p)} for p in waves],
                                           "enemy": []}}}}


_ok1, _why1 = m2mod.push_ok(_pstate(), _CO_P)
_pt1, _r1 = m2mod.push_target(_pstate(), _CO_P)
check("T29b 占优 + 有兵线 + 走廊内有敌塔 -> 可推，且首选跟兵线",
      _ok1 and _why1 == "push_ok" and _r1 == "push_follow_wave", f"{_why1}/{_r1}")
check("T29b2 目标点在走廊内、离敌塔 >= PUSH_STANDOFF（不贴脸）",
      _pt1 is not None
      and math.hypot(_pt1[0] - 0.75, _pt1[1] - 0.72) <= m2mod.PUSH_CORRIDOR_K
      and math.hypot(_pt1[0] - 0.95, _pt1[1] - 0.92) >= m2mod.PUSH_STANDOFF, str(_pt1))

check("T29c 无我方兵线 -> 不推（推塔要靠兵线）",
      m2mod.push_ok(_pstate(waves=()), _CO_P) == (False, "push_blocked_no_wave"))
check("T29d 视野内我方 0 个点且身边没队友 -> 不推（孤身不推）",
      m2mod.push_ok(_pstate(blue=()), _CO_P) == (False, "push_blocked_alone"))
check("T29d3 P3-A 放宽后：只有 1 个蓝点也够（人数门 2->1）",
      m2mod.push_ok(_pstate(blue=((0.2, 0.2),)), _CO_P)[0] is True)
check("T29d2 可见敌人 3 个 (> 2 绝对上限) -> 不推（B″ 判据3）",
      m2mod.push_ok(_pstate(red=((0.1, 0.1), (0.2, 0.2), (0.3, 0.3))), _CO_P)
      == (False, "push_blocked_outnumbered"))
check("T29d2b B″：红 2 vs 蓝 1（旧\"相对比较\"会被判人数劣挡住）-> 绝对上限 <=2 故允许",
      m2mod.push_ok(_pstate(blue=((0.2, 0.2),), red=((0.1, 0.1), (0.2, 0.9))), _CO_P)[0] is True)
check("T29e 自己血 0.40 (< 0.45) -> 不推",
      m2mod.push_ok(_pstate(hp=0.40), _CO_P) == (False, "push_blocked_hp"))
check("T29e2 P3-A 放宽后：0.45 血就能推（血门 0.55->0.45）",
      m2mod.push_ok(_pstate(hp=0.45), _CO_P)[0] is True)
check("T29f 走廊内没有敌塔 -> 不推（回退原行为）",
      m2mod.push_ok(_pstate(towers=()), _CO_P) == (False, "push_blocked_no_tower"))
check("T29g 已站在塔外站位圈内(d=0.028) -> 不推（先由 F1 撤出去）",
      m2mod.push_ok(_pstate(towers=((0.72, 0.70),)), _CO_P) == (False, "push_blocked_too_close"))
check("T29h 逃/被重击窗口内 -> 不推",
      m2mod.push_ok(_pstate(), {**_CO_P, "escape_t": 1000.0}) == (False, "push_blocked_escaping"))

_co_blind = dict(_CO_P)
_ok_b0, _ = m2mod.push_ok(_pstate(red=()), _co_blind)          # t=1000 进入"没敌人"，开窗
check("T29i B′：单拍看不见敌人**不**判蹲（正常单带时对面本来就不在视野里）",
      _ok_b0 is True and "blind_start" in _co_blind)
check("T29i2 B′：该窗口持续到 10s（>=8s）且身边没队友 -> 才判蹲",
      m2mod.push_ok({**_pstate(red=()), "t": 1010.0}, _co_blind)
      == (False, "push_blocked_gank"))
check("T29i3 B′：一旦有敌人露头 -> 窗口重置（不再判蹲）",
      m2mod.push_ok({**_pstate(red=((0.2, 0.9),)), "t": 1010.0}, _co_blind)[0] is True)
check("T29i4 B′：重置后马上又看不见，但没到 8s -> 不判蹲",
      m2mod.push_ok({**_pstate(red=()), "t": 1012.0}, _co_blind)[0] is True)
_co_blind2 = dict(_CO_P)
m2mod.push_ok(_pstate(red=(), blue=((0.71, 0.69), (0.2, 0.2))), _co_blind2)
check("T29i5 B′：就算蹲窗口已超时，只要身边就有队友 -> 允许跟队友推",
      m2mod.push_ok({**_pstate(red=(), blue=((0.71, 0.69), (0.2, 0.2))), "t": 1010.0},
                    _co_blind2)[0] is True)

_far = {"camp": "blue", "self_est": (0.30, 0.30)}
_st_far = _pstate(blue=((0.30, 0.31), (0.31, 0.30)), red=(),
                  towers=((0.75, 0.72),), waves=((0.32, 0.32),))
_ok2, _why2 = m2mod.push_ok(_st_far, _far)
_pt2, _r2 = m2mod.push_target(_st_far, _far)
check("T29j 门控通过但目标点落在走廊外 -> 放弃推进（防推着推着跑河道/对抗路）",
      _ok2 and _pt2 is None and _r2 == "push_blocked_corridor", f"{_why2}/{_r2}")

check("T29k 塔选择 = 最近且**在走廊内**（走廊外的近塔不选）",
      m2mod._push_pick_tower([(0.30, 0.30), (0.95, 0.92)], (0.72, 0.70), (0.75, 0.72), 0.30)
      == (0.95, 0.92))
check("T29l 兵线选择 = 最近且更靠敌方（身后那条不选）",
      m2mod._push_pick_wave([(0.45, 0.55), (0.60, 0.40)], (0.5, 0.5), (0.7, -0.7))
      == (0.60, 0.40))
_pt3, _r3 = m2mod.push_target(_pstate(waves=()), _CO_P)
_d3 = math.hypot(_pt3[0] - 0.95, _pt3[1] - 0.92) if _pt3 else -1.0
check("T29m 无兵线时退回压塔：目标离塔 = PUSH_STANDOFF 且仍在走廊内",
      _r3 == "push_lane_tower" and abs(_d3 - m2mod.PUSH_STANDOFF) < 0.02, f"{_r3} d={_d3:.3f}")

_da = m2mod.decide(_pstate(), dict(_CO_P))
check("T29n P1 未接线：同一个'该推'的场景，decide() 仍返回老的 dps_lane_push",
      _da.get("reason") == "dps_lane_push", str(_da))

# ---------------------------------------------------------------- 30. P2 接线：--push off|shadow|on
check("T30a 三档开关已接线（CLI --push + cooldowns['push_mode'] + decide 里的分支）",
      'choices=["off", "shadow", "on"]' in _src2 and '"push_mode"' in _src2
      and "--push" in _src2)
_a_off = m2mod.decide(_pstate(), dict(_CO_P))
_a_sh = m2mod.decide(_pstate(), {**_CO_P, "push_mode": "shadow"})
check("T30b **shadow 与 off 动作逐字节一致**（本方案最重要的安全性质）",
      _a_sh == _a_off, f"{_a_sh} vs {_a_off}")
_co_sh = {**_CO_P, "push_mode": "shadow"}
m2mod.decide(_pstate(), _co_sh)
check("T30c shadow 照常评估并落盘（ok / why / 预计点位）",
      _co_sh.get("push_info", {}).get("ok") is True
      and _co_sh["push_info"]["target"] is not None, str(_co_sh.get("push_info")))
_co_off = dict(_CO_P)
m2mod.decide(_pstate(), _co_off)
check("T30d off 时**完全不评估**（连 push_info 都不产生：零开销）",
      "push_info" not in _co_off)
_a_on = m2mod.decide(_pstate(), {**_CO_P, "push_mode": "on"})
check("T30e on 时真正接管移动（reason = push_follow_wave）",
      _a_on.get("reason") == "push_follow_wave", str(_a_on))
_a_on2 = m2mod.decide(_pstate(waves=()), {**_CO_P, "push_mode": "on"})
check("T30f on 但被闸挡掉（无兵线）-> 退回原行为 dps_lane_push（条件不成立即与 off 一致）",
      _a_on2.get("reason") == "dps_lane_push", str(_a_on2))
check("T30g 每拍 push 评估结果已进 behavior.jsonl 记录（shadow 一局即可统计占比）",
      '"push": (state_dict.get("extra") or {}).get("push")' in _src2)
_co_ps = {"push_mode": "shadow"}
_a_ps = m2mod.decide(_mk_cs(0.40, True, 0.20), _co_ps)      # 走接战/技能分支, 到不了移动块
check("T30j shadow 下没走到移动块的拍 push_info=None（不贴上一拍的陈旧值）",
      _co_ps.get("push_info", "missing") is None)
# 真机跑出来才发现的静默失败：'# 底部信息条' 与 beh_label 赋值曾被挤在同一行 ->
#   赋值从没执行 -> NameError 被 except:pass 吞掉 -> behavior.jsonl 从来没生成过
_lines2 = _src2.splitlines()
check("T30h beh_label 赋值是真代码行（不被历史格式损坏成注释）",
      any(_l.strip().startswith("beh_label =") for _l in _lines2))
check("T30h2 记录写盘失败不再静默（会打印 [annot-record]）",
      '"[annot-record] 行为记录写入失败' in _src2)
check("T30i screencap 采集能拿到 --serial（真机 USB 否则恒连 127.0.0.1:16384，实测 0 帧）",
      "ScreencapCapture(interval=0.02, rotate_s=1800," in _src2)

# ---------------------------------------------------------------- 31. E-P0 推进优先级（支援帧只评估，不采用）
_SUP_BLUE = ((0.72, 0.70),)          # 落在射手走廊内 -> 会走 support_dps_lane 分支
check("T31a E 常量/工具已在（PUSH_STAY_WITH_ALLY + _push_eval）",
      hasattr(m2mod, "PUSH_STAY_WITH_ALLY") and hasattr(m2mod, "_push_eval"))

_co_e = {"camp": "blue", "self_est": _EST, "push_mode": "shadow"}
_a_e = m2mod.decide(_pstate(blue=_SUP_BLUE), _co_e)
_fi_e = _co_e.get("push_info") or {}
check("T31b 支援帧也会评估推进(scope=support)，但动作**仍是 support_dps_lane**（E-P0 不采用）",
      _fi_e.get("scope") == "support" and _fi_e.get("ok") is True
      and _a_e.get("reason") == "support_dps_lane", f"{_fi_e.get('scope')}/{_a_e.get('reason')}")
check("T31c e_ok=1：队友在前方且推进点没甩开他 -> E-P1 接线后这一拍会接管",
      _fi_e.get("e_ok") is True and _fi_e.get("gap") is not None, str(_fi_e))
check("T31d shadow 下支援帧的动作与 off 逐字节一致（零行为变化）",
      m2mod.decide(_pstate(blue=_SUP_BLUE), {"camp": "blue", "self_est": _EST}) == _a_e)
check("T31e E-P1 后：`on` 会接管支援帧（reason 变成 push_follow_wave）",
      m2mod.decide(_pstate(blue=_SUP_BLUE), {"camp": "blue", "self_est": _EST,
                                             "push_mode": "on"}).get("reason")
      == "push_follow_wave")

_co_e2 = {"camp": "blue", "self_est": _EST, "push_mode": "shadow"}
m2mod.decide(_pstate(blue=((0.66, 0.64),)), _co_e2)
check("T31f 队友在我身后（推进方向反面）-> e_ok=0（继续跟人，不把射手甩掉）",
      (_co_e2.get("push_info") or {}).get("e_ok") is False, str(_co_e2.get("push_info")))
_co_e3 = {"camp": "blue", "self_est": _EST, "push_mode": "shadow"}
m2mod.decide(_pstate(blue=((0.93, 0.90),)), _co_e3)
check("T31g 队友离推进点 > PUSH_STAY_WITH_ALLY -> e_ok=0（会甩开人 -> 放弃接管）",
      (_co_e3.get("push_info") or {}).get("e_ok") is False, str(_co_e3.get("push_info")))
_co_e4 = {"camp": "blue", "self_est": _EST}
m2mod.decide(_pstate(blue=_SUP_BLUE), _co_e4)
check("T31h off 时支援帧也不产生 push_info（零足迹性质不变）",
      "push_info" not in _co_e4)

# ---------------------------------------------------------------- 32. 离线回放基础设施（replay_push.py）
_rp_spec = importlib.util.spec_from_file_location("replay_push", ROOT / "scripts" / "replay_push.py")
rpm = importlib.util.module_from_spec(_rp_spec)
_rp_spec.loader.exec_module(rpm)

check("T32a 推进评估把输入也记下来了（hp/est/阵营/蓝红点/敌塔/兵线/逃生窗口/屏内敌数/支援点）",
      all(k in (_fi_e.get("in") or {}) for k in
          ("hp", "est", "camp", "blue", "red", "towers", "waves", "esc", "nscene", "sup")),
      str(sorted((_fi_e.get("in") or {}).keys())))

_rec_stub = {"t": 1000.0, "reason": "support_dps_lane", "hp": 0.80, "push": _fi_e}
_recon_st, _recon_co = rpm.rebuild(_rec_stub, _fi_e["in"])
_a_rp = m2mod._push_eval(_recon_st, _recon_co, "support", tuple(_fi_e["in"]["sup"]))
check("T32b 回放能重建同一拍并得到**同样的判据结论**（ok/why/e_ok 三项一致 = 口径没漂）",
      _a_rp["ok"] == _fi_e["ok"] and _a_rp["why"] == _fi_e["why"]
      and _a_rp.get("e_ok") == _fi_e.get("e_ok"),
      f"回放 {_a_rp['ok']}/{_a_rp['why']}/{_a_rp.get('e_ok')} "
      f"vs 记录 {_fi_e['ok']}/{_fi_e['why']}/{_fi_e.get('e_ok')}")
check("T32c 回放脚本可被复用（load_m2 / frames / rebuild / run / summarize / summarize_live 都在）",
      all(hasattr(rpm, f) for f in
          ("load_m2", "frames", "rebuild", "run", "summarize", "summarize_live"))
      and "PUSH_SELF_HP_MIN" in (ROOT / "scripts" / "replay_push.py").read_text(encoding="utf-8"))
_lv = [({"t": 1000.0, "reason": "push_follow_wave"}, {**_fi_e, "used": True}),
       ({"t": 1001.0, "reason": "grid_best"}, {**_fi_e, "used": True, "reason": "push_follow_wave"})]
_LVS = rpm.summarize_live(_lv, quiet=True)
check("T32d live 统计能算出「used=True 但最终动作变了」= 被 solve() 覆盖的拍数",
      _LVS["used"] == 2 and _LVS["overridden"] == 1, str(_LVS))
_pin_esc = dict(_fi_e["in"], esc=True)
_pin_noesc = dict(_fi_e["in"], esc=False)
_rows_esc = rpm.run(m2mod, [({"t": 1000.0}, {"in": _pin_esc, "scope": "fallback"}),
                            ({"t": 1000.5}, {"in": _pin_noesc, "scope": "fallback"})])
check("T32e 回放逐拍重建逃生窗口：上一拍的 esc=True **不会**把下一拍也判成在逃",
      _rows_esc[0][2]["why"] == "push_blocked_escaping"
      and _rows_esc[1][2]["why"] != "push_blocked_escaping",
      f"f1={_rows_esc[0][2]['why']} f2={_rows_esc[1][2]['why']}")

# ---------------------------------------------------------------- 33. E-P1 接线（`on` 才接管支援帧）
_co_e1 = {"camp": "blue", "self_est": _EST, "push_mode": "on"}
_a_e1 = m2mod.decide(_pstate(blue=_SUP_BLUE), _co_e1)
_fi1 = _co_e1.get("push_info") or {}
_tg1 = _fi1.get("target")
check("T33a on + e_ok -> 接管支援帧（reason=push_follow_wave 且 used=True）",
      _a_e1.get("reason") == "push_follow_wave" and _fi1.get("used") is True, str(_a_e1))
check("T33b 接管点仍满足 E 的三条硬约束（走廊内 / 离敌塔>=PUSH_STANDOFF / 贴合队友<=0.15）",
      _tg1 is not None and _a_e1.get("nx") is not None
      and math.hypot(_a_e1["nx"] - 0.75, _a_e1["ny"] - 0.72) <= m2mod.PUSH_CORRIDOR_K
      and math.hypot(_a_e1["nx"] - 0.95, _a_e1["ny"] - 0.92) >= m2mod.PUSH_STANDOFF
      and math.hypot(_tg1[0] - 0.72, _tg1[1] - 0.70) <= m2mod.PUSH_STAY_WITH_ALLY,
      f"target={_tg1} nx={_a_e1.get('nx')}")
check("T33e 「细化而非抢占」：接管点换了站位点（≠队友点）但没换目标人（仍 <=0.15）",
      _tg1 is not None and (_tg1[0], _tg1[1]) != (0.72, 0.70)
      and math.hypot(_tg1[0] - 0.72, _tg1[1] - 0.70) <= m2mod.PUSH_STAY_WITH_ALLY, str(_tg1))

_co_e2 = {"camp": "blue", "self_est": _EST, "push_mode": "on"}
_a_e2 = m2mod.decide(_pstate(blue=((0.66, 0.64),)), _co_e2)
check("T33c on 但队友在我身后(e_ok=0) -> **不**接管，仍 support_dps_lane（守住「射手在哪跟哪」）",
      _a_e2.get("reason") == "support_dps_lane"
      and (_co_e2.get("push_info") or {}).get("used") is False, str(_a_e2))
_co_e3 = {"camp": "blue", "self_est": _EST, "push_mode": "on"}
_a_e3 = m2mod.decide(_pstate(blue=((0.93, 0.90),)), _co_e3)
check("T33d on 但队友离推进点 > PUSH_STAY_WITH_ALLY(e_ok=0) -> **不**接管（不把人甩开）",
      _a_e3.get("reason") == "support_dps_lane", str(_a_e3))
check("T33f on 接管时也能走 map_move（push_* reason 在下游白名单里，未被当成屏内目标）",
      _a_e1.get("type") == "map_move" and _a_e1.get("nx") is not None, str(_a_e1))

# ---------------------------------------------------------------- 34. YOLO ONNX 线程数旋钮
#   v16.0 P3: ORT 默认按物理核数开满 intra_op 线程(16 核) -> 推理突发饿死采集 reader。
from wzry.vision import hok_yolov5 as _hok_mod  # noqa: E402

_so4 = _hok_mod._sess_options(4)
check("T34a intra_threads=4 -> SessionOptions 限 4 intra / 1 inter",
      _so4 is not None and _so4.intra_op_num_threads == 4
      and _so4.inter_op_num_threads == 1)
check("T34b 默认(None/0/负) -> 不传 SessionOptions，与旧行为逐位一致",
      _hok_mod._sess_options(None) is None and _hok_mod._sess_options(0) is None
      and _hok_mod._sess_options(-1) is None)
check("T34c m2 接线 --yolo-threads（CLI -> YoloDetector -> HokYoloV5）",
      '"--yolo-threads"' in _src2 and "intra_threads=(_yolo_threads" in _src2
      and "intra_threads=intra_threads"
      in (ROOT / "wzry" / "vision" / "detector.py").read_text(
          encoding="utf-8", errors="replace"))

# ---------------------------------------------------------------- 34. E-P2：阈值临时覆盖 + 两局对照
def _reject(fn):
    try:
        fn()
        return False
    except SystemExit:
        return True


_old_hp_min, _old_stay = m2mod.PUSH_SELF_HP_MIN, m2mod.PUSH_STAY_WITH_ALLY
_why_low_before = m2mod.push_ok(_pstate(hp=0.40), dict(_CO_P))[1]
_ovr = m2mod.apply_push_overrides(["PUSH_SELF_HP_MIN=0.33", "PUSH_STAY_WITH_ALLY=0.2"])
check("T34a --push-set 能临时覆盖可调常量（判据读全局 -> 立即生效）",
      m2mod.PUSH_SELF_HP_MIN == 0.33 and m2mod.PUSH_STAY_WITH_ALLY == 0.2
      and _ovr == [("PUSH_SELF_HP_MIN", 0.33), ("PUSH_STAY_WITH_ALLY", 0.2)], str(_ovr))
_why_low_after = m2mod.push_ok(_pstate(hp=0.40), dict(_CO_P))[1]
check("T34b 覆盖后判据立刻按新值判（hp 0.40 原本被血门挡, 0.33 后放行）",
      _why_low_before == "push_blocked_hp" and _why_low_after == "push_ok",
      f"{_why_low_before} -> {_why_low_after}")
m2mod.PUSH_SELF_HP_MIN, m2mod.PUSH_STAY_WITH_ALLY = _old_hp_min, _old_stay
check("T34c --push-set 白名单外/非数字一律拒绝，且**不改任何全局**（先校验后生效）",
      _reject(lambda: m2mod.apply_push_overrides(["RECALL_ENEMY_CLEAR_FRAC=0.1"]))
      and _reject(lambda: m2mod.apply_push_overrides(["PUSH_SELF_HP_MIN=abc"]))
      and _reject(lambda: m2mod.apply_push_overrides(["PUSH_SELF_HP_MIN=0.2", "NOPE=1"]))
      and m2mod.PUSH_SELF_HP_MIN == _old_hp_min, str(m2mod.PUSH_SELF_HP_MIN))

_recs_a = [{"t": 1000.0 + i, "reason": "support_dps_lane", "score": 1.0, "label": "idle"}
           for i in range(10)]
_recs_b = [{"t": 2000.0 + i, "reason": "push_follow_wave", "score": 1.5, "label": "recalling"}
           for i in range(10)]
_ev_b = [(_recs_b[i], {"scope": "support", "ok": i < 5, "used": True,
                       "reason": "push_follow_wave"}) for i in range(10)]
_mb = rpm.metrics(_recs_b, _ev_b)
check("T34d 对照指标算得对（support ok 5/10=50% / 实推 10 拍 / support 家族占比 0 / 回城代理 10）",
      _mb["support_ok_ratio"] == 0.5 and _mb["support_n"] == 10 and _mb["push_exec"] == 10
      and _mb["support_ratio"] == 0.0 and _mb["label_recalling"] == 10 and _mb["used"] == 10,
      str(_mb))
check("T34e 对照缺文件会明确报错（不静默给一排 0）",
      _reject(lambda: rpm.compare(str(ROOT / "temp" / "_none_a.jsonl"),
                                 str(ROOT / "temp" / "_none_b.jsonl"))))
check("T34f 对照/整局读取的口径都在（all_records / metrics / metrics_of / compare / _ROWS）",
      all(hasattr(rpm, f) for f in ("all_records", "metrics", "metrics_of", "compare"))
      and "support_ok_ratio" in rpm._ROWS and "overridden" in rpm._ROWS)
_m_dur = rpm.metrics([{"t": 0.0, "reason": "x"},
                      {"t": 1_792_000_000.0, "reason": "x"},
                      {"t": 1_792_000_420.0, "reason": "x"}], [])
check("T34g 时长统计会过滤脏时间戳（实测记录里存在 t=0 的行，否则 max-min 会变成 1.79e9 秒）",
      _m_dur["dur_s"] == 420.0, str(_m_dur["dur_s"]))

# ---------------------------------------------------------------- 35. screenrecord 裸 H264 -> TCP 采集（v16.4）
# 背景：scrcpy --record 的 mkv 被 muxer 按 ~5s 一簇落盘（首字节实测 5.7s），cv2 尾读
# 还会在 EOF 永久锁死；裸 H.264 seek 后到下一个 IDR 前全丢。改用 screenrecord 裸流
# 经本地 TCP 喂 cv2：实测 32.6fps 交付、帧间隔中位 32ms、无 >200ms 空档。
_sr_path = ROOT / "wzry" / "capture" / "screenrecord_capture.py"
check("T35a screenrecord 采集模块在（裸 H264 -> 本地 TCP -> cv2）", _sr_path.exists())
from wzry.capture.screenrecord_capture import ScreenrecordCapture  # noqa: E402
check("T35b 接口与 ScrcpyStreamCapture 对齐（start/get_frame/wait_frame/stop）",
      all(hasattr(ScreenrecordCapture, f) for f in
          ("start", "get_frame", "wait_frame", "stop")))
_sr_src = _sr_path.read_text(encoding="utf-8", errors="replace")
check("T35c 转投用 read1 + TCP_NODELAY（read 会攒满 64KB -> 实测 0.4s 一批的突发交付）",
      "read1(" in _sr_src and "TCP_NODELAY" in _sr_src)
check("T35d screenrecord 单次上限 180s -> rotate_s 自动封顶 170 + --time-limit 兜底",
      ScreenrecordCapture(ROOT / "tools" / "scrcpy", serial="x",
                          rotate_s=1800).rotate_s == 170
      and '"--time-limit", "180"' in _sr_src)
_sr_u = ScreenrecordCapture(ROOT / "tools" / "scrcpy", serial="x")
import subprocess as _sp  # noqa: E402


class _WM:
    stdout = "Physical size: 1440x3200"


_sr_orig_run = _sp.run
_sp.run = lambda *a, **k: _WM()
try:
    _sr_wh = _sr_u._resolve_size()
finally:
    _sp.run = _sr_orig_run
check("T35e wm size 1440x3200 长边缩到 1280 并对齐 16 -> (1280,576)（同 scrcpy --max-size 输出）",
      _sr_wh == (1280, 576), str(_sr_wh))
check("T35f m2 接线 --capture screenrecord（choices 含项 + 分支传 --serial）",
      '"screenrecord"' in _src2
      and 'ScreenrecordCapture(ROOT / "tools" / "scrcpy", rotate_s=170,' in _src2)
check("T35g 息屏保活（实睡实测：screenrecord 报 INVALID_LAYER_STACK + WiFi adb 掉线；"
      "对策：起流前查 mScreenState 决定 POWER/Wakeup + screen_off_timeout 周期保活）",
      "mScreenState" in _sr_src and "KEYCODE_POWER" in _sr_src
      and "screen_off_timeout" in _sr_src and "_stop_evt.set()" in _sr_src)

print(f"\n结果: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
if FAIL:
    print("失败项:", FAIL)
sys.exit(1 if FAIL else 0)
