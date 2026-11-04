# -*- coding: utf-8 -*-
"""apphost.logging_setup —— 参数化结构化日志 + 全局异常钩子。

与旧版（wsl-manager/src/logger.py 的硬编码版）的关键差异：
- 默认日志写入「项目内 logs/」，严格遵守《项目准则》第 1/3 条
  （禁止文件外溢到项目目录外部）；旧版优先写 LOCALAPPDATA 属于外溢。
- 应用名、日志文件名、轮转参数全部参数化，不再硬编码 "WSLManager"。

除 configure 外，仍提供 get_default_log_dir / open_log_file /
open_log_directory / read_recent_logs 等兼容函数，供既有业务模块
（如 wsl_cli.py / wsl_gui.py）零改动地委托调用。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import threading
import traceback
from typing import Optional

LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(name)s:%(lineno)d] (%(threadName)s) %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_FILE_PATH: Optional[str] = None
_INITIALIZED = False
_APP_NAME = "App"
_PROJECT_ROOT: Optional[str] = None


def configure(
    app_name: str,
    project_root: str,
    log_file: str = "app.log",
    level: int = logging.INFO,
    to_console: bool = True,
    max_bytes: int = 2 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    """配置全局日志：项目内 logs/ 轮转文件 + 可选控制台 + 全局异常钩子。"""
    global _FILE_PATH, _INITIALIZED, _APP_NAME, _PROJECT_ROOT
    _APP_NAME = app_name
    _PROJECT_ROOT = project_root

    root = logging.getLogger()
    root.setLevel(level)
    if _INITIALIZED:
        return root

    fmt = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    # 文件输出：严格限定在项目内 logs/，不外溢到 LOCALAPPDATA
    log_dir = os.path.join(project_root, "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        # 若调用方传入绝对路径（wsl 的 --log-file 语义），os.path.join 会直接采用该绝对路径
        target = os.path.join(log_dir, log_file)
        fh = RotatingFileHandler(
            target,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
        fh.setLevel(level)
        fh.setFormatter(fmt)
        root.addHandler(fh)
        _FILE_PATH = target
    except Exception as e:
        sys.stderr.write(f"[{app_name}] 无法创建日志文件处理器: {e}\n")

    if to_console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(level)
        ch.setFormatter(fmt)
        root.addHandler(ch)

    _INITIALIZED = True
    install_exception_hooks()
    logging.getLogger("apphost").info(
        "日志系统就绪 [Level=%s, Path=%s]",
        logging.getLevelName(level),
        _FILE_PATH,
    )
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def shutdown() -> None:
    """关闭并移除全部日志 handler，释放文件句柄，重置初始化状态。

    用途：
    - 进程退出前显式释放（解释器退出时 logging 也会自动 shutdown，此处供需要
      立即释放的场景，如临时目录清理、单进程内多次重配日志）。
    - Windows 下避免日志文件被占用导致无法删除/移动。
    """
    global _INITIALIZED, _FILE_PATH
    root = logging.getLogger()
    for h in list(root.handlers):
        try:
            h.flush()
            h.close()
        except Exception:
            pass
        root.removeHandler(h)
    _INITIALIZED = False
    _FILE_PATH = None


def get_log_file_path() -> Optional[str]:
    return _FILE_PATH


def get_default_log_dir() -> str:
    """返回日志文件所在目录（项目内 logs/）。"""
    if _FILE_PATH:
        return os.path.dirname(_FILE_PATH)
    if _PROJECT_ROOT:
        return os.path.join(_PROJECT_ROOT, "logs")
    return os.path.join(os.getcwd(), "logs")


def install_exception_hooks() -> None:
    """把主线程与子线程未捕获异常完整写入日志。"""
    logger = logging.getLogger("apphost.unhandled")

    def sys_excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical(
            "主线程未捕获异常:\n%s",
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )

    sys.excepthook = sys_excepthook

    if hasattr(threading, "excepthook"):
        def thread_excepthook(args):
            if issubclass(args.exc_type, KeyboardInterrupt):
                return
            logger.critical(
                "子线程 '%s' 未捕获异常:\n%s",
                args.thread.name if args.thread else "Unknown",
                "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
            )

        threading.excepthook = thread_excepthook


def open_log_file() -> bool:
    """用系统默认文本编辑器打开日志文件（不存在则先创建）。"""
    path = _FILE_PATH or os.path.join(get_default_log_dir(), "app.log")
    try:
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"# {_APP_NAME} Log File\n")
        if hasattr(os, "startfile"):
            os.startfile(path)
        return True
    except Exception as e:
        logging.getLogger("apphost").error("无法打开日志文件 '%s': %s", path, e)
        return False


def open_log_directory() -> bool:
    """在系统资源管理器中打开日志所在目录。"""
    log_dir = get_default_log_dir()
    try:
        os.makedirs(log_dir, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(log_dir)
        return True
    except Exception as e:
        logging.getLogger("apphost").error("无法打开日志目录 '%s': %s", log_dir, e)
        return False


def read_recent_logs(lines: int = 150) -> str:
    """读取日志文件末尾指定行数的内容。"""
    path = _FILE_PATH or ""
    if not path or not os.path.exists(path):
        return "(日志文件尚不存在)"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        recent = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return "".join(recent)
    except Exception as e:
        return f"读取日志文件失败: {e}"
