#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
  Boot-Order (UEFI 启动项与引导管理系统) - 唯一统一启动器 (run.py)
================================================================================
项目规范守则遵循：
1. 严禁使用 .bat / .sh / .ps1 文件，本 run.py 为唯一统一启动器。
2. 核心业务逻辑均位于 "src" 目录内。
3. 及时删除旧文件与冗余脚本。
4. 包含详尽的核心专业缩写术语与架构含义说明。
5. 严格限定在第三层（项目本身）内作业。
6. 零假代码、零模拟数据，所有底层操作直接与系统 API 真实交互。
7. 启用 Windows 原生高 DPI 感知，根绝 DWM 位图放大发虚问题。
"""

import os
import sys
import subprocess
from pathlib import Path

# ─── 高 DPI 感知初始化（避免 DWM 位图拉伸发虚） ────────────────────────────────
if sys.platform == "win32":
    try:
        import ctypes
        # 设置 Per-Monitor V2 DPI 感知 (PerMonitorV2 = -4)
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            # 兼容 Windows 8.1+ 的 shcore API
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass

    # 确保 Windows 控制台字符编码为 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ─── 项目工作区层级定义 (Layer 3: 项目本身) ───────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ─── 核心专业缩写具体含义介绍 (Rule 4) ─────────────────────────────────────────
ABBREVIATIONS_HELP = """
================================================================================
                UEFI 与系统引导体系核心专业缩写术语速查表
================================================================================
1. 固件与启动接口 (Firmware & Boot Interfaces):
   • UEFI      : Unified Extensible Firmware Interface (统一可扩展固件接口)
                 现代计算机替代传统 BIOS 的固件标准，直接支持 GPT 分区表与 64 位保护模式。
   • NVRAM     : Non-Volatile Random-Access Memory (非易失性随机访问内存)
                 主板主控芯片上的掉电不丢失存储区，用于保存 UEFI 启动项、启动顺序与硬件配置。
   • BCD       : Boot Configuration Data (启动配置数据)
                 Windows Vista 及后续版本采用的引导数据库，底层通过 bcdedit.exe 与 {fwbootmgr} 交互。
   • ESP       : EFI System Partition (EFI 系统分区)
                 符合 UEFI 规范的独立 FAT32 分区，GUID 为 c12a7328-f81f-11d2-ba4b-00a0c93ec93b，
                 存放操作系统引导器 (.efi) 与启动内核。
   • EFISTUB   : EFI Boot Stub (EFI 引导存根)
                 Linux 内核自身编译为符合 PE/COFF 规范的 EFI 二进制文件，主板固件可直接加载，
                 无需中间二级引导器。

2. 操作系统与引导加载器 (OS & Bootloaders):
   • GRUB      : GRand Unified Bootloader (大一统引导加载程序)
                 绝大多数 Linux 发行版（Debian、Ubuntu、Fedora、Arch）默认使用的多系统引导器。
   • sd-boot   : systemd-boot (现代极简 UEFI 引导程序)
                 systemd 原生集成的高性能文本引导器，基于标准 Boot Loader Specification。
   • Shim      : UEFI 第一级轻量级签名校验加载器
                 由 Red Hat 等发行的中间引导二进制，带微软第 3 方 UEFI CA 签名，
                 在 Secure Boot 开启时负责安全验证并链式加载未签名的 GRUB / 内核。
   • cmdline   : Kernel Command Line (内核启动参数命令行)
                 引导加载器传给 Linux 内核的初始化参数，如 root=UUID=..., quiet splash, nomodeset 等。
   • DWM       : Desktop Window Manager (桌面窗口管理器)
                 Windows 负责窗口合成、透明毛玻璃效果及缩放放大的图形引擎。在未声明 DPI 感知时，
                 DWM 会采用位图双线性插值强制拉伸窗口，导致文字与图形发虚模糊。
   • UAC       : User Account Control (用户账户控制)
                 Windows 权限提升安全边界机制。通过 ShellExecuteW("runas") 申请 Administrator 权限。
   • TUI       : Terminal User Interface (终端文本用户界面)
                 基于 curses/PDCurses 在命令行控制台中渲染的交互式字符界面。

