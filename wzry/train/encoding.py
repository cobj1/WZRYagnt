# -*- coding: utf-8 -*-
"""GameState -> 训练特征编码器（行为克隆数据工厂 v0）。

设计：
  - 单位编码：固定长度矩阵（n_units_max x UNIT_DIM），含类别 one-hot(11)、
    屏幕归一化坐标、尺寸、置信度；不足补零 + mask 向量。
  - 小地图栅格：40x40x3（蓝/红/黄圆点密度），对应全局视野（含视野外）。
  - UI 向量：金币/等级/血条/技能冷却等标量（缺失补 -1）。
  - 动作编码：one-hot 类型 + 连续参数（theta/r/目标点）。

输出：单帧特征 dict；states_to_dataset 把 data/matches/*/states.jsonl
批量转为 npz（供 torch Dataset 使用）。
"""
import json
from pathlib import Path

import numpy as np

from wzry.vision.camp_src import TRUSTED_CAMP_SRC   # v15.2: camp_src 口径单一事实来源

# 类别表以 configs/classes.json 为准（单一来源，避免"代码/配置两处漂移"）；
# 读不到时回退到下面同序的硬编码表。v15.0 把 dragon / jungle_buff 从
# neutral_monster 里拆出来，供记忆层做"资源点刷新/争夺"推理。
_FALLBACK_CLASSES = ["enemy_hero", "ally_hero", "enemy_minion", "ally_minion",
                     "enemy_turret", "ally_turret", "enemy_crystal", "ally_crystal",
                     "neutral_monster", "dragon", "jungle_buff", "hook_aim",
                     "skill_effect"]


def _load_classes():
    try:
        p = Path(__file__).resolve().parents[2] / "configs" / "classes.json"
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        if isinstance(d, list) and d:
            return [str(x) for x in d]
    except Exception:
        pass
    return list(_FALLBACK_CLASSES)


CLASSES = _load_classes()
CLS2ID = {c: i for i, c in enumerate(CLASSES)}

# 单位特征布局: one-hot(cls) + 屏幕(cx,cy,w,h) + conf + trust + dead
UNIT_DIM = len(CLASSES) + 4 + 3
GRID = 40
GRID_CH = 4                    # 小地图栅格通道: 蓝 / 红 / 黄 / 绿
UI_KEYS = ["hp", "mp", "gold", "level", "kills", "deaths",
           "skill1_ready", "skill2_ready", "skill3_ready", "death_banner",
           "skill_point", "enemy_hp_min", "ally_hp_min",
           "skill1_cd", "skill2_cd", "skill3_cd"]
# v15.0 新增: skill_point(技能点) / enemy_hp_min·ally_hp_min(顶部头像条血条读数,
#   见 wzry/vision/top_status_bars.py) / skillN_cd(剩余CD秒估计, 见 ui_reader)。
# death_banner 原始值是像素计数 -> 入模前必须二值化(见 encode_state)。
MOVE_R = 0.8                   # 与 m2_agent_v2.MOVE_R 对齐（推杆幅度）
# v15.2 camp_src 口径单一事实来源(wzry/vision/camp_src.py)：此处只做别名，勿另写字面量
TRUST_SRC = TRUSTED_CAMP_SRC   # camp_src 中"判据可信"的来源


