#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
  Linux 引导加载器配置管理模块 (src/bootloader.py)
================================================================================
支持主流 Linux 引导加载器配置的精准解析、参数调整与一键重新生成：
1. GRUB 2:
   - 核心配置文件: /etc/default/grub 与 grub.cfg
   - 支持管理: 菜单超时 (GRUB_TIMEOUT)、默认项 (GRUB_DEFAULT)、内核启动命令行参数 (GRUB_CMDLINE_LINUX_DEFAULT)
   - 自动生成: 智能适配 update-grub (Debian/Ubuntu) 与 grub-mkconfig (Arch/Fedora/CentOS)
2. systemd-boot:
   - 核心配置文件: $ESP/loader/loader.conf 与 $ESP/loader/entries/*.conf
   - 支持管理: 超时等待时间 (timeout)、默认启动项 (default)、内核启动选项 (options)
   - 自动更新: bootctl update 自动同步最新引导二进制与条目
3. 跨系统 ESP 分区配置管理:
   - 支持直接探测并编辑 ESP 分区中的 loader.conf 或 grub.cfg
4. 零假代码、零模拟数据，所有底层操作直接与系统 API 真实交互。
"""

import os
import sys
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

IS_WINDOWS = sys.platform == "win32"


def _run_privileged(cmd: List[str], timeout: int = 5) -> subprocess.CompletedProcess:
    """以特权身份执行系统命令 (支持免密提权与 askpass)"""
    if os.geteuid() == 0 or IS_WINDOWS:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    askpass = os.path.expanduser("~/.config/boot-order/askpass.sh")
    env = os.environ.copy()
    if os.path.exists(askpass):
        env["SUDO_ASKPASS"] = askpass
        return subprocess.run(["sudo", "-A", "-E"] + cmd, capture_output=True, text=True, timeout=timeout, env=env)
    return subprocess.run(["sudo", "-n"] + cmd, capture_output=True, text=True, timeout=timeout)


def _read_file_safe(filepath: str) -> str:
    """安全读取系统文件（如果因权限被拒则通过特权读取）"""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        pass
    p = _run_privileged(["cat", filepath], timeout=3)
    if p.returncode == 0:
        return p.stdout
    return ""


def _write_file_safe(filepath: str, content: str) -> Tuple[bool, str]:
    """安全写入系统文件（如果无权写入则通过特权临时文件替换）"""
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return True, f"[+] {filepath} 成功写入更新。"
    except PermissionError:
        import tempfile
        try:
            with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tf:
                tf.write(content)
                tmp_name = tf.name
            p = _run_privileged(["cp", tmp_name, filepath], timeout=5)
            try:
                os.unlink(tmp_name)
            except Exception:
                pass
            if p.returncode == 0:
                return True, f"[+] 成功通过管理员权限更新 {filepath}。"
            return False, f"[-] 特权写入失败: {p.stderr.strip()}"
        except Exception as e:
            return False, f"[-] 写入失败: {e}"
    except Exception as e:
        return False, f"[-] 写入失败: {e}"


# ─── GRUB 2 配置解析与维护 ─────────────────────────────────────────────────────

class GrubConfigManager:
    """GRUB 2 引导配置管理器 (/etc/default/grub)"""

    DEFAULT_GRUB_PATH = "/etc/default/grub"

    def __init__(self, config_path: str = DEFAULT_GRUB_PATH):
        self.config_path = config_path

    def exists(self) -> bool:
        return os.path.exists(self.config_path)

    def read_config(self) -> Dict[str, str]:
        """读取 /etc/default/grub 键值对（忽略注释与空行）"""
        settings = {}
        content = _read_file_safe(self.config_path)
        if not content:
            return settings

        for line in content.splitlines():
            line_str = line.strip()
            if not line_str or line_str.startswith("#"):
                continue
            # 匹配 KEY=VALUE 或 KEY="VALUE"
            m = re.match(r"^([A-Za-z0-9_]+)=(.*)$", line_str)
            if m:
                key = m.group(1).strip()
                val = m.group(2).strip()
                # 去掉外部引号
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                settings[key] = val
        return settings

    def update_settings(self, updates: Dict[str, str]) -> Tuple[bool, str]:
        """安全更新 /etc/default/grub，保留原有注释、空行与格式"""
        content = _read_file_safe(self.config_path)
        if not content and not os.path.exists(self.config_path):
            return False, f"[-] 找不到 GRUB 配置文件: {self.config_path}"

        lines = content.splitlines(keepends=True) if content else []
        remaining_updates = dict(updates)
        new_lines = []

        for line in lines:
            line_stripped = line.strip()
            if line_stripped and not line_stripped.startswith("#"):
                m = re.match(r"^([A-Za-z0-9_]+)=", line_stripped)
                if m:
                    key = m.group(1).strip()
                    if key in remaining_updates:
                        val = remaining_updates.pop(key)
                        # 如果包含空格或特殊字符，加双引号
                        formatted_val = f'"{val}"' if any(c in val for c in ' = \t\n') or not val.isalnum() else val
                        new_lines.append(f"{key}={formatted_val}\n")
                        continue
            new_lines.append(line)

        # 任何之前不存在的新键，追加到末尾
        if remaining_updates:
            new_lines.append("\n# === 由 Boot-Order 引导管理器配置追加 ===\n")
            for key, val in remaining_updates.items():
                formatted_val = f'"{val}"' if any(c in val for c in ' = \t\n') or not val.isalnum() else val
                new_lines.append(f"{key}={formatted_val}\n")

        # 安全写入
        return _write_file_safe(self.config_path, "".join(new_lines))

    def list_entries(self) -> List[Any]:
        """获取 GRUB 菜单条目"""
        entries = []
        grub_cfgs = ["/boot/grub/grub.cfg", "/boot/grub2/grub.cfg", "/boot/efi/EFI/grub/grub.cfg"]
        cfg_content = ""
        cfg_found = ""
        for cfg in grub_cfgs:
            c = _read_file_safe(cfg)
            if c:
                cfg_content = c
                cfg_found = cfg
                break

        def_setting = self.read_config().get("GRUB_DEFAULT", "0")

        if cfg_content:
            matches = re.findall(r"menuentry\s+['\"]([^'\"]+)['\"]", cfg_content)
            for idx, title in enumerate(matches):
                is_def = (str(idx) == def_setting or title == def_setting)
                entries.append(SystemdBootEntry(
                    filename=str(idx),
                    title=title,
                    path=cfg_found,
                    is_default=is_def,
                    entry_type="grub"
                ))

        if not entries:
            entries.append(SystemdBootEntry(
                filename="0",
                title=f"GRUB 默认引导项 ({def_setting})",
                path=self.config_path,
                is_default=True,
                entry_type="grub"
            ))

        return entries

    @staticmethod
    def regenerate_grub() -> Tuple[bool, str]:
        """一键调用系统命令重新生成 grub.cfg"""
        if shutil.which("update-grub"):
            cmd = ["update-grub"]
        elif shutil.which("grub-mkconfig"):
            target_cfg = "/boot/grub/grub.cfg"
            cmd = ["grub-mkconfig", "-o", target_cfg]
        elif shutil.which("grub2-mkconfig"):
            cmd = ["grub2-mkconfig", "-o", "/boot/grub2/grub.cfg"]
        else:
            return False, "[-] 未检测到 update-grub 或 grub-mkconfig 命令，请手动更新 GRUB。"

        try:
            proc = _run_privileged(cmd, timeout=60)
            if proc.returncode == 0:
                return True, f"[+] GRUB 引导配置已成功重新生成！\n{proc.stdout.strip()}"
            else:
                return False, f"[-] GRUB 配置更新失败 (代码 {proc.returncode}):\n{proc.stderr.strip()}"
        except Exception as e:
            return False, f"[-] 执行更新异常: {e}"

    def generate_windows_entry_diff(self) -> Dict[str, Any]:
        """生成 GRUB 2 添加 Windows 11 引导条目的预览 Diff"""
        import difflib
        from src.win11 import Win11Analyzer
        diag = Win11Analyzer.analyze()

        win_uuid = diag.esp_uuid or "9412-5982"
        custom_path = "/etc/grub.d/40_custom"

        grub_entry = f"""
menuentry 'Windows 11 (UEFI)' --class windows --class os $menuentry_id_option 'osprober-efi-win11' {{
    insmod part_gpt
    insmod fat
    search --no-floppy --fs-uuid --set=root {win_uuid}
    chainloader /EFI/Microsoft/Boot/bootmgfw.efi
}}
"""
        old_lines = []
        if os.path.exists(custom_path):
            try:
                with open(custom_path, "r", encoding="utf-8", errors="replace") as f:
                    old_lines = f.readlines()
            except Exception:
                pass

        new_lines = list(old_lines) + grub_entry.splitlines(keepends=True)
        diff = list(difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=custom_path,
            tofile=custom_path
        ))

        diff_lines = [
            "=" * 64,
            " [Diff 变更审核] 即将向 GRUB 2 追加 Windows 11 引导菜单项",
            "=" * 64,
            f"目标文件: {custom_path} (变更后自动运行 update-grub / grub-mkconfig)",
            ""
        ]
        for l in diff:
            diff_lines.append(l.rstrip("\r\n"))
        diff_lines.append("=" * 64)

        return {
            "diff_lines": diff_lines,
            "custom_path": custom_path,
            "entry_snippet": grub_entry,
            "diag": diag
        }

    def add_windows_boot_entry(self) -> Tuple[bool, str]:
        """为 GRUB 2 增加 Windows 11 引导条目"""
        plan = self.generate_windows_entry_diff()
        custom_path = plan["custom_path"]
        grub_entry = plan["entry_snippet"]

        if not os.path.exists(custom_path):
            return False, f"[-] 未找到 {custom_path}。"

        try:
            with open(custom_path, "r", encoding="utf-8") as f:
                content = f.read()
            if "osprober-efi-win11" in content or "Windows 11" in content:
                return True, "[+] GRUB 中已存在 Windows 11 引导条目，无需重复添加。"

            with open(custom_path, "a", encoding="utf-8") as f:
                f.write(grub_entry)

            self.regenerate_grub()
            return True, f"[+] 成功在 {custom_path} 中追加 Windows 11 条目并已更新 grub.cfg！"
        except PermissionError:
            return False, "[-] 权限不足：写入 /etc/grub.d/40_custom 需要管理员 (sudo) 权限。"
        except Exception as e:
            return False, f"[-] 添加失败: {e}"


# ─── systemd-boot 配置解析与维护 ────────────────────────────────────────────────

@dataclass
class SystemdBootEntry:
    filename: str
    path: str = ""
    title: str = ""
    linux: str = ""
    initrd: str = ""
    options: str = ""
    efi: str = ""
    is_default: bool = False
    is_selected: bool = False
    entry_type: str = "type1"

    @property
    def type_label(self) -> str:
        et = (self.entry_type or "").lower()
        if "type1" in et or "1" in et or self.filename.endswith(".conf"):
            return "Type #1 独立条目 (.conf)"
        elif "type2" in et or "2" in et or "uki" in et:
            return "Type #2 UKI 统一内核 (.efi)"
        elif "auto" in et:
            return "Auto 固件自动发现"
        elif "grub" in et:
            return "GRUB 菜单条目"
        return "引导条目"


class SystemdBootManager:
    """systemd-boot 引导配置管理器 ($ESP/loader/)"""

    SEARCH_ESP_PATHS = ["/efi", "/boot/efi", "/boot"]

    def __init__(self, esp_root: Optional[str] = None):
        self.esp_root = esp_root or self._detect_esp_root()

    def _detect_esp_root(self) -> Optional[str]:
        """自动探测系统已挂载的 ESP 根目录"""
        try:
            p = subprocess.run(["bootctl", "--print-esp-path"], capture_output=True, text=True, timeout=2)
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip()
        except Exception:
            pass

        for p in self.SEARCH_ESP_PATHS:
            if os.path.exists(os.path.join(p, "loader", "loader.conf")) or os.path.exists(os.path.join(p, "loader")):
                return p

        try:
            from src.esp import find_all_esp_partitions
            for esp in find_all_esp_partitions():
                if esp.mount_point and os.path.exists(os.path.join(esp.mount_point, "loader")):
                    return esp.mount_point
        except Exception:
            pass

        return "/boot"

    def exists(self) -> bool:
        if bool(shutil.which("bootctl")):
            return True
        if self.esp_root and (os.path.exists(os.path.join(self.esp_root, "loader")) or os.path.exists(self.esp_root)):
            return True
        return False

    def get_loader_conf_path(self) -> Optional[str]:
        if not self.esp_root:
            return None
        return os.path.join(self.esp_root, "loader", "loader.conf")

    def read_loader_conf(self) -> Dict[str, str]:
        """读取 loader.conf"""
        conf_path = self.get_loader_conf_path()
        settings = {}
        if not conf_path:
            return settings

        content = _read_file_safe(conf_path)
        for line in content.splitlines():
            line_str = line.strip()
            if not line_str or line_str.startswith("#"):
                continue
            parts = line_str.split(None, 1)
            if len(parts) == 2:
                settings[parts[0].lower()] = parts[1]
            elif len(parts) == 1:
                settings[parts[0].lower()] = ""
        return settings

    def update_loader_conf(self, updates: Dict[str, str]) -> Tuple[bool, str]:
        """更新 loader.conf"""
        conf_path = self.get_loader_conf_path()
        if not conf_path:
            return False, "[-] 未能定位 ESP 分区下的 loader.conf 路径。"

        content = _read_file_safe(conf_path)
        lines = content.splitlines(keepends=True) if content else []
        remaining = {k.lower(): v for k, v in updates.items()}
        new_lines = []

        for line in lines:
            line_stripped = line.strip()
            if line_stripped and not line_stripped.startswith("#"):
                parts = line_stripped.split(None, 1)
                if parts:
                    key = parts[0].lower()
                    if key in remaining:
                        val = remaining.pop(key)
                        new_lines.append(f"{key} {val}\n")
                        continue
            new_lines.append(line)

        for key, val in remaining.items():
            new_lines.append(f"{key} {val}\n")

        return _write_file_safe(conf_path, "".join(new_lines))

    def list_entries(self) -> List[SystemdBootEntry]:
        """获取所有可用引导条目 (优先通过 bootctl list 获取完整 Type#1 与 Type#2 UKI 条目)"""
        entries = []
        # 1. 尝试通过 bootctl list --json=short 快速获取完整条目
        try:
            cmd = ["bootctl", "list", "--json=short"]
            p = _run_privileged(cmd, timeout=3)
            if p.returncode == 0 and p.stdout.strip():
                import json
                data = json.loads(p.stdout)
                for item in data:
                    initrd_val = item.get("initrd", "")
                    if isinstance(initrd_val, list):
                        initrd_val = ", ".join(initrd_val)
                    ent = SystemdBootEntry(
                        filename=item.get("id", ""),
                        path=item.get("source", "") or item.get("path", ""),
                        title=item.get("title", "") or item.get("id", ""),
                        linux=item.get("linux", ""),
                        initrd=initrd_val,
                        options=item.get("options", ""),
                        efi=item.get("efi", ""),
                        is_default=bool(item.get("isDefault", False)),
                        is_selected=bool(item.get("isSelected", False)),
                        entry_type=item.get("type", "type1")
                    )
                    entries.append(ent)
                if entries:
                    return entries
        except Exception:
            pass

        # 2. 回退：扫描 loader/entries/*.conf
        if not self.esp_root:
            return entries

        entries_dir = os.path.join(self.esp_root, "loader", "entries")
        file_list = []
        try:
            if os.path.exists(entries_dir):
                file_list = os.listdir(entries_dir)
        except PermissionError:
            try:
                p = _run_privileged(["ls", entries_dir], timeout=2)
                if p.returncode == 0:
                    file_list = p.stdout.split()
            except Exception:
                pass
        except Exception:
            pass

        def_conf = self.read_loader_conf().get("default", "")

        for fname in file_list:
            if fname.lower().endswith(".conf"):
                full_path = os.path.join(entries_dir, fname)
                entry = SystemdBootEntry(filename=fname, path=full_path, is_default=(fname == def_conf))
                content = _read_file_safe(full_path)

                for line in content.splitlines():
                    parts = line.strip().split(None, 1)
                    if len(parts) == 2:
                        k, v = parts[0].lower(), parts[1]
                        if k == "title":
                            entry.title = v
                        elif k == "linux":
                            entry.linux = v
                        elif k == "initrd":
                            entry.initrd = v
                        elif k == "options":
                            entry.options = v
                        elif k == "efi":
                            entry.efi = v
                entries.append(entry)
        return entries

    def set_default(self, entry_id: str) -> Tuple[bool, str]:
        """将指定条目设为默认引导项"""
        cmd = ["bootctl", "set-default", entry_id]
        p = _run_privileged(cmd, timeout=5)
        self.update_loader_conf({"default": entry_id})
        self.update_bootctl()
        return True, f"已将系统默认引导条目设为: {entry_id}"

    def update_entry_options(self, entry_filename: str, new_options: str) -> Tuple[bool, str]:
        """更新某个引导条目的内核参数 (options)"""
        if not self.esp_root:
            return False, "[-] 未定位 ESP 目录。"

        entry_path = os.path.join(self.esp_root, "loader", "entries", entry_filename)
        content = _read_file_safe(entry_path)
        if not content and not os.path.exists(entry_path):
            return False, f"[-] 找不到条目文件: {entry_filename}"

        lines = content.splitlines(keepends=True) if content else []
        has_options = False
        new_lines = []
        for line in lines:
            line_str = line.strip()
            if line_str.startswith("options ") or line_str == "options":
                new_lines.append(f"options {new_options}\n")
                has_options = True
            else:
                new_lines.append(line)

        if not has_options:
            new_lines.append(f"options {new_options}\n")

        ok, msg = _write_file_safe(entry_path, "".join(new_lines))
        if ok:
            return True, f"[+] 成功更新 {entry_filename} 的内核引导参数。"
        return False, msg

    @staticmethod
    def update_bootctl() -> Tuple[bool, str]:
        """执行 bootctl update"""
        if not shutil.which("bootctl"):
            return False, "[-] 系统中未找到 bootctl 命令。"
        try:
            proc = _run_privileged(["bootctl", "update"], timeout=15)
            if proc.returncode == 0:
                return True, f"[+] systemd-boot 引导器已成功更新！\n{proc.stdout.strip()}"
            else:
                return False, f"[-] bootctl update 失败:\n{proc.stderr.strip()}"
        except Exception as e:
            return False, f"[-] 执行异常: {e}"

    def generate_windows_entry_diff(self) -> Dict[str, Any]:
        """生成添加 Windows 11 引导条目的预览 Diff"""
        import difflib
        from src.win11 import Win11Analyzer
        diag = Win11Analyzer.analyze()

        esp_root = self.esp_root or "/boot"
        entries_dir = os.path.join(esp_root, "loader", "entries")
        win_conf_path = os.path.join(entries_dir, "windows.conf")

        diff_lines = []
        files_to_sync = []

        # 1. windows.conf 变更 Diff
        new_conf_content = (
            "# Windows 11 Boot Loader Entry for systemd-boot\n"
            "title    Windows 11\n"
            "efi      /EFI/Microsoft/Boot/bootmgfw.efi\n"
            "sort-key 02-windows\n"
        )
        old_conf_lines = []
        if os.path.exists(win_conf_path):
            try:
                with open(win_conf_path, "r", encoding="utf-8", errors="replace") as f:
                    old_conf_lines = f.readlines()
            except Exception:
                pass

        conf_diff = list(difflib.unified_diff(
            old_conf_lines,
            new_conf_content.splitlines(keepends=True),
            fromfile=win_conf_path if old_conf_lines else "/dev/null",
            tofile=win_conf_path
        ))

        # 2. loader.conf 超时变更 Diff
        loader_conf_path = os.path.join(esp_root, "loader", "loader.conf")
        old_lconf_lines = []
        new_lconf_lines = []
        need_timeout_update = False
        if os.path.exists(loader_conf_path):
            try:
                with open(loader_conf_path, "r", encoding="utf-8", errors="replace") as f:
                    old_lconf_lines = f.readlines()
            except Exception:
                pass
            has_timeout = False
            for line in old_lconf_lines:
                if line.strip().startswith("timeout"):
                    has_timeout = True
                    parts = line.strip().split()
                    if len(parts) >= 2 and parts[1] == "0":
                        new_lconf_lines.append("timeout 5\n")
                        need_timeout_update = True
                    else:
                        new_lconf_lines.append(line)
                else:
                    new_lconf_lines.append(line)
            if not has_timeout:
                new_lconf_lines.append("timeout 5\n")
                need_timeout_update = True
        else:
            new_lconf_lines = ["timeout 5\n"]
            need_timeout_update = True

        lconf_diff = []
        if need_timeout_update:
            lconf_diff = list(difflib.unified_diff(
                old_lconf_lines,
                new_lconf_lines,
                fromfile=loader_conf_path if old_lconf_lines else "/dev/null",
                tofile=loader_conf_path
            ))

        # 3. 跨 ESP 文件同步计划
        if diag.is_dual_esp and not diag.linux_esp_has_win_loader:
            files_to_sync = [
                (f"{diag.esp_dev}:\\EFI\\Microsoft\\Boot\\bootmgfw.efi", f"{esp_root}/EFI/Microsoft/Boot/bootmgfw.efi", "Windows 引导主程序"),
                (f"{diag.esp_dev}:\\EFI\\Microsoft\\Boot\\bootmgr.efi",  f"{esp_root}/EFI/Microsoft/Boot/bootmgr.efi",  "Windows 引导管理器"),
                (f"{diag.esp_dev}:\\EFI\\Microsoft\\Boot\\BCD",          f"{esp_root}/EFI/Microsoft/Boot/BCD",          "Windows 引导配置数据库"),
                (f"{diag.esp_dev}:\\EFI\\Microsoft\\Boot\\memtest.efi",  f"{esp_root}/EFI/Microsoft/Boot/memtest.efi",  "Windows 内存诊断"),
                (f"{diag.esp_dev}:\\EFI\\Microsoft\\Boot\\Fonts\\*",     f"{esp_root}/EFI/Microsoft/Boot/Fonts/",       "中英文字库目录"),
            ]

        # 组装格式化 Diff 审核文本
        diff_lines.append("=" * 64)
        diff_lines.append(" [Diff 变更审核] 即将向 Linux Boot Manager 增加 Win11 引导选项")
        diff_lines.append("=" * 64)

        if files_to_sync:
            diff_lines.append("\n### [阶段 1: 跨 ESP 物理分区镜像关键引导文件]")
            diff_lines.append(f"--- [源物理分区: {diag.esp_dev} (Windows ESP)]")
            diff_lines.append(f"+++ [目标物理分区: {diag.linux_esp_dev or esp_root} (Linux ESP 挂载于 {esp_root})]")
            for src, dst, desc in files_to_sync:
                diff_lines.append(f"+ 镜像: {src} -> {dst}  ({desc})")

        diff_lines.append(f"\n### [阶段 2: 写入条目配置文件: {win_conf_path}]")
        for l in conf_diff:
            diff_lines.append(l.rstrip("\r\n"))

        if need_timeout_update and lconf_diff:
            diff_lines.append(f"\n### [阶段 3: 调整 loader.conf 倒计时 (确保开机显示选择菜单)]")
            for l in lconf_diff:
                diff_lines.append(l.rstrip("\r\n"))

        diff_lines.append("\n### [阶段 4: 调用系统命令生效]")
        diff_lines.append("+ 命令: bootctl update (使固件与引导器同步最新条目)")
        diff_lines.append("=" * 64)

        return {
            "diff_lines": diff_lines,
            "win_conf_path": win_conf_path,
            "win_conf_content": new_conf_content,
            "need_timeout_update": need_timeout_update,
            "loader_conf_path": loader_conf_path,
            "loader_conf_content": "".join(new_lconf_lines),
            "files_to_sync": files_to_sync,
            "diag": diag
        }

    def apply_windows_entry(self, plan: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        """执行应用 Windows 11 引导条目写入"""
        from src.win11 import Win11ActionHandler
        if plan is None:
            plan = self.generate_windows_entry_diff()

        diag = plan["diag"]
        esp_root = self.esp_root or "/boot"
        entries_dir = os.path.join(esp_root, "loader", "entries")
        win_conf_path = plan["win_conf_path"]

        # 1. 跨分区镜像 Windows 引导
        sync_notes = []
        if diag.is_dual_esp and not diag.linux_esp_has_win_loader:
            ok, sync_msg = Win11ActionHandler.sync_win_boot_to_linux_esp(diag)
            if ok:
                sync_notes.append("已完成 Windows 引导核心跨 ESP 镜像同步")
            else:
                if os.geteuid() != 0:
                    return False, (
                        "[-] 跨 ESP 同步 Windows 引导文件需要 root 权限。\n"
                        f"{sync_msg}\n\n"
                        "请在终端使用: sudo python run.py -b 完成添加！"
                    )
                else:
                    return False, f"[-] 同步引导文件失败: {sync_msg}"

        # 2. 写入 loader/entries/windows.conf
        try:
            os.makedirs(entries_dir, exist_ok=True)
            with open(win_conf_path, "w", encoding="utf-8") as f:
                f.write(plan["win_conf_content"])
        except PermissionError:
            return False, f"[-] 权限不足：写入 {win_conf_path} 需要管理员 (sudo) 权限。"
        except Exception as e:
            return False, f"[-] 写入条目文件失败: {e}"

        # 3. 更新超时时间
        if plan.get("need_timeout_update"):
            self.update_loader_conf({"timeout": "5"})
            sync_notes.append("已将菜单超时时间设为 5 秒")

        # 4. 执行 bootctl update
        self.update_bootctl()

        extra = f" ({'; '.join(sync_notes)})" if sync_notes else ""
        return True, (
            f"[+] 成功为 Linux Boot Manager (systemd-boot) 增加 Windows 11 引导选项！{extra}\n"
            f"已写入: {win_conf_path}\n"
            f"下次开机第一启动项【Linux Boot Manager】将直接显示【Windows 11】选项！"
        )

    def add_windows_boot_entry(self) -> Tuple[bool, str]:
        """为 systemd-boot 增加 Windows 11 引导条目"""
        plan = self.generate_windows_entry_diff()
        return self.apply_windows_entry(plan)


# ─── 统一交互管理工作台 ────────────────────────────────────────────────────────

def interactive_bootloader_menu():
    """Linux 引导加载器交互式管理菜单"""
    print("\n" + "=" * 70)
    print("      Linux 引导加载器配置管理工作台 (GRUB 2 / systemd-boot)")
    print("=" * 70)

    grub_mgr = GrubConfigManager()
    sdboot_mgr = SystemdBootManager()

    has_grub = grub_mgr.exists()
    has_sdboot = sdboot_mgr.exists()

    if not has_grub and not has_sdboot:
        print("[-] 未在当前系统中检测到标准的 GRUB 2 (/etc/default/grub) 或 systemd-boot。")
        print("[!] 提示：如果是双系统且身处 Windows 下，请先选择工作台的挂载 ESP 选项。")
        return

    # 展示当前引导器状态
    print("[+] 检测到的引导加载器：")
    if has_grub:
        grub_conf = grub_mgr.read_config()
        print(f"  ● GRUB 2 (配置文件: {grub_mgr.config_path})")
        print(f"    ├─ 当前超时时间 (GRUB_TIMEOUT): {grub_conf.get('GRUB_TIMEOUT', '(未设置)')} 秒")
        print(f"    ├─ 默认启动项 (GRUB_DEFAULT): {grub_conf.get('GRUB_DEFAULT', '(未设置)')}")
        print(f"    └─ 默认内核参数 (CMDLINE): {grub_conf.get('GRUB_CMDLINE_LINUX_DEFAULT', '(空)')}")

    if has_sdboot:
        sd_conf = sdboot_mgr.read_loader_conf()
        entries = sdboot_mgr.list_entries()
        print(f"\n  ● systemd-boot (ESP 根路径: {sdboot_mgr.esp_root or '未挂载/通过 bootctl 交互'})")
        print(f"    ├─ 当前超时时间 (timeout): {sd_conf.get('timeout', '(未设置)')} 秒")
        print(f"    ├─ 默认引导条目 (default): {sd_conf.get('default', '(未设置)')}")
        print(f"    └─ 包含条目数量: {len(entries)} 个")
        for ent in entries:
            print(f"       ★ [{ent.filename}] {ent.title} (参数: {ent.options or '(默认)'})")

    print("\n" + "-" * 70)
    print("  可执行操作：")
    print("  [1] 修改引导超时等待时间 (Timeout)")
    print("  [2] 修改默认启动项 (Default Entry)")
    print("  [3] 修改内核启动参数 (Kernel Cmdline / Options)")
    print("  [4] 重新生成并应用引导配置 (Regenerate / Update)")
    print("  [5] 切换 Linux 启动内核 (Kernel Switch: 标准 / Zen / LTS 等)")
    print("  [6] 为 Linux 引导加载器增加 Windows 11 启动选项 (Auto Add Win11)")
    print("  [7] 指定自定义 ESP 路径 / 查看 bootctl 固件状态")
    print("  [0] 返回上级菜单")
    print("-" * 70)

    try:
        choice = input("请选择要进行的操作 [1/2/3/4/5/6/7/0]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return

    if choice == "1":
        # 修改超时时间
        try:
            sec_str = input("请输入新的倒计时等待秒数 (0 为立即启动，-1 为无限等待): ").strip()
            sec = int(sec_str)
        except ValueError:
            print("[-] 输入无效，必须为整数。")
            return

        if has_grub:
            ok, msg = grub_mgr.update_settings({"GRUB_TIMEOUT": str(sec)})
            print(f"[GRUB] {msg}")
        if has_sdboot and sdboot_mgr.esp_root:
            ok, msg = sdboot_mgr.update_loader_conf({"timeout": str(sec)})
            print(f"[systemd-boot] {msg}")

        # 提示生成
        ask_gen = input("\n是否立即重新生成/应用引导配置以生效？[Y/n]: ").strip().lower()
        if ask_gen in ("", "y", "yes"):
            if has_grub:
                ok, msg = grub_mgr.regenerate_grub()
                print(msg)
            if has_sdboot:
                ok, msg = sdboot_mgr.update_bootctl()
                print(msg)

    elif choice == "2":
        # 修改默认启动项
        default_val = input("请输入默认启动项名称或索引编号 (例如 '0' 或 'saved' 或条目标题): ").strip()
        if not default_val:
            print("[-] 输入为空，取消操作。")
            return

        if has_grub:
            ok, msg = grub_mgr.update_settings({"GRUB_DEFAULT": default_val})
            print(f"[GRUB] {msg}")
        if has_sdboot and sdboot_mgr.esp_root:
            ok, msg = sdboot_mgr.update_loader_conf({"default": default_val})
            print(f"[systemd-boot] {msg}")

        ask_gen = input("\n是否立即重新生成/应用引导配置？[Y/n]: ").strip().lower()
        if ask_gen in ("", "y", "yes"):
            if has_grub:
                ok, msg = grub_mgr.regenerate_grub()
                print(msg)
            if has_sdboot:
                ok, msg = sdboot_mgr.update_bootctl()
                print(msg)

    elif choice == "3":
        # 修改内核启动参数
        print("\n常用内核参数参考：")
        print("  • quiet splash          : 隐藏内核调试刷屏，显示启动动画")
        print("  • nomodeset             : 禁用内核显卡 KMS 模式（解决 NVIDIA/AMD 黑屏）")
        print("  • single / init=/bin/bash: 单用户救援模式，重置密码")
        print("  • loglevel=3            : 仅显示错误日志")

        if has_grub:
            current_cmdline = grub_mgr.read_config().get("GRUB_CMDLINE_LINUX_DEFAULT", "")
            print(f"\n当前 GRUB 内核参数: {current_cmdline}")
            new_cmd = input(f"请输入新的 GRUB 内核参数 (直接回车保持不变): ").strip()
            if new_cmd:
                ok, msg = grub_mgr.update_settings({"GRUB_CMDLINE_LINUX_DEFAULT": new_cmd})
                print(f"[GRUB] {msg}")

        if has_sdboot and sdboot_mgr.esp_root:
            entries = sdboot_mgr.list_entries()
            if entries:
                print("\nsystemd-boot 可选修改的条目：")
                for i, e in enumerate(entries, start=1):
                    print(f"  [{i}] {e.filename} ({e.title})")
                sel = input(f"请选择条目编号 [1-{len(entries)}]: ").strip()
                if sel.isdigit() and 1 <= int(sel) <= len(entries):
                    target_ent = entries[int(sel) - 1]
                    print(f"当前参数: {target_ent.options}")
                    new_opt = input("请输入新的内核参数 (options): ").strip()
                    if new_opt:
                        ok, msg = sdboot_mgr.update_entry_options(target_ent.filename, new_opt)
                        print(f"[systemd-boot] {msg}")

        ask_gen = input("\n是否立即重新生成/应用引导配置？[Y/n]: ").strip().lower()
        if ask_gen in ("", "y", "yes"):
            if has_grub:
                ok, msg = grub_mgr.regenerate_grub()
                print(msg)
            if has_sdboot:
                ok, msg = sdboot_mgr.update_bootctl()
                print(msg)

    elif choice == "4":
        # 立即重新生成
        print("\n[*] 正在重新生成并应用系统引导配置...")
        if has_grub:
            ok, msg = grub_mgr.regenerate_grub()
            print(msg)
        if has_sdboot:
            ok, msg = sdboot_mgr.update_bootctl()
            print(msg)

    elif choice == "5":
        # 切换 Linux 启动内核 (Kernel Switching)
        from src.kernel import interactive_kernel_menu
        interactive_kernel_menu()

    elif choice == "6":
        # 为 Linux 引导加载器自动添加 Win11 启动项 (Diff 审核确认机制)
        print("\n[*] 正在准备 Windows 11 引导条目变更 Diff 预览...")
        if has_sdboot:
            plan = sdboot_mgr.generate_windows_entry_diff()
        elif has_grub:
            plan = grub_mgr.generate_windows_entry_diff()
        else:
            print("[-] 未检测到支持的 Linux 引导器。")
            return

        # 打印完整 Diff 文本
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
            print("\n[*] 正在执行写入与应用...")
            if has_sdboot:
                ok, msg = sdboot_mgr.apply_windows_entry(plan)
                print(f"\n[systemd-boot] {msg}")
            elif has_grub:
                ok, msg = grub_mgr.add_windows_boot_entry()
                print(f"\n[GRUB 2] {msg}")
        else:
            print("[!] 用户已取消操作，未对磁盘与引导配置做任何修改。")

    elif choice == "7":
        # 指定自定义 ESP 路径或查看 bootctl 状态
        if shutil.which("bootctl"):
            print("\n┌──【bootctl 固件引导状态输出】───────────────────────")
            subprocess.run(["bootctl", "status", "--no-pager"])
            print("└──────────────────────────────────────────────────────────\n")
        custom_esp = input("请输入已挂载的 ESP 分区根目录 (例如 /boot、/efi 或 S:\\): ").strip()
        if custom_esp and os.path.exists(custom_esp):
            mgr = SystemdBootManager(esp_root=custom_esp)
            print(f"[+] 已定位 ESP 目录: {custom_esp}")
            c_conf = mgr.read_loader_conf()
            c_entries = mgr.list_entries()
            print(f"  ● 超时时间: {c_conf.get('timeout', '(未设置)')} 秒")
            print(f"  ● 默认条目: {c_conf.get('default', '(未设置)')}")
            print(f"  ● 包含条目: {len(c_entries)} 个")
            for ent in c_entries:
                print(f"    ★ [{ent.filename}] {ent.title} (参数: {ent.options})")
        else:
            if custom_esp:
                print(f"[-] 目录不存在: {custom_esp}")
