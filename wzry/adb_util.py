# -*- coding: utf-8 -*-
"""adb 可执行文件定位（采集层 / 控制层共用）。

本机 PATH 里没有 adb（MuMu 自带的 adb 在 `nx_main` 目录下），旧代码在
`control/executor.py` 与 `capture/screencap_capture.py` 里都直接写死 `"adb"`，
导致所有 adb 调用（截图、点击、滑动）静默失败。统一走这里解析。
"""
import os
import shutil

_ADB_CANDIDATES = [
    r"D:\app\MuMuPlayer\nx_main\adb.exe",
    r"C:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe",
    r"C:\Program Files\Netease\MuMuPlayerGlobal-12.0\shell\adb.exe",
]


def resolve_adb():
    """按优先级定位 adb：环境变量 WZRY_ADB > PATH > 常见 MuMu 安装路径。
    都找不到时回落 "adb"（交给调用方自行处理失败）。
    """
    env = os.environ.get("WZRY_ADB")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("adb")
    if found:
        return found
    for p in _ADB_CANDIDATES:
        if os.path.isfile(p):
            return p
    return "adb"
