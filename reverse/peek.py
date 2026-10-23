# -*- coding: utf-8 -*-
"""查看 live 内存某地址的内容并按 (u32 count, u32 pad, u64 ptr) 对解析。

用法：python peek.py <live_addr_hex> [len]
"""
import struct
import sys

from livemem import LiveMem

m = LiveMem()
if not m.find_pid():
    print("未找到游戏进程")
    raise SystemExit(1)
m.refresh_maps()

addr = int(sys.argv[1], 0)
ln = int(sys.argv[2]) if len(sys.argv) > 2 else 160
blob = m.read(addr, ln)
print(f"live 0x{addr:x}  len={len(blob)}")
print("--- raw u64 ---")
for i in range(0, len(blob) // 8):
    v, = struct.unpack_from("<Q", blob, i * 8)
    seg = next((f"{s:x}-{e:x}" for s, e, _o, _p in m.maps if s <= v < e), "")
    print(f"  +0x{i*8:03x}: 0x{v:016x}  {seg}")
print("--- as (count,pad,ptr) pairs ---")
for i in range(0, len(blob) // 16):
    c, p, q = struct.unpack_from("<IIQ", blob, i * 16)
    hatch = "  <= " if (p == 0 and 1 <= c <= 2_000_000) else ""
    print(f"  [{i}] count={c:<10} pad={p} ptr=0x{q:x}{hatch}")
