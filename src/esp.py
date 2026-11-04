#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
  ESP (EFI System Partition) 物理引导分区真实检测与挂载分析模块 (src/esp.py)
================================================================================
支持 Windows 与 Linux 跨平台真实探测：
- Windows: 基于 PowerShell 原生接口查询 GPT Type GUID {c12a7328-f81f-11d2-ba4b-00a0c93ec93b}，
           动态分配空闲盘符真实挂载、深度扫描、自动安全卸载。
- Linux:   基于 /proc/mounts、efibootmgr NVRAM 条目、lsblk 与 /dev/disk/by-partuuid
           智能定位物理 ESP 分区，自动检索已挂载或安全临时挂载扫描。
- 零假代码、零模拟数据，所有底层操作直接与系统 API 真实交互。
"""

import os
import sys
import json
import subprocess
import shutil
import re
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

IS_WINDOWS = sys.platform == "win32"


@dataclass
class EspPartitionInfo:
    """ESP 物理引导分区数据模型"""
    disk: str                                       # 物理磁盘标识 (如 "Disk 0" 或 "/dev/nvme0n1")
    part_num: int                                   # 分区编号 (如 1 或 5)
    device_path: str                                # 设备路径 (如 "/dev/nvme0n1p5")
    size_bytes: int                                 # 分区容量（字节）
    part_uuid: str = ""                             # GPT 分区 UUID (PARTUUID)
    mount_point: str = ""                           # 挂载点路径或盘符 (如 "S:\\" 或 "/boot/efi")
    is_temp_mounted: bool = False                   # 是否为本模块临时挂载
    files: List[Tuple[str, int]] = field(default_factory=list)      # (相对路径, 字节大小)
    efi_files: List[Tuple[str, str]] = field(default_factory=list)  # (绝对路径, 相对路径)
    cfg_files: List[Tuple[str, str]] = field(default_factory=list)  # (绝对路径, 相对路径)

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)


def get_available_drive_letters() -> List[str]:
    """获取 Windows 当前未被占用的可用盘符列表（逆序优先选 Z, Y, X, S 等）"""
    if not IS_WINDOWS:
        return []
    import ctypes
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    available = []
    for char in "ZYXWVUTSRQPONMLKJIHGFEDCBA":
        idx = ord(char) - ord('A')
        if not (bitmask & (1 << idx)):
            available.append(char)
    return available


def _scan_directory_structure(root_dir: str) -> Tuple[List[Tuple[str, int]], List[Tuple[str, str]], List[Tuple[str, str]]]:
    """递归遍历指定目录，收集所有文件、.efi 引导二进制及 .cfg/.conf 配置文件"""
    all_files = []
    efi_files = []
    cfg_files = []

    for root, dirs, files in os.walk(root_dir):
        for file in files:
            full_path = os.path.join(root, file)
            try:
                rel_path = os.path.relpath(full_path, root_dir)
                size = os.path.getsize(full_path)
            except Exception:
                continue

            # 转换为规范的反斜杠格式（符合 UEFI 路径习惯）
            efi_rel = "\\" + rel_path.replace("/", "\\")
            all_files.append((rel_path, size))

            lower_file = file.lower()
            if lower_file.endswith(".efi"):
                efi_files.append((full_path, efi_rel))
            elif lower_file.endswith(".cfg") or lower_file.endswith(".conf"):
                cfg_files.append((full_path, efi_rel))

    return all_files, efi_files, cfg_files


def find_esp_partitions_windows() -> List[EspPartitionInfo]:
    """Windows 下探测并挂载所有 ESP 分区"""
    results: List[EspPartitionInfo] = []

    # ESP 标准 GPT Type GUID
    ps_cmd = (
        "Get-Partition | Where-Object { $_.GptType -eq '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}' } | "
        "Select-Object DiskNumber, PartitionNumber, Size, DriveLetter, UniqueId | ConvertTo-Json"
    )

    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15
        )
    except Exception as e:
        print(f"[-] 查询分区失败: {e}")
        return results

    stdout = proc.stdout.strip()
    if not stdout:
        return results

    try:
        data = json.loads(stdout)
        partitions = [data] if isinstance(data, dict) else (data if isinstance(data, list) else [])
    except json.JSONDecodeError:
        return results

    for part in partitions:
        disk_num = part.get("DiskNumber", 0)
        part_num = part.get("PartitionNumber", 1)
        size_bytes = part.get("Size", 0)
        drive_letter = part.get("DriveLetter")
        unique_id = part.get("UniqueId", "")

        esp_info = EspPartitionInfo(
            disk=f"Disk {disk_num}",
            part_num=part_num,
            device_path=f"\\\\.\\Harddisk{disk_num}Partition{part_num}",
            size_bytes=size_bytes,
            part_uuid=unique_id
        )

        # 检查是否已分配盘符
        if drive_letter and os.path.exists(f"{drive_letter}:\\"):
            mount_path = f"{drive_letter}:\\"
            esp_info.mount_point = mount_path
            esp_info.is_temp_mounted = False
            files, efi_files, cfg_files = _scan_directory_structure(mount_path)
            esp_info.files = files
            esp_info.efi_files = efi_files
            esp_info.cfg_files = cfg_files
            results.append(esp_info)
            continue

        # 动态分配临时盘符挂载
        avail = get_available_drive_letters()
        if not avail:
            results.append(esp_info)
            continue

        mount_letter = avail[0]
        mount_ps = (
            f"Set-Partition -DiskNumber {disk_num} -PartitionNumber {part_num} -NewDriveLetter {mount_letter} -ErrorAction Stop"
        )
        unmount_ps = (
            f"Remove-PartitionAccessPath -DiskNumber {disk_num} -PartitionNumber {part_num} -AccessPath '{mount_letter}:\\' -ErrorAction SilentlyContinue"
        )

        try:
            m_res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", mount_ps],
                capture_output=True,
                text=True,
                timeout=10
            )
            if m_res.returncode == 0:
                drive_root = f"{mount_letter}:\\"
                if os.path.exists(drive_root):
                    esp_info.mount_point = drive_root
                    esp_info.is_temp_mounted = True
                    files, efi_files, cfg_files = _scan_directory_structure(drive_root)
                    esp_info.files = files
                    esp_info.efi_files = efi_files
                    esp_info.cfg_files = cfg_files
        finally:
            if esp_info.is_temp_mounted:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", unmount_ps],
                    capture_output=True,
                    timeout=10
                )

        results.append(esp_info)

    return results


def find_esp_partitions_linux() -> List[EspPartitionInfo]:
    """Linux 下真实探测所有 ESP 分区（已挂载与物理分区）"""
    results: List[EspPartitionInfo] = []

    # 1. 扫描当前挂载点
    mounted_vfat: Dict[str, str] = {}
    if os.path.exists("/proc/mounts"):
        try:
            with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 3 and parts[2] in ("vfat", "msdos", "fat"):
                        mounted_vfat[parts[0]] = parts[1]
        except Exception:
            pass

    # 2. 检查 efibootmgr 输出的实际 GPT ESP 分区
    efibootmgr_partitions = []
    try:
        proc = subprocess.run(["efibootmgr", "-v"], capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            # 匹配 HD(part_num, GPT, part_uuid, ...)
            matches = re.findall(r"HD\((\d+),GPT,([0-9a-fA-F-]+)", proc.stdout)
            seen_parts = set()
            for pnum, puuid in matches:
                key = (int(pnum), puuid.lower())
                if key not in seen_parts:
                    seen_parts.add(key)
                    efibootmgr_partitions.append((int(pnum), puuid.lower()))
    except Exception:
        pass

    # 3. 检查 /dev/disk/by-partuuid
    partuuid_to_dev: Dict[str, str] = {}
    partuuid_dir = "/dev/disk/by-partuuid"
    if os.path.exists(partuuid_dir):
        try:
            for entry in os.listdir(partuuid_dir):
                full_symlink = os.path.join(partuuid_dir, entry)
                real_dev = os.path.realpath(full_symlink)
                partuuid_to_dev[entry.lower()] = real_dev
        except Exception:
            pass

    # 4. 尝试使用 lsblk 查询所有块设备与分区类型
    known_devices: Dict[str, Dict] = {}
    try:
        lsblk_cmd = ["lsblk", "-b", "-J", "-o", "NAME,PATH,SIZE,FSTYPE,PARTTYPE,PARTUUID,MOUNTPOINT,LABEL"]
        p = subprocess.run(lsblk_cmd, capture_output=True, text=True, timeout=5)
        if p.returncode == 0:
            lsblk_data = json.loads(p.stdout)
            def traverse(nodes):
                for node in nodes:
                    path = node.get("path")
                    if path:
                        known_devices[path] = node
                    if "children" in node:
                        traverse(node["children"])
            traverse(lsblk_data.get("blockdevices", []))
    except Exception:
        pass

    # 综合研判候选 ESP 设备列表
    candidate_devices: List[Dict] = []
    esp_type_guid = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"

    # A. 来自 lsblk 明确标识为 ESP 的设备
    for dev_path, info in known_devices.items():
        part_type = (info.get("parttype") or "").lower()
        fstype = (info.get("fstype") or "").lower()
        mount = info.get("mountpoint") or ""
        label = (info.get("label") or "").upper()

        is_esp = (
            part_type == esp_type_guid or
            (fstype in ("vfat", "fat") and (
                mount in ("/boot", "/boot/efi", "/efi") or
                label in ("EFI", "ESP", "SYSTEM", "BOOT")
            ))
        )
        if is_esp:
            candidate_devices.append({
                "dev_path": dev_path,
                "size": info.get("size", 0),
                "partuuid": (info.get("partuuid") or "").lower(),
                "mountpoint": mount,
            })

    # B. 来自 efibootmgr 实际注册的 GPT 引导分区
    for pnum, puuid in efibootmgr_partitions:
        dev_path = partuuid_to_dev.get(puuid)
        if dev_path and not any(c["dev_path"] == dev_path for c in candidate_devices):
            node = known_devices.get(dev_path, {})
            candidate_devices.append({
                "dev_path": dev_path,
                "size": node.get("size", 0),
                "partuuid": puuid,
                "mountpoint": mounted_vfat.get(dev_path, node.get("mountpoint", "")),
                "part_num": pnum
            })

    # C. 常见常规挂载点检测兜底 (/boot/efi, /efi)
    for std_mount in ("/boot/efi", "/efi", "/boot"):
        if os.path.exists(std_mount) and os.path.ismount(std_mount):
            for dev, mnt in mounted_vfat.items():
                if mnt == std_mount and not any(c.get("mountpoint") == std_mount for c in candidate_devices):
                    node = known_devices.get(dev, {})
                    candidate_devices.append({
                        "dev_path": dev,
                        "size": node.get("size", 0),
                        "partuuid": node.get("partuuid", "").lower(),
                        "mountpoint": std_mount
                    })

    # 对候选设备逐个扫描
    for c in candidate_devices:
        dev_path = c["dev_path"]
        mountpoint = c.get("mountpoint") or mounted_vfat.get(dev_path)
        partuuid = c.get("partuuid", "")
        size_bytes = c.get("size", 0)

        # 提取磁盘名与分区编号
        m_part = re.search(r"(?:nvme\d+n\d+p|sd[a-z]|vd[a-z]|xvd[a-z])(\d+)$", dev_path)
        part_num = int(m_part.group(1)) if m_part else c.get("part_num", 1)
        disk_name = re.sub(r"p?\d+$", "", dev_path)

        esp_info = EspPartitionInfo(
            disk=disk_name,
            part_num=part_num,
            device_path=dev_path,
            size_bytes=size_bytes,
            part_uuid=partuuid
        )

        if mountpoint and os.path.exists(mountpoint):
            # 已挂载，直接安全读取
            esp_info.mount_point = mountpoint
            esp_info.is_temp_mounted = False
            files, efi_files, cfg_files = _scan_directory_structure(mountpoint)
            esp_info.files = files
            esp_info.efi_files = efi_files
            esp_info.cfg_files = cfg_files
            results.append(esp_info)
        elif os.geteuid() == 0:
            # root 权限：创建安全临时挂载点只读扫描
            tmp_mount = f"/tmp/esp_mount_{os.path.basename(dev_path)}"
            os.makedirs(tmp_mount, exist_ok=True)
            try:
                m_proc = subprocess.run(
                    ["mount", "-t", "vfat", "-o", "ro", dev_path, tmp_mount],
                    capture_output=True,
                    timeout=5
                )
                if m_proc.returncode == 0:
                    esp_info.mount_point = tmp_mount
                    esp_info.is_temp_mounted = True
                    files, efi_files, cfg_files = _scan_directory_structure(tmp_mount)
                    esp_info.files = files
                    esp_info.efi_files = efi_files
                    esp_info.cfg_files = cfg_files
            finally:
                if esp_info.is_temp_mounted:
                    subprocess.run(["umount", tmp_mount], capture_output=True, timeout=5)
                    try:
                        os.rmdir(tmp_mount)
                    except Exception:
                        pass
            results.append(esp_info)
        else:
            # 非 root 且未挂载，保留分区元信息
            results.append(esp_info)

    return results


def find_all_esp_partitions() -> List[EspPartitionInfo]:
    """统一跨平台检索入口"""
    if IS_WINDOWS:
        return find_esp_partitions_windows()
    else:
        return find_esp_partitions_linux()


def inspect_esp_partitions() -> None:
    """真实探测并分析当前系统所有磁盘的 ESP 分区，输出真实文件列表及引导配置内容"""
    print("\n" + "=" * 70)
    print("  ESP (EFI System Partition) 物理引导分区真实检测与挂载分析")
    print("=" * 70)

    partitions = find_all_esp_partitions()
    if not partitions:
        print("[-] 未在当前系统中检测到任何 EFI System Partition (ESP) 分区。")
        if not IS_WINDOWS and os.geteuid() != 0:
            print("[!] 提示：在 Linux 下读取未挂载的物理分区通常需要 root (sudo) 权限。")
        return

    print(f"[+] 成功检测到 {len(partitions)} 个 ESP 物理引导分区：\n")

    for idx, esp in enumerate(partitions, start=1):
        print("----------------------------------------------------------------------")
        print(f"【ESP 分区 #{idx}】磁盘: {esp.disk} | 分区: {esp.part_num} | 设备: {esp.device_path}")
        print(f"  容量: {esp.size_mb} MB | PARTUUID: {esp.part_uuid or '(未知)'}")
        print(f"  状态: {'已挂载 (' + esp.mount_point + ')' if esp.mount_point else '未挂载 (需 root 权限读取)'}")
        print("----------------------------------------------------------------------")

        if not esp.mount_point and not esp.files:
            print("  (分区当前未挂载；如需深度扫描文件，请使用 sudo 重新运行)")
            continue

        if not esp.files:
            print("  (分区为空或挂载后未发现文件)")
            continue

        print(f"[+] 扫描文件结构 (发现 {len(esp.files)} 个文件):")
        for rel_path, size in esp.files:
            print(f"    ├─ {rel_path}  ({size:,} bytes)")

        # 重点显示发现的 .efi 引导可执行文件
        if esp.efi_files:
            print(f"\n[+] 发现 {len(esp.efi_files)} 个 EFI 引导可执行程序 (.efi)：")
            for full_path, efi_rel in esp.efi_files:
                print(f"    ★ {efi_rel}")

        # 读取并展示所有发现的 .cfg / .conf 配置文件内容
        if esp.cfg_files:
            print(f"\n[+] 发现 {len(esp.cfg_files)} 个引导配置文件：")
            for full_path, rel_path in esp.cfg_files:
                print(f"\n    ┌──【文件内容: {rel_path}】──────────────────────")
                try:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    for line in content.splitlines():
                        print(f"    │ {line}")
                except Exception as err:
                    print(f"    │ (读取失败: {err})")
                print(f"    └──────────────────────────────────────────────────────────")

    print("\n" + "=" * 70)
    print("  所有 ESP 引导分区探测完成。")
    print("=" * 70 + "\n")
