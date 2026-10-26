# -*- coding: utf-8 -*-
"""数据证据链对账：源会话 → 行为数据集 → 切分 → golden 集。

用途：把"数据漂移"从人工排查变成一条命令。任何一处不一致都会给出精确
位置和数量，并可用退出码接入 CI（0 干净 / 1 有告警 / 2 有漂移）。

检查项
  1. 源会话集合（data/matches/*）与 dataset_behavior_v2.jsonl 的 session 集合是否一致
  2. 每个 session 的帧数是否逐一对齐（这是最容易悄悄漂移的地方）
  3. splits.json 的 wzry 键是否覆盖全部源会话，且无按 `#` 后缀产生的跨 split
  4. golden/candidates.jsonl：verified 比例、prefill_source 分布、split 是否全 test、
     图片是否存在、MD5 是否唯一
  5. 是否还有可复抽帧的媒体源（mp4/mkv/avi）；没有就说明 golden 扩量受源数据限制

运行：
    venv/Scripts/python.exe -X utf8 scripts/reconcile_behavior_dataset.py
    venv/Scripts/python.exe -X utf8 scripts/reconcile_behavior_dataset.py --json ../hokdata/reconcile_report.json
    venv/Scripts/python.exe -X utf8 scripts/reconcile_behavior_dataset.py --strict
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # WZRYagnt/
WS = ROOT.parent                                    # aiKings/
MATCHES = ROOT / "data" / "matches"
HOKDATA = WS / "hokdata"
DATASET = HOKDATA / "behavior" / "dataset_behavior_v2.jsonl"
SPLITS = HOKDATA / "splits.json"
GOLDEN = HOKDATA / "golden"
MEDIA_EXT = {".mp4", ".mkv", ".avi", ".mov", ".flv"}


def count_lines(path: Path) -> int:
    if not path.exists():
        return -1
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        return sum(1 for line in fh if line.strip())


def md5_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def scan_sources() -> dict[str, int]:
    out: dict[str, int] = {}
    if not MATCHES.is_dir():
        return out
    for d in sorted(MATCHES.iterdir()):
        if d.is_dir():
            out[d.name] = count_lines(d / "states.jsonl")
    return out


def scan_dataset() -> tuple[collections.Counter, dict[str, int]]:
    counts: collections.Counter = collections.Counter()
    bad = {"blank": 0, "json_error": 0}
    if not DATASET.exists():
        return counts, bad
    with DATASET.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                bad["blank"] += 1
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                bad["json_error"] += 1
                continue
            counts[str(rec.get("session") or "<missing>")] += 1
    return counts, bad


def load_splits() -> dict:
    if not SPLITS.exists():
        return {}
    return json.loads(SPLITS.read_text(encoding="utf-8"))


def check_wzry_split(sessions, splits) -> dict:
    table = splits.get("wzry") or {}
    missing = sorted(s for s in sessions if s not in table)
    extra = sorted(s for s in table if s not in sessions)
    # 同一基础局的多个视角必须同 split（wzry 侧目前无 # 后缀，仍检查一次防回归）
    base_to_splits: dict[str, set] = collections.defaultdict(set)
    for key, split in table.items():
        base_to_splits[str(key).split("#", 1)[0]].add(split)
    leakage = {b: sorted(v) for b, v in base_to_splits.items() if len(v) > 1}
    return {
        "sessions": len(table),
        "missing_in_splits": missing,
        "extra_in_splits": extra,
        "cross_split_bases": leakage,
        "distribution": dict(collections.Counter(table.values())),
    }


def check_golden(splits) -> dict:
    cand_path = GOLDEN / "candidates.jsonl"
    report = {
        "candidates": 0,
        "verified": 0,
        "prefill_source": {},
        "splits": {},
        "missing_image": [],
        "md5_total": 0,
        "md5_unique": 0,
        "md5_dup_groups": 0,
        "md5_extra_bytes": 0,
        "images_on_disk": {},
        "media_sources": 0,
        "expandable": False,
    }
    if not cand_path.exists():
        report["error"] = "candidates.jsonl 不存在"
        return report

    wzry = (splits.get("wzry") or {})
    prefill = collections.Counter()
    split_cnt = collections.Counter()
    md5_map: dict[str, list[str]] = collections.defaultdict(list)
    with cand_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            rec = json.loads(line)
            report["candidates"] += 1
            if rec.get("verified"):
                report["verified"] += 1
            prefill[str(rec.get("prefill_source"))] += 1
            split = rec.get("split") or wzry.get(str(rec.get("session")), "<unknown>")
            split_cnt[str(split)] += 1
            img = rec.get("image")
            if not img:
                continue
            p = Path(str(img))
            if not p.is_absolute():
                p = WS / p
            if not p.exists():
                report["missing_image"].append(str(img))
                continue
            md5_map[md5_file(p)].append(p.name)

    report["prefill_source"] = dict(prefill)
    report["splits"] = dict(split_cnt)
    report["md5_total"] = sum(len(v) for v in md5_map.values())
    report["md5_unique"] = len(md5_map)
    dups = {k: v for k, v in md5_map.items() if len(v) > 1}
    report["md5_dup_groups"] = len(dups)
    report["md5_extra_bytes"] = sum(len(v) - 1 for v in dups.values())
    for sub in ("images", "images_test", "prefill", "prefill_test"):
        d = GOLDEN / sub
        report["images_on_disk"][sub] = sum(1 for _ in d.iterdir()) if d.is_dir() else 0

    media = 0
    if MATCHES.is_dir():
        for d in MATCHES.iterdir():
            if d.is_dir():
                media += sum(1 for f in d.rglob("*") if f.suffix.lower() in MEDIA_EXT)
    report["media_sources"] = media

    # 源目录里按 split 还有多少帧图可用（决定 golden 能否扩量）
    wzry_map = splits.get("wzry") or {}
    frames_by_split: collections.Counter = collections.Counter()
    uniq_by_split: dict[str, set] = collections.defaultdict(set)
    if MATCHES.is_dir():
        for d in MATCHES.iterdir():
            fdir = d / "frames"
            if not (d.is_dir() and fdir.is_dir()):
                continue
            split = str(wzry_map.get(d.name, "<unknown>"))
            for f in fdir.iterdir():
                if f.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
                    continue
                frames_by_split[split] += 1
                uniq_by_split[split].add(md5_file(f, 1 << 16))
    report["frames_on_disk"] = dict(frames_by_split)
    report["frames_on_disk_unique"] = {k: len(v) for k, v in uniq_by_split.items()}
    test_headroom = report["frames_on_disk_unique"].get("test", 0) - report["candidates"]
    report["test_headroom"] = test_headroom
    report["expandable"] = media > 0 or test_headroom > 0
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="", help="把结果写到该路径")
    ap.add_argument("--strict", action="store_true", help="有告警也返回非 0")
    args = ap.parse_args()

    src = scan_sources()
    ds, bad = scan_dataset()
    splits = load_splits()

    src_frames = sum(v for v in src.values() if v >= 0)
    ds_frames = sum(ds.values())
    only_src = sorted(set(src) - set(ds))
    only_ds = sorted(set(ds) - set(src))
    mismatch = sorted((k, src[k], ds[k]) for k in set(src) & set(ds) if src[k] != ds[k])

    drift = bool(only_src or only_ds or mismatch or bad["json_error"])
    split_report = check_wzry_split(src, splits)
    golden = check_golden(splits)

    warn = []
    if split_report["missing_in_splits"]:
        warn.append(f"{len(split_report['missing_in_splits'])} 个源会话不在 splits.wzry")
    if split_report["cross_split_bases"]:
        warn.append(f"{len(split_report['cross_split_bases'])} 个基础局跨 split")
    if golden["candidates"] and golden["verified"] == 0:
        warn.append("golden 已验证帧 = 0（评估集没有人工真值）")
    if golden["splits"].get("train") or golden["splits"].get("val"):
        warn.append("golden 含非 test 帧（会污染感知评估）")
    if golden["md5_dup_groups"]:
        warn.append(f"golden 有 {golden['md5_dup_groups']} 组重复图（多出 {golden['md5_extra_bytes']} 张）")
    if not golden["expandable"]:
        warn.append("无媒体源（mp4/mkv/avi），golden 无法重新抽帧扩量")

    print("=== 1. 源会话 vs 行为数据集 ===")
    print(f"  源会话 {len(src)} / {src_frames} 帧；数据集 {len(ds)} / {ds_frames} 帧")
    print(f"  仅源有: {only_src or '无'}；仅数据集有: {only_ds or '无'}")
    print(f"  帧数不一致: {mismatch or '无'}；坏行: {bad}")
    print("\n=== 2. splits.json(wzry) ===")
    print(f"  {split_report['sessions']} 条，分布 {split_report['distribution']}")
    print(f"  缺: {split_report['missing_in_splits'] or '无'}；多: {split_report['extra_in_splits'] or '无'}")
    print(f"  跨 split 基础局: {split_report['cross_split_bases'] or '无'}")
    print("\n=== 3. golden ===")
    print(f"  candidates {golden['candidates']} / verified {golden['verified']}"
          f" / prefill {golden['prefill_source']} / split {golden['splits']}")
    print(f"  MD5: {golden['md5_unique']} 唯一 / {golden['md5_total']} 张"
          f"；重复组 {golden['md5_dup_groups']}（多 {golden['md5_extra_bytes']} 张）")
    print(f"  磁盘: {golden['images_on_disk']}；缺图 {len(golden['missing_image'])}")
    print(f"  媒体源 {golden['media_sources']} 个；源帧图 {golden['frames_on_disk']}"
          f"（去重 {golden['frames_on_disk_unique']}）；test 余量 {golden['test_headroom']} 帧")
    print(f"  可扩量: {golden['expandable']}")
    print("\n=== 结论 ===")
    print(f"  数据漂移: {'有' if drift else '无'}")
    for w in warn:
        print(f"  [警告] {w}")
    if not warn:
        print("  无告警")

    payload = {
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "source": {"sessions": len(src), "frames": src_frames, "per_session": src},
        "dataset": {"path": str(DATASET), "sessions": len(ds), "frames": ds_frames,
                    "per_session": dict(ds), "bad_lines": bad},
        "drift": {
            "only_in_source": only_src,
            "only_in_dataset": only_ds,
            "frame_count_mismatch": [{"session": k, "source": a, "dataset": b}
                                     for k, a, b in mismatch],
        },
        "splits_wzry": split_report,
        "golden": golden,
        "warnings": warn,
        "verdict": "drift" if drift else ("warn" if warn else "clean"),
    }
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出 {out}")

    if drift:
        return 2
    return 1 if (warn and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