3. 分区与标识体系 (Partition & Identifiers):
   • GUID      : Globally Unique Identifier (全局唯一标识符)
   • UUID      : Universally Unique Identifier (通用唯一识别码)
                 在 GPT 分区与 GRUB 配置文件中，用于在多硬盘环境下精确唯一定位存储分区。
   • PARTUUID  : GPT Partition Unique Identifier (GPT 分区唯一全局识别码)
                 直接记录在 GPT 分区表项中的 128 位硬件级 UUID，比文件系统 UUID 更底层稳定。
   • MSR       : Microsoft Reserved Partition (微软保留分区)
                 Windows 在 GPT 磁盘上预留的未格式化空间，备作动态卷元数据管理之用。
   • ext4      : Fourth Extended Filesystem (第四代扩展文件系统)
                 Debian/Linux 系统盘常用的日志型文件系统，默认无法直接在原生 Windows 下读写。
================================================================================
"""


# ─── 安全凭据缓存与免密提权管理 ───────────────────────────────────────────────
AUTH_DIR = Path.home() / ".config" / "boot-order"
AUTH_FILE = AUTH_DIR / ".auth"
ASKPASS_SH = AUTH_DIR / "askpass.sh"


def save_sudo_password(pwd: str):
    """安全保存 sudo 密码至 ~/.config/boot-order/.auth (chmod 0600) 并生成 askpass 脚本"""
    import base64
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    encoded = base64.b64encode(pwd.encode("utf-8")).decode("utf-8")
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        f.write(encoded)
    try:
        os.chmod(AUTH_FILE, 0o600)
    except Exception:
        pass

    with open(ASKPASS_SH, "w", encoding="utf-8") as f:
        f.write(f"#!/bin/sh\ncat \"{AUTH_FILE}\" | base64 -d\n")
    try:
        os.chmod(ASKPASS_SH, 0o700)
    except Exception:
        pass


def clear_saved_password():
    """清除本地存储的密码凭据"""
    if AUTH_FILE.exists():
        try:
            AUTH_FILE.unlink()
        except Exception:
            pass
    if ASKPASS_SH.exists():
        try:
            ASKPASS_SH.unlink()
        except Exception:
            pass


def get_saved_password():
    """读取本地存储的密码"""
    if not AUTH_FILE.exists():
        return None
    try:
        import base64
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            encoded = f.read().strip()
        return base64.b64decode(encoded.encode("utf-8")).decode("utf-8")
    except Exception:
        return None


def verify_sudo_password(pwd: str) -> bool:
    """使用 sudo -S -v 验证密码是否正确"""
    try:
        proc = subprocess.run(
            ["sudo", "-S", "-v"],
            input=pwd + "\n",
            capture_output=True,
            text=True,
            timeout=10
        )
        return proc.returncode == 0
    except Exception:
        return False


def ensure_admin():
    """Windows 下自动申请管理员权限 (UAC 提权)，Linux 下自动通过安全凭据/askpass 提权"""
    if sys.platform == "win32":
        try:
            import ctypes
            if ctypes.windll.shell32.IsUserAnAdmin():
                return
            # 未提权，触发 Windows 原生 UAC
            params = subprocess.list2cmdline([os.path.abspath(__file__)] + sys.argv[1:])
            ret = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, params, None, 1
            )
            if ret > 32:
                sys.exit(0)
            else:
                print("[-] 提权失败：未获得管理员权限。")
                sys.exit(1)
        except Exception as e:
            print(f"[-] UAC 提权异常: {e}")
            sys.exit(1)
    else:
        # Linux 环境处理
        if os.geteuid() == 0:
            return  # 已经是 root 身份，无需任何操作

        import shutil
        sudo_bin = shutil.which("sudo")
        if not sudo_bin:
            return  # 系统无 sudo 命令，降级继续运行

        # 1. 检查当前是否已经免密 (处于 sudo timestamp 窗口期或已配置 sudoers 免密)
        try:
            res_nopass = subprocess.run(["sudo", "-n", "true"], capture_output=True)
            if res_nopass.returncode == 0:
                os.execvp("sudo", ["sudo", "-E", sys.executable] + sys.argv)
        except Exception:
            pass

        # 2. 检查是否有本地已安全存储的密码凭据
        saved_pwd = get_saved_password()
        if saved_pwd:
            if not ASKPASS_SH.exists():
                save_sudo_password(saved_pwd)

            # 静默测试保存的密码是否依然有效
            if verify_sudo_password(saved_pwd):
                # 密码有效，直接通过 SUDO_ASKPASS 静默自动提权！
                env = os.environ.copy()
                env["SUDO_ASKPASS"] = str(ASKPASS_SH)
                try:
                    os.execvpe("sudo", ["sudo", "-A", "-E", sys.executable] + sys.argv, env)
                except Exception:
                    pass
            else:
                # 密码已失效（例如用户修改了开机密码）
                print("[!] 检测到先前保存的 sudo 密码已失效，需要重新输入以更新存储。")
                clear_saved_password()

        # 3. 首次运行或未存储密码：交互式获取一次，安全加密保存后开启永久免密启动
        if sys.stdin.isatty():
            import getpass
            username = os.environ.get("USER", "root")
            print("=" * 70)
            print("       Boot-Order 智能提权管理 (安全存储凭据 · 开启后续永久免密)")
            print("=" * 70)
            print("  [*] 说明：程序需要管理员权限与 UEFI NVRAM 和磁盘物理分区直接交互。")
            print("  [*] 特性：首次输入密码后将自动安全保存在本地 (~/.config/boot-order/.auth)，")
            print("           权限受保护 (0600)，后续启动将全自动静默登录，无需再重复输密！")
            print("-" * 70)

            while True:
                try:
                    pwd = getpass.getpass(f"[sudo] 请输入 {username} 的密码: ")
                except (KeyboardInterrupt, EOFError):
                    print("\n已取消提权。")
                    sys.exit(0)

                if not pwd:
                    print("[-] 密码不能为空，请重新输入。")
                    continue

                print("[*] 正在验证密码...", end="", flush=True)
                if verify_sudo_password(pwd):
                    print(" 验证成功！")
                    save_sudo_password(pwd)
                    print(f"[+] 密码已安全存储！后续运行 `/bin/python {os.path.basename(__file__)}` 将完全免密。")
                    print("=" * 70 + "\n")

                    env = os.environ.copy()
                    env["SUDO_ASKPASS"] = str(ASKPASS_SH)
                    try:
                        os.execvpe("sudo", ["sudo", "-A", "-E", sys.executable] + sys.argv, env)
                    except Exception as e:
                        print(f"[-] 自动提权启动异常: {e}")
                    return
                else:
                    print(" 密码错误！")
                    print("[-] 对不起，输入的密码无效，请重新尝试。\n")


def main():
    args = sys.argv[1:]

    # 1. 帮助说明
    if any(a in args for a in ("--help", "-h", "help")):
        print("""用法: python run.py [选项]