def encode_state(st: dict, n_units_max: int = 20) -> dict:
    """GameState dict -> 特征。返回 {"units": (n,UNIT_DIM) float32, "unit_mask": (n,),
    "grid": (GRID,GRID,GRID_CH) float32, "ui": (len(UI_KEYS),) float32, "meta": dict}。"""
    units = np.zeros((n_units_max, UNIT_DIM), dtype=np.float32)
    mask = np.zeros((n_units_max,), dtype=np.float32)
    for i, u in enumerate(st.get("units", [])[:n_units_max]):
        cls = u.get("cls", "")
        cid = CLS2ID.get(cls, -1)
        if cid >= 0:
            units[i, cid] = 1.0
        scr = u.get("screen") or [0, 0, 0, 0]
        units[i, len(CLASSES):len(CLASSES) + 4] = scr[:4]
        # conf: build_state 已把检测器置信度写进 Unit.conf；旧数据缺失时回退 1.0
        _c = u.get("conf")
        units[i, len(CLASSES) + 4] = float(_c) if isinstance(_c, (int, float)) else 1.0
        # trust: camp_src 是否来自可信判据（bar=血条判色 / color=框内像素 / neutral）。
        # "default" 是判据不显著时的兜底当敌（幻影敌人来源）、"extra" 是草丛噪声 -> 均记 0
        units[i, len(CLASSES) + 5] = 1.0 if u.get("camp_src") in TRUST_SRC else 0.0
        units[i, len(CLASSES) + 6] = 1.0 if u.get("dead") else 0.0
        mask[i] = 1.0

    grid = np.zeros((GRID, GRID, GRID_CH), dtype=np.float32)
    mm = st.get("minimap") or {}
    if mm.get("found"):
        for c, ch in (("blue", 0), ("red", 1), ("yellow", 2), ("green", 3)):
            for (nx, ny) in mm.get("dots", {}).get(c, []):
                gx = min(GRID - 1, max(0, int(nx * GRID)))
                gy = min(GRID - 1, max(0, int(ny * GRID)))
                grid[gy, gx, ch] += 1.0

    ui = np.full((len(UI_KEYS),), -1.0, dtype=np.float32)
    ui_map = st.get("ui") or {}
    _ss = ui_map.get("skill_states") or {}
    for i, k in enumerate(UI_KEYS):
        if k in ("skill1_ready", "skill2_ready", "skill3_ready"):
            # m2 写的是嵌套 skill_states{1:{ready,unlocked,dark_frac}}，这里展平成 0/1
            sid = int(k[5])
            s = _ss.get(sid) or _ss.get(str(sid)) or {}
            r = s.get("ready")
            if isinstance(r, bool):
                ui[i] = 1.0 if r else 0.0
            elif isinstance(r, (int, float)):
                ui[i] = float(r)
            continue
        v = ui_map.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            continue
        if k == "death_banner":
            # 原始值是"暗红像素数"(self_bars.death_banner_px)：真死亡 >=2500、存活 <=1800
            # -> 二值化，否则量级噪声（实测均值 1117、85% 帧非零）直接进模型
            ui[i] = 1.0 if v >= 2500 else 0.0
        else:
            ui[i] = float(v)

    return {"units": units, "unit_mask": mask, "grid": grid, "ui": ui,
            "meta": {"t": st.get("t"), "frame_id": st.get("frame_id"),
                     "phase": st.get("phase")}}


def encode_action(act: dict, n_actions: int = 10) -> np.ndarray:
    """动作 dict -> 向量（长度 n_actions + 6）。

    布局：
      [0:n_actions)  类型 one-hot（与 BCNet.act 输出对齐 0-9）
      [n_actions+0]  theta 归一化（0=右，逆时针）
      [n_actions+1]  r（摇杆幅度）
      [n_actions+2]  target_x 归一化（小地图坐标）
      [n_actions+3]  target_y 归一化（小地图坐标）
      [n_actions+4]  theta_valid  该样本 theta 是否可信（训练时据此掩掉 theta 监督）
      [n_actions+5]  target_valid 该样本 target 是否可信

    支持的动作：
      move      {"theta":…, "r":…}   手势 / 录像反推（theta 可信，target 不可信）
      map_move  {"nx":…, "ny":…}     规则决策（**target 可信，theta 不可信**：m2 的 theta
                                     由 self_est 航位推算得出，录像里没有 self_est，
                                     不臆造方向——theta 交给执行器在真机上现算）
      skill/attack/recall/summoner/restore/none
    """
    types = ["move", "skill1", "skill2", "skill3", "attack",
             "buy", "recall", "summoner", "restore", "none"]
    n = max(n_actions, len(types))
    vec = np.zeros((n + 6,), dtype=np.float32)
    t = act.get("type", "none")
    if t == "map_move":
        vec[types.index("move")] = 1.0
        nx, ny = act.get("nx"), act.get("ny")
        if isinstance(nx, (int, float)) and isinstance(ny, (int, float)):
            vec[n + 2] = float(nx)
            vec[n + 3] = float(ny)
            vec[n + 5] = 1.0
        vec[n + 1] = MOVE_R
        return vec
    if t == "skill":
        t = f"skill{int(act.get('id', 2))}"
    if t not in types:
        t = "none"
    vec[types.index(t)] = 1.0
    if t == "move":
        vec[n] = float(act.get("theta", 0.0) or 0.0) / (2 * np.pi + 1e-6)
        vec[n + 4] = 1.0
        tx, ty = act.get("target_x"), act.get("target_y")
        if isinstance(tx, (int, float)) and isinstance(ty, (int, float)):
            vec[n + 2] = float(tx)
            vec[n + 3] = float(ty)
            vec[n + 5] = 1.0
    vec[n + 1] = float(act.get("r", 0.0) or 0.0)
    return vec


