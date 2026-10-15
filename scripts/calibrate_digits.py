# -*- coding: utf-8 -*-
"""HUD 数字读数标定（真机）：ROI 定位 + **真实字形**模板抠取。

背景
----
`wzry/vision/digit_reader.py` 的首版模板是用**系统字体**（arialbd / msyh 等）渲染出来的
近似（`build_templates()`）。游戏 HUD 用的是自有字体（描边 + 抗锯齿），字形差别很大，
所以真机读数的置信度会偏低、甚至读错 —— 该模块的 docstring 一直指向本脚本，
但本脚本此前**不存在**，导致"真机数字标定"这条链路是断的。

为什么必须人工给 --expect
-------------------------
本机没有装任何 OCR（paddleocr / easyocr / pytesseract 都没有，也不打算引），
所以无法自动知道"屏幕上写的是几"。做法：人在手机（或截图）上读一眼，把数字填进来
（例：金币显示 2884 -> `--expect 2884`）。ROI 内每个连通域从左到右依次对应这个数字的
每一位 -> 得到**有监督**的真实字形样本，直接替换/追加进 `configs/digit_templates.npz`。

用法
----
    # 1) 先只读不改：看这个 ROI 现在被读成什么、逐位置信度多少
    venv\\Scripts\\python.exe scripts\\calibrate_digits.py temp\\live_720.png --roi 1100,6,1160,30

    # 2) 人读出屏幕上是 2884 -> 校验 + 抠真实字形 + 写 ROI + 写模板
    venv\\Scripts\\python.exe scripts\\calibrate_digits.py temp\\live_720.png \\
        --roi 1100,6,1160,30 --name gold --expect 2884 --write

    #    只写 ROI、不动模板（模板还没攒够样本时）
    ... --roi ... --name gold --expect 2884 --write --no-templates

判定规则
--------
  * ROI 内切出的字符块数量必须 == `--expect` 的位数，否则拒绝写盘（位数对不上说明
    ROI 框错了，或者数字里混了小数点 / 'k' 这类非数字字符）。
  * `--write` 且给了 `--expect` 时，先比对当前读数：不一致也会拒绝写盘
    （除非 `--force`，用于"就是要先固化 ROI"的场景）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision import digit_reader as DR  # noqa: E402

FRAME_W, FRAME_H = 1280, 720      # 生产帧空间（screencap 统一 resize 到这里）
CALIB_PATH = ROOT / "configs" / "calibration_absolute.json"


def parse_roi(s: str):
    try:
        v = [int(x) for x in s.replace(" ", "").split(",")]
    except Exception:
        raise SystemExit(f"ROI 解析失败: {s}（应为 x0,y0,x1,y1）")
    if len(v) != 4 or v[2] <= v[0] or v[3] <= v[1]:
        raise SystemExit(f"ROI 不合法: {s}")
    return tuple(v)


def load_frame(path: str) -> np.ndarray:
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"读不到图片: {path}")
    h, w = img.shape[:2]
    if (w, h) != (FRAME_W, FRAME_H):
        img = cv2.resize(img, (FRAME_W, FRAME_H), interpolation=cv2.INTER_AREA)
        print(f"帧已缩放到 {FRAME_W}x{FRAME_H}（原 {w}x{h}）以对齐生产帧空间")
    return img


def glyphs_from_crop(crop: np.ndarray, expect: int):
    """按 --expect 的位数给 ROI 内每个字符块打标签。返回 [(digit, glyph(TPL_H,TPL_W))]。"""
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    boxes = DR._blobs(g)
    want = str(expect)
    if len(boxes) != len(want):
        return None, (f"字符块数 {len(boxes)} != 期望位数 {len(want)}"
                      f"（ROI 框错了？或数字里混了 'k'/'.'）")
    out = []
    for b, ch in zip(boxes, want):
        px = g[b["y"]:b["y"] + b["h"], b["x"]:b["x"] + b["w"]]
        if px.size == 0:
            return None, "空字符块"
        t = cv2.resize(px, (DR.TPL_W, DR.TPL_H), interpolation=cv2.INTER_AREA)
        out.append((int(ch), t.astype(np.float32) / 255.0))
    return out, None


def merge_templates(pairs, out=DR.TPL_PATH):
    """把真机字形追加进模板库（保留原系统字体模板，量还不够时仍有兜底）。"""
    d = np.load(out) if Path(out).exists() else None
    if d is not None:
        tpl = list(d["tpl"].astype(np.float32))
        lab = list(d["labels"].astype(np.int32))
    else:
        tpl, lab = [], []
    for dg, t in pairs:
        tpl.append(t)
        lab.append(dg)
    arr = np.stack(tpl).astype(np.float32)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, tpl=arr, labels=np.array(lab, dtype=np.int32))
    return len(lab), len(set(lab))


def write_roi(name: str, roi):
    """写 digit_rois（保留 configs/calibration_absolute.json 既有的 2 空格缩进风格）。"""
    d = json.loads(CALIB_PATH.read_text(encoding="utf-8"))
    d.setdefault("digit_rois", {})[name] = list(roi)
    CALIB_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return d["digit_rois"]


def main() -> int:
    ap = argparse.ArgumentParser(description="HUD 数字读数标定（ROI + 真机字形）")
    ap.add_argument("frame", help="真机帧图（任意分辨率，会自动缩放到 1280x720 生产帧空间）")
    ap.add_argument("--roi", required=True, help="x0,y0,x1,y1（1280x720 帧空间）")
    ap.add_argument("--name", default="", help="ROI 名（gold/level/skill_pt...），写盘用")
    ap.add_argument("--expect", type=int, default=None,
                    help="人在屏幕上读到的数字（有监督抠字形必需）")
    ap.add_argument("--write", action="store_true", help="写 ROI / 追加真机字形模板")
    ap.add_argument("--no-templates", action="store_true", help="只写 ROI，不动模板库")
    ap.add_argument("--force", action="store_true",
                    help="当前读数与 --expect 不一致时仍继续（只建议先固化 ROI 时用）")
    ap.add_argument("--dump", default="", help="把 ROI 裁图另存到该路径（便于人工核对）")
    args = ap.parse_args()

    frame = load_frame(args.frame)
    roi = parse_roi(args.roi)
    crop = frame[roi[1]:roi[3], roi[0]:roi[2]]
    if crop.size == 0:
        print("ROI 越界:", roi)
        return 2
    if args.dump:
        cv2.imwrite(args.dump, crop)
        print("裁图已存:", args.dump)

    val, confs = DR.read_number(crop)
    print(f"当前读数: {val}   逐位置信度: {confs}")
    if val is None:
        print("（ROI 内没切出任何字符：要么框错了，要么数字颜色不满足 THR=150 的亮字假设）")

    if args.expect is not None:
        ok = (val == args.expect)
        print(f"期望值:   {args.expect}   -> {'一致' if ok else '**不一致**'}")
        if not ok and not args.force:
            print("拒绝写盘：读数与期望不一致（确认 ROI 后再跑；确要强制固化加 --force）")
            return 3
        pairs, err = glyphs_from_crop(crop, args.expect)
        if err:
            print("拒绝抠字形:", err)
            return 4
        print(f"抠到真实字形: {len(pairs)} 个 -> {[d for d, _ in pairs]}")

        if args.write:
            if args.name:
                rois = write_roi(args.name, roi)
                print("digit_rois 已更新:", rois)
            else:
                print("（未给 --name，跳过 ROI 写盘）")
            if not args.no_templates:
                n, k = merge_templates(pairs)
                print(f"模板库已追加: {DR.TPL_PATH}  共 {n} 条 / {k} 个数字")
                re_val, re_conf = DR.read_number(crop)
                print(f"用新模板回读: {re_val}  置信度: {re_conf}"
                      f"  -> {'OK' if re_val == args.expect else '仍不一致'}")
    elif args.write:
        print("--write 需要 --expect（没有人工读数值就无法给字形打标签）")
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
