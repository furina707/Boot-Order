# -*- coding: utf-8 -*-
"""apphost —— 桌面应用公共启动底座（单一真源）。

本包是「逻辑统一」的核心：把过去散落在各项目 run.py 中的进程级样板
（UTF-8 控制台、sys.path 注入、Per-Monitor V2 DPI、结构化日志、
依赖检查、无参 GUI / 带参 CLI 分派）收编到一处维护。

分发方式：本包位于 apphost/src/apphost，由 apphost/sync_vendor.py
同步到各业务项目的 src/_host/ 目录。业务项目自包含，可独立 clone 运行。

子模块：
- bootstrap : 控制台 UTF-8 修复 + 项目根注入 sys.path
- dpi       : Windows 高分屏 Per-Monitor V2 DPI 感知（准则 8-16）
- logging_setup : 参数化结构化日志 + 全局异常钩子（默认写项目内 logs/）
- deps      : 依赖检查与友好提示
- argrouter : 无参 GUI / 带参 CLI 双模式分派骨架
"""

from __future__ import annotations

from . import bootstrap, dpi, logging_setup, deps, argrouter
from .argrouter import AppConfig, dispatch, should_launch_gui, print_version

__all__ = [
    "bootstrap",
    "dpi",
    "logging_setup",
    "deps",
    "argrouter",
    "AppConfig",
    "dispatch",
    "should_launch_gui",
    "print_version",
]

__version__ = "1.0.0"