def states_to_dataset(matches_dir="data/matches", out="data/datasets/bc_v0.npz",
                      max_samples=None, n_units_max=20, stale_s=2.0):
    """扫描 data/matches/*/states.jsonl，编码全部状态为 npz 特征库。

    动作标签采用 **past-action 持续语义**：每个状态的标签 = 该时刻之前（含同时）
    最近一条动作事件（移动方向/技能按下会持续生效直到下一条事件）；
    无历史动作的样本标 none。避免"未来动作泄漏"与事件重复标注。

    stale_s：动作过期窗口。规则 Agent 的 map_move 只在推杆时落盘，停下后没有
    "撤销事件"，不加窗口会把站立帧一直误标成 move（实测同一目标点连发 100+ 条）。
    """
    matches_dir = Path(matches_dir)
    if not matches_dir.exists():
        raise FileNotFoundError(f"无对局数据: {matches_dir}（先跑 m1_live_pipeline 采集）")

    feats, acts, metas = [], [], []
    for sess in sorted(matches_dir.iterdir()):
        sf = sess / "states.jsonl"
        af = sess / "actions.jsonl"
        if not sf.exists():
            continue
        actions = []
        if af.exists():
            actions = [json.loads(l) for l in af.read_text(encoding="utf-8").splitlines()]
        actions.sort(key=lambda a: a.get("t", 0.0))
        a_idx = 0
        for line in sf.read_text(encoding="utf-8").splitlines():
            st = json.loads(line)
            enc = encode_state(st, n_units_max=n_units_max)
            t = st.get("t", 0.0)
            while a_idx < len(actions) and actions[a_idx].get("t", 0.0) <= t:
                a_idx += 1
            act = actions[a_idx - 1] if a_idx > 0 else {"type": "none"}
            if a_idx > 0 and float(t) - float(act.get("t", 0.0)) > stale_s:
                act = {"type": "none"}
            feats.append(enc)
            acts.append(encode_action(act))
            metas.append(enc["meta"])
            if max_samples and len(feats) >= max_samples:
                break
        if max_samples and len(feats) >= max_samples:
            break

    if not feats:
        raise RuntimeError("没有可编码的状态样本")

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out,
        units=np.stack([f["units"] for f in feats]),
        unit_mask=np.stack([f["unit_mask"] for f in feats]),
        grid=np.stack([f["grid"] for f in feats]),
        ui=np.stack([f["ui"] for f in feats]),
        actions=np.stack(acts),
        metas=np.array([json.dumps(m, ensure_ascii=False) for m in metas]),
    )
    print(f"数据集已写出: {out}  ({len(feats)} 样本, 单位矩阵 {feats[0]['units'].shape}, "
          f"栅格 {feats[0]['grid'].shape})")
    return out


