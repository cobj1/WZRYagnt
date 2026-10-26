#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""golden 标注台本地服务（仅标准库）。

启动：
    python tools/golden_labeler/server.py --root D:/work/aiKings/hokdata/golden --port 8765 --open

设计要点：
- 只扫描 root/images、root/images_test 下的图片，不复制、不修改任何原始数据。
- 预填框读取预填目录（实际是 YOLO txt：cls cx cy w h 归一化），也可读 json。
- 标注结果 append 到 root/labels_work.jsonl，undo 也写事件，文件永远只追加。
- 任何 split != test 的帧拒绝保存（golden 只允许 test）。
"""

import argparse
import hashlib
import json
import mimetypes
import os
import re
import struct
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = Path("D:/work/aiKings/hokdata/golden")
DEFAULT_CLASSES = [
    "enemy_hero", "ally_hero", "enemy_minion", "ally_minion",
    "enemy_turret", "ally_turret", "enemy_crystal", "ally_crystal",
    "neutral_monster", "hook_aim", "skill_effect",
]
IMAGE_DIRS = ("images", "images_test")
MAX_BODY = 2 * 1024 * 1024
FRAME_RE = re.compile(r"__frame_(\d+)")

_md5_cache = {}
_md5_lock = threading.Lock()
_manifest_cache = {"key": None, "data": None}
_write_lock = threading.Lock()


# ---------------------------------------------------------------- 基础工具

def md5_file(path):
    st = os.stat(path)
    key = (str(path), st.st_size, int(st.st_mtime))
    with _md5_lock:
        hit = _md5_cache.get(key)
    if hit:
        return hit
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    val = h.hexdigest()
    with _md5_lock:
        _md5_cache[key] = val
    return val


def image_size(path):
    """只解析文件头拿宽高，支持 PNG / JPEG，失败返回 (0, 0)。"""
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
                        h = int.from_bytes(data[1:3], "big")
                        w = int.from_bytes(data[3:5], "big")
                        return int(w), int(h)
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


def load_splits(root, explicit=None):
    candidates = [Path(explicit)] if explicit else [
        root / "splits.json",
        root.parent / "splits.json",
        root.parent.parent / "splits.json",
    ]
    for p in candidates:
        data = load_json(p)
        if isinstance(data, dict):
            return data, p
    return {}, None


def resolve_split(splits, session):
    """按 基础局 → 原始局 → wzry(session) 的顺序找 split。"""
    for dataset in ("tencent_rt_base", "tencent_rt", "wzry"):
        table = splits.get(dataset)
        if isinstance(table, dict) and session in table:
            return table[session], dataset
    return None, None


def load_candidates(root):
    """basename -> candidate 行。"""
    rows = {}
    p = root / "candidates.jsonl"
    if not p.exists():
        return rows
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            name = os.path.basename(str(row.get("image", "")).replace("\\", "/"))
            if name:
                rows[name] = row
    return rows


def read_events(path):
    events = []
    if not path.exists():
        return events
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("id"):
                events.append(ev)
    return events


def replay(events):
    """回放事件流，undo 撤销该 id 最近一条未撤销事件。"""
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
    return state, applied


def prefill_for(root, stem, width, height):
    """优先 prefill/*.txt（YOLO 归一化），再找 json。返回 (rel_source, boxes)。"""
    for sub in ("prefill", "prefill_test"):
        for ext in (".txt", ".json"):
            p = root / sub / (stem + ext)
            if not p.is_file():
                continue
            try:
                boxes = parse_prefill(p, width, height)
            except (OSError, ValueError):
                continue
            return f"{sub}/{p.name}", boxes
    return None, []


def parse_prefill(path, width, height):
    boxes = []
    if path.suffix.lower() == ".json":
        data = load_json(path, {})
        raw = data.get("boxes") if isinstance(data, dict) else data
        for b in raw or []:
            if isinstance(b, dict):
                cls = int(b.get("cls", b.get("class_id", 0)))
                x1, y1, x2, y2 = (float(b[k]) for k in ("x1", "y1", "x2", "y2"))
            else:
                cls = int(b[0])
                cx, cy, bw, bh = (float(v) for v in b[1:5])
                x1, y1 = (cx - bw / 2) * width, (cy - bh / 2) * height
                x2, y2 = (cx + bw / 2) * width, (cy + bh / 2) * height
            boxes.append({"cls": cls, "x1": x1, "y1": y1, "x2": x2, "y2": y2})
        return boxes
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) < 5:
                continue
            cls = int(float(parts[0]))
            cx, cy, bw, bh = (float(v) for v in parts[1:5])
            boxes.append({
                "cls": cls,
                "x1": (cx - bw / 2) * width,
                "y1": (cy - bh / 2) * height,
                "x2": (cx + bw / 2) * width,
                "y2": (cy + bh / 2) * height,
            })
    return boxes


def collect_images(root):
    """images 优先，其余 images_test 独有的补进来。"""
    seen = set()
    out = []
    for sub in IMAGE_DIRS:
        d = root / sub
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir(), key=lambda x: x.name):
            if not p.is_file() or p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".bmp"):
                continue
            if p.name in seen:
                continue
            seen.add(p.name)
            out.append((sub, p))
    return out


# ---------------------------------------------------------------- manifest

def build_manifest(cfg):
    root = cfg["root"]
    summary = load_json(root / "summary.json", {}) or {}
    class_names = summary.get("class_names") or DEFAULT_CLASSES
    splits, splits_path = load_splits(root, cfg.get("splits"))
    cands = load_candidates(root)
    events = read_events(root / "labels_work.jsonl")
    state, applied = replay(events)
    label_counts = {}

    items = []
    md5_groups = {}
    for sub, path in collect_images(root):
        name = path.name
        stem = path.stem
        session = name.split("__")[0]
        m = FRAME_RE.search(name)
        frame_id = int(m.group(1)) if m else None
        width, height = image_size(path)
        digest = md5_file(path)
        split, split_source = resolve_split(splits, session)
        p_src, p_boxes = prefill_for(root, stem, width, height)
        cand = cands.get(name)
        ev = state.get(f"{sub}/{name}")
        status = "unlabeled"
        boxes = []
        note = ""
        labeled_at = None
        if ev:
            op = ev.get("op", "save")
            status = {"save": "saved", "skip": "skip", "flag": "flag"}.get(op, "saved")
            boxes = ev.get("boxes") or []
            note = ev.get("note") or ""
            labeled_at = ev.get("ts")
            label_counts[status] = label_counts.get(status, 0) + 1
        md5_groups.setdefault(digest, []).append(f"{sub}/{name}")
        items.append({
            "id": f"{sub}/{name}",
            "name": name,
            "image": f"{sub}/{name}",
            "session": session,
            "game_id": session,
            "frame_id": frame_id,
            "split": split or "unknown",
            "split_source": split_source,
            "md5": digest,
            "width": width,
            "height": height,
            "prefill_source": p_src,
            "prefill": p_boxes,
            "in_candidates": cand is not None,
            "candidate_verified": bool(cand.get("verified")) if cand else None,
            "status": status,
            "boxes": boxes,
            "note": note,
            "labeled_at": labeled_at,
            "dup_count": 0,
        })

    dup_groups = {}
    for digest, ids in md5_groups.items():
        if len(ids) > 1:
            dup_groups[digest] = ids
    for it in items:
        it["dup_count"] = len(md5_groups.get(it["md5"], []))

    by_split = {}
    for it in items:
        by_split[it["split"]] = by_split.get(it["split"], 0) + 1
    counts = {
        "total": len(items),
        "test": by_split.get("test", 0),
        "train": by_split.get("train", 0),
        "val": by_split.get("val", 0),
        "unknown": by_split.get("unknown", 0),
        "saved": label_counts.get("saved", 0),
        "skip": label_counts.get("skip", 0),
        "flag": label_counts.get("flag", 0),
        "dup_groups": len(dup_groups),
        "dup_images": sum(len(v) for v in dup_groups.values()),
        "candidates": len(cands),
    }
    return {
        "root": str(root),
        "splits_file": str(splits_path) if splits_path else None,
        "class_names": class_names,
        "labeler": cfg["labeler"],
        "labels_file": str(root / "labels_work.jsonl"),
        "counts": counts,
        "items": items,
    }


def get_manifest(cfg, force=False):
    root = cfg["root"]
    key = (str(root), cfg.get("splits"))
    if not force and _manifest_cache["data"] is not None and _manifest_cache["key"] == key:
        age = time.time() - _manifest_cache.get("ts", 0)
        if age < 5:
            return _manifest_cache["data"]
    data = build_manifest(cfg)
    _manifest_cache.update({"key": key, "data": data, "ts": time.time()})
    return data


def invalidate_manifest():
    _manifest_cache["data"] = None


# ---------------------------------------------------------------- 校验

def as_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def validate_boxes(boxes, width, height, n_classes):
    if not isinstance(boxes, list):
        return "boxes 必须是数组"
    for i, b in enumerate(boxes):
        if not isinstance(b, dict):
            return f"框 #{i} 不是对象"
        cls = b.get("cls")
        if not isinstance(cls, int) or isinstance(cls, bool) or not 0 <= cls < n_classes:
            return f"框 #{i} 类别非法：{cls!r}"
        for k in ("x1", "y1", "x2", "y2"):
            if not as_number(b.get(k)):
                return f"框 #{i} 的 {k} 不是数值：{b.get(k)!r}"
        if not (0 <= b["x1"] < b["x2"] <= width and 0 <= b["y1"] < b["y2"] <= height):
            return f"框 #{i} 坐标越界或退化：({b['x1']:.1f},{b['y1']:.1f})-({b['x2']:.1f},{b['y2']:.1f})"
    return None


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    cfg = None
    server_version = "GoldenLabeler/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code, obj):
        self._send(code, obj)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path in ("/", "/index.html"):
            page = HERE / "index.html"
            if not page.is_file():
                self._json(500, {"ok": False, "error": "index.html 缺失"})
                return
            self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/manifest":
            try:
                self._json(200, get_manifest(self.cfg, force="reload=1" in (parsed.query or "")))
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"ok": False, "error": f"构建 manifest 失败：{exc}"})
            return
        if path.startswith("/img/"):
            self._serve_image(path[len("/img/"):])
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        self._json(404, {"ok": False, "error": "not found"})

    def _serve_image(self, raw_name):
        name = os.path.basename(raw_name.replace("\\", "/"))
        root = self.cfg["root"]
        target = None
        for sub in IMAGE_DIRS:
            p = root / sub / name
            if p.is_file():
                target = p
                break
        if target is None:
            self._json(404, {"ok": False, "error": f"图片不存在：{name}"})
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        try:
            data = target.read_bytes()
        except OSError as exc:
            self._json(500, {"ok": False, "error": str(exc)})
            return
        self._send(200, data, ctype, {"Cache-Control": "public, max-age=3600"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/save", "/api/undo"):
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._json(400, {"ok": False, "error": "请求体为空或过大"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json(400, {"ok": False, "error": "JSON 解析失败"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"ok": False, "error": "请求体必须是对象"})
            return
        if path == "/api/undo":
            self._undo(payload)
        else:
            self._save(payload)

    def _find_item(self, item_id, manifest):
        for it in manifest["items"]:
            if it["id"] == item_id or it["name"] == item_id:
                return it
        return None

    def _save(self, payload):
        manifest = get_manifest(self.cfg)
        item_id = str(payload.get("id") or "")
        item = self._find_item(item_id, manifest)
        if item is None:
            self._json(404, {"ok": False, "error": f"未知条目：{item_id}"})
            return
        action = str(payload.get("action") or "save")
        if action not in ("save", "skip", "flag"):
            self._json(400, {"ok": False, "error": f"未知 action：{action}"})
            return
        labeler = str(payload.get("labeler") or self.cfg["labeler"])[:64]
        note = str(payload.get("note") or "")[:500]
        boxes = payload.get("boxes") or []
        verified = payload.get("verified") is True
        ts = datetime.now().astimezone().isoformat(timespec="seconds")

        if action == "save":
            if item["split"] != "test":
                self._json(409, {
                    "ok": False,
                    "error": f"split={item['split']}，golden 只允许 test 帧",
                    "code": "split_not_test",
                })
                return
            if not verified:
                self._json(400, {"ok": False, "error": "verified 必须为 true", "code": "not_verified"})
                return
            err = validate_boxes(boxes, item["width"], item["height"], len(manifest["class_names"]))
            if err:
                self._json(400, {"ok": False, "error": err, "code": "schema"})
                return
            clean = [
                {"cls": int(b["cls"]), "x1": round(float(b["x1"]), 2), "y1": round(float(b["y1"]), 2),
                 "x2": round(float(b["x2"]), 2), "y2": round(float(b["y2"]), 2)}
                for b in boxes
            ]
        else:
            clean = []
            if action == "skip":
                verified = False

        ev = {
            "op": action,
            "id": item["id"],
            "image": item["image"],
            "session": item["session"],
            "split": item["split"],
            "source_image_md5": item["md5"],
            "width": item["width"],
            "height": item["height"],
            "boxes": clean,
            "verified": bool(verified),
            "note": note,
            "ts": ts,
            "labeler": labeler,
        }
        try:
            append_event(self.cfg["root"] / "labels_work.jsonl", ev)
        except OSError as exc:
            self._json(500, {"ok": False, "error": f"写入失败：{exc}"})
            return
        invalidate_manifest()
        self._json(200, {"ok": True, "id": item["id"], "op": action, "ts": ts, "saved_boxes": len(clean)})

    def _undo(self, payload):
        manifest = get_manifest(self.cfg)
        item_id = str(payload.get("id") or "")
        item = self._find_item(item_id, manifest)
        if item is None:
            self._json(404, {"ok": False, "error": f"未知条目：{item_id}"})
            return
        labeler = str(payload.get("labeler") or self.cfg["labeler"])[:64]
        ts = datetime.now().astimezone().isoformat(timespec="seconds")
        ev = {
            "op": "undo",
            "id": item["id"],
            "image": item["image"],
            "ts": ts,
            "labeler": labeler,
            "note": str(payload.get("note") or "")[:200],
        }
        try:
            append_event(self.cfg["root"] / "labels_work.jsonl", ev)
        except OSError as exc:
            self._json(500, {"ok": False, "error": f"写入失败：{exc}"})
            return
        invalidate_manifest()
        self._json(200, {"ok": True, "id": item["id"], "op": "undo", "ts": ts})


def append_event(path, event):
    line = json.dumps(event, ensure_ascii=False) + "\n"
    with _write_lock:
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())


# ---------------------------------------------------------------- 启动

def main(argv=None):
    ap = argparse.ArgumentParser(description="golden 标注台（标准库，本地运行）")
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="golden 目录")
    ap.add_argument("--splits", default=None, help="splits.json 路径，默认自动向上查找")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    ap.add_argument("--labeler", default=os.environ.get("USERNAME") or os.environ.get("USER") or "anon")
    args = ap.parse_args(argv)

    root = Path(args.root).expanduser()
    if not root.is_dir():
        print(f"[错误] --root 不存在：{root}")
        return 2
    if not (root / "images").is_dir():
        print(f"[警告] {root/'images'} 不存在，只有 images_test 可用")

    cfg = {"root": root, "splits": args.splits, "labeler": args.labeler}
    Handler.cfg = cfg

    print("golden 标注台")
    print(f"  root    : {root}")
    try:
        m = get_manifest(cfg, force=True)
        c = m["counts"]
        print(f"  图片    : {c['total']} 张（test {c['test']} / train {c['train']} / val {c['val']}）")
        print(f"  预填    : {sum(1 for it in m['items'] if it['prefill'])} 张有预填框")
        print(f"  已标注  : {c['saved']}（skip {c['skip']} / flag {c['flag']}）")
        print(f"  MD5 重复: {c['dup_groups']} 组 / {c['dup_images']} 张")
        if m["splits_file"]:
            print(f"  splits  : {m['splits_file']}")
    except Exception as exc:  # noqa: BLE001
        print(f"[警告] 预扫描失败：{exc}")

    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print(f"[错误] 端口 {args.port} 无法监听：{exc}")
        return 2

    url = f"http://{args.host}:{args.port}/"
    print(f"\n  打开 {url}    (Ctrl+C 退出)\n")
    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
