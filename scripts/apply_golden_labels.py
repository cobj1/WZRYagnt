#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 golden 标注台产出的 labels_work.jsonl 校验后合并进 candidates.jsonl。

默认 dry-run，只打印校验结果与 diff 摘要；--apply 才写，写前自动备份。

用法：
    python scripts/apply_golden_labels.py                       # dry-run
    python scripts/apply_golden_labels.py --report              # 覆盖率报告
    python scripts/apply_golden_labels.py --apply               # 真正写入
    python scripts/apply_golden_labels.py --self-test           # 临时目录端到端自测

退出码：0 成功；2 校验失败 / 无法继续。
"""

import argparse
import hashlib
import json
import math
import os
import shutil
import struct
import sys
import tempfile
import zlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")

DEFAULT_ROOT = Path("D:/work/aiKings/hokdata/golden")
IMAGE_DIRS = ("images", "images_test")
DEFAULT_CLASSES = [
    "enemy_hero", "ally_hero", "enemy_minion", "ally_minion",
    "enemy_turret", "ally_turret", "enemy_crystal", "ally_crystal",
    "neutral_monster", "hook_aim", "skill_effect",
]
TARGET_FRAMES = 200


# ---------------------------------------------------------------- 基础

def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def image_size(path):
    try:
        with open(path, "rb") as f:
            head = f.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
                w, h = struct.unpack(">II", head[16:24])
                return int(w), int(h)
            if head[:2] == b"\xff\xd8":
                f.seek(2)
                while True:
                    b = f.read(1)
                    while b and b != b"\xff":
                        b = f.read(1)
                    while b == b"\xff":
                        b = f.read(1)
                    if not b:
                        break
                    marker = b[0]
                    if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                        continue
                    raw = f.read(2)
                    if len(raw) < 2:
                        break
                    seglen = int.from_bytes(raw, "big")
                    if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                        data = f.read(5)
                        return int.from_bytes(data[3:5], "big"), int.from_bytes(data[1:3], "big")
                    f.seek(seglen - 2, 1)
    except OSError:
        pass
    return 0, 0


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def find_splits(root, explicit=None):
    for p in ([Path(explicit)] if explicit else [root / "splits.json", root.parent / "splits.json"]):
        data = load_json(p)
        if isinstance(data, dict):
            return data, p
    return {}, None


def resolve_split(splits, session):
    for dataset in ("tencent_rt_base", "tencent_rt", "wzry"):
        table = splits.get(dataset)
        if isinstance(table, dict) and session in table:
            return table[session], dataset
    return None, None


def read_events(path):
    events, bad = [], 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(ev, dict) and ev.get("id"):
                events.append(ev)
            else:
                bad += 1
    return events, bad


def replay(events):
    """undo 撤销该 id 最近一条未撤销事件；返回 id -> 最新事件。"""
    applied = []
    for ev in events:
        if ev.get("op") == "undo":
            tid = ev.get("id")
            for k in range(len(applied) - 1, -1, -1):
                if tid is None or applied[k].get("id") == tid:
                    applied.pop(k)
                    break
        else:
            applied.append(ev)
    state = {}
    for ev in applied:
        state[ev["id"]] = ev
    return state


def load_candidates(path):
    rows = []
    if not path.exists():
        return rows
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def basename_of(image_field):
    return os.path.basename(str(image_field).replace("\\", "/"))


def as_number(v):
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(float(v)))


def yolo_text(boxes, width, height):
    lines = []
    for b in boxes:
        cx = (b["x1"] + b["x2"]) / 2.0 / width
        cy = (b["y1"] + b["y2"]) / 2.0 / height
        bw = (b["x2"] - b["x1"]) / width
        bh = (b["y2"] - b["y1"]) / height
        lines.append(f"{int(b['cls'])} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 校验

def validate_event(ev, ctx):
    """返回 (record, errors)。errors 里每条都是 '规则N ...' 文本。"""
    errors = []
    rel = str(ev.get("id") or "").replace("\\", "/")
    name = basename_of(ev.get("image") or rel)
    path = ctx["root"] / rel
    if rel.split("/")[-1] != name:
        errors.append(f"规则1 schema：id ({rel}) 与 image 文件名 ({name}) 不一致")

    boxes = ev.get("boxes")
    # 规则 1：schema
    if not isinstance(boxes, list) or not boxes:
        errors.append("规则1 schema：boxes 为空或不是数组")
        boxes = boxes if isinstance(boxes, list) else []
    if boxes:
        width, height = image_size(path) if path.is_file() else (0, 0)
        for i, b in enumerate(boxes):
            if not isinstance(b, dict):
                errors.append(f"规则1 schema：框 #{i} 不是对象")
                continue
            cls = b.get("cls")
            if not isinstance(cls, int) or isinstance(cls, bool) or not 0 <= cls < len(ctx["classes"]):
                errors.append(f"规则1 schema：框 #{i} 类别非法 {cls!r}")
            for key in ("x1", "y1", "x2", "y2"):
                if not as_number(b.get(key)):
                    errors.append(f"规则1 schema：框 #{i} 的 {key} 不是数值 {b.get(key)!r}")
                    break
            else:
                if not (b["x2"] > b["x1"] and b["y2"] > b["y1"]):
                    errors.append(f"规则1 schema：框 #{i} x2<=x1 或 y2<=y1")
                elif width and height and not (0 <= b["x1"] and 0 <= b["y1"]
                                               and b["x2"] <= width and b["y2"] <= height):
                    errors.append(f"规则1 schema：框 #{i} 超出图片范围 {width}x{height}")

    # 规则 2：verified
    if ev.get("verified") is not True:
        errors.append("规则2 verified：不是 true")

    # 规则 5 的前半：图片存在 / md5 与保存时一致
    if not path.is_file():
        errors.append(f"规则5 图片：文件不存在 {rel}")
        digest = None
    else:
        digest = md5_file(path)
        saved = ev.get("source_image_md5")
        if not saved:
            errors.append("规则5 图片：事件缺少 source_image_md5，无法核对")
        elif str(saved).lower() != digest:
            errors.append(f"规则5 图片：md5 与保存时不一致（磁盘 {digest[:8]} / 记录 {str(saved)[:8]}）")

    # 规则 4：test-only
    session = str(ev.get("session") or name.split("__")[0])
    split, split_src = resolve_split(ctx["splits"], session)
    if split is None:
        errors.append(f"规则4 test-only：splits.json 里找不到 session {session}")
    elif split != "test":
        errors.append(f"规则4 test-only：session {session} 在 {split_src} 中是 {split}")

    record = {
        "id": rel,
        "name": name,
        "path": path,
        "session": session,
        "split": split,
        "md5": digest,
        "boxes": boxes,
        "width": image_size(path)[0] if path.is_file() else 0,
        "height": image_size(path)[1] if path.is_file() else 0,
        "note": ev.get("note") or "",
        "ts": ev.get("ts"),
        "labeler": ev.get("labeler") or "unknown",
        "existing": name in ctx["cand_by_name"],
    }
    if record["width"] == 0 and path.is_file():
        errors.append("规则5 图片：无法读取尺寸")
    return record, errors


def plan_merge(candidates, records):
    """records 已通过校验。返回 (merged_rows, updated_names, added_records)。"""
    updates = {r["name"]: r for r in records}
    merged, updated = [], []
    for row in candidates:
        name = basename_of(row.get("image", ""))
        rec = updates.get(name)
        if rec:
            row = dict(row)
            row["verified"] = True
            row["ground_truth"] = yolo_text(rec["boxes"], rec["width"], rec["height"])
            row["prefill_source"] = "human"
            updated.append(name)
        merged.append(row)
    existing = {basename_of(r.get("image", "")) for r in candidates}
    added = [r for r in records if r["name"] not in existing]
    for rec in added:
        merged.append({
            "image": str(rec["path"]),
            "session": rec["session"],
            "frame_id": rec["name"].split("__frame_")[-1].split(".")[0],
            "state_frame_id": None,
            "state_offset": None,
            "wall_t": None,
            "game_time_s": None,
            "scenario": None,
            "scenario_reason": "human_labeled",
            "split": "test",
            "prefill_boxes": len(rec["boxes"]),
            "prefill_source": "human",
            "verified": True,
            "ground_truth": yolo_text(rec["boxes"], rec["width"], rec["height"]),
            "md5": rec["md5"],
            "width": rec["width"],
            "height": rec["height"],
            "labeler": rec["labeler"],
            "labeled_at": rec["ts"],
        })
    return merged, updated, added


def coverage_report(merged, root, classes, applied_note):
    out = []
    total = len(merged)
    verified = sum(1 for r in merged if r.get("verified") is True)
    by_split = {}
    by_class = {}
    for row in merged:
        by_split[row.get("split") or "unknown"] = by_split.get(row.get("split") or "unknown", 0) + 1
        gt = row.get("ground_truth")
        if not isinstance(gt, str):
            continue
        for line in gt.splitlines():
            parts = line.split()
            if not parts:
                continue
            try:
                cid = int(float(parts[0]))
            except ValueError:
                continue
            cname = classes[cid] if 0 <= cid < len(classes) else f"cls{cid}"
            by_class[cname] = by_class.get(cname, 0) + 1

    out.append("===== golden 覆盖率报告" + (f"（{applied_note}）" if applied_note else "") + " =====")
    out.append(f"  候选帧总数      {total}")
    out.append(f"  已验证 verified {verified}" + (f"  ({verified / total * 100:.1f}%)" if total else ""))
    out.append("  split 分布      " + (", ".join(f"{k} {v}" for k, v in sorted(by_split.items())) or "-"))
    out.append("  类别分布        " + (", ".join(f"{k} {v}" for k, v in sorted(by_class.items(), key=lambda x: -x[1])) or "无"))
    need = max(0, TARGET_FRAMES - verified)
    out.append(f"  距 {TARGET_FRAMES} 帧目标   " + (f"还差 {need}" if need else "已达标"))
    if by_split.get("train") or by_split.get("val"):
        out.append("  [警告] candidates 里混有 train/val 帧，golden 只允许 test")

    pool = {"test": set(), "other": set()}
    disk_total = 0
    seen_names = set()
    splits, _ = find_splits(root)
    for sub in IMAGE_DIRS:
        d = root / sub
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if not p.is_file() or p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".bmp"):
                continue
            if p.name in seen_names:
                continue
            seen_names.add(p.name)
            disk_total += 1
            session = p.name.split("__")[0]
            split, _ = resolve_split(splits, session)
            try:
                digest = md5_file(p)
            except OSError:
                continue
            pool["test" if split == "test" else "other"].add(digest)
    out.append(f"  磁盘图片         {disk_total} 张；test 唯一图 {len(pool['test'])} 张，非 test 唯一图 {len(pool['other'])} 张")
    if need and len(pool["test"]) < TARGET_FRAMES:
        out.append(f"  [约束] 现有 test 图片池只有 {len(pool['test'])} 张唯一图，"
                   f"想到 {TARGET_FRAMES} 帧必须重新从 test 局抽帧（同图不能重复计入）")
    return "\n".join(out)


# ---------------------------------------------------------------- 主流程

def run(args, quiet=False):
    root = Path(args.root).expanduser()
    labels = Path(args.labels).expanduser() if args.labels else root / "labels_work.jsonl"
    cand_path = root / "candidates.jsonl"

    def say(msg=""):
        if not quiet:
            print(msg)

    if not root.is_dir():
        print(f"[错误] --root 不存在：{root}")
        return 2
    if not cand_path.is_file():
        print(f"[错误] 找不到 candidates.jsonl：{cand_path}")
        return 2
    summary = load_json(root / "summary.json", {}) or {}
    classes = summary.get("class_names") or DEFAULT_CLASSES
    splits, splits_path = find_splits(root, args.splits)
    if not splits:
        print("[错误] 找不到 splits.json，无法做 test-only 校验")
        return 2

    if not labels.is_file():
        if args.report:
            print(coverage_report(load_candidates(cand_path), root, classes, "当前 candidates，标注文件尚未生成"))
            return 0
        print(f"[错误] 找不到标注文件：{labels}")
        print("       先用 tools/golden_labeler/server.py 标注若干帧；或用 --labels 指定其他文件。")
        print("       只想看覆盖率可加 --report（不依赖标注文件）。")
        return 2

    events, bad_lines = read_events(labels)
    state = replay(events)
    candidates = load_candidates(cand_path)
    cand_by_name = {basename_of(r.get("image", "")): r for r in candidates}

    # candidates 已有图片的 md5 -> 名称
    cand_md5 = {}
    for row in candidates:
        name = basename_of(row.get("image", ""))
        p = root / "images" / name
        if not p.is_file():
            p = root / "images_test" / name
        if p.is_file():
            try:
                cand_md5.setdefault(md5_file(p), name)
            except OSError:
                pass

    say(f"[1/4] 标注事件  {labels}")
    say(f"      {len(events)} 条事件（坏行 {bad_lines}），回放后 {len(state)} 帧有记录")

    ctx = {"root": root, "splits": splits, "classes": classes, "cand_by_name": cand_by_name}
    records, errors = [], []
    skipped = 0
    seen_md5 = {}
    for item_id, ev in state.items():
        op = ev.get("op") or "save"
        if op in ("skip", "flag"):
            skipped += 1
            continue
        rec, errs = validate_event(ev, ctx)
        # 规则 3：MD5 唯一（批内 + 与已有 candidates）
        if rec["md5"]:
            other = seen_md5.get(rec["md5"])
            if other and other != rec["name"]:
                errs.append(f"规则3 MD5 唯一：同一 md5 在本批次重复出现（另有 {other}）")
            seen_md5.setdefault(rec["md5"], rec["name"])
            prev = cand_md5.get(rec["md5"])
            if prev and prev != rec["name"]:
                errs.append(f"规则3 MD5 唯一：与 candidates 里已有条目 {prev} 图片相同")
        if errs:
            errors.append((item_id, errs))
        else:
            records.append(rec)

    say(f"[2/4] 校验      通过 {len(records)} 条，失败 {len(errors)} 条，跳过/标记 {skipped} 条")
    for item_id, errs in errors:
        say(f"      [FAIL] {item_id}")
        for e in errs:
            say(f"             - {e}")
    if not records and not errors:
        say("      没有可导入的标注（全是 skip/flag 或空文件）")
        if args.report:
            say("")
            say(coverage_report(candidates, root, classes, "当前 candidates"))
        return 2

    merged, updated, added = plan_merge(candidates, records)
    say(f"[3/4] 合并计划  更新 {len(updated)} 条 / 新增 {len(added)} 条"
        f"（candidates 原有 {len(candidates)} 条，合并后 {len(merged)} 条）")
    for name in updated[:10]:
        say(f"      ~ {name}  (verified=true, prefill_source=human)")
    if len(updated) > 10:
        say(f"      ~ … 另有 {len(updated) - 10} 条")
    for rec in added[:10]:
        say(f"      + {rec['name']}  {len(rec['boxes'])} 框")
    if len(added) > 10:
        say(f"      + … 另有 {len(added) - 10} 条")

    if args.report:
        say("")
        say(coverage_report(merged, root, classes, "已应用" if args.apply and not args.dry_run else "dry-run 预估"))

    if args.dry_run or not args.apply:
        say("[4/4] dry-run：未写入。确认无误后加 --apply。")
        if errors:
            say(f"校验失败 {len(errors)} 条，已拒绝导入（退出码 2）。")
            return 2
        return 0

    if errors:
        say("[4/4] 校验未全通过，拒绝写入（退出码 2）。")
        return 2

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = cand_path.with_name(f"{cand_path.name}.bak-{ts}")
    shutil.copy2(cand_path, backup)
    say(f"[4/4] 备份      {backup}")

    tmp = cand_path.with_name(cand_path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for row in merged:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, cand_path)
    say(f"      已写入    {cand_path}（{len(merged)} 条）")
    return 0


# ---------------------------------------------------------------- 自测

def write_png(path, w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    blob = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))
    path.write_bytes(blob)


def self_test():
    """在临时目录端到端验证：save / undo / 5 条校验，不碰真实 golden。"""
    print("== apply_golden_labels.py --self-test（全部在临时目录）==")
    failures = []

    def check(label, cond):
        print(f"  [{'OK' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    with tempfile.TemporaryDirectory(prefix="gl_selftest_") as td:
        tmp = Path(td)
        root = tmp / "golden"
        (root / "images").mkdir(parents=True)
        for session, rgb in (("s_test", (200, 30, 30)), ("s_test2", (30, 200, 30)), ("s_train", (30, 30, 200))):
            write_png(root / "images" / f"{session}__frame_000000.png", 64, 36, rgb)
        (root / "summary.json").write_text(json.dumps({"class_names": DEFAULT_CLASSES}), encoding="utf-8")
        (root / "candidates.jsonl").write_text("", encoding="utf-8")
        splits = tmp / "splits.json"
        splits.write_text(json.dumps({"wzry": {"s_test": "test", "s_test2": "test", "s_train": "train"}}),
                          encoding="utf-8")

        labels = root / "labels_work.jsonl"

        def md5(name):
            return md5_file(root / "images" / name)

        def ev(name, boxes, verified=True, op="save"):
            return {"op": op, "id": f"images/{name}", "image": f"images/{name}",
                    "session": name.split("__")[0], "split": "test",
                    "source_image_md5": md5(name), "boxes": boxes, "verified": verified,
                    "note": "", "ts": "2026-10-24T12:00:00+08:00", "labeler": "selftest"}

        good = ev("s_test__frame_000000.png",
                  [{"cls": 1, "x1": 4, "y1": 4, "x2": 40, "y2": 30}])

        ctx_args = SimpleNamespace(root=str(root), labels=str(labels), splits=str(splits),
                                   apply=False, dry_run=True, report=False)

        # A. 正常一条
        labels.write_text(json.dumps(good, ensure_ascii=False) + "\n", encoding="utf-8")
        rc = run(ctx_args, quiet=True)
        check("正常 save 通过校验并生成合并计划", rc == 0)

        # B. 同 md5 另一张（不同 id，字节相同）
        dup = dict(good)
        dup["id"] = "images/s_test2__frame_000000.png"
        dup["image"] = "images/s_test2__frame_000000.png"
        dup["session"] = "s_test2"
        dup["source_image_md5"] = md5("s_test2__frame_000000.png")
        labels.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in (good, dup)) + "\n",
                          encoding="utf-8")
        # 让 s_test2 与 s_test 字节相同：先用 good 的 md5 替换文件
        shutil.copy2(root / "images" / "s_test__frame_000000.png",
                     root / "images" / "s_test2__frame_000000.png")
        dup["source_image_md5"] = md5("s_test2__frame_000000.png")
        labels.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in (good, dup)) + "\n",
                          encoding="utf-8")
        rc = run(ctx_args, quiet=True)
        check("规则3 同 md5 不同图被拒绝", rc == 2)

        # C. train 帧
        bad_train = ev("s_train__frame_000000.png",
                       [{"cls": 0, "x1": 2, "y1": 2, "x2": 30, "y2": 20}])
        labels.write_text(json.dumps(bad_train, ensure_ascii=False) + "\n", encoding="utf-8")
        rc = run(ctx_args, quiet=True)
        check("规则4 train 帧被拒绝", rc == 2)

        # D. verified=false
        not_verified = dict(good, verified=False)
        labels.write_text(json.dumps(not_verified, ensure_ascii=False) + "\n", encoding="utf-8")
        rc = run(ctx_args, quiet=True)
        check("规则2 verified=false 被拒绝", rc == 2)

        # E. schema 越界
        bad_box = ev("s_test__frame_000000.png",
                     [{"cls": 1, "x1": 10, "y1": 10, "x2": 999, "y2": 30}])
        labels.write_text(json.dumps(bad_box, ensure_ascii=False) + "\n", encoding="utf-8")
        rc = run(ctx_args, quiet=True)
        check("规则1 坐标越界被拒绝", rc == 2)

        # F. undo 后不再计入
        undo = {"op": "undo", "id": good["id"], "ts": "2026-10-24T12:01:00+08:00", "labeler": "selftest"}
        labels.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in (good, undo)) + "\n",
                          encoding="utf-8")
        rc = run(ctx_args, quiet=True)
        check("undo 回放后无标注可导入（退出码 2）", rc == 2)

        # G. md5 被篡改（磁盘换图）规则5
        other = ev("s_test__frame_000000.png",
                   [{"cls": 1, "x1": 4, "y1": 4, "x2": 40, "y2": 30}])
        other["source_image_md5"] = "0" * 32
        labels.write_text(json.dumps(other, ensure_ascii=False) + "\n", encoding="utf-8")
        rc = run(ctx_args, quiet=True)
        check("规则5 md5 与保存时不符被拒绝", rc == 2)

        # H. --apply 真正写入 + 备份
        labels.write_text(json.dumps(good, ensure_ascii=False) + "\n", encoding="utf-8")

        ctx_apply = SimpleNamespace(root=str(root), labels=str(labels), splits=str(splits),
                                    apply=True, dry_run=False, report=False)
        rc = run(ctx_apply, quiet=True)
        rows = load_candidates(root / "candidates.jsonl")
        backups = list(root.glob("candidates.jsonl.bak-*"))
        check("--apply 写入 1 条并留备份", rc == 0 and len(rows) == 1 and len(backups) == 1)
        check("ground_truth 为 YOLO 行格式",
              isinstance(rows[0].get("ground_truth"), str) and len(rows[0]["ground_truth"].split()) == 5)

    print("结果：" + ("全部通过" if not failures else f"{len(failures)} 项失败"))
    return 0 if not failures else 2


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="校验并合并 golden 人工标注")
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="golden 目录")
    ap.add_argument("--labels", default=None, help="默认 <root>/labels_work.jsonl")
    ap.add_argument("--splits", default=None, help="splits.json，默认向上查找")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    ap.add_argument("--dry-run", action="store_true", help="只校验与打印 diff（默认）")
    ap.add_argument("--report", action="store_true", help="输出覆盖率报告")
    ap.add_argument("--self-test", action="store_true", help="临时目录自测，不碰真实数据")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.apply and args.dry_run:
        print("[错误] --apply 与 --dry-run 不能同时使用")
        return 2
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
