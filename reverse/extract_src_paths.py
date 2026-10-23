# -*- coding: utf-8 -*-
"""从 IL2CPP 元数据字符串索引中抽取编译期源码路径，重建 GameCore 源码树。

IL2CPP 的 global-metadata.dat 会把每个类型的定义文件路径（含 .cs 文件名）
作为字符串保留下来，文件名等于构建机上的真实路径。据此可以在不解包任何
资源容器的前提下，拿到游戏（含 AI/行为树）的完整源码文件清单。
"""
import re
import sys
from collections import Counter
from pathlib import Path

PATH_RE = re.compile(r"(?:[A-Za-z]:)?\\?(?:[A-Za-z0-9_.\- ]+\\)+[A-Za-z0-9_\-]+\.cs")
KEYS = ("AI", "Bot", "Robot", "Behavior", "Behaviour", "Commander", "Offline",
        "JueWu", "Logic", "Agent", "Skill", "Decision")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python extract_src_paths.py <metadata_strings.txt> [out_dir]")
        return 2
    src = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else src.parent
    text = src.read_text(encoding="utf-8", errors="ignore")

    hit = sorted({m.group(0).replace("\\", "/") for m in PATH_RE.finditer(text)})
    print(f".cs paths found : {len(hit)}")

    (out / "src_paths.txt").write_text("\n".join(hit), encoding="utf-8")
    print(f"written         : {out / 'src_paths.txt'}")

    dirs = Counter()
    for p in hit:
        d = p.rsplit("/", 1)[0]
        dirs[d] += 1
    print("--- top 40 dirs by file count ---")
    for d, n in dirs.most_common(40):
        print(f"{n:>5}  {d}")

    print("--- keyword counts among .cs paths ---")
    for k in KEYS:
        kl = k.lower()
        n = sum(1 for p in hit if kl in p.lower())
        print(f"{k:>12}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
