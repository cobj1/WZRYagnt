# -*- coding: utf-8 -*-
"""接线核验：Laya 打分器到底有没有进入生产决策路径。

为什么需要这个脚本
------------------
`m2_agent_v2.py --laya ensemble` 看起来接上了融合，但 `main()` 里只是调
`optimizer.set_scorer(...)`；而**生产决策函数 `decide()` 并不调用 `optimizer`**。
于是开关是"装饰性"的：模型跑了也没人用它。

实测（2026-10-26，120 帧）：把 beta 拉到 100.0 接上 scorer，`decide()` 输出
**120/120 帧完全不变**，scorer 侧 `submits=0`（`reorder()` 一次都没被调用）。

本脚本把这件事变成一条可复跑的静态检查：
  1. `decide()` 的函数体里是否出现 `optimizer.` / `solve(` / `_SCORER`；
  2. `set_scorer(` 的调用点是否只在 `main()` 等非决策函数里；
  3. 输出结论：融合对生产是否可达。

用法：
  python -X utf8 scripts/check_laya_wiring.py
  python -X utf8 scripts/check_laya_wiring.py --json runs/laya_wiring.json
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "scripts" / "m2_agent_v2.py"
DECISION_FNS = ("decide", "apply_action", "_policy_action_to_m2")

# 生产者侧：能真正触发融合的调用
REACH_MARKERS = ("optimizer.", "solve(", "_SCORER", "reorder(")


def find_fn(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def body_sources(src: str, node: ast.AST) -> str:
    return ast.get_source_segment(src, node) or ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    src = TARGET.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)

    decision_hits = {}
    for fn in DECISION_FNS:
        node = find_fn(tree, fn)
        if node is None:
            decision_hits[fn] = {"exists": False, "markers": []}
            continue
        seg = body_sources(src, node)
        hits = [m for m in REACH_MARKERS if m in seg]
        decision_hits[fn] = {
            "exists": True,
            "lineno": node.lineno,
            "end_lineno": getattr(node, "end_lineno", None),
            "lines": (getattr(node, "end_lineno", node.lineno) - node.lineno + 1),
            "markers": hits,
        }

    scorer_sites = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            fname = getattr(f, "attr", None) or getattr(f, "id", None)
            if fname == "set_scorer":
                scorer_sites.append({"lineno": node.lineno})

    owners = []
    for site in scorer_sites:
        owner = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.lineno <= site["lineno"] <= getattr(node, "end_lineno", node.lineno):
                if owner is None or node.lineno > owner.lineno:
                    owner = node
        site["owns"] = owner.name if owner else "<module>"
        owners.append(site["owns"])

    reachable = any(decision_hits.get(fn, {}).get("markers") for fn in DECISION_FNS)

    print(f"目标：{TARGET.relative_to(ROOT)}")
    for fn, info in decision_hits.items():
        if not info["exists"]:
            print(f"  - {fn}: 不存在")
        else:
            m = ", ".join(info["markers"]) or "无"
            print(f"  - {fn}(): {info['lines']} 行（{info['lineno']}–{info['end_lineno']}），"
                  f"可达标记：{m}")
    print(f"  - set_scorer 调用点 {len(scorer_sites)} 个，位于：{owners or '无'}")

    verdict = "reachable" if reachable else "unreachable"
    print()
    if reachable:
        print("结论：融合**可达**生产决策路径（决策函数里出现了 optimizer/solve/_SCORER）。")
    else:
        print("结论：融合**不可达**生产决策路径。")
        print("      `--laya` 只是设置了 `optimizer._SCORER`，而 decide() 从不调用 optimizer；")
        print("      要让融合生效，必须在 decide() 的某一分支里显式调用打分器（另需 shadow 验证）。")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "target": str(TARGET.relative_to(ROOT)),
            "decision_fns": decision_hits,
            "set_scorer_sites": scorer_sites,
            "verdict": verdict,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
