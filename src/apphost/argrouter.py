# -*- coding: utf-8 -*-
"""apphost.argrouter —— 无参 GUI / 带参 CLI 双模式分派骨架。

统一收编过去在 QR、wsl-manager、youtubu、aiokx、wifi、ventoy-next 各自实现的
「前置初始化顺序 + 无参进 GUI / 带参进 CLI」重复骨架。

各项目只需注入三处业务：
- build_parser(parser) : 注册自定义 CLI 参数（可选；不提供则 cli_main 直接收 argv 列表）
- cli_main(...)         : 执行 CLI 逻辑，返回退出码
- gui_main()            : 无参时启动 GUI，返回退出码（None 视为 0）

apphost 统一负责：控制台 UTF-8、sys.path、结构化日志、DPI V2、依赖检查、
以及「无参 / gui 别名 → GUI，其余 → CLI」的分派决策。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

from . import bootstrap, deps, dpi, logging_setup


@dataclass
class AppConfig:
    """描述一个桌面应用，供 dispatch 统一编排。"""

    name: str
    project_root: str
    version: str = "0.0.0"
    subtitle: str = ""
    description: str = ""
    log_file: str = "app.log"
    required_modules: Tuple[str, ...] = field(default_factory=tuple)
    enable_dpi: bool = True
    enable_logging: bool = True
    requirements_hint: str = "pip install -r requirements.txt"


def should_launch_gui(argv: Sequence[str]) -> bool:
    """默认分派决策：无参，或首个参数为 gui / --gui 时启动 GUI。"""
    if not argv:
        return True
    return str(argv[0]).lower() in ("gui", "--gui")


def print_version(config: AppConfig) -> int:
    """统一版本输出，供各项目 cli_main 复用。"""
    line = f"{config.name} v{config.version}"
    if config.subtitle:
        line += f" - {config.subtitle}"
    print(line)
    return 0


def dispatch(
    config: AppConfig,
    argv: Optional[Sequence[str]] = None,
    *,
    gui_main: Callable[[], Optional[int]],
    cli_main: Callable,
    build_parser: Optional[Callable[[argparse.ArgumentParser], None]] = None,
    gui_decider: Optional[Callable[[List[str]], bool]] = None,
) -> int:
    """执行统一的启动编排并返回进程退出码。

    - 前置：bootstrap（UTF-8 + sys.path）→ 结构化日志
    - GUI 分支：依赖检查（若配置）→ DPI V2 → gui_main()
    - CLI 分支：若提供 build_parser 则解析为 Namespace 交 cli_main(ns)，
      否则把原始 argv 列表交 cli_main(argv)
    """
    if argv is None:
        import sys
        argv = sys.argv[1:]
    argv = list(argv)

    root = bootstrap.bootstrap(config.project_root)
    if config.enable_logging:
        logging_setup.configure(config.name, root, log_file=config.log_file)

    decider = gui_decider or should_launch_gui
    if decider(argv):
        if config.required_modules:
            if not deps.check_dependencies(config.required_modules, config.requirements_hint):
                return 1
        if config.enable_dpi:
            dpi.enable_dpi_awareness()
        result = gui_main()
        return int(result) if result is not None else 0

    # CLI 分支
    if build_parser is not None:
        parser = argparse.ArgumentParser(
            prog="python run.py",
            description=config.description or f"{config.name} 命令行",
        )
        build_parser(parser)
        ns = parser.parse_args(argv)
        return int(cli_main(ns) or 0)

    return int(cli_main(argv) or 0)
