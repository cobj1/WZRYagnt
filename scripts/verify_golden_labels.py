# -*- coding: utf-8 -*-
"""golden 标注核验：把"目视核验结论"固化成可复现的规则 + 显式人工覆写。

背景：71 张 golden 候选的 `prefill_source` 全是 `yolo_units`——即"模型给自己
出题"，审计 R4 判定为**零人工验证**。本脚本做两件事：

1. **规则过滤**（通用、可解释）：
   - UI 区排除：左上小地图、右上头像/经济条、底部技能区；
   - 出框排除：框面积 >35% 在画面外（半截物体不算目标）；
   - 重复排除：同类 IoU>0.6 只留高置信；异类高重叠（同一目标被判两次）留高置信。
2. **人工覆写**（逐帧目视结论，带原因）：见 `MANUAL` 表。

产出 `<golden>/labels_work.jsonl`（apply_golden_labels.py 的输入格式），
`verified=true` 只在**目视确认过**的帧上给；有疑问的帧宁可留 false。

用法：
    python -X utf8 scripts/verify_golden_labels.py            # dry-run，打印统计
    python -X utf8 scripts/verify_golden_labels.py --write    # 写 labels_work.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT.parent / "hokdata" / "golden"
VERIFY = GOLDEN / "verify"
CLASSES = ["enemy_hero", "ally_hero", "enemy_minion", "ally_minion",
           "enemy_turret", "ally_turret", "enemy_crystal", "ally_crystal",
           "neutral_monster", "hook_aim", "skill_effect"]
CLS_ID = {n: i for i, n in enumerate(CLASSES)}

# UI 区（归一化）：这些区域里出现的"目标"基本都是图标/文字误检
UI_ZONES = [
    ("minimap", 0.00, 0.00, 0.185, 0.30),      # 左上小地图
    ("portrait", 0.78, 0.00, 1.00, 0.16),      # 右上头像/经济条
    ("scorebar", 0.00, 0.00, 1.00, 0.055),     # 顶部计分条
    ("skillbar", 0.00, 0.80, 1.00, 1.00),      # 底部技能/摇杆
]

# 逐帧目视结论（全局下标 = candidates.jsonl 顺序）
#   drop: 要删掉的框下标（在 detections.json 的 boxes 顺序里）
#   fix : {框下标: 新类别名}
#   verified: 该帧是否已目视确认（False = 存疑，不写 verified）
#   scenario / note
MANUAL = {
    2:  {"drop": [0], "note": "小地图下缘图标被判成敌方小兵"},
    7:  {"drop": [0], "note": "顶部塔只露出 ~10%，半截目标不计"},
    8:  {"drop": [0], "note": "同上：塔在画面上边缘被裁掉"},
    9:  {"drop": [0], "note": "同上：塔在画面上边缘被裁掉"},
    14: {"drop": [1], "verified": False,
         "note": "死亡界面遮挡，敌我血条不可判读"},
    27: {"drop": [0, 2], "note": "同一英雄被重复判成小兵/敌方英雄（与友方框同心）"},
    31: {"drop": [0], "note": "召唤师技能/装备按钮区误检"},
    36: {"drop": [1], "note": "右上头像区下方的树丛，无单位"},
    37: {"drop": [0, 2], "note": "小地图边缘误检 + 同一英雄重复框"},
    42: {"drop": [2], "note": "小地图角击杀提示误检"},
    44: {"drop": [0, 1], "note": "底部技能区误检 + 顶部分数条误检"},
    45: {"drop": [0, 2], "note": "底部技能图标误检 + 同一英雄重复框"},
    50: {"drop": [0, 1], "note": "左下角（技能/摇杆区）两个小兵误检"},
    53: {"drop": [1], "note": "死亡界面：切人面板头像被判成敌方英雄"},
    58: {"drop": [0], "verified": False, "note": "团战特效区，红框目标无法确认"},
    66: {"fix": {0: "ally_hero"}, "note": "同一静止英雄上一帧判友、本帧判敌（血条为绿）"},
    68: {"fix": {0: "ally_hero"}, "note": "同 66：阵营抖动，按绿血条改判友方"},
}

# 场景桶（按全局下标；依据是目视所见 UI 状态）
SCENARIO = {}
for i in range(0, 14):
    SCENARIO[i] = "lane"
for i in range(14, 25):
    SCENARIO[i] = "death"
for i in (25, 26):
    SCENARIO[i] = "base"
for i in range(27, 53):
    SCENARIO[i] = "lane"
for i in range(53, 58):
    SCENARIO[i] = "death"
for i in (58, 59):
    SCENARIO[i] = "teamfight"
for i in range(60, 64):
    SCENARIO[i] = "base"
for i in range(64, 71):
    SCENARIO[i] = "base"


def md5_file(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def in_ui_zone(b, w, h):
    cx = (b["x1"] + b["x2"]) / 2.0 / w
    cy = (b["y1"] + b["y2"]) / 2.0 / h
    for name, x0, y0, x1, y1 in UI_ZONES:
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            return name
    return None


def out_of_frame_frac(b, w, h):
    area = max(1e-6, (b["x2"] - b["x1"]) * (b["y2"] - b["y1"]))
    ix = max(0.0, min(b["x2"], w) - max(b["x1"], 0.0))
    iy = max(0.0, min(b["y2"], h) - max(b["y1"], 0.0))
    return 1.0 - (ix * iy) / area


def iou(a, b):
    ix = max(0.0, min(a["x2"], b["x2"]) - max(a["x1"], b["x1"]))
    iy = max(0.0, min(a["y2"], b["y2"]) - max(a["y1"], b["y1"]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    aa = (a["x2"] - a["x1"]) * (a["y2"] - a["y1"])
    bb = (b["x2"] - b["x1"]) * (b["y2"] - b["y1"])
    return inter / max(1e-6, aa + bb - inter)


def clean(boxes, w, h, manual):
    """规则过滤 + 人工覆写，返回 (保留框, 丢弃原因列表)。"""
    drops = {}
    for i, b in enumerate(boxes):
        zone = in_ui_zone(b, w, h)
        if zone:
            drops[i] = f"ui:{zone}"
        elif out_of_frame_frac(b, w, h) > 0.35:
            drops[i] = "out_of_frame"
    # 重复：高 IoU 只留置信最高的
    idx = [i for i in range(len(boxes)) if i not in drops]
    for a in range(len(idx)):
        for c in range(a + 1, len(idx)):
            i, j = idx[a], idx[c]
            if i in drops or j in drops:
                continue
            if iou(boxes[i], boxes[j]) > 0.6:
                lo = j if boxes[i]["conf"] >= boxes[j]["conf"] else i
                drops[lo] = "duplicate"
    # 人工覆写优先于规则（覆写里 drop 的是"即使规则没删也要删"的）
    for i in (manual or {}).get("drop", []):
        drops.setdefault(i, "manual")
    out = []
    for i, b in enumerate(boxes):
        if i in drops:
            continue
        cls = CLS_ID[b["cls"]] if isinstance(b["cls"], str) else int(b["cls"])
        out.append({"cls": cls, "x1": b["x1"], "y1": b["y1"],
                    "x2": b["x2"], "y2": b["y2"]})
    for i, name in ((manual or {}).get("fix") or {}).items():
        if i < len(out) and name in CLS_ID:
            out[i]["cls"] = CLS_ID[name]
    return out, drops


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    dets = json.loads((VERIFY / "detections.json").read_text(encoding="utf-8"))
    cands = [json.loads(l) for l in
             (GOLDEN / "candidates.jsonl").read_text(encoding="utf-8").splitlines()
             if l.strip()]
    assert len(dets) == len(cands), f"{len(dets)} vs {len(cands)}"

    events = []
    n_verified = n_empty = 0
    for i, (d, c) in enumerate(zip(dets, cands)):
        m = MANUAL.get(i, {})
        boxes, drops = clean(d["boxes"], d["w"], d["h"], m)
        verified = m.get("verified", True) and bool(boxes)
        if verified:
            n_verified += 1
        if not boxes:
            n_empty += 1
        rel = Path(c["image"]).relative_to(GOLDEN).as_posix()
        events.append({
            "id": rel,
            "image": c["image"],
            "session": c["session"],
            "source_image_md5": md5_file(Path(c["image"])),
            "boxes": boxes,
            "verified": verified,
            "scenario": SCENARIO.get(i, c.get("scenario") or "other"),
            "note": m.get("note") or (";".join(f"{k}:{v}" for k, v in drops.items())
                                      if drops else "目视确认"),
            "dropped": drops,
        })

    print(f"帧数 {len(events)}；verified {n_verified}；空框帧 {n_empty}")
    n_drop = sum(len(e["dropped"]) for e in events)
    print(f"共删除 {n_drop} 个框（规则+人工）")
    from collections import Counter
    why = Counter(v.split(":")[0] for e in events for v in e["dropped"].values())
    print("删除原因:", dict(why))
    print("场景分布:", dict(Counter(e["scenario"] for e in events)))
    print("类别分布:", dict(Counter(CLASSES[b["cls"]] for e in events for b in e["boxes"])))

    if args.write:
        # 只写"可导入"的帧：有框且已目视确认。空框帧（画面里确实没有目标）
        # 与存疑帧不进 labels_work——它们需要从 golden 里剔除或补拍，而不是当标注。
        out = GOLDEN / "labels_work.jsonl"
        keep = [e for e in events if e["verified"] and e["boxes"]]
        with out.open("w", encoding="utf-8") as f:
            for e in keep:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        skip = [e for e in events if not (e["verified"] and e["boxes"])]
        print(f"已写 {out}：{len(keep)} 条可导入")
        print(f"未导入 {len(skip)} 条：" + ", ".join(
            f"{Path(e['id']).stem.split('__')[-1]}({e['note'][:14]})" for e in skip[:6])
            + (" …" if len(skip) > 6 else ""))
        (GOLDEN / "verify" / "excluded.json").write_text(
            json.dumps([{"id": e["id"], "reason": e["note"]} for e in skip],
                       ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
