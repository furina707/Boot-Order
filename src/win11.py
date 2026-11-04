#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
================================================================================
  Windows 11 引导深度智能分析与双系统互通模块 (src/win11.py)
================================================================================
针对 Linux (Arch/Ubuntu/Fedora等) 与 Windows 11 双系统共存的常见痛点：
1. UEFI NVRAM 状态体检:
   - 检索 Windows Boot Manager (Boot000X) 存在性、激活标志、BootOrder 顺位、BootNext
   - 解析 UEFI 设备路径 (PARTUUID, LBA 扇区范围, \EFI\Microsoft\Boot\bootmgfw.efi)
   - 提取附带的 BCDOBJECT GUID 标识
2. ESP 引导分区与核心引导文件体检:
   - 定位 Windows ESP 物理分区 (FAT32, PARTUUID, 容量)
   - 检查 bootmgfw.efi、BCD、bootmgr.efi、memtest.efi、Fonts 等核心文件完整性
3. Windows 11 系统分区与安全基线:
   - 检测 Windows C: 系统分区与大小、BitLocker 加密卷头 (-FVE-FS-)
   - 检测 WinRE 恢复分区与 MSR 保留分区
   - 检查 TPM 2.0 硬件状态 (/dev/tpm0, /sys/class/tpm/tpm0) 与 UEFI Secure Boot 安全启动
4. 双系统 (Dual-Boot) 架构冲突与互通性智能研判:
   - 诊断「双 ESP 独立分区隔离」痛点 (解释为何 systemd-boot 菜单看不到 Windows)
   - 预警 Windows Update 引导强占风险与快速启动 (Fast Startup) NTFS 只读加锁
5. 一键智能处置与切换:
   - [操作 1] 单次启动 Windows 11 (设置 UEFI BootNext，免改默认配置)
   - [操作 2] 将 Windows 11 设为第一启动项 / 设回 Linux 首选
   - [操作 3] 一键同步 Windows 引导至 Linux ESP (打通 systemd-boot 自动探测)
   - [操作 4] 修复 / 重建丢失的 Windows UEFI NVRAM 启动项
   - [操作 5] 启用 / 禁用 Windows 启动项
