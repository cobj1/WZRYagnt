# -*- coding: utf-8 -*-
"""内存感知 live pipeline（实验）：不截图，直接用 MemSource 驱动决策优化器。

用法：
  python scripts/mem_live_pipeline.py --hero 106
  python scripts/mem_live_pipeline.py --hero 106 --interval 0.25 --loop 20

说明：
  - 只读 /proc/<pid>/mem，不注入、不改客户端。
  - 当前只有英雄有世界坐标；小兵/塔位置仍缺，to_game_state 会自动过滤掉。
  - 这是**旁路验证脚本**，不替换现有视觉主循环。
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wzry.vision.mem_source import MemSource          # noqa: E402
from wzry.decision.optimizer import solve as opt_solve  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hero", type=int, default=None,
                    help="自己的英雄配置 ID（如小乔=106）；不给则取第一个蓝方英雄")
    ap.add_argument("--camp", default="blue", choices=("blue", "red"))
    ap.add_argument("--interval", type=float, default=0.3)
    ap.add_argument("--loop", type=int, default=20)
    ap.add_argument("--serial", default="127.0.0.1:16384")
    a = ap.parse_args()

    src = MemSource(serial=a.serial)
    print("pid", src.connect(), flush=True)
    self_id = a.hero
    last = None
    for k in range(a.loop):
        t0 = time.time()
        st = src.to_game_state(self_config_id=self_id)
        units = st.to_dict()["units"]
        heroes = [u for u in units if u.get("kind") == "hero"
                  or str(u.get("cls", "")).endswith("hero")]
        self_u = None
        for u in heroes:
            if self_id is not None and (u.get("hero_cid") == self_id
                                        or u.get("id") == self_id):
                self_u = u
                break
        if self_u is None and heroes:
            self_u = next((u for u in heroes if u.get("camp") == a.camp), heroes[0])
            if self_id is None:
                self_id = self_u.get("hero_cid") or self_u.get("id")
        hp = float(self_u.get("hp") or 1.0) if self_u else 1.0
        camp = (self_u or {}).get("camp") or a.camp
        state = st.to_dict()
        state["ui"] = {"hp": hp}
        action = opt_solve(state, {"type": "none", "reason": "memory_bootstrap"},
                           hp, camp)
        dt = (time.time() - t0) * 1000
        pos = (self_u or {}).get("pos_minimap") or (self_u or {}).get("screen")
        print(f"[{k:02d}] {dt:5.0f}ms units={len(units)} heroes={len(heroes)} "
              f"self={self_id} hp={hp:.2f} pos={pos and [round(x, 2) for x in pos[:2]]} "
              f"-> {action.get('type')} {action.get('reason', '')}", flush=True)
        if action is not last and action.get("type") not in (None, "none"):
            last = action
        time.sleep(max(0.0, a.interval - (time.time() - t0)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
