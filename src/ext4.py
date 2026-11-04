#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
  Ext4 物理分区免驱动解析与数据访问引擎 (src/ext4.py)
================================================================================
纯 Python 标准库原生实现（基于 ctypes 与 Windows 裸盘访问接口）：
- 支持直接读取 Windows 物理硬盘 (\\.\\PhysicalDrive0) 上的 ext4 分区
- 自动解析 Ext4 超级块 (Superblock)、块组描述符 (GDT)
- 支持 Inode 索引表解析与 Extent 目录树 (Extent Tree, 0xF30A) 递归遍历
- 支持解析 ext4_dir_entry_2 目录项，按绝对路径定位文件
- 支持读取文件数据与符号链接 (Fast Symlinks & Regular Symlinks)
- 零第三方依赖、零内核驱动、零蓝屏风险
"""

import os
import sys
import struct
import ctypes
from ctypes import wintypes
from typing import Optional, Tuple, List, Dict


# ─── Windows 裸盘访问底层接口 (ctypes) ──────────────────────────────────────────

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
INVALID_HANDLE_VALUE = -1


class RawDiskReader:
    """Windows 物理磁盘扇区对齐裸设备读取器"""

    def __init__(self, disk_path: str = r"\\.\PhysicalDrive0"):
        self.disk_path = disk_path
        self.handle = None
        self._open()

    def _open(self):
        kernel32 = ctypes.windll.kernel32
        self.handle = kernel32.CreateFileW(
            self.disk_path,
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            None
        )
        if self.handle == INVALID_HANDLE_VALUE or self.handle == 0:
            err = kernel32.GetLastError()
            raise PermissionError(
                f"无法以只读权限打开物理磁盘 {self.disk_path} (错误码: {err})。"
                "请确认已具有管理员权限 (Run as Administrator)。"
            )

    def read_exact(self, byte_offset: int, size: int) -> bytes:
        """从物理磁盘指定绝对字节偏移处读取数据（自动处理 4096 扇区对齐）"""
        if not self.handle:
            self._open()

        kernel32 = ctypes.windll.kernel32
        sector_size = 4096
        aligned_offset = (byte_offset // sector_size) * sector_size
        delta = byte_offset - aligned_offset
        needed_bytes = delta + size
        aligned_size = ((needed_bytes + sector_size - 1) // sector_size) * sector_size

        # 设置指针
        li_offset = wintypes.LARGE_INTEGER(aligned_offset)
        new_fp = wintypes.LARGE_INTEGER(0)
        res = kernel32.SetFilePointerEx(self.handle, li_offset, ctypes.byref(new_fp), 0)
        if not res:
            err = kernel32.GetLastError()
            raise OSError(f"SetFilePointerEx 失败，偏移: {aligned_offset} (错误码: {err})")

        # 读取扇区
        buf = ctypes.create_string_buffer(aligned_size)
        bytes_read = wintypes.DWORD(0)
        res = kernel32.ReadFile(self.handle, buf, aligned_size, ctypes.byref(bytes_read), None)
        if not res or bytes_read.value < needed_bytes:
            err = kernel32.GetLastError()
            raise OSError(f"ReadFile 读取失败，请求 {aligned_size} 字节 (错误码: {err})")

        raw_data = buf.raw[delta:delta + size]
        return raw_data

    def close(self):
        if self.handle and self.handle != INVALID_HANDLE_VALUE:
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# ─── Ext4 文件系统解析器 ───────────────────────────────────────────────────────

EXT4_SUPER_MAGIC = 0xEF53
EXT4_EXTENTS_FL = 0x00080000
EXT4_EXTENT_HEADER_MAGIC = 0xF30A
EXT4_ROOT_INO = 2

# 文件类型常数
S_IFMT = 0xF000
S_IFDIR = 0x4000
S_IFREG = 0x8000
S_IFLNK = 0xA000


class Ext4Partition:
    """表示一个位于裸磁盘特定偏移处的 Ext4 物理分区"""

    def __init__(self, reader: RawDiskReader, partition_offset: int, partition_size: int):
        self.reader = reader
        self.part_offset = partition_offset
        self.part_size = partition_size

        # 超级块信息
        self.block_size: int = 4096
        self.inodes_count: int = 0
        self.blocks_count: int = 0
        self.blocks_per_group: int = 0
        self.inodes_per_group: int = 0
        self.inode_size: int = 256
        self.uuid: str = ""
        self.volume_name: str = ""
        self.desc_size: int = 32
        self.is_64bit: bool = False

        self._parse_superblock()

    def _parse_superblock(self):
        """解析位于分区偏移 1024 字节处的超级块"""
        sb_data = self.reader.read_exact(self.part_offset + 1024, 1024)
        magic = struct.unpack_from("<H", sb_data, 0x38)[0]
        if magic != EXT4_SUPER_MAGIC:
            raise ValueError(f"不是合法的 Ext4 分区 (魔数不匹配: 0x{magic:04X} != 0xEF53)")

        self.inodes_count = struct.unpack_from("<I", sb_data, 0x00)[0]
        self.blocks_count = struct.unpack_from("<I", sb_data, 0x04)[0]
        log_block_size = struct.unpack_from("<I", sb_data, 0x18)[0]
        self.block_size = 1024 << log_block_size

        self.blocks_per_group = struct.unpack_from("<I", sb_data, 0x20)[0]
        self.inodes_per_group = struct.unpack_from("<I", sb_data, 0x28)[0]
        self.inode_size = struct.unpack_from("<H", sb_data, 0x58)[0]

        # 64位支持与描述符大小
        feature_incompat = struct.unpack_from("<I", sb_data, 0x60)[0]
        self.is_64bit = bool(feature_incompat & 0x0080)
        self.desc_size = struct.unpack_from("<H", sb_data, 0xFE)[0] if self.is_64bit else 32
        if self.desc_size < 32:
            self.desc_size = 32

        # UUID
        raw_uuid = sb_data[0x68:0x78]
        self.uuid = (
            f"{raw_uuid[0:4].hex()}-{raw_uuid[4:6].hex()}-"
            f"{raw_uuid[6:8].hex()}-{raw_uuid[8:10].hex()}-{raw_uuid[10:16].hex()}"
        )

        # 卷标
        raw_vol = sb_data[0x78:0x88].split(b"\x00")[0]
        self.volume_name = raw_vol.decode("utf-8", errors="replace")

    def _read_block(self, block_num: int, count: int = 1) -> bytes:
        """读取指定编号的块"""
        offset = self.part_offset + block_num * self.block_size
        return self.reader.read_exact(offset, count * self.block_size)

    def _get_inode_table_block(self, group_idx: int) -> int:
        """获取指定块组 (Block Group) 的 Inode 表起始块号"""
        # 描述符表通常位于超级块之后的块（如果 block_size > 1024，位于 block 1）
        gdt_start_block = 1 if self.block_size > 1024 else 2
        gdt_offset = self.part_offset + gdt_start_block * self.block_size + group_idx * self.desc_size
        desc_bytes = self.reader.read_exact(gdt_offset, self.desc_size)

        table_lo = struct.unpack_from("<I", desc_bytes, 0x08)[0]
        if self.is_64bit and self.desc_size >= 64:
            table_hi = struct.unpack_from("<I", desc_bytes, 0x28)[0]
            return (table_hi << 32) | table_lo
        return table_lo

    def read_inode(self, ino: int) -> Tuple[dict, bytes]:
        """读取指定 inode 编号的元数据字典及 i_block 载荷"""
        if ino < 1 or ino > self.inodes_count:
            raise ValueError(f"非法的 Inode 编号: {ino}")

        group_idx = (ino - 1) // self.inodes_per_group
        idx_in_group = (ino - 1) % self.inodes_per_group

        table_block = self._get_inode_table_block(group_idx)
        inode_byte_offset = (
            self.part_offset + table_block * self.block_size + idx_in_group * self.inode_size
        )

        inode_data = self.reader.read_exact(inode_byte_offset, self.inode_size)
        mode, uid_lo, size_lo = struct.unpack_from("<HHI", inode_data, 0x00)
        flags = struct.unpack_from("<I", inode_data, 0x20)[0]
        size_hi = struct.unpack_from("<I", inode_data, 0x6C)[0] if (mode & S_IFMT) == S_IFREG else 0
        total_size = (size_hi << 32) | size_lo

        meta = {
            "ino": ino,
            "mode": mode,
            "is_dir": (mode & S_IFMT) == S_IFDIR,
            "is_reg": (mode & S_IFMT) == S_IFREG,
            "is_lnk": (mode & S_IFMT) == S_IFLNK,
            "size": total_size,
            "flags": flags,
            "has_extents": bool(flags & EXT4_EXTENTS_FL),
        }
        i_block = inode_data[0x28:0x28 + 60]
        return meta, i_block

    def _read_extent_blocks(self, header_data: bytes) -> List[Tuple[int, int]]:
        """递归解析 Extent 树，返回 [(physical_block, block_count)] 列表"""
        magic, entries, max_entries, depth = struct.unpack_from("<HHHH", header_data, 0x00)
        if magic != EXT4_EXTENT_HEADER_MAGIC:
            raise ValueError(f"非法的 Extent 头部魔数: 0x{magic:04X} != 0xF30A")

        blocks = []
        if depth == 0:
            # 叶子节点: ext4_extent (12 字节)
            for i in range(entries):
                offset = 12 + i * 12
                ee_block, ee_len, ee_start_hi, ee_start_lo = struct.unpack_from("<IHHI", header_data, offset)
                actual_len = ee_len if ee_len <= 32768 else (ee_len - 32768)
                pblock = (ee_start_hi << 32) | ee_start_lo
                blocks.append((pblock, actual_len))
        else:
            # 内部节点: ext4_extent_idx (12 字节)
            for i in range(entries):
                offset = 12 + i * 12
                ei_block, ei_leaf_lo, ei_leaf_hi = struct.unpack_from("<IIH", header_data, offset)
                pblock = (ei_leaf_hi << 32) | ei_leaf_lo
                # 读取下一层 Extent 块
                next_block_data = self._read_block(pblock, 1)
                blocks.extend(self._read_extent_blocks(next_block_data))

        return blocks

    def read_file_content(self, meta: dict, i_block: bytes) -> bytes:
        """根据 Inode 信息读取文件的全部字节内容"""
        file_size = meta["size"]
        if file_size == 0:
            return b""

        # 针对小于 60 字节的快速符号链接 (Fast Symlink)
        if meta["is_lnk"] and file_size <= 60:
            return i_block[:file_size]

        if not meta["has_extents"]:
            raise NotImplementedError("暂不支持非 Extent 模式的老旧 Ext2/3 直接/间接块映射文件。")

        extent_runs = self._read_extent_blocks(i_block)
        result = bytearray()
        bytes_left = file_size

        for pblock, count in extent_runs:
            if bytes_left <= 0:
                break
            chunk = self._read_block(pblock, count)
            take = min(bytes_left, len(chunk))
            result.extend(chunk[:take])
            bytes_left -= take

        return bytes(result)

    def list_dir(self, dir_meta: dict, dir_iblock: bytes) -> Dict[str, int]:
        """读取目录下的全部文件与子目录项，返回 {filename: inode_number}"""
        dir_bytes = self.read_file_content(dir_meta, dir_iblock)
        entries = {}
        offset = 0

        while offset + 8 <= len(dir_bytes):
            ino, rec_len, name_len, file_type = struct.unpack_from("<IHBB", dir_bytes, offset)
            if rec_len == 0 or offset + rec_len > len(dir_bytes):
                break
            if ino != 0 and name_len > 0:
                name_bytes = dir_bytes[offset + 8:offset + 8 + name_len]
                name = name_bytes.decode("utf-8", errors="replace")
                entries[name] = ino
            offset += rec_len

        return entries

    def resolve_path(self, path: str) -> Optional[Tuple[dict, bytes]]:
        """根据形如 '/boot/grub/grub.cfg' 的路径解析文件 Inode"""
        parts = [p for p in path.strip("/").split("/") if p]
        curr_ino = EXT4_ROOT_INO
        meta, iblock = self.read_inode(curr_ino)

        for part in parts:
            if not meta["is_dir"]:
                return None
            entries = self.list_dir(meta, iblock)
            if part not in entries:
                return None
            next_ino = entries[part]
            meta, iblock = self.read_inode(next_ino)

            # 如果中途遇到符号链接，解析链接目标
            if meta["is_lnk"]:
                target = self.read_file_content(meta, iblock).decode("utf-8", errors="replace")
                if target.startswith("/"):
                    return self.resolve_path(target)
                else:
                    # 相对路径暂简略处理
                    pass

        return meta, iblock


# ─── 统一对外高层功能 ──────────────────────────────────────────────────────────

def inspect_linux_ext4_partitions() -> None:
    """自动扫描 Disk 0 上的所有 Ext4 分区并提取 grub 配置文件"""
    print("\n" + "=" * 75)
    print("  Linux Ext4 物理分区真实免驱解析与 GRUB 配置查看引擎")
    print("=" * 75)

    # 1. 查询 GPT 物理分区表
    import subprocess
    import json

    ps_cmd = (
        "Get-Partition -DiskNumber 0 | Where-Object { $_.GptType -eq '{0fc63daf-8483-4772-8e79-3d69d8477de4}' } | "
        "Select-Object DiskNumber, PartitionNumber, Offset, Size | ConvertTo-Json"
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
        data = json.loads(proc.stdout.strip()) if proc.stdout.strip() else []
        partitions = [data] if isinstance(data, dict) else (data if isinstance(data, list) else [])
    except Exception as e:
        print(f"[-] 检测磁盘分区信息失败: {e}")
        return

    if not partitions:
        print("[-] 未在磁盘 0 上检测到 Linux 数据分区 (GPT GUID: {0fc63daf...})。")
        return

    print(f"[+] 发现 {len(partitions)} 个 Linux 数据物理分区，开始免驱深度探测：\n")

    try:
        reader = RawDiskReader(r"\\.\PhysicalDrive0")
    except PermissionError as e:
        print(f"[-] 权限不足: {e}")
        return
    except Exception as e:
        print(f"[-] 打开裸设备失败: {e}")
        return

    with reader:
        for idx, part in enumerate(partitions, start=1):
            pnum = part["PartitionNumber"]
            offset = part["Offset"]
            size = part["Size"]
            size_gb = round(size / (1024 ** 3), 2)

            print(f"---------------------------------------------------------------------------")
            print(f"【Linux 分区 #{idx}】磁盘 0 - 分区 {pnum} (起始偏移: {offset:,} | 容量: {size_gb} GB)")
            print(f"---------------------------------------------------------------------------")

            try:
                ext4 = Ext4Partition(reader, offset, size)
                print(f"[+] 识别为 Ext4 物理文件系统！")
                print(f"    ├─ 文件系统 UUID : {ext4.uuid}")
                print(f"    ├─ 卷标 (Volume) : '{ext4.volume_name or '(无卷标)'}'")
                print(f"    ├─ 块大小 (Block): {ext4.block_size} 字节 | Inode 大小: {ext4.inode_size} 字节")
                print(f"    └─ 64位模式 (64b): {ext4.is_64bit} | 总 Inode 数: {ext4.inodes_count:,}")

                # 探测核心配置文件
                target_files = [
                    "/boot/grub/grub.cfg",
                    "/etc/default/grub",
                ]

                for tf in target_files:
                    resolved = ext4.resolve_path(tf)
                    if resolved:
                        meta, iblock = resolved
                        content = ext4.read_file_content(meta, iblock)
                        text = content.decode("utf-8", errors="replace")
                        line_count = len(text.splitlines())
                        print(f"\n[+] 成功解析到文件: {tf} (大小: {meta['size']:,} 字节, 行数: {line_count} 行)")
                        print(f"    ┌──【文件内容预览: {tf} (前 35 行)】────────────────────────")
                        for line in text.splitlines()[:35]:
                            print(f"    │ {line}")
                        if line_count > 35:
                            print(f"    │ ... (还有 {line_count - 35} 行未展示) ...")
                        print(f"    └─────────────────────────────────────────────────────────────")
                    else:
                        print(f"[-] 未在该分区中找到文件: {tf}")

            except Exception as err:
                print(f"[-] 分区解析失败: {err}")

    print("\n" + "=" * 75)
    print("  Ext4 物理分区免驱解析完成。")
    print("=" * 75 + "\n")