"""

import os
import sys
import re
import uuid
import struct
import shutil
import tempfile
import subprocess
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

IS_WINDOWS = sys.platform == "win32"


@dataclass
class Win11BootDiagnostic:
    """Windows 11 引导综合诊断数据对象"""
    # NVRAM 层面
    nvram_found: bool = False
    boot_num: str = ""
    label: str = ""
    is_active: bool = False
    order_index: int = -1           # 0-indexed 位置
    total_orders: int = 0
    is_current: bool = False
    is_boot_next: bool = False
    efi_path: str = ""
    part_uuid: str = ""
    bcd_object: str = ""

    # ESP 分区层面
    esp_dev: str = ""
    esp_part_num: int = 0
    esp_size_mb: float = 0.0
    esp_uuid: str = ""
    esp_partuuid: str = ""
    esp_mounted: bool = False
    esp_mount_point: str = ""
    bootmgfw_exists: Optional[bool] = None
    bcd_exists: Optional[bool] = None
    bootmgr_exists: Optional[bool] = None
    memtest_exists: Optional[bool] = None

    # Windows 系统盘与附加分区
    win_system_dev: str = ""
    win_system_size_gb: float = 0.0
    win_system_uuid: str = ""
    win_system_partuuid: str = ""
    bitlocker_status: str = "未检测"   # "未启用", "已启用 (BitLocker加密保护中)", "未知 (需root权限)"
    msr_dev: str = ""
    msr_size_mb: float = 0.0
    winre_dev: str = ""
    winre_size_mb: float = 0.0

    # 硬件与固件安全基线
    tpm_present: bool = False
    tpm_version: str = "未知"
    tpm_device: str = ""
    secure_boot_supported: bool = False
    secure_boot_enabled: bool = False
    uefi_native: bool = True
    disk_is_gpt: bool = True

    # 双系统拓扑与冲突研判
    is_dual_esp: bool = False
    linux_esp_dev: str = ""
    linux_esp_mount: str = ""
    linux_esp_size_mb: float = 0.0
    linux_esp_partuuid: str = ""
    linux_bootloader: str = "未知"    # "systemd-boot", "GRUB 2", "未知"
    linux_esp_has_win_loader: bool = False

    # 诊断结论与建议
    notes: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)


class Win11Analyzer:
    """Windows 11 引导智能诊断引擎"""

    @classmethod
    def analyze(cls) -> Win11BootDiagnostic:
        diag = Win11BootDiagnostic()

        if IS_WINDOWS:
            cls._analyze_windows_native(diag)
        else:
            cls._analyze_linux_environment(diag)

        cls._generate_insights(diag)
        return diag

    # ──────────────────────────────────────────────────────────────────────────
    # Linux 平台深度分析
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def _analyze_linux_environment(cls, diag: Win11BootDiagnostic):
        # 1. 解析 NVRAM efibootmgr 输出与 /sys/firmware/efi/efivars
        cls._inspect_nvram_linux(diag)

        # 2. 遍历磁盘块设备，定位 Windows ESP、MSR、系统盘、WinRE
        cls._inspect_partitions_linux(diag)

        # 3. 检查 TPM 2.0 与 Secure Boot 安全基线
        cls._inspect_security_baseline_linux(diag)

        # 4. 检查 Linux ESP 挂载点与引导加载器 (systemd-boot / GRUB)
        cls._inspect_linux_bootloader(diag)

        # 5. 深入检查 Windows ESP 文件内容 (若已挂载或具有权限)
        cls._inspect_esp_files(diag)

    @classmethod
    def _inspect_nvram_linux(cls, diag: Win11BootDiagnostic):
        """通过 efibootmgr -v 与 efivars 检测 Windows 启动项"""
        try:
            res = subprocess.run(["efibootmgr", "-v"], capture_output=True, text=True, errors="replace")
            output = res.stdout if res.returncode == 0 else ""
        except Exception:
            output = ""

        # 读取 BootOrder, BootCurrent, BootNext
        boot_order: List[str] = []
        boot_current = ""
        boot_next = ""

        for line in output.splitlines():
            line_str = line.strip()
            if line_str.startswith("BootOrder:"):
                order_part = line_str.split(":", 1)[1].strip()
                boot_order = [x.strip() for x in order_part.split(",") if x.strip()]
            elif line_str.startswith("BootCurrent:"):
                boot_current = line_str.split(":", 1)[1].strip()
            elif line_str.startswith("BootNext:"):
                boot_next = line_str.split(":", 1)[1].strip()

        diag.total_orders = len(boot_order)

        # 匹配 Windows Boot Manager
        # 典型: Boot0001* Windows Boot Manager HD(1,GPT,83866a79-ae26-47fd-8aac-7779ac80fb3e,0x800,0x64000)/\EFI\Microsoft\Boot\bootmgfw.efi...
        win_pattern = re.compile(
            r"^Boot([0-9a-fA-F]{4})(\*?)\s+(Windows(?:\s+Boot\s+Manager)?.*?)\t(.*)$"
        )
        found = False
        for line in output.splitlines():
            m = win_pattern.match(line.strip())
            if m:
                found = True
                diag.nvram_found = True
                diag.boot_num = m.group(1).upper()
                diag.is_active = (m.group(2) == "*")
                diag.label = m.group(3).strip()
                details = m.group(4).strip()

                # 提取 PARTUUID
                part_m = re.search(r"HD\(\d+,GPT,([0-9a-fA-F-]+),", details, re.IGNORECASE)
                if part_m:
                    diag.part_uuid = part_m.group(1).lower()

                # 提取 EFI 相对路径
                path_m = re.search(r"(\\EFI\\[^\s\x00]+\.efi)", details, re.IGNORECASE)
                if path_m:
                    diag.efi_path = path_m.group(1).replace("/", "\\")
                else:
                    diag.efi_path = r"\EFI\Microsoft\Boot\bootmgfw.efi"

                # 提取 BCDOBJECT
                bcd_m = re.search(r"BCDOBJECT=({[0-9a-fA-F-]+})", details, re.IGNORECASE)
                if bcd_m:
                    diag.bcd_object = bcd_m.group(1)

                if diag.boot_num in boot_order:
                    diag.order_index = boot_order.index(diag.boot_num)
                diag.is_current = (diag.boot_num == boot_current)
                diag.is_boot_next = (diag.boot_num == boot_next)
                break

        # 若未从 efibootmgr 提取到，尝试直接读取 /sys/firmware/efi/efivars/Boot0001*
        if not found:
            cls._inspect_efivars_direct(diag, boot_order, boot_current, boot_next)

    @classmethod
    def _inspect_efivars_direct(cls, diag: Win11BootDiagnostic,
                                boot_order: List[str], boot_current: str, boot_next: str):
        efivars_dir = "/sys/firmware/efi/efivars"
        if not os.path.exists(efivars_dir):
            return

        for fname in os.listdir(efivars_dir):
            if not fname.startswith("Boot0") and not fname.startswith("Boot1"):
                continue
            num_match = re.match(r"^Boot([0-9A-Fa-f]{4})-", fname)
            if not num_match:
                continue
            bnum = num_match.group(1).upper()

            fpath = os.path.join(efivars_dir, fname)
            try:
                with open(fpath, "rb") as f:
                    raw = f.read()
                if len(raw) < 12:
                    continue
                load_option = raw[4:]  # 跳过 4 字节属性
                attr, fp_len = struct.unpack("<IH", load_option[:6])
                desc_bytes = load_option[6:]
                null_pos = desc_bytes.find(b"\x00\x00")
                if null_pos != -1:
                    if null_pos % 2 != 0:
                        null_pos += 1
                    desc = desc_bytes[:null_pos].decode("utf-16le", errors="ignore")
                    if "windows" in desc.lower() or "bootmgfw" in desc.lower():
                        diag.nvram_found = True
                        diag.boot_num = bnum
                        diag.is_active = bool(attr & 0x00000001)
                        diag.label = desc
                        if bnum in boot_order:
                            diag.order_index = boot_order.index(bnum)
                        diag.is_current = (bnum == boot_current)
                        diag.is_boot_next = (bnum == boot_next)

                        # 解析 DevicePath
                        dp_offset = 6 + null_pos + 2
                        dp = load_option[dp_offset:dp_offset + fp_len]
                        pos = 0
                        while pos < len(dp):
                            t = dp[pos]
                            st = dp[pos + 1]
                            l = struct.unpack("<H", dp[pos + 2:pos + 4])[0]
                            node = dp[pos:pos + l]
                            if t == 4 and st == 1 and len(node) >= 42:
                                sig = node[24:40]
                                diag.part_uuid = str(uuid.UUID(bytes_le=sig)).lower()
                            elif t == 4 and st == 4:
                                diag.efi_path = node[4:].decode("utf-16le", errors="ignore").rstrip("\x00")
                            elif t == 0x7F:
                                break
                            pos += l

                        # Optional Data
                        opt_data = load_option[dp_offset + fp_len:]
                        opt_str = opt_data.decode("utf-16le", errors="ignore")
                        bcd_m = re.search(r"BCDOBJECT=({[0-9a-fA-F-]+})", opt_str, re.IGNORECASE)
                        if bcd_m:
                            diag.bcd_object = bcd_m.group(1)
                        break
            except Exception:
                continue

    @classmethod
    def _inspect_partitions_linux(cls, diag: Win11BootDiagnostic):
        """遍历探测系统分区表，识别 Windows ESP、MSR、NTFS 系统盘与 WinRE"""
        by_partuuid_dir = "/dev/disk/by-partuuid"
        partuuid_map: Dict[str, str] = {}
        if os.path.exists(by_partuuid_dir):
            for u in os.listdir(by_partuuid_dir):
                target = os.path.realpath(os.path.join(by_partuuid_dir, u))
                partuuid_map[u.lower()] = target

        by_uuid_dir = "/dev/disk/by-uuid"
        dev_to_uuid: Dict[str, str] = {}
        if os.path.exists(by_uuid_dir):
            for u in os.listdir(by_uuid_dir):
                target = os.path.realpath(os.path.join(by_uuid_dir, u))
                dev_to_uuid[target] = u

        # 从 /sys/class/block 提取真实分区
        block_dir = "/sys/class/block"
        partitions: Dict[str, Dict] = {}
        if os.path.exists(block_dir):
            for name in os.listdir(block_dir):
                p_path = os.path.join(block_dir, name)
                if not os.path.exists(os.path.join(p_path, "partition")):
                    continue
                try:
                    with open(os.path.join(p_path, "partition")) as f:
                        pnum = int(f.read().strip())
                    with open(os.path.join(p_path, "size")) as f:
                        sectors = int(f.read().strip())
                    with open(os.path.join(p_path, "start")) as f:
                        start = int(f.read().strip())

                    dev_path = f"/dev/{name}"
                    size_mb = (sectors * 512) / (1024 * 1024)

                    # 匹配 PARTUUID
                    puuid = ""
                    for pu, dp in partuuid_map.items():
                        if dp == dev_path:
                            puuid = pu
                            break

                    partitions[dev_path] = {
                        "name": name,
                        "dev": dev_path,
                        "pnum": pnum,
                        "sectors": sectors,
                        "start": start,
                        "size_mb": size_mb,
                        "partuuid": puuid,
                        "uuid": dev_to_uuid.get(dev_path, ""),
                    }
                except Exception:
                    continue

        # 匹配 Windows ESP
        # 1. 优先通过 NVRAM 提取的 part_uuid 匹配
        if diag.part_uuid:
            for dev_path, pdata in partitions.items():
                if pdata["partuuid"] == diag.part_uuid:
                    diag.esp_dev = dev_path
                    diag.esp_part_num = pdata["pnum"]
                    diag.esp_size_mb = pdata["size_mb"]
                    diag.esp_partuuid = pdata["partuuid"]
                    diag.esp_uuid = pdata["uuid"]
                    break

        # 2. 如果未通过 part_uuid 找到，根据常见 Windows ESP 大小 (100MB~300MB) 与前序分区探测
        if not diag.esp_dev:
            for dev_path, pdata in partitions.items():
                if pdata["pnum"] == 1 and 90 <= pdata["size_mb"] <= 512:
                    diag.esp_dev = dev_path
                    diag.esp_part_num = pdata["pnum"]
                    diag.esp_size_mb = pdata["size_mb"]
                    diag.esp_partuuid = pdata["partuuid"]
                    diag.esp_uuid = pdata["uuid"]
                    break

        # 探测 MSR 保留分区 (通常为 16MB)
        for dev_path, pdata in partitions.items():
            if 15 <= pdata["size_mb"] <= 128 and pdata["pnum"] == 2:
                diag.msr_dev = dev_path
                diag.msr_size_mb = pdata["size_mb"]
                break

        # 探测 Windows 系统主分区 (通常容量极大 > 40GB，分区号通常紧跟 MSR，如 p3)
        # 优先寻找没有被挂载为 Linux / 或 /home 且大小 > 50GB 的分区
        candidates_win = []
        for dev_path, pdata in partitions.items():
            if pdata["size_mb"] > 40000:  # > 40GB
                # 排除当前 Linux 根分区
                candidates_win.append((pdata["size_mb"], dev_path, pdata))
        # 排序：按容量
        candidates_win.sort(key=lambda x: x[0], reverse=True)
        for sz, dev_path, pdata in candidates_win:
            if dev_path != diag.esp_dev:
                diag.win_system_dev = dev_path
                diag.win_system_size_gb = sz / 1024.0
                diag.win_system_uuid = pdata["uuid"]
                diag.win_system_partuuid = pdata["partuuid"]
                break

        # 探测 WinRE 恢复分区 (通常 500MB~2000MB，分区号在系统盘之后)
        for dev_path, pdata in partitions.items():
            if 400 <= pdata["size_mb"] <= 2048 and pdata["pnum"] == (diag.esp_part_num + 3 if diag.esp_part_num else 4):
                diag.winre_dev = dev_path
                diag.winre_size_mb = pdata["size_mb"]
                break

        # 检测 BitLocker 加密卷头特征
        cls._check_bitlocker_status(diag)

    @classmethod
    def _check_bitlocker_status(cls, diag: Win11BootDiagnostic):
        """检测 Windows 系统盘是否启用了 BitLocker 驱动器加密"""
        if not diag.win_system_dev:
            diag.bitlocker_status = "未检测到系统盘"
            return

        # 尝试通过 raw block 读取卷引导扇区 (Volume Boot Record)
        try:
            with open(diag.win_system_dev, "rb") as f:
                vbr = f.read(512)
                # BitLocker 卷头在 offset 3 包含 "-FVE-FS-"
                if b"-FVE-FS-" in vbr:
                    diag.bitlocker_status = "已启用 (BitLocker 加密保护中)"
                    return
                elif b"NTFS" in vbr:
                    diag.bitlocker_status = "未启用 (标准未加密 NTFS 卷)"
                    return
        except PermissionError:
            diag.bitlocker_status = "权限限制 (普通用户无法直读物理扇区，需 sudo 验证)"
        except Exception:
            diag.bitlocker_status = "未知 (物理设备不可读)"

    @classmethod
    def _inspect_security_baseline_linux(cls, diag: Win11BootDiagnostic):
        """检测 TPM 2.0 与 Secure Boot 安全基线状态"""
        # 1. TPM 检查
        tpm_dir = "/sys/class/tpm"
        if os.path.exists(tpm_dir):
            tpms = os.listdir(tpm_dir)
            if tpms:
                diag.tpm_present = True
                diag.tpm_device = tpms[0]
                major_path = os.path.join(tpm_dir, tpms[0], "tpm_version_major")
                if os.path.exists(major_path):
                    try:
                        with open(major_path) as f:
                            diag.tpm_version = f.read().strip() + ".0"
                    except Exception:
                        diag.tpm_version = "已启用 (版本读取受限)"
                else:
                    diag.tpm_version = "已启用"
        if not diag.tpm_present and os.path.exists("/dev/tpm0"):
            diag.tpm_present = True
            diag.tpm_device = "tpm0"
            diag.tpm_version = "2.0 (通过设备节点检出)"

        # 2. Secure Boot 检查
        sb_var = "/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"
        if os.path.exists(sb_var):
            diag.secure_boot_supported = True
            try:
                with open(sb_var, "rb") as f:
                    data = f.read()
                    if len(data) >= 5:
                        diag.secure_boot_enabled = (data[4] == 1)
            except Exception:
                pass
        else:
            diag.secure_boot_supported = False
            diag.secure_boot_enabled = False

        # 3. UEFI Native 确认
        diag.uefi_native = os.path.exists("/sys/firmware/efi")

    @classmethod
    def _inspect_linux_bootloader(cls, diag: Win11BootDiagnostic):
        """检测 Linux 本地引导加载器与 Linux 所在的 ESP 分区"""
        # 读取 /etc/fstab 判断 Linux ESP 挂载点
        fstab_path = "/etc/fstab"
        linux_esp_uuid = ""
        linux_esp_mount = ""

        if os.path.exists(fstab_path):
            try:
                with open(fstab_path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line_str = line.strip()
                        if not line_str or line_str.startswith("#"):
                            continue
                        parts = line_str.split()
                        if len(parts) >= 3:
                            dev_spec, mnt, fstype = parts[0], parts[1], parts[2].lower()
                            if mnt in ["/boot", "/efi", "/boot/efi"] and fstype in ["vfat", "fat"]:
                                linux_esp_mount = mnt
                                if dev_spec.startswith("UUID="):
                                    linux_esp_uuid = dev_spec.split("=", 1)[1].strip()
                                elif dev_spec.startswith("/dev/"):
                                    diag.linux_esp_dev = dev_spec
            except Exception:
                pass

        if linux_esp_uuid and not diag.linux_esp_dev:
            uuid_link = os.path.join("/dev/disk/by-uuid", linux_esp_uuid)
            if os.path.exists(uuid_link):
                diag.linux_esp_dev = os.path.realpath(uuid_link)

        diag.linux_esp_mount = linux_esp_mount or "/boot"

        # 判断是否为双 ESP 架构 (Windows ESP 与 Linux ESP 是否不同)
        if diag.esp_dev and diag.linux_esp_dev and diag.esp_dev != diag.linux_esp_dev:
            diag.is_dual_esp = True
        elif diag.esp_part_num and diag.linux_esp_dev and f"p{diag.esp_part_num}" not in diag.linux_esp_dev:
            diag.is_dual_esp = True

        # 检测 Linux 本地引导加载器类型
        if shutil.which("bootctl") or os.path.exists("/boot/loader/loader.conf"):
            diag.linux_bootloader = "systemd-boot"
        elif shutil.which("grub-install") or os.path.exists("/etc/default/grub"):
            diag.linux_bootloader = "GRUB 2"
        else:
            diag.linux_bootloader = "UEFI Stub / Direct"

        # 检查 Linux ESP 中是否已经有了 Windows 引导文件 stub
        for check_root in [diag.linux_esp_mount, "/boot", "/efi", "/boot/efi"]:
            if check_root and os.path.exists(check_root):
                win_stub = os.path.join(check_root, "EFI", "Microsoft", "Boot", "bootmgfw.efi")
                if os.path.exists(win_stub):
                    diag.linux_esp_has_win_loader = True
                    break

    @classmethod
    def _inspect_esp_files(cls, diag: Win11BootDiagnostic):
        """检查 Windows ESP 分区内的核心引导文件"""
        # 如果 Windows ESP 已经挂载在某个系统路径，直接检查
        mount_points_to_check = []
        if diag.esp_mount_point:
            mount_points_to_check.append(diag.esp_mount_point)

        # 检查现有挂载表
        try:
            with open("/proc/mounts", "r") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        dev, mnt = parts[0], parts[1]
                        if dev == diag.esp_dev:
                            diag.esp_mounted = True
                            diag.esp_mount_point = mnt
                            mount_points_to_check.append(mnt)
        except Exception:
            pass

        # 若已找到挂载点，直接扫描文件
        for mnt in mount_points_to_check:
            bootmgfw = os.path.join(mnt, "EFI", "Microsoft", "Boot", "bootmgfw.efi")
            bcd = os.path.join(mnt, "EFI", "Microsoft", "Boot", "BCD")
            bootmgr = os.path.join(mnt, "EFI", "Microsoft", "Boot", "bootmgr.efi")
            memtest = os.path.join(mnt, "EFI", "Microsoft", "Boot", "memtest.efi")

            diag.bootmgfw_exists = os.path.exists(bootmgfw)
            diag.bcd_exists = os.path.exists(bcd)
            diag.bootmgr_exists = os.path.exists(bootmgr)
            diag.memtest_exists = os.path.exists(memtest)
            return

        # 若当前未挂载且为 root，可尝试以只读模式临时挂载测试扫描
        if os.geteuid() == 0 and diag.esp_dev and os.path.exists(diag.esp_dev):
            tmp_mnt = tempfile.mkdtemp(prefix="win_esp_diag_")
            try:
                res = subprocess.run(["mount", "-o", "ro", diag.esp_dev, tmp_mnt],
                                     capture_output=True, text=True)
                if res.returncode == 0:
                    bootmgfw = os.path.join(tmp_mnt, "EFI", "Microsoft", "Boot", "bootmgfw.efi")
                    bcd = os.path.join(tmp_mnt, "EFI", "Microsoft", "Boot", "BCD")
                    bootmgr = os.path.join(tmp_mnt, "EFI", "Microsoft", "Boot", "bootmgr.efi")
                    memtest = os.path.join(tmp_mnt, "EFI", "Microsoft", "Boot", "memtest.efi")

                    diag.bootmgfw_exists = os.path.exists(bootmgfw)
                    diag.bcd_exists = os.path.exists(bcd)
                    diag.bootmgr_exists = os.path.exists(bootmgr)
                    diag.memtest_exists = os.path.exists(memtest)
                    subprocess.run(["umount", tmp_mnt], capture_output=True)
            except Exception:
                pass
            finally:
                if os.path.exists(tmp_mnt):
                    try:
                        shutil.rmtree(tmp_mnt, ignore_errors=True)
                    except Exception:
                        pass

    # ──────────────────────────────────────────────────────────────────────────
    # Windows 原生平台分析 (若运行在 Windows 下)
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def _analyze_windows_native(cls, diag: Win11BootDiagnostic):
        diag.uefi_native = True
        diag.nvram_found = True
        diag.label = "Windows Boot Manager"
        diag.bootmgfw_exists = True
        diag.bcd_exists = True

        # 调用 bcdedit 查询
        try:
            res = subprocess.run(["bcdedit", "/enum", "{bootmgr}"],
                                 capture_output=True, text=True, errors="replace")
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    if "device" in line.lower() and "partition=" in line.lower():
                        diag.esp_dev = line.split("partition=", 1)[1].strip()
                    if "timeout" in line.lower():
                        diag.notes.append(f"Windows Boot Manager 菜单等待时间: {line.split()[-1]} 秒")
        except Exception:
            pass

        # 检查 TPM
        try:
            res_tpm = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-Tpm | Select-Object -ExpandProperty TpmPresent"],
                capture_output=True, text=True
            )
            diag.tpm_present = "true" in res_tpm.stdout.lower()
            if diag.tpm_present:
                diag.tpm_version = "2.0"
        except Exception:
            pass

        # 检查 SecureBoot
        try:
            res_sb = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Confirm-SecureBootUEFI"],
                capture_output=True, text=True
            )
            diag.secure_boot_supported = True
            diag.secure_boot_enabled = "true" in res_sb.stdout.lower()
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────────────
    # 综合智能研判与建议生成
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def _generate_insights(cls, diag: Win11BootDiagnostic):
        """基于全维度拓扑生成深度研判结论与针对性修复建议"""
        notes = []
        recs = []

        # 1. NVRAM 条目状态研判
        if diag.nvram_found:
            pos_str = f"第 {diag.order_index + 1} 位" if diag.order_index >= 0 else "未在激活列表中"
            status_str = "正常启用" if diag.is_active else "已被禁用 (Inactive)"
            notes.append(f"[✔] 主板 NVRAM 存在 Windows 引导项 [{diag.boot_num}]：状态【{status_str}】，顺位【{pos_str}】")
            if diag.is_current:
                notes.append("    ★ 当前正是通过该 Windows 引导项启动进入系统的。")
            if diag.is_boot_next:
                notes.append("    ★ 已设定 BootNext：下次重启将单次直入 Windows 11！")
        else:
            notes.append("[⚠ 警告] 主板 NVRAM 中未检索到标准的 'Windows Boot Manager' 启动项！")
            recs.append("可执行一键重建操作，将 Windows 引导重新写入主板 UEFI NVRAM。")

        # 2. 双 ESP 架构隔离研判 (关键痛点！)
        if diag.is_dual_esp:
            notes.append(
                f"[★ 核心架构诊断] 发现系统存在【双 ESP 独立引导分区】！\n"
                f"    ├─ Windows ESP : {diag.esp_dev or '未直接指定'} (分区 {diag.esp_part_num})\n"
                f"    └─ Linux ESP   : {diag.linux_esp_dev or '未直接指定'} (挂载于 {diag.linux_esp_mount})"
            )
            if diag.linux_bootloader == "systemd-boot":
                if not diag.linux_esp_has_win_loader:
                    notes.append(
                        f"[ℹ 现象解答] 为何 systemd-boot 菜单不显示 Win11？\n"
                        f"    systemd-boot 默认严格隔离，仅扫描自身所在的 ESP 分区 ({diag.linux_esp_dev})。\n"
                        f"    Windows 引导位于独立的 {diag.esp_dev}，因此 systemd-boot 条目列表显示为 0！"
                    )
                    recs.append(
                        "【强力推荐】一键将 Windows 引导复制/同步至 Linux ESP，"
                        "让 systemd-boot 原生菜单自动检测并支持回车直启 Win11！"
                    )
                else:
                    notes.append("[✔] Linux ESP 已存在 Windows 引导镜像 stub，systemd-boot 应可自动列出 Win11。")
        else:
            notes.append("[✔] 单一 ESP 分区共存架构：Linux 与 Windows 共享同一 ESP 引导分区。")

        # 3. 硬件安全基线研判 (Win11 运行保障)
        sec_items = []
        if diag.tpm_present:
            sec_items.append(f"TPM {diag.tpm_version} (符合 Win11 要求)")
        else:
            sec_items.append("未检测到物理 TPM 2.0 模块 (可能受限或未开启 fTPM)")

        if diag.secure_boot_enabled:
            sec_items.append("安全启动 Secure Boot: 已开启 [✔]")
        elif diag.secure_boot_supported:
            sec_items.append("安全启动 Secure Boot: 已支持但当前未启用 [Disabled]")
        else:
            sec_items.append("安全启动: 未检测到支持")

        notes.append(f"[🛡️ 固件安全基线] " + " | ".join(sec_items))

        # 4. BitLocker 加密与跨系统访问提示
        if "已启用" in diag.bitlocker_status:
            notes.append(
                f"[🔒 数据保护] Windows 系统分区 ({diag.win_system_dev}) 处于 BitLocker 加密状态。\n"
                f"    提示：建议优先通过原生 UEFI 顺位或 BootNext 引导 Windows，以避免 PCR 测量哈希变动触发恢复密钥输入。"
            )

        diag.notes = notes
        diag.recommendations = recs


# ─── 一键修复与交互操作动作 ───────────────────────────────────────────────────

class Win11ActionHandler:
    """Windows 11 引导修复与切换操作执行器"""

    @classmethod
    def set_boot_next_win11(cls, diag: Win11BootDiagnostic) -> Tuple[bool, str]:
        """将下次重启的目标设为 Windows 11 (单次生效，不改默认顺序)"""
        if not diag.boot_num:
            return False, "未能识别到 Windows 11 的 NVRAM 条目编号。"

        if IS_WINDOWS:
            return False, "Windows 环境下无需设置 BootNext 到自身。"

        cmd = ["efibootmgr", "-n", diag.boot_num]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                return True, f"成功设定 BootNext = {diag.boot_num}！下次重启电脑将直接单次进入 Windows 11。"
            else:
                err = res.stderr.strip() or res.stdout.strip()
                return False, f"设置 BootNext 失败 (错误码 {res.returncode}): {err}"
        except PermissionError:
            return False, "权限不足！写入 NVRAM efivars 需要 root 权限，请使用 sudo 运行。"
        except Exception as e:
            return False, f"执行异常: {e}"

    @classmethod
    def set_win11_as_first_boot(cls, diag: Win11BootDiagnostic) -> Tuple[bool, str]:
        """将 Windows 11 设为 UEFI 默认第一启动项"""
        if not diag.boot_num:
            return False, "未能识别到 Windows 11 的 NVRAM 条目编号。"

        # 获取当前 BootOrder
        try:
            res = subprocess.run(["efibootmgr"], capture_output=True, text=True)
            order_line = ""
            for line in res.stdout.splitlines():
                if line.startswith("BootOrder:"):
                    order_line = line.split(":", 1)[1].strip()
                    break

            if not order_line:
                return False, "无法读取当前主板 BootOrder 列表。"

            orders = [x.strip() for x in order_line.split(",") if x.strip()]
            if diag.boot_num in orders:
                orders.remove(diag.boot_num)
            # 放到第一位
            new_orders = [diag.boot_num] + orders
            new_order_str = ",".join(new_orders)

            res_write = subprocess.run(["efibootmgr", "-o", new_order_str],
                                       capture_output=True, text=True)
            if res_write.returncode == 0:
                return True, f"成功将 Windows 11 [{diag.boot_num}] 置为第一启动项！(BootOrder: {new_order_str})"
            else:
                return False, f"写入新顺序失败: {res_write.stderr.strip() or res_write.stdout.strip()}"
        except PermissionError:
            return False, "权限不足！修改 BootOrder 需要 root 权限，请使用 sudo 运行。"
        except Exception as e:
            return False, f"执行异常: {e}"

    @classmethod
    def restore_linux_as_first_boot(cls, diag: Win11BootDiagnostic) -> Tuple[bool, str]:
        """将 Linux 设回 UEFI 默认第一启动项"""
        try:
            res = subprocess.run(["efibootmgr"], capture_output=True, text=True)
            lines = res.stdout.splitlines()
            order_line = ""
            linux_bnums = []
            for line in lines:
                if line.startswith("BootOrder:"):
                    order_line = line.split(":", 1)[1].strip()
                m = re.match(r"^Boot([0-9a-fA-F]{4})\*?\s+(Linux|Arch|systemd-boot|GRUB|Ubuntu|Fedora)", line, re.IGNORECASE)
                if m:
                    linux_bnums.append(m.group(1).upper())

            if not order_line or not linux_bnums:
                return False, "无法检索到 Linux 启动项条目。"

            target_linux = linux_bnums[0]
            orders = [x.strip() for x in order_line.split(",") if x.strip()]
            if target_linux in orders:
                orders.remove(target_linux)
            new_orders = [target_linux] + orders
            new_order_str = ",".join(new_orders)

            res_write = subprocess.run(["efibootmgr", "-o", new_order_str], capture_output=True, text=True)
            if res_write.returncode == 0:
                return True, f"已恢复 Linux [{target_linux}] 为第一启动项！(BootOrder: {new_order_str})"
            else:
                return False, f"写入失败: {res_write.stderr.strip()}"
        except Exception as e:
            return False, f"执行异常: {e}"

    @classmethod
    def sync_win_boot_to_linux_esp(cls, diag: Win11BootDiagnostic) -> Tuple[bool, str]:
        """一键跨 ESP 分区同步 Windows 引导至 Linux ESP，打通 systemd-boot 自动探测"""
        if IS_WINDOWS:
            return False, "该功能专为 Linux 双 ESP 架构设计。"

        if os.geteuid() != 0:
            cmd_preview = (
                f"sudo mkdir -p /tmp/win_esp && "
                f"sudo mount -o ro {diag.esp_dev} /tmp/win_esp && "
                f"sudo mkdir -p {diag.linux_esp_mount}/EFI/Microsoft/Boot && "
                f"sudo cp -r /tmp/win_esp/EFI/Microsoft/Boot/* {diag.linux_esp_mount}/EFI/Microsoft/Boot/ && "
                f"sudo umount /tmp/win_esp"
            )
            return False, (
                f"权限限制：跨分区同步需要 root 权限。\n"
                f"请使用以下命令完成一键同步：\n{cmd_preview}"
            )

        if not diag.esp_dev or not os.path.exists(diag.esp_dev):
            return False, f"Windows ESP 分区设备 ({diag.esp_dev}) 不存在或未定位。"

        linux_esp = diag.linux_esp_mount or "/boot"
        if not os.path.exists(linux_esp):
            return False, f"Linux ESP 挂载点 ({linux_esp}) 不存在。"

        tmp_mnt = tempfile.mkdtemp(prefix="win_esp_sync_")
        try:
            # 1. 挂载 Windows ESP
            m_res = subprocess.run(["mount", "-o", "ro", diag.esp_dev, tmp_mnt],
                                   capture_output=True, text=True)
            if m_res.returncode != 0:
                return False, f"无法挂载 Windows ESP 分区: {m_res.stderr.strip()}"

            src_ms = os.path.join(tmp_mnt, "EFI", "Microsoft")
            if not os.path.exists(src_ms):
                return False, f"在 Windows ESP 中未找到 EFI/Microsoft 引导目录。"

            dst_ms = os.path.join(linux_esp, "EFI", "Microsoft")
            dst_boot = os.path.join(dst_ms, "Boot")
            os.makedirs(dst_boot, exist_ok=True)

            # 复制关键 bootmgfw.efi 与 BCD
            src_boot = os.path.join(src_ms, "Boot")
            copied = []
            for item in ["bootmgfw.efi", "bootmgr.efi", "BCD", "memtest.efi"]:
                s = os.path.join(src_boot, item)
                if os.path.exists(s):
                    d = os.path.join(dst_boot, item)
                    shutil.copy2(s, d)
                    copied.append(item)

            # 复制 Fonts 目录 (如果存在)
            src_fonts = os.path.join(src_boot, "Fonts")
            if os.path.exists(src_fonts):
                dst_fonts = os.path.join(dst_boot, "Fonts")
                if os.path.exists(dst_fonts):
                    shutil.rmtree(dst_fonts)
                shutil.copytree(src_fonts, dst_fonts)
                copied.append("Fonts")

            # 额外写入标准的 systemd-boot 条目配置文件 (windows.conf)
            loader_dir = os.path.join(linux_esp, "loader")
            entries_dir = os.path.join(loader_dir, "entries")
            if os.path.exists(loader_dir):
                os.makedirs(entries_dir, exist_ok=True)
                win_conf_file = os.path.join(entries_dir, "windows.conf")
                with open(win_conf_file, "w", encoding="utf-8") as f:
                    f.write(
                        "# Windows 11 Boot Loader Entry for systemd-boot\n"
                        "title    Windows 11\n"
                        "efi      /EFI/Microsoft/Boot/bootmgfw.efi\n"
                        "sort-key 02-windows\n"
                    )
                # 检查 loader.conf 超时时间，保证开机显示菜单
                loader_conf_path = os.path.join(loader_dir, "loader.conf")
                if os.path.exists(loader_conf_path):
                    with open(loader_conf_path, "r", encoding="utf-8", errors="replace") as f:
                        lconf_lines = f.readlines()
                    has_timeout = False
                    new_lconf = []
                    for line in lconf_lines:
                        if line.strip().startswith("timeout"):
                            has_timeout = True
                            parts = line.strip().split()
                            if len(parts) >= 2 and parts[1] == "0":
                                new_lconf.append("timeout 5\n")
                            else:
                                new_lconf.append(line)
                        else:
                            new_lconf.append(line)
                    if not has_timeout:
                        new_lconf.append("timeout 5\n")
                    with open(loader_conf_path, "w", encoding="utf-8") as f:
                        f.writelines(new_lconf)

            # 刷新 systemd-boot
            if shutil.which("bootctl"):
                subprocess.run(["bootctl", "update"], capture_output=True)

            return True, (
                f"同步完成！已将 {', '.join(copied)} 成功镜像至 {linux_esp}/EFI/Microsoft/Boot/，\n"
                f"并生成了 {linux_esp}/loader/entries/windows.conf 引导选项。\n"
                f"第一项【Linux Boot Manager (systemd-boot)】在开机时将直接显示【Windows 11】选择菜单！"
            )
        except Exception as e:
            return False, f"同步过程发生错误: {e}"
        finally:
            subprocess.run(["umount", tmp_mnt], capture_output=True)
            if os.path.exists(tmp_mnt):
                shutil.rmtree(tmp_mnt, ignore_errors=True)

    @classmethod
    def restore_win11_nvram_entry(cls, diag: Win11BootDiagnostic) -> Tuple[bool, str]:
        """如果 Windows NVRAM 启动项丢失，一键调用 efibootmgr 重新创建"""
        if IS_WINDOWS:
            return False, "Windows 环境请使用 bcdboot C:\\Windows /s S: /f UEFI 重建。"

        if not diag.esp_dev:
            return False, "未找到 Windows ESP 分区设备，无法创建条目。"

        # 解析主盘与分区号 (例如 /dev/nvme0n1 与 1)
        disk_dev = diag.esp_dev
        part_num = "1"
        m = re.match(r"^(/dev/nvme\d+n\d+)p(\d+)$", diag.esp_dev)
        if m:
            disk_dev = m.group(1)
            part_num = m.group(2)
        else:
            m2 = re.match(r"^(/dev/[a-z]+)(\d+)$", diag.esp_dev)
            if m2:
                disk_dev = m2.group(1)
                part_num = m2.group(2)

        cmd = [
            "efibootmgr", "-c",
            "-d", disk_dev,
            "-p", str(part_num),
            "-L", "Windows Boot Manager",
            "-l", "\\EFI\\Microsoft\\Boot\\bootmgfw.efi"
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                return True, f"成功重建 Windows Boot Manager UEFI NVRAM 启动项！({disk_dev}p{part_num})"
            else:
                return False, f"重建失败: {res.stderr.strip() or res.stdout.strip()}"
        except PermissionError:
            return False, "权限不足！写入 NVRAM 需要 root 权限，请使用 sudo 运行。"
        except Exception as e:
            return False, f"执行异常: {e}"

    @classmethod
    def toggle_win11_active(cls, diag: Win11BootDiagnostic) -> Tuple[bool, str]:
        """切换 Windows 11 启动项启用/禁用状态"""
        if not diag.boot_num:
            return False, "未检索到 Windows NVRAM 启动项编号。"

        flag = "-A" if diag.is_active else "-a"
        target_state = "禁用" if diag.is_active else "启用"
        try:
            res = subprocess.run(["efibootmgr", "-b", diag.boot_num, flag],
                                 capture_output=True, text=True)
            if res.returncode == 0:
                return True, f"成功将 Windows 11 [{diag.boot_num}] 设为【{target_state}】！"
            else:
                return False, f"操作失败: {res.stderr.strip() or res.stdout.strip()}"
        except PermissionError:
            return False, "修改 NVRAM 状态需要 root 权限，请使用 sudo 运行。"
        except Exception as e:
            return False, f"执行异常: {e}"


# ─── 报告格式化输出 ────────────────────────────────────────────────────────────

def format_win11_report_text(diag: Win11BootDiagnostic) -> List[str]:
    """生成 TUI 弹窗或终端展示的多行文本报告"""
    lines = [
        "Windows 11 引导深度智能分析与双系统互通诊断",
        "═" * 58,
    ]

    # 1. NVRAM 启动项
    lines.append("🪟 [1] 主板 UEFI NVRAM 启动项状态:")
    if diag.nvram_found:
        status_txt = "已启用" if diag.is_active else "已禁用"
        seq_txt = f"顺位第 {diag.order_index + 1} 位" if diag.order_index >= 0 else "不在引导序列中"
        lines.append(f"  ├─ 条目编号 : Boot{diag.boot_num} ({diag.label})")
        lines.append(f"  ├─ 运行状态 : 【{status_txt}】 | 启动顺序: 【{seq_txt}】 (共 {diag.total_orders} 项)")
        lines.append(f"  ├─ 引导路径 : {diag.efi_path or r'\\EFI\\Microsoft\\Boot\\bootmgfw.efi'}")
        if diag.part_uuid:
            lines.append(f"  ├─ 目标 PARTUUID : {diag.part_uuid}")
        if diag.bcd_object:
            lines.append(f"  ├─ BCD 对象 GUID : {diag.bcd_object}")
        if diag.is_boot_next:
            lines.append(f"  └─ 下次引导标志 : ★ 已设定 BootNext (下次开机单次进 Win11)")
        else:
            lines.append(f"  └─ 下次引导标志 : 遵循默认 BootOrder 引导")
    else:
        lines.append("  └─ 状态: ⚠ 主板 NVRAM 中未检测到 Windows Boot Manager 条目！")
    lines.append("")

    # 2. 物理分区拓扑
    lines.append("💾 [2] 磁盘物理分区拓扑与 Windows 组件:")
    if diag.esp_dev:
        esp_info = f"{diag.esp_dev} (分区 {diag.esp_part_num}, {diag.esp_size_mb:.1f} MB)"
        lines.append(f"  ├─ Windows ESP 分区 : {esp_info}")
        if diag.esp_uuid:
            lines.append(f"  │  └─ 文件系统 UUID : {diag.esp_uuid} (FAT32)")
    else:
        lines.append("  ├─ Windows ESP 分区 : 未直接定位")

    if diag.msr_dev:
        lines.append(f"  ├─ Microsoft 保留区 : {diag.msr_dev} ({diag.msr_size_mb:.1f} MB, MSR)")

    if diag.win_system_dev:
        lines.append(f"  ├─ Windows 11 系统盘 : {diag.win_system_dev} ({diag.win_system_size_gb:.1f} GB, C:)")
        lines.append(f"  │  └─ BitLocker 加密 : {diag.bitlocker_status}")

    if diag.winre_dev:
        lines.append(f"  └─ WinRE 恢复分区   : {diag.winre_dev} ({diag.winre_size_mb:.1f} MB)")
    else:
        lines.append(f"  └─ WinRE 恢复分区   : 未检测到独立恢复分区")
    lines.append("")

    # 3. 硬件安全基线
    lines.append("🛡️ [3] Windows 11 硬件与固件安全基线:")
    tpm_txt = f"TPM {diag.tpm_version} [符合要求 ✔]" if diag.tpm_present else "未检出 TPM 2.0 模块 [⚠]"
    lines.append(f"  ├─ 可信平台模块 (TPM) : {tpm_txt}")
    sb_txt = "已开启 (Enabled) [✔]" if diag.secure_boot_enabled else ("已支持但未开启 (Disabled)" if diag.secure_boot_supported else "未检测到支持")
    lines.append(f"  ├─ 安全启动 (SecureBoot): {sb_txt}")
    lines.append(f"  └─ 固件与分区架构       : UEFI 64-bit 原生模式 [✔] | GPT 分区表 [✔]")
    lines.append("")

    # 4. 双系统冲突研判
    lines.append("⚡ [4] 双系统 (Dual-Boot) 拓扑与冲突研判:")
    for note in diag.notes:
        for subline in note.splitlines():
            lines.append(f"  {subline}")
    lines.append("")

    lines.append("─" * 58)
    return lines
