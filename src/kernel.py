#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
src/kernel.py - Linux 多内核深度探测、引导条目生成与一键切换管理模块
支持 systemd-boot (Type #1 .conf 与 Type #2 UKI) 及 GRUB 2 环境下的内核切换
"""

import os
import sys
import subprocess
import shutil
import json
import re
import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any


@dataclass
class KernelInfo:
    """Linux 内核信息数据模型"""
    package_name: str          # 软件包名称: linux, linux-zen, linux-lts, linux-hardened
    display_name: str          # 展示名称: Arch Linux (Standard), Arch Linux (Zen)
    release: str               # 内核版本发布号: 7.2.11-arch1-1, 7.2.11-zen1-1-zen
    vmlinuz_path: str          # 内核镜像路径: /boot/vmlinuz-linux-zen
    initramfs_path: str        # Initramfs 路径: /boot/initramfs-linux-zen.img
    ucode_path: Optional[str]  # CPU 微码镜像路径: /boot/intel-ucode.img 或 /boot/amd-ucode.img
    uki_path: Optional[str]    # UKI 一体化镜像路径: /boot/EFI/Linux/arch-linux.efi
    is_current: bool = False   # 是否为当前系统正在运行的内核 (uname -r)
    is_default: bool = False   # 是否为引导加载器默认启动项
    is_oneshot: bool = False   # 是否被设定为下次单次一次性启动
    entry_id: Optional[str] = None  # 引导条目 ID (例如 arch-linux.efi 或 arch-linux-zen.conf)
    entry_type: str = "none"   # uki, type1, grub, none
    has_boot_entry: bool = False    # 是否已经在引导加载器中配置好条目
    cmdline: str = ""          # 内核启动参数命令行

    @property
    def entry_type_label(self) -> str:
        """格式化友好引导类型展示"""
        et = (self.entry_type or "").lower()
        if "type1" in et or "1" in et:
            return "systemd-boot (Type #1 独立条目)"
        elif "uki" in et or "type2" in et or "2" in et:
            return "systemd-boot (Type #2 UKI 统一内核)"
        elif "grub" in et:
            return "GRUB 2 引导加载器"
        return "systemd-boot 引导器"


class KernelManager:
    """Linux 内核全能探测与切换管理器"""

    SEARCH_BOOT_PATHS = ["/boot", "/efi", "/boot/efi"]

    @classmethod
    def get_esp_root(cls) -> str:
        """获取当前 ESP 挂载路径"""
        for p in cls.SEARCH_BOOT_PATHS:
            if os.path.exists(os.path.join(p, "loader", "loader.conf")) or os.path.exists(os.path.join(p, "loader")):
                return p
        return "/boot"

    @classmethod
    def get_current_cmdline(cls) -> str:
        """获取当前系统的内核启动参数命令行 (/proc/cmdline)"""
        try:
            with open("/proc/cmdline", "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return "rw rootfstype=ext4"

    @classmethod
    def detect_microcode(cls, esp_root: str) -> Optional[str]:
        """探测 CPU 微码镜像路径 (intel-ucode.img 或 amd-ucode.img)"""
        # 1. 直接检查文件
        for ucode in ["intel-ucode.img", "amd-ucode.img"]:
            full = os.path.join(esp_root, ucode)
            if os.path.exists(full):
                return f"/{ucode}"
        if os.path.exists("/boot/intel-ucode.img"):
            return "/intel-ucode.img"
        if os.path.exists("/boot/amd-ucode.img"):
            return "/amd-ucode.img"

        # 2. 检查包管理器中已安装的微码包 (Arch Linux pacman)
        if shutil.which("pacman"):
            try:
                res = subprocess.run(["pacman", "-Q", "intel-ucode"], capture_output=True)
                if res.returncode == 0:
                    return "/intel-ucode.img"
                res_amd = subprocess.run(["pacman", "-Q", "amd-ucode"], capture_output=True)
                if res_amd.returncode == 0:
                    return "/amd-ucode.img"
            except Exception:
                pass

        # 3. 检查 /proc/cpuinfo
        try:
            with open("/proc/cpuinfo", "r") as f:
                cpuinfo = f.read()
                if "GenuineIntel" in cpuinfo:
                    return "/intel-ucode.img"
                elif "AuthenticAMD" in cpuinfo:
                    return "/amd-ucode.img"
        except Exception:
            pass

        return None

    @classmethod
    def detect_kernels(cls) -> List[KernelInfo]:
        """全面探测系统中已安装的所有 Linux 内核"""
        uname_r = os.uname().release
        esp_root = cls.get_esp_root()
        current_cmdline = cls.get_current_cmdline()
        ucode_rel = cls.detect_microcode(esp_root)

        # 1. 扫描 /usr/lib/modules/ 下的所有内核模块目录
        modules_dir = "/usr/lib/modules"
        discovered_releases = []
        if os.path.exists(modules_dir):
            try:
                for entry in sorted(os.listdir(modules_dir)):
                    mpath = os.path.join(modules_dir, entry)
                    if os.path.isdir(mpath):
                        discovered_releases.append(entry)
            except Exception:
                pass

        if uname_r not in discovered_releases:
            discovered_releases.append(uname_r)

        # 2. 读取 systemd-boot 条目 (若安装了 bootctl)
        sdboot_entries = cls._query_bootctl_entries()
        default_entry_id = cls._query_default_entry_id(esp_root)

        kernels: List[KernelInfo] = []

        for rel in discovered_releases:
            # 探测软件包基础名称 (pkgbase)
            pkgbase_file = os.path.join(modules_dir, rel, "pkgbase")
            pkg_name = ""
            if os.path.exists(pkgbase_file):
                try:
                    with open(pkgbase_file, "r", encoding="utf-8") as f:
                        pkg_name = f.read().strip()
                except Exception:
                    pass

            if not pkg_name:
                if "zen" in rel.lower():
                    pkg_name = "linux-zen"
                elif "lts" in rel.lower():
                    pkg_name = "linux-lts"
                elif "hardened" in rel.lower():
                    pkg_name = "linux-hardened"
                else:
                    pkg_name = "linux"

            # 友好显示名称 (保持适中宽度，避免在各尺寸终端被截断)
            name_map = {
                "linux": "Arch Linux (官方标准内核)",
                "linux-zen": "Arch Linux (Zen 性能优化)",
                "linux-lts": "Arch Linux (LTS 长期支持)",
                "linux-hardened": "Arch Linux (Hardened 安全加固)"
            }
            display_title = name_map.get(pkg_name, f"Arch Linux ({pkg_name})")

            # 路径
            vmlinuz_path = os.path.join(esp_root, f"vmlinuz-{pkg_name}")
            initramfs_path = os.path.join(esp_root, f"initramfs-{pkg_name}.img")

            # 探测 UKI
            uki_candidates = [
                os.path.join(esp_root, "EFI", "Linux", f"arch-{pkg_name}.efi"),
                os.path.join(esp_root, "EFI", "Linux", "arch-linux.efi") if pkg_name == "linux" else ""
            ]
            found_uki = None
            for u in uki_candidates:
                if u and (os.path.exists(u) or any(e.get("path") == u for e in sdboot_entries)):
                    found_uki = u
                    break

            is_curr = (rel == uname_r)

            # 匹配引导加载器中的条目
            matching_entry = None
            for entry in sdboot_entries:
                # 检查 uname / version / linux / id
                e_uname = entry.get("uname", "")
                e_ver = entry.get("version", "")
                e_id = entry.get("id", "")
                e_linux = entry.get("linux", "")

                if (e_uname == rel) or (e_ver == rel):
                    matching_entry = entry
                    break
                if pkg_name in e_id or (pkg_name == "linux" and e_id in ("arch-linux.efi", "arch.conf")):
                    matching_entry = entry
                    break
                if f"vmlinuz-{pkg_name}" in e_linux:
                    matching_entry = entry
                    break

            # 检查 loader/entries/*.conf
            if not matching_entry:
                entries_dir = os.path.join(esp_root, "loader", "entries")
                if os.path.exists(entries_dir):
                    try:
                        for conf_file in os.listdir(entries_dir):
                            if conf_file.endswith(".conf"):
                                cpath = os.path.join(entries_dir, conf_file)
                                try:
                                    with open(cpath, "r", encoding="utf-8", errors="replace") as cf:
                                        text = cf.read()
                                        if f"vmlinuz-{pkg_name}" in text or f"initramfs-{pkg_name}" in text:
                                            matching_entry = {
                                                "id": conf_file,
                                                "title": conf_file,
                                                "type": "type1",
                                                "isDefault": (conf_file == default_entry_id)
                                            }
                                            break
                                except Exception:
                                    pass
                    except Exception:
                        pass

            entry_id = matching_entry.get("id") if matching_entry else None
            entry_type = matching_entry.get("type", "none") if matching_entry else ("uki" if found_uki else "none")
            has_entry = matching_entry is not None or (found_uki is not None)

            is_def = False
            if matching_entry:
                is_def = matching_entry.get("isDefault", False) or (entry_id == default_entry_id)
            elif default_entry_id:
                if entry_id and entry_id == default_entry_id:
                    is_def = True
                elif pkg_name in default_entry_id:
                    is_def = True

            kernel_info = KernelInfo(
                package_name=pkg_name,
                display_name=display_title,
                release=rel,
                vmlinuz_path=vmlinuz_path,
                initramfs_path=initramfs_path,
                ucode_path=ucode_rel,
                uki_path=found_uki,
                is_current=is_curr,
                is_default=is_def,
                is_oneshot=matching_entry.get("isOneshot", False) if matching_entry else False,
                entry_id=entry_id,
                entry_type=entry_type,
                has_boot_entry=has_entry,
                cmdline=current_cmdline
            )
            kernels.append(kernel_info)

        # 排序：当前正在运行的优先排在前面，其余按包名升序
        kernels.sort(key=lambda k: (not k.is_current, not k.is_default, k.package_name))
        return kernels

    @classmethod
    def _query_bootctl_entries(cls) -> List[Dict[str, Any]]:
        """通过 bootctl list --json=short 查询所有 systemd-boot 条目"""
        if not shutil.which("bootctl"):
            return []
        try:
            cmd = ["bootctl", "list", "--json=short"]
            # 优先检查是否需要 sudo
            if os.geteuid() != 0:
                askpass = Path.home() / ".config" / "boot-order" / "askpass.sh"
                if askpass.exists():
                    env = os.environ.copy()
                    env["SUDO_ASKPASS"] = str(askpass)
                    res = subprocess.run(["sudo", "-A", "-E"] + cmd, capture_output=True, text=True, env=env, timeout=5)
                else:
                    res = subprocess.run(["sudo", "-n"] + cmd, capture_output=True, text=True, timeout=5)
            else:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)

            if res.returncode == 0:
                return json.loads(res.stdout)
        except Exception:
            pass
        return []

    @classmethod
    def _query_default_entry_id(cls, esp_root: str) -> Optional[str]:
        """读取 loader.conf 中的 default 条目"""
        lconf = os.path.join(esp_root, "loader", "loader.conf")
        if os.path.exists(lconf):
            try:
                with open(lconf, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line_s = line.strip()
                        if line_s.startswith("default ") or line_s.startswith("default\t"):
                            parts = line_s.split(None, 1)
                            if len(parts) == 2:
                                return parts[1].strip()
            except Exception:
                pass
        return None

    @classmethod
    def generate_entry_plan(cls, kernel: KernelInfo) -> Dict[str, Any]:
        """为指定内核生成 systemd-boot 引导条目写入计划与 Diff 预览"""
        esp_root = cls.get_esp_root()
        entries_dir = os.path.join(esp_root, "loader", "entries")
        conf_filename = f"arch-{kernel.package_name}.conf"
        target_conf_path = os.path.join(entries_dir, conf_filename)

        lines = [
            f"# Boot entry for {kernel.display_name} ({kernel.release})\n",
            f"title   {kernel.display_name}\n",
            f"linux   /vmlinuz-{kernel.package_name}\n",
        ]
        if kernel.ucode_path:
            lines.append(f"initrd  {kernel.ucode_path}\n")
        lines.append(f"initrd  /initramfs-{kernel.package_name}.img\n")
        lines.append(f"options {kernel.cmdline}\n")
        new_content = "".join(lines)

        old_content_lines = []
        if os.path.exists(target_conf_path):
            try:
                with open(target_conf_path, "r", encoding="utf-8", errors="replace") as f:
                    old_content_lines = f.readlines()
            except Exception:
                pass

        diff_lines = list(difflib.unified_diff(
            old_content_lines,
            new_content.splitlines(keepends=True),
            fromfile=target_conf_path if old_content_lines else "/dev/null",
            tofile=target_conf_path
        ))

        # 检查 loader.conf 超时时间，若为 0 则建议调整为 5 秒方便用户开机自选
        loader_conf_path = os.path.join(esp_root, "loader", "loader.conf")
        need_timeout_fix = False
        lconf_diff = []
        if os.path.exists(loader_conf_path):
            try:
                with open(loader_conf_path, "r", encoding="utf-8", errors="replace") as f:
                    old_lconf = f.readlines()
                new_lconf = []
                for line in old_lconf:
                    parts = line.strip().split()
                    if len(parts) >= 2 and parts[0] == "timeout" and parts[1] == "0":
                        new_lconf.append("timeout 5\n")
                        need_timeout_fix = True
                    else:
                        new_lconf.append(line)
                if need_timeout_fix:
                    lconf_diff = list(difflib.unified_diff(
                        old_lconf, new_lconf, fromfile=loader_conf_path, tofile=loader_conf_path
                    ))
            except Exception:
                pass

        formatted_diff = [
            "=" * 66,
            f" [Diff 变更审核] 即将为【{kernel.display_name}】生成独立引导条目",
            "=" * 66,
            f"\n### [目标文件: {target_conf_path}]"
        ]
        for d in diff_lines:
            formatted_diff.append(d.rstrip("\r\n"))

        if need_timeout_fix and lconf_diff:
            formatted_diff.append(f"\n### [同步优化 loader.conf: 设置引导菜单倒计时为 5 秒]")
            for d in lconf_diff:
                formatted_diff.append(d.rstrip("\r\n"))

        formatted_diff.append("\n### [系统动作: 调用 bootctl update 同步 UEFI NVRAM]")
        formatted_diff.append("=" * 66)

        return {
            "conf_path": target_conf_path,
            "conf_filename": conf_filename,
            "conf_content": new_content,
            "diff_lines": formatted_diff,
            "kernel": kernel,
            "need_timeout_fix": need_timeout_fix,
            "loader_conf_path": loader_conf_path
        }

    @classmethod
    def create_boot_entry(cls, kernel: KernelInfo) -> Tuple[bool, str]:
        """执行写入引导条目文件并运行 bootctl update"""
        plan = cls.generate_entry_plan(kernel)
        target_path = plan["conf_path"]

        try:
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(plan["conf_content"])
        except PermissionError:
            # 尝试通过 sudo 写入
            try:
                cmd = f"cat << 'EOF' > '{target_path}'\n{plan['conf_content']}\nEOF\n"
                res = cls._run_privileged_sh(cmd)
                if res != 0:
                    return False, f"[-] 权限不足：无法写入 {target_path} (请使用 sudo 运行)。"
            except Exception as e:
                return False, f"[-] 提权写入失败: {e}"
        except Exception as e:
            return False, f"[-] 写入失败: {e}"

        # 调整 timeout 为 5 秒
        if plan.get("need_timeout_fix"):
            cls._ensure_loader_timeout()

        # 执行 bootctl update
        cls._run_bootctl_update()

        kernel.entry_id = plan["conf_filename"]
        kernel.has_boot_entry = True
        kernel.entry_type = "type1"

        return True, f"[+] 成功为【{kernel.display_name}】生成引导配置文件:\n    {target_path}\n已自动同步至 UEFI systemd-boot 菜单。"

    @classmethod
    def switch_default_kernel(cls, kernel: KernelInfo) -> Tuple[bool, str]:
        """将选定内核设为主板永久默认启动内核 (Permanent Default)"""
        # 1. 若当前内核尚无引导条目，先自动为其生成条目
        if not kernel.has_boot_entry:
            ok, msg = cls.create_boot_entry(kernel)
            if not ok:
                return False, f"[-] 自动创建内核引导条目失败:\n{msg}"

        entry_id = kernel.entry_id or f"arch-{kernel.package_name}.conf"
        esp_root = cls.get_esp_root()
        loader_conf = os.path.join(esp_root, "loader", "loader.conf")

        # 2. 调用 bootctl set-default
        ok_cmd = False
        if shutil.which("bootctl"):
            ret = cls._run_privileged(["bootctl", "set-default", entry_id])
            if ret == 0:
                ok_cmd = True

        # 3. 双保险：同步更新 loader.conf 中的 default 行
        cls._update_loader_conf_default(loader_conf, entry_id)

        # 4. 执行 bootctl update
        cls._run_bootctl_update()

        return True, (
            f"[✔ 切换成功] 默认启动内核已设为:【{kernel.display_name}】\n"
            f"引导项标识 (ID): {entry_id}\n"
            f"内核版本: {kernel.release}\n"
            f"下次计算机冷启动或重启时，系统将直接默认载入此内核！"
        )

    @classmethod
    def switch_oneshot_kernel(cls, kernel: KernelInfo) -> Tuple[bool, str]:
        """仅下次启动单次使用该内核 (One-Shot Boot Next)"""
        if not kernel.has_boot_entry:
            ok, msg = cls.create_boot_entry(kernel)
            if not ok:
                return False, f"[-] 自动创建内核引导条目失败:\n{msg}"

        entry_id = kernel.entry_id or f"arch-{kernel.package_name}.conf"

        if not shutil.which("bootctl"):
            return False, "[-] 未找到 bootctl 命令，无法设置 NVRAM One-Shot 条目。"

        ret = cls._run_privileged(["bootctl", "set-oneshot", entry_id])
        if ret == 0:
            return True, (
                f"[✔ 设定成功] 已设置【仅下次单次启动】载入内核:【{kernel.display_name}】\n"
                f"条目 ID: {entry_id} (Release: {kernel.release})\n"
                f"本次生效后，再下一次开机将自动恢复原默认内核设置！"
            )
        else:
            return False, f"[-] bootctl set-oneshot {entry_id} 执行失败 (可能需要 sudo 权限)。"

    @classmethod
    def _run_privileged(cls, cmd: List[str]) -> int:
        """执行特权系统命令"""
        if os.geteuid() == 0:
            return subprocess.run(cmd, capture_output=True).returncode

        askpass = Path.home() / ".config" / "boot-order" / "askpass.sh"
        if askpass.exists():
            env = os.environ.copy()
            env["SUDO_ASKPASS"] = str(askpass)
            res = subprocess.run(["sudo", "-A", "-E"] + cmd, capture_output=True, env=env)
            return res.returncode
        else:
            res = subprocess.run(["sudo", "-n"] + cmd, capture_output=True)
            return res.returncode

    @classmethod
    def _run_privileged_sh(cls, script: str) -> int:
        """通过 shell 执行特权脚本片段"""
        if os.geteuid() == 0:
            return subprocess.run(["sh", "-c", script], capture_output=True).returncode

        askpass = Path.home() / ".config" / "boot-order" / "askpass.sh"
        if askpass.exists():
            env = os.environ.copy()
            env["SUDO_ASKPASS"] = str(askpass)
            res = subprocess.run(["sudo", "-A", "-E", "sh", "-c", script], capture_output=True, env=env)
            return res.returncode
        else:
            res = subprocess.run(["sudo", "-n", "sh", "-c", script], capture_output=True)
            return res.returncode

    @classmethod
    def _update_loader_conf_default(cls, lconf_path: str, new_default: str):
        """更新 loader.conf 中的 default 项"""
        if not os.path.exists(lconf_path):
            return
        try:
            with open(lconf_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            new_lines = []
            found = False
            for line in lines:
                parts = line.strip().split()
                if parts and parts[0] == "default":
                    new_lines.append(f"default {new_default}\n")
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                new_lines.append(f"default {new_default}\n")

            content = "".join(new_lines)
            try:
                with open(lconf_path, "w", encoding="utf-8") as f:
                    f.write(content)
            except PermissionError:
                cls._run_privileged_sh(f"cat << 'EOF' > '{lconf_path}'\n{content}\nEOF\n")
        except Exception:
            pass

    @classmethod
    def _ensure_loader_timeout(cls):
        """确保 loader.conf 超时倒计时至少为 3-5 秒，方便开机自选"""
        esp_root = cls.get_esp_root()
        lconf = os.path.join(esp_root, "loader", "loader.conf")
        if not os.path.exists(lconf):
            return
        try:
            with open(lconf, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            new_lines = []
            has_timeout = False
            for line in lines:
                parts = line.strip().split()
                if parts and parts[0] == "timeout":
                    has_timeout = True
                    if len(parts) >= 2 and parts[1] == "0":
                        new_lines.append("timeout 5\n")
                    else:
                        new_lines.append(line)
                else:
                    new_lines.append(line)
            if not has_timeout:
                new_lines.append("timeout 5\n")
            content = "".join(new_lines)
            try:
                with open(lconf, "w", encoding="utf-8") as f:
                    f.write(content)
            except PermissionError:
                cls._run_privileged_sh(f"cat << 'EOF' > '{lconf}'\n{content}\nEOF\n")
        except Exception:
            pass

    @classmethod
    def _run_bootctl_update(cls):
        """执行 bootctl update 同步固件"""
        if shutil.which("bootctl"):
            cls._run_privileged(["bootctl", "update"])

    @classmethod
    def format_kernel_report_lines(cls, kernels: List[KernelInfo]) -> List[str]:
        """生成格式化内核状态列表文本"""
        lines = [
            "=" * 72,
            "              Linux 已安装内核深度检测与切换工作台",
            "=" * 72,
            f"[*] 当前运行内核 (uname -r): {os.uname().release}",
            f"[*] ESP 引导分区挂载点     : {cls.get_esp_root()}",
            "-" * 72,
        ]

        if not kernels:
            lines.append("[-] 未在系统中检测到可用的 Linux 内核。")
            return lines

        for i, k in enumerate(kernels, 1):
            curr_badge = "★ [当前运行]" if k.is_current else "            "
            def_badge = "✔ [默认启动]" if k.is_default else ("🚀 [单次启动]" if k.is_oneshot else "           ")
            entry_badge = f"[引导项: {k.entry_id}]" if k.has_boot_entry else "⚠️ [未生成引导项]"

            lines.append(f"  [{i}] {k.display_name}")
            lines.append(f"      ├─ 内核版本 (Release): {k.release}")
            lines.append(f"      ├─ 状态标志         : {curr_badge}  {def_badge}")
            lines.append(f"      ├─ 引导条目状态     : {entry_badge} ({k.entry_type.upper()})")
            lines.append(f"      ├─ 镜像路径 (vmlinuz): {k.vmlinuz_path}")
            if k.uki_path:
                lines.append(f"      ├─ UKI 镜像路径     : {k.uki_path}")
            lines.append(f"      └─ 内核启动参数     : {k.cmdline[:60]}...")
            lines.append("")

        lines.append("=" * 72)
        return lines


def interactive_kernel_menu():
    """CLI 下的交互式内核切换菜单"""
    mgr = KernelManager()
    kernels = mgr.detect_kernels()

    for line in mgr.format_kernel_report_lines(kernels):
        print(line)

    print("快捷操作选项：")
    print("  [1] 将指定内核设为【永久默认启动内核】(Set Default Kernel)")
    print("  [2] 设定【仅下次单次临时启动】此内核 (One-Shot Boot Next)")
    print("  [3] 为尚未配置引导的内核【生成独立条目】(Generate Boot Entry)")
    print("  [4] 查看内核启动参数与文件路径详情")
    print("  [0] 返回上级 / 退出")

    try:
        choice = input("\n请输入选项编号 [1/2/3/4/0] (默认 0 退出): ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n已取消。")
        return

    if choice in ("1", "2", "3", "4"):
        print("\n请选择目标内核编号：")
        for i, k in enumerate(kernels, 1):
            tag = " (当前运行)" if k.is_current else ""
            dtag = " [默认]" if k.is_default else ""
            print(f"  [{i}] {k.display_name} - {k.release}{tag}{dtag}")
        try:
            k_choice = input(f"请输入内核编号 [1-{len(kernels)}]: ").strip()
            k_idx = int(k_choice) - 1
            if k_idx < 0 or k_idx >= len(kernels):
                print("[-] 无效的编号。")
                return
            target_kernel = kernels[k_idx]
        except (ValueError, KeyboardInterrupt, EOFError):
            print("\n已取消。")
            return

        if choice == "1":
            # 切换默认内核 (带 Diff 预览)
            if not target_kernel.has_boot_entry:
                plan = mgr.generate_entry_plan(target_kernel)
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
                    elif line.startswith("===") or line.startswith("###"):
                        print(f"\033[1;33m{line}\033[0m")
                    else:
                        print(line)
                print()
                try:
                    c = input(f"是否同意生成引导条目并将【{target_kernel.display_name}】设为默认内核？ [y/N]: ").strip().lower()
                except (KeyboardInterrupt, EOFError):
                    print("\n已取消。")
                    return
                if c not in ("y", "yes"):
                    print("\n[!] 操作已取消。")
                    return

            ok, msg = mgr.switch_default_kernel(target_kernel)
            print(f"\n[{'✔ 成功' if ok else '✖ 失败'}]\n{msg}")

        elif choice == "2":
            ok, msg = mgr.switch_oneshot_kernel(target_kernel)
            print(f"\n[{'✔ 成功' if ok else '✖ 失败'}]\n{msg}")

        elif choice == "3":
            plan = mgr.generate_entry_plan(target_kernel)
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
                elif line.startswith("===") or line.startswith("###"):
                    print(f"\033[1;33m{line}\033[0m")
                else:
                    print(line)
            print()
            try:
                c = input(f"是否确认写入该条目配置文件？ [y/N]: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                print("\n已取消。")
                return
            if c in ("y", "yes"):
                ok, msg = mgr.create_boot_entry(target_kernel)
                print(f"\n[{'✔ 成功' if ok else '✖ 失败'}]\n{msg}")
            else:
                print("\n[!] 操作已取消。")

        elif choice == "4":
            print("\n" + "-" * 60)
            print(f"内核名称: {target_kernel.display_name}")
            print(f"软件包名: {target_kernel.package_name}")
            print(f"发布版本: {target_kernel.release}")
            print(f"当前运行: {'是' if target_kernel.is_current else '否'}")
            print(f"默认启动: {'是' if target_kernel.is_default else '否'}")
            print(f"条目 ID  : {target_kernel.entry_id or '未生成'}")
            print(f"内核镜像: {target_kernel.vmlinuz_path}")
            print(f"初始化盘: {target_kernel.initramfs_path}")
            print(f"CPU 微码: {target_kernel.ucode_path or '无'}")
            print(f"启动参数: {target_kernel.cmdline}")
            print("-" * 60)
            input("\n按回车键继续...")
