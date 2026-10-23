# -*- coding: utf-8 -*-
"""扫描内存中的 AI 敌方记录（0x3f0 步长）。

静态依据（0x28117e8 附近的日志代码）：
  record + 0x04  = 英雄配置 ID
  record + 0x18  = 英雄配置 ID（另一处）
  record + 0x58  = 计数（<=0 时打印 IsDeath）
  record + 0x20/0x24/0x28 = 三个 int32 坐标
  record 之间步长 0x3f0，由 ctx+0x68..+0x70 的 vector 管理

用法：python find_ai_records.py 106 163 152 ...
"""
import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from livemem import LiveMem  # noqa: E402


def main():
    ids = [int(x) for x in sys.argv[1:]] or [106]
    print("ids", ids)
    m = LiveMem()
    m.find_pid()
    m.refresh_maps()

    regions = []
    for st, en, _off, p in m.maps:
        if not p:
            continue
        if (p in ("[anon:scudo:primary]", "[anon:scudo:secondary]")
                or p.startswith("[anon:Mem_")):
            if en - st < 300 * 1024 * 1024:
                regions.append((st, en, p))
    # .bss（libGameCore 之后的匿名区）
    segs = m.segments("libGameCore.so")
    if segs:
        bss_start = max(s[1] for s in segs)
        for st, en, _off, p in m.maps:
            if st <= bss_start + 0x1000 < en:
                regions.append((st, en, "[bss]"))
                break
    print("regions", len(regions), "total MB",
          sum(en - st for st, en, _ in regions) / 1e6)

    idset = np.array(ids, dtype=np.uint32)
    CH = 0x800000
    hits = []
    for st, en, name in regions:
        addr = st
        while addr < en:
            n = min(CH, en - addr)
            buf = m.read(addr, n)
            if len(buf) < 0x3f0:
                break
            u = np.frombuffer(buf, dtype=np.uint32)
            lim = len(u) - 0x3f0 // 4
            if lim <= 0:
                break
            idx = np.arange(0, lim)
            a_id = u[idx + 1]
            b_id = u[idx + 6]
            x = u[idx + 8]
            y = u[idx + 9]
            z = u[idx + 10]
            cnt = u[idx + 22]
            # 位置至少有一个轴 >1000（排除 ID 表/元数据）；x,z 都 >1000
            mask = ((np.isin(a_id, idset) | np.isin(b_id, idset))
                    & (x > 1000) & (z > 1000)
                    & (x < 200000) & (y < 200000) & (z < 200000)
                    & (cnt > 0) & (cnt < 200))
            for i in idx[mask][:30]:
                p = addr + int(i) * 4
                rec = m.read(p, 0x60)
                print(f"hit 0x{p:x} region={name} id={struct.unpack_from('<i', rec, 4)[0]} "
                      f"b={struct.unpack_from('<i', rec, 0x18)[0]} "
                      f"pos=({struct.unpack_from('<i', rec, 0x20)[0]},"
                      f"{struct.unpack_from('<i', rec, 0x24)[0]},"
                      f"{struct.unpack_from('<i', rec, 0x28)[0]}) "
                      f"cnt={struct.unpack_from('<i', rec, 0x58)[0]}")
                hits.append(p)
            addr += n
    print("done hits", len(hits))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
