# -*- coding: utf-8 -*-
"""apphost.bootstrap —— 进程级启动样板。

统一收编过去在 12 个 run.py 中重复出现的两段逻辑：
1. Windows 控制台 UTF-8 重配置（防中文乱码）
2. 项目根目录注入 sys.path（保证可直接 import src.*）
"""

from __future__ import annotations

import os
import sys


def configure_console_utf8() -> bool:
    """在 Windows 下把标准输出/错误重配置为 UTF-8，防止中文输出乱码。

    返回是否实际生效。非 Windows 或不支持 reconfigure 时返回 False（不致命）。
    """
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
            return True
        except Exception:
            return False
    return False


def inject_project_root(root_dir: str) -> str:
    """把项目根目录加入模块搜索路径，返回规范化后的绝对路径。"""
    root = os.path.abspath(root_dir)
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


def bootstrap(root_dir: str) -> str:
    """一次性完成进程前置初始化，返回项目根绝对路径。"""
    configure_console_utf8()
    return inject_project_root(root_dir)