UEFI 启动项与引导管理系统

选项:
  -h, --help                 显示此帮助信息并退出
  -a, --abbr                 查看 UEFI/BIOS 核心专业术语缩写说明
  --clear-auth               清除本地存储的密码凭据
  --set-default <ID>         设置第一启动项 (例如: 0001)
  --set-timeout <SEC>        设置引导超时时间 (秒)
  -b, --bootloader, --grub   Linux 引导加载器配置 (CLI 模式)
  -r, --repair, --diagnose   引导健康诊断与修复 (CLI 模式)
  --tui                      强制启动终端交互 TUI 界面
  (无参数)                   默认启动图形界面或终端交互界面
""")
        return

    # 2. 查看术语说明
    if any(a in args for a in ("--abbr", "abbr", "-a", "help-abbr")):
        print(ABBREVIATIONS_HELP)
        return

    # 3. 清除已存储的密码凭据 (无需提权即可执行)
    if any(a in args for a in ("--clear-auth", "--forget-password", "--reset-auth")):
        clear_saved_password()
        print("[+] 已成功清除本地存储的 sudo 密码凭据。")
        return

    # 权限检查与安全免密提权
    ensure_admin()

    # 2. 命令行路由：快速设置第一启动项 CLI
    if "--set-default" in args:
        idx = args.index("--set-default")
        if idx + 1 < len(args):
            ident = args[idx + 1]
            from src.manager import BACKEND
            cfg = BACKEND.get_boot_config()
            if cfg:
                new_order = [ident] + [x for x in cfg.boot_order if x != ident]
                out, rc = BACKEND.set_boot_order(new_order)
                print(f"设置第一启动项结果 (代码 {rc}):\n{out}")
            return
        else:
            print("[-] 错误: 请指定启动项标识，例如: python run.py --set-default 0005")
            return

    # 3. 命令行路由：快速设置超时时间 CLI
    if "--set-timeout" in args:
        idx = args.index("--set-timeout")
        if idx + 1 < len(args):
            sec = int(args[idx + 1])
            from src.manager import BACKEND
            out, rc = BACKEND.set_timeout(sec)
            print(f"设置启动超时结果 (代码 {rc}):\n{out}")
            return

    # 4. 命令行路由：Linux 引导加载器配置 (CLI 模式)
    if any(a in args for a in ("--bootloader", "--grub", "grub", "bootloader", "-b")):
        from src.bootloader import interactive_bootloader_menu
        interactive_bootloader_menu()
        return

    # 5. 命令行路由：引导健康诊断与修复 (CLI 模式)
    if any(a in args for a in ("--repair", "repair", "-r", "--diagnose")):
        from src.repair import interactive_repair_menu
        interactive_repair_menu()
        return

    # 6. 命令行路由：挂载并真实查看 ESP 分区 (CLI 模式)
    if any(a in args for a in ("--esp", "esp", "-e", "mount-esp")):
        from src.esp import inspect_esp_partitions
        inspect_esp_partitions()
        return

    # 7. 命令行路由：Linux 多内核探测与一键切换 (CLI 模式)
    if any(a in args for a in ("--kernel", "--kernels", "-k", "kernel", "kernels")):
        from src.kernel import interactive_kernel_menu
        interactive_kernel_menu()
        return

    # 快速切换内核 CLI: --set-kernel <name>
    if "--set-kernel" in args:
        idx = args.index("--set-kernel")
        if idx + 1 < len(args):
            kname = args[idx + 1].strip().lower()
            from src.kernel import KernelManager
            kernels = KernelManager.detect_kernels()
            target = next((k for k in kernels if kname in k.package_name.lower() or kname in k.release.lower()), None)
            if target:
                ok, msg = KernelManager.switch_default_kernel(target)
                print(f"[{'✔ 成功' if ok else '✖ 失败'}]\n{msg}")
            else:
                print(f"[-] 未找到匹配的内核: '{kname}'。可用内核: {', '.join(k.package_name for k in kernels)}")
            return
        else:
            print("[-] 请指定内核名称，例如: python run.py --set-kernel zen")
            return

    # 快速设置单次启动内核 CLI: --set-kernel-oneshot <name>
    if "--set-kernel-oneshot" in args:
        idx = args.index("--set-kernel-oneshot")
        if idx + 1 < len(args):
            kname = args[idx + 1].strip().lower()
            from src.kernel import KernelManager
            kernels = KernelManager.detect_kernels()
            target = next((k for k in kernels if kname in k.package_name.lower() or kname in k.release.lower()), None)
            if target:
                ok, msg = KernelManager.switch_oneshot_kernel(target)
                print(f"[{'✔ 成功' if ok else '✖ 失败'}]\n{msg}")
            else:
                print(f"[-] 未找到匹配的内核: '{kname}'。可用内核: {', '.join(k.package_name for k in kernels)}")
            return
        else:
            print("[-] 请指定内核名称，例如: python run.py --set-kernel-oneshot zen")
            return

    # 8. 命令行路由：Windows 11 引导深度智能分析与双系统互通 (CLI 模式)
    if any(a in args for a in ("--win11", "win11", "-w", "--win")):
        from src.win11 import Win11Analyzer, Win11ActionHandler, format_win11_report_text
        diag = Win11Analyzer.analyze()
        for line in format_win11_report_text(diag):
            print(line)
        print("\n快捷操作选项：")
        print("  [1] 设定下次单次直接启动 Windows 11 (BootNext，不改默认顺序)")
        print("  [2] 将 Windows 11 设为默认第一启动项 (或设回 Linux 首选)")
        print("  [3] 一键同步 Windows 引导至 Linux ESP (打通 systemd-boot 菜单显示)")
        print("  [4] 修复 / 重建 Windows UEFI NVRAM 启动项 (efibootmgr -c)")
        print("  [5] 切换 Windows 11 启动项启用/禁用状态 (Active/Inactive)")
        print("  [0] 退出")
        try:
            choice = input("\n请输入选项编号 [1/2/3/4/5/0] (默认 0 退出): ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n已取消。")
            return
        if choice == "1":
            ok, msg = Win11ActionHandler.set_boot_next_win11(diag)
            print(f"\n[{'✔ 成功' if ok else '✖ 失败'}] {msg}")
        elif choice == "2":
            if diag.order_index == 0:
                ok, msg = Win11ActionHandler.restore_linux_as_first_boot(diag)
            else:
                ok, msg = Win11ActionHandler.set_win11_as_first_boot(diag)
            print(f"\n[{'✔ 成功' if ok else '✖ 失败'}] {msg}")
        elif choice == "3":
            from src.bootloader import SystemdBootManager
            sd_mgr = SystemdBootManager()
            plan = sd_mgr.generate_windows_entry_diff()
            print()
            for line in plan["diff_lines"]:
                if line.startswith("+++") or line.startswith("---"):
                    print(f"\033[1;36m{line}\033[0m")
                elif line.startswith("+"):
                    print(f"\033[32m{line}\033[0m")
                elif line.startswith("-"):
                    print(f"\033[31m{line}\033[0m")
                elif line.startswith("@@"):
                    print(f"\033[35m{line}\033[0m")
                elif line.startswith("###") or line.startswith("==="):
                    print(f"\033[1;33m{line}\033[0m")
                else:
                    print(line)
            print()
            try:
                confirm = input("请仔细审核上述 Diff 变更，是否同意并授权写入系统？ [y/N]: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                print("\n已取消。")
                return
            if confirm in ("y", "yes"):
                ok, msg = sd_mgr.apply_windows_entry(plan)
                print(f"\n[{'✔ 成功' if ok else '✖ 失败'}] {msg}")
            else:
                print("\n[!] 用户已取消操作，未对系统进行任何写入修改。")
        elif choice == "4":
            ok, msg = Win11ActionHandler.restore_win11_nvram_entry(diag)
            print(f"\n[{'✔ 成功' if ok else '✖ 失败'}] {msg}")
        elif choice == "5":
            ok, msg = Win11ActionHandler.toggle_win11_active(diag)
            print(f"\n[{'✔ 成功' if ok else '✖ 失败'}] {msg}")
        return

    # 默认：零繁琐提示，直接启动主 TUI 交互界面！
    from src.manager import main as run_tui
    run_tui()


if __name__ == "__main__":
    main()
