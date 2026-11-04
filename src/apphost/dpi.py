# -*- coding: utf-8 -*-
"""apphost.dpi —— Windows 高分屏 Per-Monitor V2 DPI 感知。

对应《项目准则.md》第 8-16 条：通知 DWM 绝不进行位图拉伸
（DPI Virtualization），窗口缩放变化或跨屏移动时由应用按物理像素自行重绘，
杜绝文字边缘发虚、图标模糊发灰。

必须在任何窗口创建之前调用。返回实际生效的感知等级：
  2 = Per-Monitor V2（准则要求，理想）
  1 = Per-Monitor V1（回退）
  0 = System DPI Aware 或设置失败
"""

from __future__ import annotations

import ctypes
import sys

# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == (DPI_AWARENESS_CONTEXT)(-4)
_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
_PROCESS_PER_MONITOR_DPI_AWARE = 2


def enable_dpi_awareness() -> int:
    """启用尽可能高的 DPI 感知等级，返回生效等级（见模块文档）。"""
    if sys.platform != "win32":
        return 0

    # 首选：Per-Monitor V2（准则 10/14）
    try:
        fn = ctypes.windll.user32.SetProcessDpiAwarenessContext
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = ctypes.c_bool
        if fn(_PER_MONITOR_AWARE_V2):
            return 2
    except Exception:
        pass

    # 回退 1：Per-Monitor V1（shcore，旧实现 QR/src/utils.py 用的等级）
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(_PROCESS_PER_MONITOR_DPI_AWARE) == 0:
            return 1
    except Exception:
        pass

    # 回退 2：System DPI Aware（user32）
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            return 0
    except Exception:
        pass

    return 0
