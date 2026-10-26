# -*- coding: utf-8 -*-
"""把"停用过滤会新增进攻"的帧里的 enemy_hero 框裁出来拼成对照图（只读）。

用途：人工/视觉判定 `camp_src="default"` 的框到底是真敌人、我方英雄、HUD 头像还是特效。
输入：`runs/eval/trusted_filter_ab.json`（影子 A/B 的翻转记录）+ `data/matches/*/frames/*.png`

输出：`runs/eval/verify/sheet_XX.png`（每格一个框，带序号 / camp_src / conf / 框尺寸）

用法：
  cd WZRYagnt
  venv/Scripts/python.exe -X utf8 scripts/make_trusted_crops.py --per-sheet 12
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
CELL = 220          # 单元格边长
PAD = 0.25          # 裁剪外扩比例


def crop_box(img: Image.Image, scr, pad=PAD) -> Image.Image:
    w, h = img.size
    x, y, bw, bh = (float(v) for v in scr[:4])
    px, py, pw, ph = x * w, y * h, bw * w, bh * h
    dx, dy = pw * pad, ph * pad
    box = (max(0, px - dx), max(0, py - dy), min(w, px + pw + dx), min(h, py + ph + dy))
    if box[2] - box[0] < 4 or box[3] - box[1] < 4:
        return Image.new("RGB", (8, 8), (30, 30, 30))
    c = img.crop(tuple(int(v) for v in box))
    scale = CELL / max(c.size)
    return c.resize((max(1, int(c.width * scale)), max(1, int(c.height * scale))), Image.BICUBIC)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ab", default="runs/eval/trusted_filter_ab.json")
    ap.add_argument("--out", default="runs/eval/verify")
    ap.add_argument("--per-sheet", type=int, default=12)
    ap.add_argument("--cols", type=int, default=4)
    args = ap.parse_args()

    d = json.loads(Path(args.ab).read_text(encoding="utf-8"))
    flips = [f for f in d["flips"]
             if f["a"] in ("map_move", "move", "none") and f["b"] in ("skill", "attack", "combo")]

    items = []
    for f in flips:
        p = ROOT / "data" / "matches" / f["s"] / "frames" / f"frame_{int(f['fid']):06d}.png"
        if not p.exists():
            continue
        sp = p.parent.parent / "states.jsonl"
        st = None
        for line in sp.open(encoding="utf-8", errors="replace"):
            if not line.strip():
                continue
            o = json.loads(line)
            if o.get("frame_id") == f["fid"]:
                st = o
                break
        if not st:
            continue
        for u in st.get("units") or []:
            if u.get("cls") != "enemy_hero":
                continue
            items.append({"img": p, "unit": u, "f": f})

    if not items:
        print("没有可裁的样本")
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for i in range(0, len(items), args.per_sheet):
        chunk = items[i:i + args.per_sheet]
        rows = (len(chunk) + args.cols - 1) // args.cols
        sheet = Image.new("RGB", (args.cols * CELL, rows * (CELL + 26)), (18, 18, 18))
        dr = ImageDraw.Draw(sheet)
        for k, it in enumerate(chunk):
            img = Image.open(it["img"]).convert("RGB")
            c = crop_box(img, it["unit"]["screen"])
            cx, cy = (k % args.cols) * CELL, (k // args.cols) * (CELL + 26)
            sheet.paste(c, (cx + (CELL - c.width) // 2, cy))
            u = it["unit"]
            txt = f"{i+k}|{u.get('camp_src')}|{float(u.get('conf', 0)):.2f}"
            dr.text((cx + 4, cy + CELL + 4), txt, fill=(230, 230, 230))
            dr.text((cx + 4, cy + CELL + 14),
                    f'{u["screen"][2]:.3f}x{u["screen"][3]:.3f}', fill=(150, 150, 150))
        p = out / f"sheet_{i//args.per_sheet:02d}.png"
        sheet.save(p)
        print("写入", p, f"({len(chunk)} 格)")

    print(f"\n共 {len(items)} 个框，来自 {len({it['f']['s'] for it in items})} 个会话")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
