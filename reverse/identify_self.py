# -*- coding: utf-8 -*-
"""识别「本人是哪个英雄槽位」。

三个判据的现状（2026-10-22，均为实测后修订）
------------------------------------------
1. `--self-cid`（显式配置）：**推荐**。生产路径本来就是 `--mem-self-id`，
   一次性配置即可，无任何不确定性。
2. **HUD 金币对照**（`source=hud_gold`）：读右上角金币数字 ↔ 内存 `gold`。
   金币在 10 个英雄间差异通常很大（实测 33 ~ 1057），且单调增长，
   与截图到读内存的时间差兼容 → 判据强。
3. **血条比例对照**（`source=bars`）：`match_self()` 保留作 fallback。
   ⚠️ 实测在 1920×1080 下 `self_hp_mp()` 连续 4 帧都检不出自己血条，
   **本环境不可用**，只能当最后手段。

**已证伪、不再使用**（记录以避免重蹈）：
- `+0x0088 / +0x008c` 作为身份标记：新局 10 槽全 0，同局几分钟后又变成
  slot5=1 → 它是状态位（相机跟随/高亮），不是身份；
- 记录 `0x0000–0x0090` 的"唯一非零布尔"：该段只有 slot0 有数据，
  其余 9 槽全 0 → `base + n*stride` 对其他槽落在上一对象尾部，**不是对象头**；
- "位移为 0 = 挂机 AI"：人挂机/看装备/等人也不动。**不动不是非本人的判据**。

用法
----
    python reverse/identify_self.py --self-cid 106      # 显式（推荐）
    python reverse/identify_self.py                     # HUD 金币自动识别
    python reverse/identify_self.py --shot frame.png    # 用已有图片离线验
    python reverse/identify_self.py --selftest

输出：`SELF_CID=106  SELF_SLOT=0  source=hud_gold  (HUD 968 vs mem 1057, 差 89)`
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision.mem_source import MemSource  # noqa: E402

# 本项目设备必须走绝对路径：裸 adb 不在 PATH，AdbCapture 会静默返回 None
ADB = r"D:\app\MuMuPlayer\nx_main\adb.exe"
# HUD 金币 ROI（归一化坐标，按 1920×1080 实测标定；其他分辨率自动缩放）
GOLD_ROI = (0.935, 0.039, 0.992, 0.080)
MIN_CONF = 0.6            # 低于此置信度的字符丢弃（实测噪声 blob 约 0.4）
MIN_MARGIN = 0.03         # 血条比例匹配的次优差下限


# ------------------------------------------------------------- 视觉取数字
def read_hud_gold(frame, tpl=None):
    """读右上角金币。返回 (gold, confs) 或 (None, [])。

    只取置信度 >= MIN_CONF 的字符：实测同一 ROI 会带进噪声 blob，
    全量拼接会读出 9968（真值 968），过滤后对得上。
    """
    from wzry.vision.digit_reader import load_templates, read_number
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = (GOLD_ROI[0] * w, GOLD_ROI[1] * h,
                      GOLD_ROI[2] * w, GOLD_ROI[3] * h)
    crop = frame[int(y1):int(y2), int(x1):int(x2)]
    if crop.size == 0:
        return None, []
    if tpl is None:
        tpl = load_templates()
    val, confs = read_number(crop, tpl, max_digits=6)
    if val is None:
        return None, []
    s = str(val)
    keep = "".join(ch for ch, c in zip(s, confs) if c >= MIN_CONF)
    return (int(keep) if keep else None), list(confs)


def gold_match(hs, hud_gold, min_gap: int = 200):
    """HUD 金币 → 内存英雄。返回 (best_h, diff, gap_to_second)。

    min_gap：第一名与第二名的金币差至少这么大才判"唯一"。
    金币是单调增长量，所以差异容忍度用"谁最接近"而不是"必须相等"。
    """
    if hud_gold is None:
        return None, None, None
    best = sorted(((abs(h["gold"] - hud_gold), h) for h in hs
                   if h.get("gold") is not None), key=lambda t: t[0])
    if not best:
        return None, None, None
    d0, h0 = best[0]
    gap = (best[1][0] - d0) if len(best) > 1 else 9e9
    return h0, d0, gap


# ------------------------------------------------------------- 血条 fallback
def match_self(hs, hp_v: float, mp_v=None, min_margin: float = MIN_MARGIN):
    """血条比例匹配（fallback，本环境不可用，见文件头说明）。"""
    best = []
    for h in hs:
        hr = h["hp"] / h["hp_max"] if h.get("hp_max") else None
        if hr is None:
            continue
        s = abs(hr - hp_v)
        er = h.get("ep_rate")
        if er is not None and mp_v is not None:
            s += 0.5 * abs(er - mp_v)
        best.append((s, h))
    if not best:
        return None, None, None
    best.sort(key=lambda x: x[0])
    s0, h0 = best[0]
    margin = (best[1][0] - s0) if len(best) > 1 else 9.9
    return h0, s0, margin


def selftest() -> int:
    ok = True
    # 1) 金币匹配：968 明显接近 1057 而非 296
    hs = [{"slot": i, "id": 100 + i, "gold": g, "hp": 3000, "hp_max": 4000}
          for i, g in enumerate([1057, 33, 295, 32, 201, 184, 187, 296, 83, 114])]
    h, d, gap = gold_match(hs, 968)
    if h is None or h["slot"] != 0 or gap < 200:
        print(f"FAIL 金币匹配 {h} diff={d} gap={gap}")
        ok = False
    # 2) 金币歧义（两人同值）→ gap=0 必须判不出
    hs2 = [{"slot": i, "id": 200 + i, "gold": 500, "hp": 3000, "hp_max": 4000}
           for i in range(3)]
    _h2, _d2, gap2 = gold_match(hs2, 500)
    if gap2 is not None and gap2 > 0:
        print(f"FAIL 金币歧义未识别 gap={gap2}")
        ok = False
    # 3) 置信度过滤：低置信字符必须被丢掉（9968 → 968 这类场景）
    keep = "".join(ch for ch, c in zip(str(9968), [0.80, 0.41, 0.80, 0.94])
                   if c >= MIN_CONF)
    if keep != "968":
        print(f"FAIL 置信度过滤 {keep!r} != '968'")
        ok = False
    # 4) ROI 归一化：换算回 1280×720 仍在画面右上角
    x1, y1, x2, y2 = GOLD_ROI
    if not (x1 > 0.9 and y1 < 0.1 and x2 > x1 and y2 > y1):
        print(f"FAIL ROI 位置异常 {GOLD_ROI}")
        ok = False
    # 5) 血条歧义仍要判不确定（fallback 逻辑保留）
    hs3 = [{"slot": i, "id": 300 + i, "gold": 0, "hp": 2000, "hp_max": 4000,
            "ep_rate": 0.5} for i in range(4)]
    _h3, _s3, mg3 = match_self(hs3, 0.5)
    if mg3 >= MIN_MARGIN:
        print(f"FAIL 血条歧义未判不确定 margin={mg3}")
        ok = False
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def grab(serial: str):
    r = subprocess.run([ADB, "-s", serial, "exec-out", "screencap", "-p"],
                       capture_output=True)
    if not r.stdout:
        return None
    return cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-cid", type=int, default=0,
                    help="显式指定本人英雄 config_id（推荐，生产路径即 --mem-self-id）")
    ap.add_argument("--shot", default="", help="用已有图片离线识别，不连设备")
    ap.add_argument("--frames", type=int, default=3)
    ap.add_argument("--serial", default="127.0.0.1:16384")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    if a.shot:
        img = cv2.imread(a.shot)
        if img is None:
            print(f"读不到图片 {a.shot}")
            return 2
        gold, confs = read_hud_gold(img)
        print(f"{a.shot}: HUD 金币 {gold}  confs={[round(c, 2) for c in confs]}")
        return 0 if gold is not None else 2

    src = MemSource(serial=a.serial)
    src.connect()
    hs = src.read_hero_states()
    if not hs:
        print("读不到英雄数组：不在对局中")
        return 1

    if a.self_cid:
        h = next((x for x in hs if x["id"] == a.self_cid), None)
        if h is None:
            print(f"--self-cid {a.self_cid} 不在本局 {[x['id'] for x in hs]}")
            return 2
        print(f"SELF_CID={h['id']}  SELF_SLOT={h['slot']}  source=explicit")
        return 0

    votes: dict[int, int] = {}
    for i in range(a.frames):
        frame = grab(a.serial)
        if frame is None:
            continue
        hud, confs = read_hud_gold(frame)
        if hud is None:
            continue
        h, d, gap = gold_match(hs, hud)
        if h is None:
            continue
        tag = "确定" if gap >= 200 else "歧义"
        print(f"HUD 金币 {hud} → slot{h['slot']} cid={h['id']} "
              f"(mem {h['gold']}, 差 {d}, 次近差 {gap:.0f}) {tag}")
        if tag == "确定":
            votes[h["id"]] = votes.get(h["id"], 0) + 1

    if not votes:
        print("\n未能确定本人。可用 --self-cid 显式指定（推荐），"
              "或确认在对局内且 HUD 金币可见。")
        return 2
    self_cid = max(votes.items(), key=lambda kv: kv[1])[0]
    slot = next(x["slot"] for x in hs if x["id"] == self_cid)
    print(f"\nSELF_CID={self_cid}  SELF_SLOT={slot}  source=hud_gold  "
          f"票数={votes[self_cid]}/{sum(votes.values())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
