# -*- coding: utf-8 -*-
"""ELF64 地址换算的最小公共模块（**不依赖 capstone**）。

存在的唯一原因：`.text` 的 VA 与文件偏移不相等（本仓 libGameCore.so 差 0x1000），
把 VA 当文件偏移读会整体偏移 256 条 ARM64 指令。这类 bug 曾让 52 条函数入口
被误判成"函数中段"（见 `docs/反汇编地址口径勘误-2026-10-28.md`）。

所有读 libGameCore.so 的脚本都应走这里，不要自己写 `data[addr:]`。
"""
from __future__ import annotations

import struct

PT_LOAD = 1


def va_to_off(data: bytes, va: int) -> int:
    """VA → 文件偏移（按 PT_LOAD 段映射）。不在任何段内返回 -1。"""
    e_phoff, = struct.unpack_from("<Q", data, 0x20)
    e_phentsize, = struct.unpack_from("<H", data, 0x36)
    e_phnum, = struct.unpack_from("<H", data, 0x38)
    for i in range(e_phnum):
        o = e_phoff + i * e_phentsize
        p_type, = struct.unpack_from("<I", data, o)
        if p_type != PT_LOAD:
            continue
        p_offset, = struct.unpack_from("<Q", data, o + 0x08)
        p_vaddr, = struct.unpack_from("<Q", data, o + 0x10)
        p_filesz, = struct.unpack_from("<Q", data, o + 0x20)
        if p_vaddr <= va < p_vaddr + p_filesz:
            return p_offset + (va - p_vaddr)
    return -1


def sections(data: bytes) -> dict[str, dict]:
    """段表 → {name: {addr, off, size}}。"""
    e_shoff, = struct.unpack_from("<Q", data, 0x28)
    e_shentsize, = struct.unpack_from("<H", data, 0x3A)
    e_shnum, = struct.unpack_from("<H", data, 0x3C)
    e_shstrndx, = struct.unpack_from("<H", data, 0x3E)
    base = struct.unpack_from("<IIQQQQIIQQ", data, e_shoff + e_shstrndx * e_shentsize)[4]
    out = {}
    for i in range(e_shnum):
        o = e_shoff + i * e_shentsize
        nm, t, f, a, off, sz, lk, inf, al, es = struct.unpack_from("<IIQQQQIIQQ", data, o)
        end = data.find(b"\0", base + nm)
        name = data[base + nm:end].decode("ascii", "replace")
        out[name] = {"addr": a, "off": off, "size": sz}
    return out


def text_section(data: bytes) -> dict:
    return sections(data)[".text"]


def read_word(data: bytes, va: int) -> int | None:
    """按 VA 读 32 位小端（自动换算偏移）。越界返回 None。"""
    off = va_to_off(data, va)
    if off < 0 or off + 4 > len(data):
        return None
    return struct.unpack_from("<I", data, off)[0]