def _session_items(sess, n_units_max=20, stale_s=2.0):
    """一个会话 -> [(enc, act_dict, reset_flag)]（保持时间顺序）。"""
    sf, af = sess / "states.jsonl", sess / "actions.jsonl"
    if not sf.exists():
        return []
    actions = []
    if af.exists():
        actions = [json.loads(l) for l in af.read_text(encoding="utf-8").splitlines()]
    actions.sort(key=lambda a: a.get("t", 0.0))
    items, a_idx, prev_phase = [], 0, None
    for line in sf.read_text(encoding="utf-8").splitlines():
        st = json.loads(line)
        enc = encode_state(st, n_units_max=n_units_max)
        t = st.get("t", 0.0)
        while a_idx < len(actions) and actions[a_idx].get("t", 0.0) <= t:
            a_idx += 1
        act = actions[a_idx - 1] if a_idx > 0 else {"type": "none"}
        if a_idx > 0 and float(t) - float(act.get("t", 0.0)) > stale_s:
            act = {"type": "none"}
        phase = st.get("phase")
        ui = st.get("ui") or {}
        dead = isinstance(ui.get("death_banner"), (int, float)) and ui["death_banner"] >= 2500
        # 记忆必须断开的位置：阶段切换 / 阵亡
        reset = bool(prev_phase is not None and phase != prev_phase) or bool(dead)
        prev_phase = phase
        items.append((enc, act, reset))
    return items


def states_to_sequences(matches_dir="data/matches", out="data/datasets/bc_seq_v1.npz",
                        seq_len=24, stride=None, n_units_max=20, stale_s=2.0):
    """按会话切片成定长序列（供 BCNetMem 的 BPTT 训练）。

    与 states_to_dataset 的区别：
      - 保留时间顺序；窗口不跨会话（每个会话单独切）
      - 额外产出 reset 标记：窗口首帧、阶段切换帧、阵亡帧 -> 训练时把这些位置
        hidden 清零（记忆在这些点必须断开，否则会跨局/跨死亡污染）
      - 动作标签口径与 states_to_dataset 完全一致（past-action + 过期窗口）

    输出：units (W,L,N,U) unit_mask (W,L,N) grid (W,L,G,Gch) ui (W,L,K)
          actions (W,L,10+6) reset (W,L)bool metas (W,)
    """
    matches_dir = Path(matches_dir)
    if not matches_dir.exists():
        raise FileNotFoundError(f"无对局数据: {matches_dir}")
    stride = stride or seq_len
    U, M, G, K = [], [], [], []
    A, R, metas = [], [], []
    for sess in sorted(matches_dir.iterdir()):
        if not sess.is_dir():
            continue
        items = _session_items(sess, n_units_max=n_units_max, stale_s=stale_s)
        n = len(items)
        if n < seq_len:
            continue
        for s in range(0, n - seq_len + 1, stride):
            win = items[s:s + seq_len]
            U.append(np.stack([w[0]["units"] for w in win]))
            M.append(np.stack([w[0]["unit_mask"] for w in win]))
            G.append(np.stack([w[0]["grid"] for w in win]))
            K.append(np.stack([w[0]["ui"] for w in win]))
            A.append(np.stack([encode_action(w[1]) for w in win]))
            r = np.array([bool(w[2]) for w in win], dtype=bool)
            r[0] = True                     # 窗口首帧必然重置
            R.append(r)
            metas.append(json.dumps({"sess": sess.name, "start": s,
                                     "t0": win[0][0]["meta"]["t"]}, ensure_ascii=False))
    if not U:
        raise RuntimeError("没有可切片的序列（对局太短或数据缺失）")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out,
                        units=np.stack(U), unit_mask=np.stack(M), grid=np.stack(G),
                        ui=np.stack(K), actions=np.stack(A), reset=np.stack(R),
                        metas=np.array(metas))
    print(f"序列数据集已写出: {out}  ({len(U)} 窗口 x {seq_len} 帧, "
          f"units {np.stack(U).shape}, grid {np.stack(G).shape})")
    return out
