# -*- coding: utf-8 -*-
"""在自定义容器 (.db) 中定位拼接的 UnityFS AssetBundle 段。

王者荣耀的 assets/bin/Data/Package/builtin/<id>/<id>_0.db 是若干 UnityFS
bundle 顺序拼接而成（每段头里带 Unity 版本串 2022.3.5f1）。本脚本扫描
UnityFS 签名、解析每段的压缩/未压缩长度字段，据此切出可被 UnityPy 直接
读取的 .unity3d 片段。

UnityFS 头（大端）：
  "UnityFS\0" | i32 version | sz unityVersion | i64 size |
  u32 compressedBlocksInfoSize | u32 uncompressedBlocksInfoSize | u32 flags
"""
import struct
import sys
from pathlib import Path

SIG = b"UnityFS\0"


def parse(data: bytes, pos: int):
    if data[pos:pos + 8] != SIG:
        return None
    p = pos + 8
    version = struct.unpack_from(">i", data, p)[0]
    p += 4
    end = data.find(b"\0", p, p + 64)
    if end < 0:
        return None
    unity_ver = data[p:end].decode("ascii", "replace")
    p = end + 1
    size = struct.unpack_from(">q", data, p)[0]
    p += 8
    ci, ui, flags = struct.unpack_from(">III", data, p)
    return {
        "offset": pos, "version": version, "unity": unity_ver,
        "size": size, "blocks_info_c": ci, "blocks_info_u": ui, "flags": flags,
    }


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python find_bundles.py <container> [limit]")
        return 2
    p = Path(sys.argv[1])
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    data = p.read_bytes()
    print(f"{p.name} size={len(data)}")
    pos, n, shown = 0, 0, 0
    while True:
        pos = data.find(SIG, pos)
        if pos < 0:
            break
        n += 1
        if shown < limit:
            print("  ", parse(data, pos))
            shown += 1
        pos += 8
    print(f"UnityFS segments: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
