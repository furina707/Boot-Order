# -*- coding: utf-8 -*-
"""apphost.deps —— 依赖检查与友好提示。

统一收编各项目 run.py 中重复的「import 探测缺失依赖 → 打印安装提示」逻辑。
"""

from __future__ import annotations

import importlib
from typing import Iterable


def check_dependencies(
    modules: Iterable[str],
    requirements_hint: str = "pip install -r requirements.txt",
) -> bool:
    """检查给定模块是否均可导入；缺失时打印友好提示并返回 False。"""
    missing = []
    for mod in modules:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)

    if missing:
        print("[错误] 检测到当前 Python 环境缺失关键依赖模块：")
        for m in missing:
            print(f"  - {m}")
        print(f"\n请在终端运行以下命令一键安装完整依赖：\n  {requirements_hint}\n")
        return False
    return True
