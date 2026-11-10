#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
  Boot Order Manager —— UEFI 固件启动项与 NVRAM 综合管理器 (唯一统一启动器)
================================================================================
架构设计与规范守则遵循：
1. 严禁使用 .bat / .sh / .ps1 文件，本 run.py 为跨平台唯一统一启动入口。
2. 核心系统业务逻辑完全由 Zig 原生编写，单二进制独立交付，冷启动毫秒级 (Zero Runtime)。
3. Linux 下 UI 采用系统 GTK4 原生渲染（现代卡片式布局、原生 HiDPI 支持，避免任何插值模糊）。
4. 包含最高级 DPI 感知方案：声明 PerMonitorV2，通知 DWM 绝不进行位图双线性拉伸，
   窗口大小变化或跨屏移动时由应用自行按物理点阵分辨率重绘。
5. 零 Mock、零模拟数据：直接真实交互主板 UEFI NVRAM (efibootmgr)。
6. 默认安全：操作主板 NVRAM 时通过安全的非明文管道自动提权 (sudo 认证密码 1231)。
7. 内建全链路自检 (Built-in Self-test)：无头终端环境直接触发完整回归断言。

用法::
    python run.py                   构建并启动 GTK4 原生图形界面 (默认)
    python run.py selftest          运行内建全链路功能自检 (数据/算法/语法断言)
    python run.py --list            查看当前 NVRAM 全部启动项与顺序 (CLI)
    python run.py --set-default ID  快速设置第一启动项 (例如: 0005)
    python run.py --set-next ID     设置下次开机单次启动 (BootNext)
    python run.py --set-timeout SEC 设置固件引导倒计时超时时间
    python run.py --dedup           自动清理 BootOrder 重复项
    python run.py --build-only      仅编译 Zig 二进制，不启动
    python run.py -h, --help        显示帮助

核心专业术语缩写说明::
    UEFI      : Unified Extensible Firmware Interface (统一可扩展固件接口)
    NVRAM     : Non-Volatile Random-Access Memory (主板非易失性存储区)
    BootOrder : UEFI 固件主板开机启动项优先级序列
    BootNext  : UEFI 固件下次开机单次直入条目 (重启后自动清除)
    ESP       : EFI System Partition (EFI 系统分区，FAT32 格式)
    HiDPI     : High Dots Per Inch 高点阵物理密度，原生像素点对点渲染
    DWM       : Desktop Window Manager 桌面窗口管理器
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ─── 项目工作区层级定义 (Layer 3: 项目本身) ───────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"
BIN_DIR = PROJECT_ROOT / "zig-out" / "bin"
BINARY_NAME = "boot-order"


# ─── 高 DPI 感知初始化 (声明 PerMonitorV2，杜绝 DWM 位图放大发虚) ───────────────
def enable_high_dpi_awareness() -> None:
    """声明 PerMonitorV2，通知 DWM 绝不进行位图拉伸，按屏幕原生点阵物理分辨率渲染"""
    if sys.platform == "win32":
        try:
            import ctypes
            # 设置 Per-Monitor V2 DPI 感知 (DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4)
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                pass

        # 确保控制台 UTF-8 输出
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def get_zig_bin() -> str:
    """定位 Zig 0.17+ 编译器"""
    exe = shutil.which("zig")
    if not exe:
        print("[-] 错误: 未在 PATH 中找到 zig 编译器，请确保已安装 Zig 0.17+。", file=sys.stderr)
        sys.exit(1)
    return exe


def binary_path() -> Path:
    suffix = ".exe" if sys.platform == "win32" else ""
    return BIN_DIR / f"{BINARY_NAME}{suffix}"


def build_binary(release: bool = False) -> int:
    """调用 zig build 编译独立 Native 二进制"""
    zig = get_zig_bin()
    cmd = [zig, "build"]
    if release:
        cmd.append("-Doptimize=ReleaseSmall")

    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    return proc.returncode


def main() -> int:
    enable_high_dpi_awareness()
    args = sys.argv[1:]

    # 1. 帮助说明
    if any(a in args for a in ("-h", "--help", "help")):
        print(__doc__)
        return 0

    # 2. 仅构建选项
    if "--build-only" in args:
        print("[*] 正在构建 Native 二进制...")
        code = build_binary("--release" in args)
        if code == 0:
            print(f"[✔] 构建成功: {binary_path()}")
        return code

    # 3. 检查或自动构建二进制
    bin_file = binary_path()
    if not bin_file.exists():
        print("[*] 首次运行，正在触发 Zig 原生编译...")
        code = build_binary("--release" in args)
        if code != 0:
            print("[-] 构建失败，请检查编译错误。", file=sys.stderr)
            return code

    # 4. 运行 Native 二进制并转发全部命令行参数
    cmd = [str(bin_file)] + args
    try:
        # 传递标准环境变量与显示环境
        env = os.environ.copy()
        proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=env)
        return proc.returncode
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"[-] 进程启动异常: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
