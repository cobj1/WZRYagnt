# -*- coding: utf-8 -*-
"""批量扫描资源容器，定位 AI 配置与模型文件所在的包。

用法：python scan_containers.py <file> [file ...]
"""
import re
import sys
from pathlib import Path

PATS = [b"AILab/", b"tactics_config", b"game_ai_model", b"prototxt", b".weights",
        b"ai_config_rl", b"ssg_config", b"obstacle_config", b"skill_manager_config",
        b"hero_lane", b"multi_task"]
PATH_RE = re.compile(rb"AILab/[A-Za-z0-9_./-]{3,140}")


def main() -> int:
    for name in sys.argv[1:]:
        p = Path(name)
        data = p.read_bytes()
        print(f"=== {p.name} ({len(data)} bytes) ===")
        for pat in PATS:
            total, first, pos = 0, [], 0
            while True:
                pos = data.find(pat, pos)
                if pos < 0:
                    break
                total += 1
                if len(first) < 3:
                    first.append(pos)
                pos += len(pat)
            if total:
                print(f"    {pat.decode():<22} count={total} first={first}")
        paths = sorted({m.group(0).decode("ascii") for m in PATH_RE.finditer(data)})
        if paths:
            print(f"    AILab paths ({len(paths)}):")
            for s in paths:
                print("      ", s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
