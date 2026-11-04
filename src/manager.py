#!/usr/bin/env python3
"""
Boot Order Manager - TUI for managing UEFI boot entries

Backends:
  Linux:   efibootmgr (root privileges required for write operations)
  Windows: bcdedit (run the terminal as Administrator)

One-click launch:
  Linux:   curl -sL https://raw.githubusercontent.com/furina707/Boot-Order/master/boot-order-manager.py | sudo python3 -
  Windows: irm https://raw.githubusercontent.com/furina707/Boot-Order/master/boot-order-manager.py | python -
"""

import locale
import subprocess
import re
import os
import sys
import time
import shutil
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

try:
    import curses
except ImportError as _curses_import_error:
    # Kept importable without curses so the parsers can be unit-tested;
    # main() turns this into a platform-specific install hint.
    curses = None
    _CURSES_IMPORT_ERROR = _curses_import_error

import unicodedata

IS_WINDOWS = os.name == "nt"


def str_display_width(s: str) -> int:
    """Calculate terminal display width of a string (handling wide East Asian chars)."""
    return sum(2 if unicodedata.east_asian_width(c) in ('F', 'W') else 1 for c in s)


def pad_to_width(s: str, width: int) -> str:
    """Pad or truncate string to exact terminal display width, handling East Asian characters."""
    cur_w = 0
    res = []
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in ('F', 'W') else 1
        if cur_w + cw > width:
            break
        res.append(c)
        cur_w += cw
    return ''.join(res) + (' ' * max(0, width - cur_w))


def center_to_width(s: str, width: int) -> str:
    """Center string to exact terminal display width."""
    w = str_display_width(s)
    if w >= width:
        return pad_to_width(s, width)
    left_pad = (width - w) // 2
    right_pad = width - w - left_pad
    return (' ' * left_pad) + s + (' ' * right_pad)


# ─── Data Models ──────────────────────────────────────────────────────────────

@dataclass
class BootEntry:
    """Represents a single UEFI boot entry."""
    boot_num: str          # short display id: "0000" (Linux) or "9BD2F12C" (Windows)
    label: str             # e.g., "Windows Boot Manager"
    active: bool           # active (has '*') or not
    is_current: bool = False
    is_next: bool = False
    ident: str = ""        # canonical id for backend commands (defaults to boot_num)
    path: str = ""         # loader path (e.g. \EFI\ubuntu\shimx64.efi), when known


@dataclass
class BootConfig:
    """Complete UEFI boot configuration."""
    entries: List[BootEntry] = field(default_factory=list)
    boot_order: List[str] = field(default_factory=list)
    boot_current: Optional[str] = None
    boot_next: Optional[str] = None
    timeout: Optional[int] = None

    def get_ordered_entries(self) -> List[BootEntry]:
        """Return entries sorted by boot order, deduplicating identical numbers in boot_order."""
        entry_map = {(e.ident or e.boot_num): e for e in self.entries}
        ordered = []
        seen = set()
        for num in self.boot_order:
            if num in entry_map and num not in seen:
                ordered.append(entry_map[num])
                seen.add(num)
        # Add entries not in boot_order at the end
        for e in self.entries:
            key = e.ident or e.boot_num
            if key not in seen:
                ordered.append(e)
                seen.add(key)
        return ordered


@dataclass
class DeduplicatedItem:
    """Represents an aggregated boot item that groups duplicate entries."""
    key: str                         # normalized key, e.g. "windows boot manager"
    label: str                       # Display label
    entries: List[BootEntry]         # All underlying duplicate entries
    selected_sub_idx: int = 0        # Currently selected/focused sub-entry (index in entries)

    @property
    def primary_entry(self) -> BootEntry:
        if 0 <= self.selected_sub_idx < len(self.entries):
            return self.entries[self.selected_sub_idx]
        return self.entries[0] if self.entries else BootEntry("", "", False)

    @property
    def has_duplicates(self) -> bool:
        return len(self.entries) > 1

    @property
    def boot_num(self) -> str:
        return self.primary_entry.boot_num

    @property
    def active(self) -> bool:
        return self.primary_entry.active

    @property
    def is_current(self) -> bool:
        return any(e.is_current for e in self.entries)

    @property
    def is_next(self) -> bool:
        return any(e.is_next for e in self.entries)

    @property
    def path(self) -> str:
        return self.primary_entry.path

    @property
    def ident(self) -> str:
        return self.primary_entry.ident


# ─── Backends ─────────────────────────────────────────────────────────────────

class EfibootmgrBackend:
    """Manage UEFI boot entries through efibootmgr (Linux)."""

    name = "efibootmgr"
    supports_active_toggle = True

    def __init__(self):
        self.exe = shutil.which("efibootmgr") or "/usr/sbin/efibootmgr"

    def _run(self, args: List[str]) -> Tuple[str, str, int]:
        """Run efibootmgr and return (stdout, stderr, returncode)."""
        cmd = [self.exe] + args
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10,
                env={**os.environ, "PATH": "/usr/bin:/usr/sbin:/bin:/sbin"}
            )
            return result.stdout, result.stderr, result.returncode
        except FileNotFoundError:
            return "", "Error: efibootmgr not found. Please install it first.", -1
        except subprocess.TimeoutExpired:
            return "", "Error: efibootmgr command timed out.", -1
        except PermissionError:
            return "", "Error: Permission denied. Try running with sudo.", -1
        except Exception as e:
            return "", f"Error: {e}", -1

    def get_boot_config(self) -> Optional[BootConfig]:
        """Fetch and parse current boot configuration."""
        stdout, stderr, rc = self._run(["-v"])
        if rc != 0:
            return None
        return parse_boot_config(stdout)

    def set_boot_order(self, order: List[str]) -> Tuple[str, int]:
        """Set the boot order. Returns (output, returncode)."""
        stdout, stderr, rc = self._run(["-o"] + [",".join(order)])
        return stdout + stderr, rc

    def set_boot_next(self, ident: str) -> Tuple[str, int]:
        """Set the boot entry for next boot only."""
        stdout, stderr, rc = self._run(["-n", ident])
        return stdout + stderr, rc

    def set_active(self, ident: str, active: bool) -> Tuple[str, int]:
        """Enable or disable a boot entry."""
        flag = "-a" if active else "-A"
        stdout, stderr, rc = self._run([flag, ident])
        return stdout + stderr, rc

    def delete_boot_entry(self, ident: str) -> Tuple[str, int]:
        """Delete a boot entry."""
        stdout, stderr, rc = self._run(["-b", ident, "-B"])
        return stdout + stderr, rc

    def set_timeout(self, seconds: int) -> Tuple[str, int]:
        """Set the boot manager timeout."""
        stdout, stderr, rc = self._run(["-t", str(seconds)])
        return stdout + stderr, rc

    def create_boot_entry(self, disk: str, part: int, label: str, loader_path: str) -> Tuple[str, int]:
        """Create a new boot entry."""
        loader = loader_path.replace("/", "\\")
        if not loader.startswith("\\"):
            loader = "\\" + loader
        cmd = ["-c", "-d", disk, "-p", str(part), "-L", label, "-l", loader]
        stdout, stderr, rc = self._run(cmd)
        return stdout + stderr, rc


class BcdeditBackend:
    """Manage UEFI firmware boot entries through bcdedit (Windows).

    Firmware applications live in the BCD system store; the firmware boot
    order is the displayorder element of the {fwbootmgr} entry.
    """

    name = "bcdedit"
    supports_active_toggle = False

    IDENT_LINE_RE = re.compile(r"^\s*(?:identifier|标识符)\s+(\{[^}]+\})\s*$")
    DESC_LINE_RE = re.compile(r"^\s*(?:description|描述|说明)\s+(.+?)\s*$")
    PATH_LINE_RE = re.compile(r"^\s*(?:path|路径)\s+(.+?)\s*$")
    ORDER_LINE_RE = re.compile(r"^\s*(?:displayorder|显示顺序)\s+(.+?)\s*$")
    TIMEOUT_LINE_RE = re.compile(r"^\s*(?:timeout|超时)\s+(\d+)\s*$")
    BOOTSEQ_LINE_RE = re.compile(r"^\s*bootsequence\s+(.+?)\s*$")
    GUIDISH_RE = re.compile(r"\{[0-9a-zA-Z-]+\}")
    DASHES_RE = re.compile(r"^\s*-{3,}\s*$")

    def __init__(self):
        self.exe = (shutil.which("bcdedit")
                    or os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                    "System32", "bcdedit.exe"))
        self.bootmgr_ident: Optional[str] = None

    @staticmethod
    def _decode(data: bytes) -> str:
        """bcdedit writes pipe output in the console code page; try UTF-8 first."""
        if not data:
            return ""
        for encoding in ("utf-8", "mbcs"):
            try:
                return data.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                pass
        return data.decode(locale.getpreferredencoding(False) or "utf-8",
                           errors="replace")

    def _run(self, args: List[str]) -> Tuple[str, str, int]:
        try:
            result = subprocess.run([self.exe] + args, capture_output=True,
                                    timeout=15)
            return (self._decode(result.stdout), self._decode(result.stderr),
                    result.returncode)
        except FileNotFoundError:
            return "", "Error: bcdedit not found.", -1
        except subprocess.TimeoutExpired:
            return "", "Error: bcdedit command timed out.", -1
        except Exception as e:
            return "", f"Error: {e}", -1

    def get_boot_config(self) -> Optional[BootConfig]:
        stdout, stderr, rc = self._run(["/enum", "firmware", "/v"])
        if rc != 0:
            return None
        return self._parse(stdout)

    def _parse(self, output: str) -> BootConfig:
        """Parse `bcdedit /enum firmware /v` output (en-US or zh-CN)."""
        config = BootConfig()
        fwbootmgr_done = False
        bootmgr_ident = None

        for block in re.split(r"\n\s*\n", output):
            # Drop blank, section-title and dashed-underline lines; lines[0]
            # after that cleanup is the localized section title.
            lines = [l for l in block.splitlines()
                     if l.strip() and not self.DASHES_RE.match(l)]
            if len(lines) < 2:
                continue

            ident = None
            label = ""
            path = ""
            order: List[str] = []
            timeout: Optional[int] = None
            boot_seq: List[str] = []

            for line in lines[1:]:
                if m := self.IDENT_LINE_RE.match(line):
                    ident = m.group(1)
                elif m := self.ORDER_LINE_RE.match(line):
                    order.extend(self.GUIDISH_RE.findall(m.group(1)))
                elif self.GUIDISH_RE.fullmatch(line.strip()):
                    # displayorder continuation lines hold one GUID each
                    order.extend(self.GUIDISH_RE.findall(line.strip()))
                elif m := self.TIMEOUT_LINE_RE.match(line):
                    timeout = int(m.group(1))
                elif m := self.BOOTSEQ_LINE_RE.match(line):
                    boot_seq.extend(self.GUIDISH_RE.findall(m.group(1)))
                elif m := self.DESC_LINE_RE.match(line):
                    label = m.group(1)
                elif m := self.PATH_LINE_RE.match(line):
                    path = m.group(1)

            if not fwbootmgr_done and order:
                # The first block listing a display order is the firmware
                # boot manager itself, not an entry.
                config.boot_order = order
                config.timeout = timeout
                if boot_seq:
                    config.boot_next = boot_seq[0].lower()
                fwbootmgr_done = True
                continue

            if not ident:
                continue

            # The Windows Boot Manager is referenced by a full GUID under /v;
            # remember it so we can refuse to delete it later.
            if (label.lower() == "windows boot manager"
                    or "bootmgfw.efi" in path.lower()):
                bootmgr_ident = ident

            boot_num = ident.strip("{}").split("-")[0].upper()
            entry = BootEntry(
                boot_num=boot_num,
                label=label or path or "(unnamed entry)",
                active=True,  # bcdedit does not expose efibootmgr's active flag
                is_next=(config.boot_next is not None
                         and ident.lower() == config.boot_next),
                ident=ident,
                path=path,
            )
            config.entries.append(entry)

        self.bootmgr_ident = bootmgr_ident
        return config

    def set_boot_order(self, order: List[str]) -> Tuple[str, int]:
        if not order:
            return "Error: refusing to set an empty boot order.", -1
        stdout, stderr, rc = self._run(["/set", "{fwbootmgr}", "displayorder"]
                                       + list(order))
        return stdout + stderr, rc

    def set_boot_next(self, ident: str) -> Tuple[str, int]:
        stdout, stderr, rc = self._run(["/set", "{fwbootmgr}", "bootsequence",
                                        ident])
        return stdout + stderr, rc

    def set_active(self, ident: str, active: bool) -> Tuple[str, int]:
        return ("Error: not supported on Windows (bcdedit cannot enable/disable "
                "firmware boot entries)."), -1

    def delete_boot_entry(self, ident: str) -> Tuple[str, int]:
        if self.bootmgr_ident and ident.lower() == self.bootmgr_ident.lower():
            return ("Refusing to delete the Windows Boot Manager entry "
                    "(it would break Windows boot)."), -1
        stdout, stderr, rc = self._run(["/delete", ident])
        return stdout + stderr, rc

    def set_timeout(self, seconds: int) -> Tuple[str, int]:
        stdout, stderr, rc = self._run(["/set", "{fwbootmgr}", "timeout",
                                        str(seconds)])
        return stdout + stderr, rc

    def create_boot_entry(self, label: str, loader_path: str) -> Tuple[str, int]:
        """Create a new firmware boot entry via bcdedit."""
        stdout, stderr, rc = self._run(["/create", "/d", label, "/application", "bootapp"])
        if rc != 0:
            return stdout + stderr, rc
        m = self.GUIDISH_RE.search(stdout)
        if not m:
            return f"Failed to parse created GUID: {stdout}", -1
        new_guid = m.group(0)
        loader = loader_path.replace("/", "\\")
        if not loader.startswith("\\"):
            loader = "\\" + loader
        s_out, s_err, s_rc = self._run(["/set", new_guid, "path", loader])
        if s_rc != 0:
            return s_out + s_err, s_rc
        o_out, o_err, o_rc = self._run(["/set", "{fwbootmgr}", "displayorder", new_guid, "/addfirst"])
        return f"Successfully created entry {new_guid} for {label}: {o_out}", o_rc


BACKEND = BcdeditBackend() if IS_WINDOWS else EfibootmgrBackend()


def parse_boot_config(output: str) -> BootConfig:
    """Parse efibootmgr -v output into a BootConfig object."""
    config = BootConfig()

    for line in output.splitlines():
        line_stripped = line.strip()

        # Parse Timeout
        m = re.search(r'^Timeout:\s*(\d+)', line_stripped)
        if m:
            config.timeout = int(m.group(1))

        # Parse BootCurrent
        m = re.search(r'^BootCurrent:\s*([0-9A-Fa-f]{4})', line_stripped)
        if m:
            config.boot_current = m.group(1)

        # Parse BootNext
        m = re.search(r'^BootNext:\s*([0-9A-Fa-f]{4})', line_stripped)
        if m:
            config.boot_next = m.group(1)

        # Parse BootOrder
        m = re.search(r'^BootOrder:\s*([0-9A-Fa-f,]+)', line_stripped)
        if m:
            config.boot_order = m.group(1).split(',')

        # Parse individual boot entries: BootXXXX[*]  Label
        m = re.match(r'^Boot([0-9A-Fa-f]{4})(\*?)\s+(.+)', line_stripped)
        if m:
            boot_num = m.group(1)
            active = m.group(2) == '*'
            rest = m.group(3)

            # Split label and optional path (tab or multiple spaces separated)
            parts = re.split(r'\t|  +', rest, maxsplit=1)
            label = parts[0].strip() if parts else rest.strip()

            entry = BootEntry(
                boot_num=boot_num,
                label=label,
                active=active,
                is_current=(boot_num == config.boot_current),
                is_next=(boot_num == config.boot_next),
                ident=boot_num,
                path=parts[1].strip() if len(parts) > 1 else "",
            )
            config.entries.append(entry)

    return config


# ─── TUI Application ───────────────────────────────────────────────────────────

class BootOrderTUI:
    """Curses-based TUI for managing boot order."""

    # Color pairs
    HEADER = 1
    SELECTED = 2
    CURRENT = 3
    ACTIVE = 4
    INACTIVE = 5
    HIGHLIGHT = 6
    STATUS_BAR = 7
    TITLE = 8
    HELP = 9
    WARNING = 10

    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.config: Optional[BootConfig] = None
        self.ordered_entries: List[Any] = []
        self.raw_entries: List[BootEntry] = []
        self.dedup_mode: bool = True  # 智能去重模式 (默认开启)
        self.selected_idx = 0
        self.scroll_offset = 0
        self.message = ""
        self.message_type = "info"  # "info", "error", "success"
        self._footer_buttons = []
        self._table_row_hitboxes = []
        self._card_hitbox = None
        self._card_action_hitboxes = []
        self._last_row_click_time = 0.0
        self._last_clicked_row = None
        self._last_mouse_event_time = 0.0
        self._last_card_click_time = 0.0

        # Terminal dimensions
        self.max_y, self.max_x = stdscr.getmaxyx()

        # Initialize colors and mouse
        self._init_colors()
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
            curses.mouseinterval(300)
        except Exception:
            pass

        # Load initial data
        self.refresh()

    def _init_colors(self):
        """Initialize color pairs."""
        curses.start_color()
        try:
            curses.use_default_colors()
        except Exception:
            pass

        curses.init_pair(self.HEADER, curses.COLOR_CYAN, -1)
        curses.init_pair(self.SELECTED, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(self.CURRENT, curses.COLOR_GREEN, -1)
        curses.init_pair(self.ACTIVE, curses.COLOR_GREEN, -1)
        curses.init_pair(self.INACTIVE, curses.COLOR_RED, -1)
        curses.init_pair(self.HIGHLIGHT, curses.COLOR_YELLOW, -1)
        curses.init_pair(self.HELP, curses.COLOR_MAGENTA, -1)
        curses.init_pair(self.STATUS_BAR, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(self.TITLE, curses.COLOR_YELLOW, -1)
        curses.init_pair(self.WARNING, curses.COLOR_RED, curses.COLOR_YELLOW)
        curses.init_pair(11, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(12, curses.COLOR_BLACK, curses.COLOR_YELLOW)
        curses.init_pair(13, curses.COLOR_BLACK, curses.COLOR_GREEN)
        curses.init_pair(14, curses.COLOR_WHITE, curses.COLOR_RED)
        self.BUTTON_NORMAL = 11
        self.BUTTON_ACTIVE = 12
        self.BUTTON_SUCCESS = 13
        self.BUTTON_DANGER = 14

    def refresh(self, keep_message: bool = False):
        """Reload boot configuration with intelligent deduplication support.

        keep_message=True preserves any pending action feedback (success/error)
        that was set before the reload.
        """
        self.config = BACKEND.get_boot_config()
        if self.config:
            self.raw_entries = self.config.get_ordered_entries()
            if self.dedup_mode:
                # Group entries by normalized label
                groups: Dict[str, List[BootEntry]] = {}
                group_keys: List[str] = []
                for e in self.raw_entries:
                    k = e.label.strip().lower()
                    if k not in groups:
                        groups[k] = []
                        group_keys.append(k)
                    if not any(existing.ident == e.ident for existing in groups[k]):
                        groups[k].append(e)

                dedup_items: List[DeduplicatedItem] = []
                for k in group_keys:
                    sub_entries = groups[k]
                    # Select current or highest priority entry
                    cur_idx = 0
                    for idx, sub in enumerate(sub_entries):
                        if sub.is_current:
                            cur_idx = idx
                            break
                    dedup_items.append(DeduplicatedItem(
                        key=k,
                        label=sub_entries[0].label,
                        entries=sub_entries,
                        selected_sub_idx=cur_idx
                    ))
                self.ordered_entries = dedup_items
            else:
                self.ordered_entries = self.raw_entries

            self.selected_idx = min(self.selected_idx, max(0, len(self.ordered_entries) - 1))
            if not keep_message:
                if self.dedup_mode:
                    merged_cnt = len(self.raw_entries) - len(self.ordered_entries)
                    if merged_cnt > 0:
                        self.message = f"启动配置已加载 (已智能去重合并 {merged_cnt} 个冗余项，当前显示 {len(self.ordered_entries)} 个核心引导)"
                    else:
                        self.message = f"启动配置已加载 (去重模式，共 {len(self.ordered_entries)} 个引导项)"
                else:
                    self.message = f"启动配置已加载 (原始条目模式，共 {len(self.ordered_entries)} 个引导项)"
                self.message_type = "success"
        else:
            self.ordered_entries = []
            self.raw_entries = []
            if IS_WINDOWS:
                self.message = "无法读取固件启动配置 (请以管理员身份运行)。"
            else:
                self.message = "无法读取固件启动配置 (请使用 sudo 提权运行)。"
            self.message_type = "error"

    # ─── Drawing Methods ────────────────────────────────────────────────────

    def draw(self):
        """Main draw method."""
        self.max_y, self.max_x = self.stdscr.getmaxyx()
        self.stdscr.erase()

        if self.max_y < 12 or self.max_x < 48:
            self._draw_min_size_warning()
            return

        self._draw_header()
        self._draw_info_bar()
        self._draw_table_and_details()
        self._draw_footer()
        self.stdscr.noutrefresh()
        curses.doupdate()

    def _draw_min_size_warning(self):
        """Draw warning when terminal is too small."""
        warn = "终端窗口尺寸过小，至少需要 48x12 列行空间。"
        try:
            self.stdscr.addstr(0, 0, warn, curses.color_pair(self.WARNING) | curses.A_BOLD)
        except curses.error:
            pass

    def _parse_entry_path_and_device(self, entry: BootEntry) -> Tuple[str, str]:
        """Parse raw device path into clean EFI executable path and human-readable partition info."""
        raw = entry.path or ""
        efi_m = re.search(r"([/\\][^\x00\r\n\t]+?\.(?:efi|EFI))", raw, re.IGNORECASE)
        efi_path = efi_m.group(1).replace("/", "\\") if efi_m else ""
        hd_m = re.search(r"HD\((\d+),(GPT|MBR),([0-9a-fA-F-]+)", raw)
        if hd_m:
            device_info = f"{hd_m.group(2)} 物理分区 #{hd_m.group(1)} (PARTUUID: {hd_m.group(3)[:8]}...)"
        elif "usb" in raw.lower() or "usb" in entry.label.lower():
            device_info = "USB 可移动设备 (Removable Media)"
        elif any(k in entry.label.lower() for k in ["hard drive", "solid state", "nvme"]):
            device_info = "主板 NVMe / SSD 物理固态硬盘"
        else:
            device_info = "UEFI 固件主板原生 NVRAM 节点"
        return efi_path, device_info

    def _draw_header(self):
        """Draw the title bar."""
        if self.max_x >= 75:
            title = " UEFI 启动项与多系统引导管理工作台 "
        else:
            title = " UEFI 启动管理工作台 "
        mode = " [UEFI 64位模式] " if self.config else " [无EFI数据] "

        try:
            self.stdscr.addstr(0, 0, "┌" + "─" * (self.max_x - 2) + "┐",
                               curses.color_pair(self.HEADER))
            self.stdscr.addstr(1, 0, "│", curses.color_pair(self.HEADER))
            self.stdscr.addstr(1, 2, title, curses.color_pair(self.TITLE) | curses.A_BOLD)

            mode_color = curses.color_pair(self.CURRENT) if self.config else curses.color_pair(self.INACTIVE)
            mode_w = str_display_width(mode)
            self.stdscr.addstr(1, max(str_display_width(title) + 3, self.max_x - 1 - mode_w), mode, mode_color)

            self.stdscr.addstr(1, self.max_x - 1, "│", curses.color_pair(self.HEADER))
            self.stdscr.addstr(2, 0, "└" + "─" * (self.max_x - 2) + "┘",
                               curses.color_pair(self.HEADER))
        except curses.error:
            pass

    def _draw_info_bar(self):
        """Draw system info bar."""
        if not self.config:
            return

        boot_current = f" 当前运行: {self.config.boot_current or '无'} "
        boot_next = f" 下次启动: {self.config.boot_next or '未指定'} "
        if self.config.timeout is not None:
            timeout = f" 引导超时: {self.config.timeout}秒 "
        else:
            timeout = " 引导超时: 未设置 "
        entry_count = f" 启动项总数: {len(self.config.entries)}项 "

        if self.max_x >= 85:
            info = f" {boot_current}│{boot_next}│{timeout}│{entry_count} "
        else:
            info = f" {boot_current}│{timeout}│{entry_count} "

        try:
            formatted = pad_to_width(info, self.max_x)
            self.stdscr.addstr(3, 0, formatted, curses.color_pair(self.STATUS_BAR))
        except curses.error:
            pass

    def _draw_table_and_details(self):
        """Draw the boot entries table and, if height permits, the live inspection pane."""
        if not self.ordered_entries:
            try:
                self.stdscr.addstr(5, 2, "未检测到 UEFI 启动项或无法访问主板 EFI 变量。",
                                   curses.color_pair(self.INACTIVE))
                hint = ("请以管理员身份 (Run as Administrator) 运行终端。" if IS_WINDOWS
                        else "请尝试使用 root 权限运行: sudo python " + os.path.basename(sys.argv[0]))
                self.stdscr.addstr(6, 2, hint, curses.color_pair(self.HELP))
            except curses.error:
                pass
            return

        table_top = 5
        footer_h = 5 if self.max_x < 85 else (4 if self.max_x < 155 else 3)
        footer_y = self.max_y - footer_h

        # Determine layout: Split-View (Table + Live Inspection Card) or Single-View
        available_total_h = (footer_y - 1) - table_top
        use_split_view = (self.max_y >= 20) and (available_total_h >= 12)

        if use_split_view:
            table_data_h = max(3, min(len(self.ordered_entries), available_total_h - 8))
            table_bottom_y = table_top + 2 + table_data_h + 1
            card_top = table_bottom_y + 1
            card_bottom = footer_y - 1
        else:
            table_bottom_y = footer_y - 1
            table_data_h = max(1, table_bottom_y - (table_top + 3))
            card_top = -1
            card_bottom = -1

        w = self.max_x - 2  # content width between │ and │

        # Column widths responsive to terminal width
        if self.max_x < 75:
            idx_w = 3
            num_w = 6
            stat_w = 6
            fixed_w = 2 + idx_w + 3 + num_w + 3 + 3 + stat_w + 1
            lbl_w = max(8, w - fixed_w)

            h_idx = pad_to_width('#', idx_w)
            h_num = pad_to_width('编号', num_w)
            h_lbl = pad_to_width('启动项名称 / 标签', lbl_w)
            h_stat = center_to_width('状态', stat_w)
            header_str = f"  {h_idx} │ {h_num} │ {h_lbl} │ {h_stat} "
        elif self.max_x < 95:
            idx_w = 3
            num_w = 7
            stat_w = 6
            flag_w = 8
            fixed_w = 2 + idx_w + 3 + num_w + 3 + 3 + stat_w + 3 + flag_w + 1
            lbl_w = max(8, w - fixed_w)

            h_idx = pad_to_width('#', idx_w)
            h_num = pad_to_width('编号', num_w)
            h_lbl = pad_to_width('启动项名称 / 标签', lbl_w)
            h_stat = center_to_width('状态', stat_w)
            h_flag = center_to_width('标志', flag_w)
            header_str = f"  {h_idx} │ {h_num} │ {h_lbl} │ {h_stat} │ {h_flag} "
        else:
            idx_w = 3
            num_w = 9
            stat_w = 8
            flag_w = 10
            fixed_w = 2 + idx_w + 3 + num_w + 3 + 3 + stat_w + 3 + flag_w + 1
            lbl_w = max(10, w - fixed_w)

            h_idx = pad_to_width('#', idx_w)
            h_num = pad_to_width('条目编号', num_w)
            h_lbl = pad_to_width('启动项名称 / 标签', lbl_w)
            h_stat = center_to_width('启用状态', stat_w)
            h_flag = center_to_width('引导标志', flag_w)
            header_str = f"  {h_idx} │ {h_num} │ {h_lbl} │ {h_stat} │ {h_flag} "

        # Draw Table Box Frame & Header with Deduplication Status Tag
        try:
            table_title = " UEFI 固件引导项列表 [已启用智能去重 (右键/x操作菜单 · v切换视图)] " if self.dedup_mode else " UEFI 固件引导项列表 [原始全部条目视图 (按 v 开启智能去重)] "
            title_dashes = max(0, self.max_x - 4 - str_display_width(table_title))
            top_border = "┌─" + table_title + ("─" * title_dashes) + "┐"
            self.stdscr.addstr(table_top, 0, top_border[:self.max_x],
                               curses.color_pair(self.HEADER) | curses.A_BOLD)
            self.stdscr.addstr(table_top + 1, 0, "│", curses.color_pair(self.HEADER))
            self.stdscr.addstr(table_top + 1, 1, pad_to_width(header_str, w),
                               curses.color_pair(self.HEADER) | curses.A_BOLD)
            self.stdscr.addstr(table_top + 1, self.max_x - 1, "│", curses.color_pair(self.HEADER))
            self.stdscr.addstr(table_top + 2, 0, "│" + "─" * (self.max_x - 2) + "│",
                               curses.color_pair(self.HEADER))
        except curses.error:
            pass

        # Adjust scroll offset
        if self.selected_idx < self.scroll_offset:
            self.scroll_offset = self.selected_idx
        elif self.selected_idx >= self.scroll_offset + table_data_h:
            self.scroll_offset = self.selected_idx - table_data_h + 1

        display_entries = self.ordered_entries[self.scroll_offset:self.scroll_offset + table_data_h]

        self._table_row_hitboxes = []
        for i, entry in enumerate(display_entries):
            row = table_top + 3 + i
            abs_idx = self.scroll_offset + i
            self._table_row_hitboxes.append((row, abs_idx))
            is_selected = (abs_idx == self.selected_idx)

            if is_selected:
                entry_color = curses.color_pair(self.SELECTED)
            elif entry.is_current:
                entry_color = curses.color_pair(self.CURRENT)
            elif entry.active:
                entry_color = curses.color_pair(self.ACTIVE)
            else:
                entry_color = curses.color_pair(self.INACTIVE)

            is_dedup_item = isinstance(entry, DeduplicatedItem) and entry.has_duplicates
            dup_cnt = len(entry.entries) if isinstance(entry, DeduplicatedItem) else 1

            if is_dedup_item:
                if num_w >= 8:
                    boot_num_str = f"{'*' if entry.active else ' '}{entry.boot_num} ⧉{dup_cnt}"
                else:
                    boot_num_str = f"{'*' if entry.active else ' '}{entry.boot_num}+"
            else:
                boot_num_str = f"{'*' if entry.active else ' '}{entry.boot_num}"

            status_parts = []
            if entry.is_current:
                status_parts.append(f"当前系统(⧉{dup_cnt})" if is_dedup_item else "当前系统")
            if entry.is_next:
                status_parts.append(f"下次引导(⧉{dup_cnt})" if is_dedup_item else "下次引导")
            if is_dedup_item and not status_parts:
                status_parts.append(f"⧉{dup_cnt}项(右键)")
            status = ",".join(status_parts) if status_parts else ""

            label_text = entry.label
            if is_dedup_item:
                suffix_full = f" [已去重 · 包含 {dup_cnt} 项]"
                suffix_compact = f" (⧉{dup_cnt}项)"
                if str_display_width(label_text + suffix_full) <= lbl_w:
                    label_text += suffix_full
                elif str_display_width(label_text + suffix_compact) <= lbl_w:
                    label_text += suffix_compact
            elif self.max_x < 75 and entry.is_current and (str_display_width(label_text) + 7 <= lbl_w):
                label_text += " [当前]"

            if self.max_x < 75:
                row_str = (
                    f"  {pad_to_width(str(abs_idx + 1), idx_w)} │ "
                    f"{pad_to_width(boot_num_str, num_w)} │ "
                    f"{pad_to_width(label_text, lbl_w)} │ "
                    f"{center_to_width('已启用' if entry.active else '已禁用', stat_w)} "
                )
            elif self.max_x < 95:
                row_str = (
                    f"  {pad_to_width(str(abs_idx + 1), idx_w)} │ "
                    f"{pad_to_width(boot_num_str, num_w)} │ "
                    f"{pad_to_width(label_text, lbl_w)} │ "
                    f"{center_to_width('已启用' if entry.active else '已禁用', stat_w)} │ "
                    f"{center_to_width(status or '常规', flag_w)} "
                )
            else:
                row_str = (
                    f"  {pad_to_width(str(abs_idx + 1), idx_w)} │ "
                    f"{pad_to_width(boot_num_str, num_w)} │ "
                    f"{pad_to_width(label_text, lbl_w)} │ "
                    f"{center_to_width('已启用' if entry.active else '已禁用', stat_w)} │ "
                    f"{center_to_width(status, flag_w)} "
                )

            padded_row = pad_to_width(row_str, w)

            try:
                self.stdscr.addstr(row, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(row, 1, padded_row, entry_color)
                self.stdscr.addstr(row, self.max_x - 1, "│", curses.color_pair(self.HEADER))
            except curses.error:
                pass

        # Clear remaining lines in table
        for i in range(len(display_entries), table_data_h):
            row = table_top + 3 + i
            try:
                self.stdscr.addstr(row, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(row, 1, " " * w)
                self.stdscr.addstr(row, self.max_x - 1, "│", curses.color_pair(self.HEADER))
            except curses.error:
                pass

        # Table bottom border
        try:
            self.stdscr.addstr(table_bottom_y, 0, "└" + "─" * (self.max_x - 2) + "┘",
                               curses.color_pair(self.HEADER))
        except curses.error:
            pass

        # If in Split-View mode, render the Live Inspection Card
        if use_split_view and card_top > 0 and card_bottom > card_top:
            self._draw_entry_details_card(card_top, card_bottom)

    def _draw_entry_details_card(self, card_top: int, card_bottom: int):
        """Draw live inspection and diagnostic card for the currently selected entry."""
        if not self.ordered_entries or self.selected_idx >= len(self.ordered_entries):
            return

        entry = self.ordered_entries[self.selected_idx]
        w = self.max_x - 2
        card_h = card_bottom - card_top + 1
        if card_h < 3:
            return

        efi_path, device_info = self._parse_entry_path_and_device(entry)

        is_linux = any(k in entry.label.lower() for k in ["linux", "systemd", "grub", "arch", "ubuntu", "fedora"])
        is_windows = any(k in entry.label.lower() for k in ["windows", "bootmgfw", "microsoft"])

        if is_windows:
            diag_text = "✔ Windows 11 引导管理器 (可按 [w] 深度诊断与打通双系统)"
            role_hint = "Windows 操作系统"
        elif is_linux:
            diag_text = "✔ Linux 引导 (按 [k] 切换内核，按 [b] 调参，按 [w] 注入 Win11)"
            role_hint = "Linux 操作系统引导器"
        elif any(k in entry.label.lower() for k in ["hard drive", "solid state", "nvme"]):
            diag_text = "ℹ 主板原生 NVRAM 固态硬盘硬件条目"
            role_hint = "NVMe / SSD 物理介质"
        elif "usb" in entry.label.lower():
            diag_text = "ℹ USB 可移动存储设备启动条目"
            role_hint = "USB 移动存储"
        else:
            diag_text = "ℹ UEFI 固件硬件启动条目"
            role_hint = "主板硬件固件"

        role_status = "当前运行系统" if entry.is_current else ("下次引导" if entry.is_next else "常规项")
        active_str = "已启用 (Active)" if entry.active else "已禁用 (Disabled)"

        is_dedup_item = isinstance(entry, DeduplicatedItem) and entry.has_duplicates
        dup_cnt = len(entry.entries) if isinstance(entry, DeduplicatedItem) else 1

        if is_dedup_item:
            sub_boots = " / ".join(f"Boot{e.boot_num}" for e in entry.entries)
            lines_to_show = [
                f"【属性】编号: Boot{entry.boot_num} │ 状态: {active_str} │ 角色: {role_status} │ 聚合: ⧉{dup_cnt} 项",
                f"【名称】{entry.label} ({role_hint})",
                f"【去重】底层条目: {sub_boots} (鼠标右键或按 [x] 选择具体条目进行操作)",
                f"【路径】{efi_path or '主板 NVRAM 内建固件硬件地址'}",
                f"【分区】{device_info}",
                f"【诊断】{diag_text}",
                "【操作】[x/右键] 操作菜单 │ [1] 置顶 │ [m/M] 调序 │ [空格] 启禁 │ [c] 清理冗余 │ [回车] 详情",
            ]
        else:
            lines_to_show = [
                f"【属性】编号: Boot{entry.boot_num} │ 状态: {active_str} │ 角色: {role_status}",
                f"【名称】{entry.label} ({role_hint})",
                f"【路径】{efi_path or '主板 NVRAM 内建固件硬件地址'}",
                f"【分区】{device_info}",
                f"【诊断】{diag_text}",
                "【操作】[x/右键] 操作菜单 │ [1] 置顶 │ [m/M] 调序 │ [空格] 启禁 │ [回车] 详细参数",
            ]

        self._card_hitbox = (card_top, card_bottom)
        self._card_action_hitboxes = []

        card_title = f" 选中项深度解析与实时体检 [Boot{entry.boot_num}] "
        if str_display_width(card_title) > w - 4:
            card_title = f" 实时解析 [Boot{entry.boot_num}] "

        dash_count = max(0, self.max_x - 3 - str_display_width(card_title))
        title_line = "┌─" + card_title + ("─" * dash_count) + "┐"

        try:
            self.stdscr.addstr(card_top, 0, title_line[:self.max_x], curses.color_pair(self.HEADER))
            for row_idx in range(1, card_h - 1):
                cur_y = card_top + row_idx
                self.stdscr.addstr(cur_y, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_y, self.max_x - 1, "│", curses.color_pair(self.HEADER))

                content_idx = row_idx - 1
                if content_idx < len(lines_to_show):
                    c_text = lines_to_show[content_idx]
                    padded = pad_to_width(" " + c_text, w)
                    color = curses.A_NORMAL
                    if "【诊断】" in c_text:
                        color = curses.color_pair(self.ACTIVE) | curses.A_BOLD if ("✔" in c_text) else curses.color_pair(self.CURRENT)
                    elif "【操作】" in c_text:
                        color = curses.color_pair(self.HIGHLIGHT)
                        # Register interactive clickable hitboxes dynamically
                        self._card_action_hitboxes = []
                        import re
                        for m in re.finditer(r'\[([^\]]+)\]', c_text):
                            b_start = 1 + str_display_width(" " + c_text[:m.start()])
                            b_end = b_start + str_display_width(m.group(0))
                            act_name = m.group(1).strip()
                            self._card_action_hitboxes.append({
                                "y": cur_y, "x1": b_start, "x2": b_end, "action": act_name
                            })
                    elif "【属性】" in c_text:
                        color = curses.color_pair(self.TITLE) | curses.A_BOLD
                    elif "【去重】" in c_text:
                        color = curses.color_pair(self.CURRENT) | curses.A_BOLD
                    self.stdscr.addstr(cur_y, 1, padded, color)
                else:
                    self.stdscr.addstr(cur_y, 1, " " * w)

            self.stdscr.addstr(card_bottom, 0, "└" + "─" * (self.max_x - 2) + "┘", curses.color_pair(self.HEADER))
        except curses.error:
            pass

    def _draw_footer(self):
        """Draw the footer with key bindings, fully responsive to terminal width."""
        if self.max_x < 85:
            # 3-row layout for narrow terminals (e.g. 55-80 columns)
            footer_h = 5
            control_rows = [
                [("↑↓", "选择"), ("回车", "详情"), ("x/右键", "菜单"), ("1", "置顶"), ("m/M", "调序")],
                [("空格", "启禁"), ("v", "去重"), ("t", "超时"), ("k", "内核"), ("b", "引导")],
                [("w", "Win11"), ("c", "修复"), ("e", "ESP分区"), ("q", "退出")],
            ]
        elif self.max_x < 155:
            # 2-row layout for standard terminals (85-154 columns)
            footer_h = 4
            control_rows = [
                [("↑↓", "选择"), ("回车", "详情"), ("x/右键", "操作菜单"), ("1", "置顶"), ("m/M", "调序"), ("空格", "启禁"), ("v", "去重视图")],
                [("k", "内核切换"), ("b", "Linux引导"), ("w", "Win11分析"), ("c", "修复/清理"), ("t", "超时"), ("e", "ESP分区"), ("?", "帮助"), ("q", "退出")],
            ]
        else:
            # 1-row layout for wide terminals (>= 155 columns)
            footer_h = 3
            control_rows = [
                [("↑↓", "选择"), ("回车", "详情"), ("x/右键", "操作菜单"), ("1", "置顶"), ("m/M", "调序"), ("空格", "启禁"), ("v", "去重视图"),
                 ("k", "内核切换"), ("b", "Linux引导"), ("w", "Win11分析"), ("c", "修复/清理"), ("t", "超时"), ("e", "ESP分区"), ("?", "帮助"), ("q", "退出")],
            ]

        footer_y = self.max_y - footer_h
        w = self.max_x - 2
        self._footer_buttons = []

        try:
            self.stdscr.addstr(footer_y, 0, "┌" + "─" * (self.max_x - 2) + "┐",
                               curses.color_pair(self.HEADER))

            def draw_row(y, items):
                self.stdscr.addstr(y, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(y, 1, " " * w)
                self.stdscr.addstr(y, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                x = 2
                for i, (key, action) in enumerate(items):
                    item_w = str_display_width(f"[{key}] {action} ") + (2 if i > 0 else 0)
                    if x + item_w >= self.max_x - 2:
                        break
                    if i > 0:
                        self.stdscr.addstr(y, x, "│ ", curses.color_pair(self.HEADER))
                        x += 2
                    start_x = x
                    self.stdscr.addstr(y, x, f"[{key}]",
                                       curses.color_pair(self.HIGHLIGHT) | curses.A_BOLD)
                    x += str_display_width(f"[{key}]")
                    self.stdscr.addstr(y, x, f" {action} ")
                    x += str_display_width(f" {action} ")
                    self._footer_buttons.append({
                        "y": y,
                        "x_start": start_x,
                        "x_end": x,
                        "key": key
                    })

            for r_idx, items in enumerate(control_rows):
                draw_row(footer_y + 1 + r_idx, items)

            # Bottom border with integrated message badge
            bottom_y = self.max_y - 1
            if not self.message:
                self.stdscr.addstr(bottom_y, 0, "└" + "─" * (self.max_x - 2) + "┘",
                                   curses.color_pair(self.HEADER))
            else:
                color = curses.color_pair(self.ACTIVE) | curses.A_BOLD
                icon = "✔"
                if self.message_type == "error":
                    color = curses.color_pair(self.INACTIVE) | curses.A_BOLD
                    icon = "✖"
                elif self.message_type == "info":
                    color = curses.color_pair(self.CURRENT) | curses.A_BOLD
                    icon = "ℹ"

                # Ensure badge fits comfortably
                msg = self.message
                badge = f" [ {icon} {msg} ] "
                badge_w = str_display_width(badge)
                if badge_w > self.max_x - 8:
                    avail_w = max(6, self.max_x - 12)
                    safe_msg = pad_to_width(msg, avail_w)
                    badge = f" [ {icon} {safe_msg} ] "
                    badge_w = str_display_width(badge)

                left_len = 2
                right_len = max(0, self.max_x - 2 - left_len - badge_w)
                self.stdscr.addstr(bottom_y, 0, "└" + "─" * left_len, curses.color_pair(self.HEADER))
                self.stdscr.addstr(bottom_y, 1 + left_len, badge, color)
                self.stdscr.addstr(bottom_y, 1 + left_len + badge_w, "─" * right_len + "┘", curses.color_pair(self.HEADER))
        except curses.error:
            pass

    def _draw_message(self):
        """Deprecated: status message is now cleanly integrated directly into the footer bottom border."""
        pass

    def set_message(self, msg: str, msg_type: str = "info"):
        """Set a status message."""
        self.message = msg
        self.message_type = msg_type

    # ─── Actions ────────────────────────────────────────────────────────────

    def move_entry(self, direction: int):
        """Move selected entry up (-1) or down (+1) in boot order."""
        if not self.config or len(self.ordered_entries) < 2:
            return

        idx = self.selected_idx
        new_idx = idx + direction
        if new_idx < 0 or new_idx >= len(self.ordered_entries):
            self.set_message("已位于列表边缘，无法继续移动。", "info")
            return

        cur_item = self.ordered_entries[idx]
        target_item = self.ordered_entries[new_idx]
        cur_ident = cur_item.ident
        target_ident = target_item.ident

        order = list(self.config.boot_order)
        if cur_ident in order and target_ident in order:
            cur_pos = order.index(cur_ident)
            target_pos = order.index(target_ident)
            order.remove(cur_ident)
            order.insert(target_pos, cur_ident)
            order = list(dict.fromkeys(order))
        else:
            if cur_ident in order:
                order.remove(cur_ident)
            order.insert(max(0, min(new_idx, len(order))), cur_ident)
            order = list(dict.fromkeys(order))

        output, rc = BACKEND.set_boot_order(order)
        if rc == 0:
            self.config.boot_order = order
            self.selected_idx = new_idx
            self.set_message(f"已将 '{cur_item.label}' {'上移' if direction < 0 else '下移'} 一位。", "success")
        else:
            self.set_message(f"修改启动顺序失败: {output.strip()}", "error")

        self.refresh(keep_message=True)

    def set_as_first_for_entry(self, entry: BootEntry):
        """Set a specific entry as the first boot entry."""
        if not self.config:
            return
        if not entry.active:
            self.set_message(f"启动项 '{entry.label}' ({entry.boot_num}) 当前为禁用状态，请先按空格键启用。", "error")
            return

        order = [x for x in self.config.boot_order if x != entry.ident]
        order.insert(0, entry.ident)
        order = list(dict.fromkeys(order))

        output, rc = BACKEND.set_boot_order(order)
        if rc == 0:
            self.config.boot_order = order
            self.set_message(f"已成功将 '{entry.label}' (Boot{entry.boot_num}) 设为主板第一优先启动项！", "success")
        else:
            self.set_message(f"设置第一启动项失败: {output.strip()}", "error")

        self.refresh(keep_message=True)

    def set_as_first(self):
        """Set the selected entry as the first boot entry."""
        if not self.config or not self.ordered_entries:
            return
        entry = self.ordered_entries[self.selected_idx]
        target = entry.primary_entry if isinstance(entry, DeduplicatedItem) else entry
        self.set_as_first_for_entry(target)

    def set_next_boot_for_entry(self, entry: BootEntry):
        """Set a specific entry as the one-time boot entry for next restart."""
        if not self.config:
            return
        output, rc = BACKEND.set_boot_next(entry.ident)
        if rc == 0:
            self.set_message(f"已设置下次重启临时引导: '{entry.label}' (Boot{entry.boot_num})。", "success")
        else:
            self.set_message(f"设置下次启动失败: {output.strip()}", "error")

        self.refresh(keep_message=True)

    def set_next_boot(self):
        """Set the selected entry as the one-time boot entry for next restart."""
        if not self.config or not self.ordered_entries:
            return
        entry = self.ordered_entries[self.selected_idx]
        target = entry.primary_entry if isinstance(entry, DeduplicatedItem) else entry
        self.set_next_boot_for_entry(target)

    def toggle_active_for_entry(self, entry: BootEntry):
        """Toggle active/inactive state of a specific entry."""
        if not self.config:
            return
        if not BACKEND.supports_active_toggle:
            self.set_message("当前平台不支持：Windows BCD 无法禁用/启用固件启动项。", "error")
            return

        new_active = not entry.active
        output, rc = BACKEND.set_active(entry.ident, new_active)
        if rc == 0:
            entry.active = new_active
            status = "已启用" if new_active else "已禁用"
            self.set_message(f"{status}启动项 '{entry.label}' (Boot{entry.boot_num})。", "success")
        else:
            self.set_message(f"切换状态失败: {output.strip()}", "error")

        self.refresh(keep_message=True)

    def toggle_active(self):
        """Toggle active/inactive state of the selected entry."""
        if not self.config or not self.ordered_entries:
            return
        entry = self.ordered_entries[self.selected_idx]
        target = entry.primary_entry if isinstance(entry, DeduplicatedItem) else entry
        self.toggle_active_for_entry(target)

    def delete_entry_for_entry(self, entry: BootEntry):
        """Delete a specific entry after confirmation."""
        if not self.config:
            return
        if self._confirm(f"确定从主板 NVRAM 中删除启动项 '{entry.label}' (Boot{entry.boot_num}) 吗？"):
            output, rc = BACKEND.delete_boot_entry(entry.ident)
            if rc == 0:
                self.set_message(f"已成功删除启动项 '{entry.label}' (Boot{entry.boot_num})。", "success")
            else:
                self.set_message(f"删除失败: {output.strip()}", "error")
            self.refresh(keep_message=True)

    def delete_entry(self):
        """Delete the selected boot entry after confirmation."""
        if not self.config or not self.ordered_entries:
            return
        entry = self.ordered_entries[self.selected_idx]
        target = entry.primary_entry if isinstance(entry, DeduplicatedItem) else entry
        self.delete_entry_for_entry(target)

    def cleanup_duplicate_entries(self, item: DeduplicatedItem):
        """Clean up redundant duplicate entries for this bootloader item, keeping the selected one."""
        if not item.has_duplicates:
            self.set_message("当前引导项无多余重复条目，无需清理。", "info")
            return

        target = item.primary_entry
        duplicates = [e for e in item.entries if e.ident != target.ident]
        if not duplicates:
            return

        dup_names = ", ".join([f"Boot{d.boot_num}" for d in duplicates])
        prompt = (f"确定清理【{item.label}】的 {len(duplicates)} 个重复副本 ({dup_names})，\n"
                  f"仅保留当前选中的主条目 Boot{target.boot_num} 吗？\n"
                  f"(此操作将从主板 NVRAM 安全注销冗余条目并清理 BootOrder 重复编号)")

        if self._confirm(prompt):
            success_cnt = 0
            for d in duplicates:
                out, rc = BACKEND.delete_boot_entry(d.ident)
                if rc == 0:
                    success_cnt += 1

            if self.config:
                dup_idents = set(d.ident for d in duplicates)
                new_order = [x for x in self.config.boot_order if x not in dup_idents]
                new_order = list(dict.fromkeys(new_order))
                self.config.boot_order = new_order
                BACKEND.set_boot_order(new_order)

            self.refresh(keep_message=True)
            self.set_message(f"已成功清理 {success_cnt} 个冗余重复条目，NVRAM 引导配置已恢复纯净！", "success")

    def show_context_menu(self, item_idx: int, click_x: int = -1, click_y: int = -1):
        """Native TUI right-click context menu for inspecting and operating bootloader entries."""
        if not self.ordered_entries or item_idx >= len(self.ordered_entries):
            return

        item = self.ordered_entries[item_idx]
        is_dedup = isinstance(item, DeduplicatedItem)
        sub_entries = item.entries if is_dedup else [item]

        if is_dedup and not (0 <= item.selected_sub_idx < len(sub_entries)):
            item.selected_sub_idx = 0

        menu_w = min(74, self.max_x - 4)

        while True:
            target = item.primary_entry if is_dedup else item
            lbl_lower = target.label.lower()
            is_linux = any(k in lbl_lower for k in ["linux", "systemd", "grub", "arch", "ubuntu", "fedora", "debian"])
            is_windows = any(k in lbl_lower for k in ["windows", "bootmgfw", "microsoft"])

            lines_needed = 9
            if is_dedup and len(sub_entries) > 1:
                lines_needed += 2 + len(sub_entries)
            if is_linux or is_windows:
                lines_needed += 2

            menu_h = min(lines_needed, self.max_y - 2)

            if click_x >= 0 and click_y >= 0:
                menu_x = max(2, min(click_x - 6, self.max_x - menu_w - 2))
                menu_y = max(1, min(click_y - 2, self.max_y - menu_h - 1))
            else:
                menu_x = max(2, (self.max_x - menu_w) // 2)
                menu_y = max(1, (self.max_y - menu_h) // 2)

            inner_w = menu_w - 2
            sub_hitboxes = []
            btn_hitboxes = []

            # Clear menu box background
            for y in range(menu_h):
                py = menu_y + y
                if y == 0:
                    title_str = f" 🖱️ 引导项操作菜单: {target.label} "
                    if str_display_width(title_str) > inner_w - 4:
                        title_str = f" 🖱️ 操作菜单: {target.label[:20]}... "
                    pad_d = max(0, inner_w - str_display_width(title_str))
                    self.stdscr.addstr(py, menu_x, "┌" + title_str + "─" * pad_d + "┐", curses.color_pair(self.HEADER) | curses.A_BOLD)
                elif y == menu_h - 1:
                    self.stdscr.addstr(py, menu_x, "└" + "─" * inner_w + "┘", curses.color_pair(self.HEADER))
                else:
                    self.stdscr.addstr(py, menu_x, "│" + " " * inner_w + "│", curses.color_pair(self.HEADER))

            cur_y = menu_y + 1

            # Status line
            p_active = "✔已启用" if target.active else "✖已禁用"
            p_cur = "★当前运行系统" if target.is_current else ("🚀下次单次启动" if target.is_next else "常规引导")
            info_txt = f" 当前目标: Boot{target.boot_num} │ 状态: {p_active} │ 角色: {p_cur}"
            self.stdscr.addstr(cur_y, menu_x + 2, pad_to_width(info_txt, inner_w - 2), curses.color_pair(self.CURRENT) | curses.A_BOLD)
            cur_y += 1

            self.stdscr.addstr(cur_y, menu_x, "├" + "─" * inner_w + "┤", curses.color_pair(self.HEADER))
            cur_y += 1

            # Sub-entry selection if multiple
            if is_dedup and len(sub_entries) > 1:
                sec_sub_title = " 【选择关联底层引导条目】(鼠标点击单选切换当前操作目标):"
                self.stdscr.addstr(cur_y, menu_x + 2, pad_to_width(sec_sub_title, inner_w - 2), curses.color_pair(self.TITLE) | curses.A_BOLD)
                cur_y += 1

                for s_i, sub in enumerate(sub_entries):
                    is_sel_sub = (s_i == item.selected_sub_idx)
                    radio = "(*)" if is_sel_sub else "( )"
                    m_hd = re.search(r"HD\((\d+),(GPT|MBR)", sub.path or "")
                    part_desc = f"分区 #{m_hd.group(1)}" if m_hd else "固件内建"
                    m_f = re.search(r"[/\\]EFI[/\\]([^\s\r\n\x00]+?\.efi)", sub.path or "", re.IGNORECASE)
                    f_desc = m_f.group(1) if m_f else (sub.path[:22] if sub.path else "无独立.efi")

                    sub_row_txt = f" {radio} [Boot{sub.boot_num}] {part_desc} │ \\EFI\\{f_desc}"
                    sub_color = (curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD) if is_sel_sub else curses.A_NORMAL
                    self.stdscr.addstr(cur_y, menu_x + 2, pad_to_width(sub_row_txt, inner_w - 2), sub_color)
                    sub_hitboxes.append((cur_y, menu_x + 2, menu_x + menu_w - 2, s_i))
                    cur_y += 1

                self.stdscr.addstr(cur_y, menu_x, "├" + "─" * inner_w + "┤", curses.color_pair(self.HEADER))
                cur_y += 1

            # Core Actions
            act_header = " 【引导快捷操作】(点击按钮或按键盘直接执行):"
            self.stdscr.addstr(cur_y, menu_x + 2, pad_to_width(act_header, inner_w - 2), curses.color_pair(self.TITLE) | curses.A_BOLD)
            cur_y += 1

            # Row 1: [1: ★ 设为主板第一项]  [n: 🚀 单次启动]
            b1_txt = " [ 1: ★ 设为主板第一项 ] "
            bn_txt = " [ n: 🚀 单次启动 (BootNext) ] "
            row1_gap = max(2, (inner_w - str_display_width(b1_txt) - str_display_width(bn_txt)) // 3)
            b1_x = menu_x + 1 + row1_gap
            bn_x = b1_x + str_display_width(b1_txt) + row1_gap
            self.stdscr.addstr(cur_y, b1_x, b1_txt, curses.color_pair(self.BUTTON_NORMAL))
            self.stdscr.addstr(cur_y, bn_x, bn_txt, curses.color_pair(self.BUTTON_NORMAL))
            btn_hitboxes.append(("1", cur_y, b1_x, b1_x + str_display_width(b1_txt)))
            btn_hitboxes.append(("n", cur_y, bn_x, bn_x + str_display_width(bn_txt)))
            cur_y += 1

            # Row 2: [空格: 🔄 启禁状态]  [回车: 🔍 查看EFI详情]
            b_sp_txt = " [ 空格: 🔄 切换启用/禁用 ] "
            b_en_txt = " [ 回车: 🔍 查看完整EFI详情 ] "
            row2_gap = max(2, (inner_w - str_display_width(b_sp_txt) - str_display_width(b_en_txt)) // 3)
            b_sp_x = menu_x + 1 + row2_gap
            b_en_x = b_sp_x + str_display_width(b_sp_txt) + row2_gap
            self.stdscr.addstr(cur_y, b_sp_x, b_sp_txt, curses.color_pair(self.BUTTON_NORMAL))
            self.stdscr.addstr(cur_y, b_en_x, b_en_txt, curses.color_pair(self.BUTTON_NORMAL))
            btn_hitboxes.append(("space", cur_y, b_sp_x, b_sp_x + str_display_width(b_sp_txt)))
            btn_hitboxes.append(("enter", cur_y, b_en_x, b_en_x + str_display_width(b_en_txt)))
            cur_y += 1

            # OS Actions
            if is_windows:
                self.stdscr.addstr(cur_y, menu_x, "├" + "─" * inner_w + "┤", curses.color_pair(self.HEADER))
                cur_y += 1
                bw_txt = " [ w: 🩺 Win11深度体检与双系统 ] "
                if is_dedup and len(sub_entries) > 1:
                    bc_txt = " [ c: 🧹 一键清理冗余重复条目 ] "
                    w_gap = max(2, (inner_w - str_display_width(bw_txt) - str_display_width(bc_txt)) // 3)
                    bw_x = menu_x + 1 + w_gap
                    bc_x = bw_x + str_display_width(bw_txt) + w_gap
                    self.stdscr.addstr(cur_y, bw_x, bw_txt, curses.color_pair(self.BUTTON_NORMAL))
                    self.stdscr.addstr(cur_y, bc_x, bc_txt, curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD)
                    btn_hitboxes.append(("w", cur_y, bw_x, bw_x + str_display_width(bw_txt)))
                    btn_hitboxes.append(("c", cur_y, bc_x, bc_x + str_display_width(bc_txt)))
                else:
                    bw_x = menu_x + max(2, (inner_w - str_display_width(bw_txt)) // 2)
                    self.stdscr.addstr(cur_y, bw_x, bw_txt, curses.color_pair(self.BUTTON_NORMAL))
                    btn_hitboxes.append(("w", cur_y, bw_x, bw_x + str_display_width(bw_txt)))
                cur_y += 1
            elif is_linux:
                self.stdscr.addstr(cur_y, menu_x, "├" + "─" * inner_w + "┤", curses.color_pair(self.HEADER))
                cur_y += 1
                bk_txt = " [ k: 🔄 切换内核 ] "
                bb_txt = " [ b: ⚙️ 引导配置 ] "
                bwin_txt = " [ w: 🛠 注入Win11 ] "
                tot_l_w = str_display_width(bk_txt) + str_display_width(bb_txt) + str_display_width(bwin_txt)
                l_gap = max(1, (inner_w - tot_l_w) // 4)
                bk_x = menu_x + 1 + l_gap
                bb_x = bk_x + str_display_width(bk_txt) + l_gap
                bwin_x = bb_x + str_display_width(bb_txt) + l_gap
                self.stdscr.addstr(cur_y, bk_x, bk_txt, curses.color_pair(self.BUTTON_NORMAL))
                self.stdscr.addstr(cur_y, bb_x, bb_txt, curses.color_pair(self.BUTTON_NORMAL))
                self.stdscr.addstr(cur_y, bwin_x, bwin_txt, curses.color_pair(self.BUTTON_NORMAL))
                btn_hitboxes.append(("k", cur_y, bk_x, bk_x + str_display_width(bk_txt)))
                btn_hitboxes.append(("b", cur_y, bb_x, bb_x + str_display_width(bb_txt)))
                btn_hitboxes.append(("w", cur_y, bwin_x, bwin_x + str_display_width(bwin_txt)))
                cur_y += 1

            # Maintenance & Exit
            self.stdscr.addstr(cur_y, menu_x, "├" + "─" * inner_w + "┤", curses.color_pair(self.HEADER))
            cur_y += 1
            bd_txt = " [ d: 🗑️ 从NVRAM删除条目 ] "
            b_esc_txt = " [ Esc / 点击外部: 🔙 关闭 ] "
            m_gap = max(2, (inner_w - str_display_width(bd_txt) - str_display_width(b_esc_txt)) // 3)
            bd_x = menu_x + 1 + m_gap
            besc_x = bd_x + str_display_width(bd_txt) + m_gap
            self.stdscr.addstr(cur_y, bd_x, bd_txt, curses.color_pair(self.BUTTON_DANGER))
            self.stdscr.addstr(cur_y, besc_x, b_esc_txt, curses.color_pair(self.BUTTON_NORMAL))
            btn_hitboxes.append(("d", cur_y, bd_x, bd_x + str_display_width(bd_txt)))
            btn_hitboxes.append(("esc", cur_y, besc_x, besc_x + str_display_width(b_esc_txt)))

            self.stdscr.noutrefresh()
            curses.doupdate()

            key = self.stdscr.getch()

            if key in [ord('q'), ord('Q'), 27]:
                break
            elif key == ord('1'):
                self.set_as_first_for_entry(target)
                break
            elif key in [ord('n'), ord('N')]:
                self.set_next_boot_for_entry(target)
                break
            elif key == ord(' '):
                self.toggle_active_for_entry(target)
                continue
            elif key in [10, 13, curses.KEY_ENTER]:
                self.show_detail(target_entry=target)
                break
            elif key in [ord('w'), ord('W')] and (is_windows or is_linux):
                self.show_win11_dialog()
                break
            elif key in [ord('k'), ord('K')] and is_linux:
                self.show_kernel_dialog()
                break
            elif key in [ord('b'), ord('B')] and is_linux:
                self.show_bootloader_dialog()
                break
            elif key in [ord('c'), ord('C')] and is_dedup and len(sub_entries) > 1:
                self.cleanup_duplicate_entries(item)
                break
            elif key in [ord('d'), ord('D')]:
                self.delete_entry_for_entry(target)
                break
            elif key in [curses.KEY_UP, curses.KEY_DOWN]:
                if is_dedup and len(sub_entries) > 1:
                    delta = -1 if key == curses.KEY_UP else 1
                    item.selected_sub_idx = (item.selected_sub_idx + delta) % len(sub_entries)
                    continue
            elif key == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, bstate = curses.getmouse()
                    # Right click closes menu
                    if bstate & 0x1c00:
                        break
                    # Click outside closes menu
                    if mx < menu_x or mx >= menu_x + menu_w or my < menu_y or my >= menu_y + menu_h:
                        break
                    # Left click on sub-entries
                    clicked_sub = False
                    for sy, sx1, sx2, s_idx in sub_hitboxes:
                        if my == sy and sx1 <= mx <= sx2:
                            item.selected_sub_idx = s_idx
                            clicked_sub = True
                            break
                    if clicked_sub:
                        continue
                    # Left click on buttons
                    for act_id, by, bx1, bx2 in btn_hitboxes:
                        if my == by and bx1 <= mx <= bx2:
                            if act_id == "1":
                                self.set_as_first_for_entry(target)
                                return
                            elif act_id == "n":
                                self.set_next_boot_for_entry(target)
                                return
                            elif act_id == "space":
                                self.toggle_active_for_entry(target)
                                break
                            elif act_id == "enter":
                                self.show_detail(target_entry=target)
                                return
                            elif act_id == "w":
                                self.show_win11_dialog()
                                return
                            elif act_id == "k":
                                self.show_kernel_dialog()
                                return
                            elif act_id == "b":
                                self.show_bootloader_dialog()
                                return
                            elif act_id == "c":
                                self.cleanup_duplicate_entries(item)
                                return
                            elif act_id == "d":
                                self.delete_entry_for_entry(target)
                                return
                            elif act_id == "esc":
                                return
                except Exception:
                    pass

    def show_detail(self, target_entry: Optional[BootEntry] = None):
        """Dedicated visual workstation for inspecting and operating a UEFI boot entry."""
        if not self.ordered_entries:
            return

        focused_btn = 0
        detail_msg = ""
        detail_msg_type = "info"

        while True:
            self.max_y, self.max_x = self.stdscr.getmaxyx()
            self.stdscr.erase()

            if target_entry is not None:
                entry = target_entry
            else:
                if self.selected_idx >= len(self.ordered_entries):
                    self.selected_idx = max(0, len(self.ordered_entries) - 1)
                item = self.ordered_entries[self.selected_idx]
                entry = item.primary_entry if isinstance(item, DeduplicatedItem) else item

            # Determine entry category
            lbl_lower = entry.label.lower()
            is_linux_mgr = any(k in lbl_lower for k in ["linux", "systemd", "grub", "arch", "ubuntu", "fedora", "debian"])
            is_win_mgr = any(k in lbl_lower for k in ["windows", "bootmgfw", "microsoft"])

            # Parse EFI path cleanly
            import re
            part_str = "(主板固件内建 / 硬件自检引导)"
            file_str = "(无独立物理 .efi 文件)"
            m_hd = re.search(r"HD\((\d+),(GPT|MBR),([0-9a-fA-F-]+)", entry.path)
            if m_hd:
                part_str = f"第 {m_hd.group(1)} 分区 ({m_hd.group(2)}, PARTUUID: {m_hd.group(3)})"
            m_file = re.search(r"[/\\]EFI[/\\]([^\s\r\n\x00]+?\.efi)", entry.path, re.IGNORECASE)
            if m_file:
                file_str = r"\EFI" + "\\" + m_file.group(1).replace("/", "\\")
            elif entry.path and entry.path != "RC":
                file_str = entry.path

            w = self.max_x - 2
            order_rank = self.selected_idx + 1
            tot_count = len(self.ordered_entries)

            try:
                # 1. Header (Lines 0..2)
                title = " UEFI 启动项详情与高级管理工作台 "
                mode = f" [条目: *{entry.boot_num}] "
                self.stdscr.addstr(0, 0, "┌" + "─" * (self.max_x - 2) + "┐", curses.color_pair(self.HEADER))
                self.stdscr.addstr(1, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(1, 2, title, curses.color_pair(self.TITLE) | curses.A_BOLD)
                mode_w = str_display_width(mode)
                self.stdscr.addstr(1, max(str_display_width(title) + 4, self.max_x - 1 - mode_w), mode, curses.color_pair(self.CURRENT))
                self.stdscr.addstr(1, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(2, 0, "├" + "─" * (self.max_x - 2) + "┤", curses.color_pair(self.HEADER))

                # 2. Status Bar (Line 3)
                rank_str = f"第 1 顺位 (开机首选)" if order_rank == 1 else f"第 {order_rank} 顺位 / 共 {tot_count} 项"
                status_txt = f" 启动项: {entry.label} │ 编号: {entry.boot_num} │ 状态: {'已启用' if entry.active else '已禁用'} │ 启动优先级: {rank_str} "
                self.stdscr.addstr(3, 0, pad_to_width(status_txt, self.max_x), curses.color_pair(self.STATUS_BAR))

                # 3. Card 1: 核心属性解析 (Lines 5..)
                cur_line = 5
                self.stdscr.addstr(4, 0, "│" + " " * w + "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                sec1_title = f" 【UEFI 启动项核心属性】[{entry.label}]"
                self.stdscr.addstr(cur_line, 2, pad_to_width(sec1_title, w - 2), curses.color_pair(self.HEADER) | curses.A_BOLD)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                p_active = "✔ 已启用 (Active)" if entry.active else "✖ 已禁用 (Disabled)"
                p_current = "★ 当前运行系统 (BootCurrent)" if entry.is_current else "待机系统"
                p_next = "🚀 已设为下次一次性启动 (BootNext)" if entry.is_next else "否 (未设 BootNext)"

                l1 = f" 条目编号: {entry.boot_num} │ 显示标签: {entry.label} │ 启用状态: {p_active}"
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(l1, w - 2), curses.A_NORMAL)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                l2 = f" 运行状态: {p_current} │ 单次引导: {p_next} │ 启动顺位: {rank_str}"
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(l2, w - 2), curses.A_NORMAL)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                # 4. Divider
                self.stdscr.addstr(cur_line, 0, "├" + "─" * (self.max_x - 2) + "┤", curses.color_pair(self.HEADER))
                cur_line += 1

                # 5. Card 2: 存储物理定位与二进制路径 (No truncation!)
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                sec2_title = " 【EFI 物理硬件与引导二进制解析】(完整定位，绝不截断):"
                self.stdscr.addstr(cur_line, 2, pad_to_width(sec2_title, w - 2), curses.color_pair(self.TITLE) | curses.A_BOLD)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                p_l1 = f" 目标物理分区: {part_str}"
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(p_l1, w - 2), curses.A_NORMAL)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                p_l2 = f" 核心引导文件: {file_str}"
                file_color = curses.color_pair(self.ACTIVE) | curses.A_BOLD if file_str.endswith(".efi") else curses.A_NORMAL
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(p_l2, w - 2), file_color)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                # Wrap raw hardware path if available so it NEVER gets cut off
                if entry.path and entry.path != "RC":
                    raw_title = " 固件原生路径: "
                    prefix_w = str_display_width(raw_title)
                    max_c_w = max(20, w - 4 - prefix_w)
                    raw_content = entry.path
                    chunks = [raw_content[i:i + max_c_w] for i in range(0, len(raw_content), max_c_w)]
                    for idx, ch in enumerate(chunks[:2]):
                        prefix = raw_title if idx == 0 else " " * prefix_w
                        self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                        self.stdscr.addstr(cur_line, 2, pad_to_width(prefix + ch, w - 2), curses.color_pair(self.HELP))
                        self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                        cur_line += 1

                # 6. Card 3: 状态研判与系统诊断
                if is_linux_mgr:
                    diag_str = " 状态诊断: ✔ 识别为 Linux 核心引导管理器，可直接进行内核切换、Win11条目注入或引导加载器配置。"
                    diag_col = curses.color_pair(self.CURRENT) | curses.A_BOLD
                elif is_win_mgr:
                    diag_str = " 状态诊断: ✔ 识别为 Windows 11 官方引导，可一键设为首选启动、单次启动或查看双系统互通架构体检。"
                    diag_col = curses.color_pair(self.CURRENT) | curses.A_BOLD
                else:
                    diag_str = " 状态诊断: ✔ 识别为主板固件硬件/外设自检启动条目，可通过下方按钮置顶或设为单次启动。"
                    diag_col = curses.color_pair(self.ACTIVE) | curses.A_BOLD

                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(diag_str, w - 2), diag_col)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                # 7. Divider before Buttons
                self.stdscr.addstr(cur_line, 0, "├" + "─" * (self.max_x - 2) + "┤", curses.color_pair(self.HEADER))
                cur_line += 1

                # 8. Interactive Action Buttons
                btn_sec_title = " 【可视化快捷操作按钮】(鼠标直接点击，或按 Tab / ← / → 切换焦点后回车执行):"
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(btn_sec_title, w - 2), curses.color_pair(self.HEADER) | curses.A_BOLD)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                btn_hitboxes = []
                num_btns = 0

                if is_linux_mgr:
                    num_btns = 6
                    # Row 1: 0 (切换内核), 1 (注入Win11), 2 (引导配置)
                    b0_txt = " [ ▶ 🔄 切换启动内核 ◀ ] " if focused_btn == 0 else " [   🔄 切换启动内核   ] "
                    b1_txt = " [ ▶ 🛠 注入 Win11 引导 ◀ ] " if focused_btn == 1 else " [   🛠 注入 Win11 引导   ] "
                    b2_txt = " [ ▶ ⚙️ Linux 引导配置 ◀ ] " if focused_btn == 2 else " [   ⚙️ Linux 引导配置   ] "

                    c0 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 0 else curses.color_pair(self.BUTTON_NORMAL)
                    c1 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 1 else curses.color_pair(self.BUTTON_NORMAL)
                    c2 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 2 else curses.color_pair(self.BUTTON_NORMAL)

                    tot_r1_w = str_display_width(b0_txt) + 3 + str_display_width(b1_txt) + 3 + str_display_width(b2_txt)
                    r1_start_x = max(2, (self.max_x - tot_r1_w) // 2)
                    b0_x = r1_start_x
                    b1_x = b0_x + str_display_width(b0_txt) + 3
                    b2_x = b1_x + str_display_width(b1_txt) + 3
                    r1_y = cur_line

                    self.stdscr.addstr(r1_y, 0, "│", curses.color_pair(self.HEADER))
                    self.stdscr.addstr(r1_y, 1, " " * w)
                    self.stdscr.addstr(r1_y, b0_x, b0_txt, c0)
                    self.stdscr.addstr(r1_y, b1_x, b1_txt, c1)
                    self.stdscr.addstr(r1_y, b2_x, b2_txt, c2)
                    self.stdscr.addstr(r1_y, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                    cur_line += 1

                    # Row 2: 3 (置顶第一项), 4 (单次启动), 5 (返回主列表)
                    b3_txt = " [ ▶ ★ 设为主板第一项 ◀ ] " if focused_btn == 3 else " [   ★ 设为主板第一项   ] "
                    b4_txt = " [ ▶ 🚀 单次启动 (BootNext) ◀ ] " if focused_btn == 4 else " [   🚀 单次启动 (BootNext)   ] "
                    b5_txt = " [ ▶ 🔙 返回主列表 ◀ ] " if focused_btn == 5 else " [   🔙 返回主列表   ] "

                    c3 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 3 else curses.color_pair(self.BUTTON_NORMAL)
                    c4 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 4 else curses.color_pair(self.BUTTON_NORMAL)
                    c5 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 5 else curses.color_pair(self.BUTTON_NORMAL)

                    tot_r2_w = str_display_width(b3_txt) + 3 + str_display_width(b4_txt) + 3 + str_display_width(b5_txt)
                    r2_start_x = max(2, (self.max_x - tot_r2_w) // 2)
                    b3_x = r2_start_x
                    b4_x = b3_x + str_display_width(b3_txt) + 3
                    b5_x = b4_x + str_display_width(b4_txt) + 3
                    r2_y = cur_line

                    self.stdscr.addstr(r2_y, 0, "│", curses.color_pair(self.HEADER))
                    self.stdscr.addstr(r2_y, 1, " " * w)
                    self.stdscr.addstr(r2_y, b3_x, b3_txt, c3)
                    self.stdscr.addstr(r2_y, b4_x, b4_txt, c4)
                    self.stdscr.addstr(r2_y, b5_x, b5_txt, c5)
                    self.stdscr.addstr(r2_y, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                    cur_line += 1

                    btn_hitboxes = [
                        (0, r1_y, b0_x, b0_x + str_display_width(b0_txt)),
                        (1, r1_y, b1_x, b1_x + str_display_width(b1_txt)),
                        (2, r1_y, b2_x, b2_x + str_display_width(b2_txt)),
                        (3, r2_y, b3_x, b3_x + str_display_width(b3_txt)),
                        (4, r2_y, b4_x, b4_x + str_display_width(b4_txt)),
                        (5, r2_y, b5_x, b5_x + str_display_width(b5_txt)),
                    ]

                elif is_win_mgr:
                    num_btns = 5
                    # Row 1: 0 (Win11分析), 1 (设为第一项), 2 (单次启动)
                    b0_txt = " [ ▶ 🔍 Win11 引导深度分析 ◀ ] " if focused_btn == 0 else " [   🔍 Win11 引导深度分析   ] "
                    b1_txt = " [ ▶ ★ 设为主板第一项 ◀ ] " if focused_btn == 1 else " [   ★ 设为主板第一项   ] "
                    b2_txt = " [ ▶ 🚀 单次启动 (BootNext) ◀ ] " if focused_btn == 2 else " [   🚀 单次启动 (BootNext)   ] "

                    c0 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 0 else curses.color_pair(self.BUTTON_NORMAL)
                    c1 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 1 else curses.color_pair(self.BUTTON_NORMAL)
                    c2 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 2 else curses.color_pair(self.BUTTON_NORMAL)

                    tot_r1_w = str_display_width(b0_txt) + 3 + str_display_width(b1_txt) + 3 + str_display_width(b2_txt)
                    r1_start_x = max(2, (self.max_x - tot_r1_w) // 2)
                    b0_x = r1_start_x
                    b1_x = b0_x + str_display_width(b0_txt) + 3
                    b2_x = b1_x + str_display_width(b1_txt) + 3
                    r1_y = cur_line

                    self.stdscr.addstr(r1_y, 0, "│", curses.color_pair(self.HEADER))
                    self.stdscr.addstr(r1_y, 1, " " * w)
                    self.stdscr.addstr(r1_y, b0_x, b0_txt, c0)
                    self.stdscr.addstr(r1_y, b1_x, b1_txt, c1)
                    self.stdscr.addstr(r1_y, b2_x, b2_txt, c2)
                    self.stdscr.addstr(r1_y, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                    cur_line += 1

                    # Row 2: 3 (启禁切换), 4 (返回主列表)
                    b3_txt = " [ ▶ 🔄 切换启用/禁用状态 ◀ ] " if focused_btn == 3 else " [   🔄 切换启用/禁用状态   ] "
                    b4_txt = " [ ▶ 🔙 返回主列表 ◀ ] " if focused_btn == 4 else " [   🔙 返回主列表   ] "

                    c3 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 3 else curses.color_pair(self.BUTTON_NORMAL)
                    c4 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 4 else curses.color_pair(self.BUTTON_NORMAL)

                    tot_r2_w = str_display_width(b3_txt) + 4 + str_display_width(b4_txt)
                    r2_start_x = max(2, (self.max_x - tot_r2_w) // 2)
                    b3_x = r2_start_x
                    b4_x = b3_x + str_display_width(b3_txt) + 4
                    r2_y = cur_line

                    self.stdscr.addstr(r2_y, 0, "│", curses.color_pair(self.HEADER))
                    self.stdscr.addstr(r2_y, 1, " " * w)
                    self.stdscr.addstr(r2_y, b3_x, b3_txt, c3)
                    self.stdscr.addstr(r2_y, b4_x, b4_txt, c4)
                    self.stdscr.addstr(r2_y, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                    cur_line += 1

                    btn_hitboxes = [
                        (0, r1_y, b0_x, b0_x + str_display_width(b0_txt)),
                        (1, r1_y, b1_x, b1_x + str_display_width(b1_txt)),
                        (2, r1_y, b2_x, b2_x + str_display_width(b2_txt)),
                        (3, r2_y, b3_x, b3_x + str_display_width(b3_txt)),
                        (4, r2_y, b4_x, b4_x + str_display_width(b4_txt)),
                    ]
                else:
                    num_btns = 4
                    # Row 1: 0 (设为第一项), 1 (单次启动), 2 (启禁切换)
                    b0_txt = " [ ▶ ★ 设为主板第一项 ◀ ] " if focused_btn == 0 else " [   ★ 设为主板第一项   ] "
                    b1_txt = " [ ▶ 🚀 单次启动 (BootNext) ◀ ] " if focused_btn == 1 else " [   🚀 单次启动 (BootNext)   ] "
                    b2_txt = " [ ▶ 🔄 切换启用/禁用状态 ◀ ] " if focused_btn == 2 else " [   🔄 切换启用/禁用状态   ] "

                    c0 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 0 else curses.color_pair(self.BUTTON_NORMAL)
                    c1 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 1 else curses.color_pair(self.BUTTON_NORMAL)
                    c2 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 2 else curses.color_pair(self.BUTTON_NORMAL)

                    tot_r1_w = str_display_width(b0_txt) + 3 + str_display_width(b1_txt) + 3 + str_display_width(b2_txt)
                    r1_start_x = max(2, (self.max_x - tot_r1_w) // 2)
                    b0_x = r1_start_x
                    b1_x = b0_x + str_display_width(b0_txt) + 3
                    b2_x = b1_x + str_display_width(b1_txt) + 3
                    r1_y = cur_line

                    self.stdscr.addstr(r1_y, 0, "│", curses.color_pair(self.HEADER))
                    self.stdscr.addstr(r1_y, 1, " " * w)
                    self.stdscr.addstr(r1_y, b0_x, b0_txt, c0)
                    self.stdscr.addstr(r1_y, b1_x, b1_txt, c1)
                    self.stdscr.addstr(r1_y, b2_x, b2_txt, c2)
                    self.stdscr.addstr(r1_y, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                    cur_line += 1

                    # Row 2: 3 (返回主列表)
                    b3_txt = " [ ▶ 🔙 返回主列表 ◀ ] " if focused_btn == 3 else " [   🔙 返回主列表   ] "
                    c3 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 3 else curses.color_pair(self.BUTTON_NORMAL)
                    b3_x = max(2, (self.max_x - str_display_width(b3_txt)) // 2)
                    r2_y = cur_line

                    self.stdscr.addstr(r2_y, 0, "│", curses.color_pair(self.HEADER))
                    self.stdscr.addstr(r2_y, 1, " " * w)
                    self.stdscr.addstr(r2_y, b3_x, b3_txt, c3)
                    self.stdscr.addstr(r2_y, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                    cur_line += 1

                    btn_hitboxes = [
                        (0, r1_y, b0_x, b0_x + str_display_width(b0_txt)),
                        (1, r1_y, b1_x, b1_x + str_display_width(b1_txt)),
                        (2, r1_y, b2_x, b2_x + str_display_width(b2_txt)),
                        (3, r2_y, b3_x, b3_x + str_display_width(b3_txt)),
                    ]

                # Fill remaining rows up to bottom
                while cur_line < self.max_y - 1:
                    self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                    self.stdscr.addstr(cur_line, 1, " " * w)
                    self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                    cur_line += 1

                # 9. Bottom Border with integrated Status Message Badge
                bottom_y = self.max_y - 1
                if not detail_msg:
                    self.stdscr.addstr(bottom_y, 0, "└" + "─" * (self.max_x - 2) + "┘", curses.color_pair(self.HEADER))
                else:
                    b_color = curses.color_pair(self.ACTIVE) | curses.A_BOLD if detail_msg_type == "success" else (curses.color_pair(self.INACTIVE) | curses.A_BOLD if detail_msg_type == "error" else curses.color_pair(self.CURRENT) | curses.A_BOLD)
                    b_icon = "✔" if detail_msg_type == "success" else ("✖" if detail_msg_type == "error" else "ℹ")
                    badge_str = f" [ {b_icon} {detail_msg} ] "
                    b_w = str_display_width(badge_str)
                    left_d = 2
                    right_d = max(0, self.max_x - 2 - left_d - b_w)
                    self.stdscr.addstr(bottom_y, 0, "└" + "─" * left_d, curses.color_pair(self.HEADER))
                    self.stdscr.addstr(bottom_y, 1 + left_d, badge_str, b_color)
                    self.stdscr.addstr(bottom_y, 1 + left_d + b_w, "─" * right_d + "┘", curses.color_pair(self.HEADER))

                self.stdscr.noutrefresh()
                curses.doupdate()

                key = self.stdscr.getch()

                def do_action(action_idx: int):
                    nonlocal detail_msg, detail_msg_type
                    if is_linux_mgr:
                        if action_idx == 0:
                            self.show_kernel_dialog()
                        elif action_idx == 1:
                            self.show_win11_dialog()
                        elif action_idx == 2:
                            self.show_bootloader_dialog()
                        elif action_idx == 3:
                            self.set_as_first()
                            detail_msg = f"已将 '{entry.label}' 设为主板第一优先启动项。"
                            detail_msg_type = "success"
                        elif action_idx == 4:
                            self.set_next_boot()
                            detail_msg = f"已将 '{entry.label}' 设为下次重启单次引导 (BootNext)。"
                            detail_msg_type = "success"
                        elif action_idx == 5:
                            return "RETURN"
                    elif is_win_mgr:
                        if action_idx == 0:
                            self.show_win11_dialog()
                        elif action_idx == 1:
                            self.set_as_first()
                            detail_msg = f"已将 Windows 设为主板第一启动项。"
                            detail_msg_type = "success"
                        elif action_idx == 2:
                            self.set_next_boot()
                            detail_msg = f"已设置下次重启单次启动 Windows 11。"
                            detail_msg_type = "success"
                        elif action_idx == 3:
                            self.toggle_active()
                            detail_msg = f"状态已切换为: {'已启用' if entry.active else '已禁用'}。"
                            detail_msg_type = "success"
                        elif action_idx == 4:
                            return "RETURN"
                    else:
                        if action_idx == 0:
                            self.set_as_first()
                            detail_msg = f"已将 '{entry.label}' 设为主板第一启动项。"
                            detail_msg_type = "success"
                        elif action_idx == 1:
                            self.set_next_boot()
                            detail_msg = f"已将 '{entry.label}' 设为下次重启单次引导。"
                            detail_msg_type = "success"
                        elif action_idx == 2:
                            self.toggle_active()
                            detail_msg = f"状态已切换为: {'已启用' if entry.active else '已禁用'}。"
                            detail_msg_type = "success"
                        elif action_idx == 3:
                            return "RETURN"

                if key in [ord('q'), ord('Q'), 27]:
                    break
                elif key in [curses.KEY_RIGHT, 9]:  # Tab or Right
                    focused_btn = (focused_btn + 1) % num_btns
                elif key in [curses.KEY_LEFT, curses.KEY_BTAB]:  # Shift-Tab or Left
                    focused_btn = (focused_btn - 1) % num_btns
                elif key in [curses.KEY_DOWN]:
                    if is_linux_mgr and focused_btn in [0, 1, 2]:
                        focused_btn = min(num_btns - 1, focused_btn + 3)
                    elif is_win_mgr and focused_btn in [0, 1, 2]:
                        focused_btn = min(num_btns - 1, focused_btn + 3)
                    elif not is_linux_mgr and not is_win_mgr and focused_btn in [0, 1, 2]:
                        focused_btn = 3
                elif key in [curses.KEY_UP]:
                    if is_linux_mgr and focused_btn in [3, 4, 5]:
                        focused_btn = focused_btn - 3
                    elif is_win_mgr and focused_btn in [3, 4]:
                        focused_btn = min(2, focused_btn - 3)
                    elif not is_linux_mgr and not is_win_mgr and focused_btn == 3:
                        focused_btn = 1
                elif key in [10, 13, curses.KEY_ENTER, ord(' ')]:
                    ret = do_action(focused_btn)
                    if ret == "RETURN":
                        break
                elif is_linux_mgr and key in [ord('k'), ord('K')]:
                    do_action(0)
                elif (is_linux_mgr or is_win_mgr) and key in [ord('w'), ord('W')]:
                    do_action(1 if is_linux_mgr else 0)
                elif is_linux_mgr and key in [ord('b'), ord('B')]:
                    do_action(2)
                elif key == ord('1'):
                    do_action(3 if is_linux_mgr else (1 if is_win_mgr else 0))
                elif key in [ord('n'), ord('N')]:
                    do_action(4 if is_linux_mgr else (2 if is_win_mgr else 1))
                elif key == ord(' ') and not is_linux_mgr:
                    do_action(3 if is_win_mgr else 2)
                elif key == curses.KEY_MOUSE:
                    try:
                        _, mx, my, _, bstate = curses.getmouse()
                        if bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED):
                            for bi, by, x1, x2 in btn_hitboxes:
                                if my == by and x1 <= mx <= x2:
                                    focused_btn = bi
                                    ret = do_action(bi)
                                    if ret == "RETURN":
                                        return
                                    break
                    except Exception:
                        pass
            except curses.error:
                break

    def show_help(self):
        """Show help screen."""
        help_lines = [
            "UEFI 启动项与多系统引导管理系统 - 快捷键帮助",
            "═" * 56,
            "",
            "【基础导航】",
            "  ↑ / ↓  或  k / j : 上下移动选择启动项",
            "  PgUp / PgDn      : 上翻页 / 下翻页快速滚动",
            "  Home / End       : 跳转至首项 / 末项",
            "  回车 (Enter)     : 查看选中的启动项完整详情 (含 EFI 硬件路径)",
            "",
            "【固件启动项操作】",
            "  1                : 将当前选中的启动项一键置顶 (设为第一启动项)",
            "  m / M            : 上移 (m) / 下移 (M) 调整启动项优先级",
            "  空格 (Space)     : 切换当前项启用 / 禁用 (Active/Inactive, 仅 Linux)",
            "  n                : 设为下次重启一次性引导项 (BootNext)",
            "  t                : 设置主板固件菜单倒计时超时时间 (Timeout)",
            "  d                : 删除选中的启动项 (带二次安全确认)",
            "",
            "【Linux 高级引导与系统修复】",
            "  k                : Linux 多内核探测与一键切换 (标准 / Zen / LTS 等)",
            "                     - 自动识别已安装内核、为未配置内核生成条目、切换默认/单次启动",
            "  b                : Linux 引导配置管理 (修改 GRUB / systemd-boot)",
            "                     - 修改倒计时、默认内核、内核启动参数并自动应用",
            "  w                : Windows 11 引导深度智能分析与双系统互通",
            "                     - NVRAM、ESP、BitLocker、TPM 2.0、SecureBoot 综合体检",
            "                     - 诊断双 ESP 架构隔离并可一键同步 Windows 引导到 systemd-boot",
            "  c / f            : 引导深度诊断与一键修复重建",
            "                     - 自动扫描全盘 ESP 分区，将丢失的 Linux 引导写回 NVRAM",
            "  e                : 查看物理 ESP 分区与 .efi 引导文件结构",
            "",
            "【其它操作】",
            "  r                : 刷新并重新读取主板引导配置",
            "  ?                : 显示本帮助窗口",
            "  q / ESC          : 退出程序",
            "",
            "【权限说明】",
            "  Linux:   写入 NVRAM 或挂载物理分区需要 root (sudo) 权限",
            "  Windows: 读写 BCD 固件需要管理员权限 (已支持 UAC 自动提权)",
        ]
        self._show_info_popup(help_lines, "快捷键使用帮助")

    def set_timeout_dialog(self, initial_val: Optional[int] = None, title: Optional[str] = None) -> Optional[int]:
        """Interactive visual dialog to set boot timeout with stepper buttons and presets."""
        if initial_val is not None:
            current_val = max(0, initial_val)
        else:
            if not self.config:
                return None
            current_val = self.config.timeout if (self.config.timeout is not None and self.config.timeout >= 0) else 0
        temp_val = current_val

        # 0: [ ➖ 减少 1 秒 ], 1: [ ➕ 增加 1 秒 ], 2: [0秒], 3: [3秒], 4: [5秒], 5: [10秒], 6: [ ✔ 保存并应用 ], 7: [ ✖ 取消返回 ]
        focused_idx = 6

        dialog_w = min(62, self.max_x - 4)
        dialog_h = 11
        dialog_y = (self.max_y - dialog_h) // 2
        dialog_x = (self.max_x - dialog_w) // 2
        dlg_title = f" {title} " if title else " 主板引导倒计时设置 (Boot Timeout) "

        while True:
            try:
                self.stdscr.erase()
                for y in range(dialog_h):
                    if y == 0:
                        self.stdscr.addstr(dialog_y + y, dialog_x,
                                           "┌" + "─" * (dialog_w - 2) + "┐",
                                           curses.color_pair(self.HEADER))
                    elif y == dialog_h - 1:
                        self.stdscr.addstr(dialog_y + y, dialog_x,
                                           "└" + "─" * (dialog_w - 2) + "┘",
                                           curses.color_pair(self.HEADER))
                    elif y == 2 or y == dialog_h - 3:
                        self.stdscr.addstr(dialog_y + y, dialog_x,
                                           "├" + "─" * (dialog_w - 2) + "┤",
                                           curses.color_pair(self.HEADER))
                    else:
                        self.stdscr.addstr(dialog_y + y, dialog_x,
                                           "│" + " " * (dialog_w - 2) + "│",
                                           curses.color_pair(self.HEADER))

                tx = dialog_x + max(2, (dialog_w - str_display_width(dlg_title)) // 2)
                self.stdscr.addstr(dialog_y + 1, tx, dlg_title, curses.color_pair(self.TITLE) | curses.A_BOLD)

                val_str = f"设定倒计时: 【 {temp_val} 】 秒"
                if temp_val == 0:
                    val_str += " (即刻启动 / 跳过菜单)"
                vx = dialog_x + max(2, (dialog_w - str_display_width(val_str)) // 2)
                self.stdscr.addstr(dialog_y + 3, vx, val_str, curses.color_pair(self.CURRENT) | curses.A_BOLD)

                # Row 1: Steppers
                btn_minus_txt = " [ ▶ ➖ 减少 1 秒 ◀ ] " if focused_idx == 0 else " [   ➖ 减少 1 秒   ] "
                btn_plus_txt = " [ ▶ ➕ 增加 1 秒 ◀ ] " if focused_idx == 1 else " [   ➕ 增加 1 秒   ] "
                c_minus = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_idx == 0 else curses.color_pair(self.BUTTON_NORMAL)
                c_plus = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_idx == 1 else curses.color_pair(self.BUTTON_NORMAL)

                stepper_y = dialog_y + 5
                tot_step_w = str_display_width(btn_minus_txt) + 4 + str_display_width(btn_plus_txt)
                m_x = dialog_x + max(2, (dialog_w - tot_step_w) // 2)
                p_x = m_x + str_display_width(btn_minus_txt) + 4
                self.stdscr.addstr(stepper_y, m_x, btn_minus_txt, c_minus)
                self.stdscr.addstr(stepper_y, p_x, btn_plus_txt, c_plus)

                # Row 2: Presets
                preset_y = dialog_y + 6
                p_items = [(2, "0秒 (即刻)", 0), (3, "3秒", 3), (4, "5秒", 5), (5, "10秒", 10)]
                tot_p_w = sum(str_display_width(f"[{lbl}]") + 2 for _, lbl, _ in p_items) - 2
                px_cur = dialog_x + max(2, (dialog_w - tot_p_w) // 2)
                preset_boxes = []
                for p_idx, p_lbl, p_val in p_items:
                    is_p_foc = (focused_idx == p_idx)
                    p_txt = f" [▶{p_lbl}◀] " if is_p_foc else f" [ {p_lbl} ] "
                    p_col = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if is_p_foc else (curses.color_pair(self.HIGHLIGHT) if temp_val == p_val else curses.color_pair(self.BUTTON_NORMAL))
                    self.stdscr.addstr(preset_y, px_cur, p_txt, p_col)
                    preset_boxes.append((p_idx, px_cur, px_cur + str_display_width(p_txt), preset_y, p_val))
                    px_cur += str_display_width(p_txt) + 2

                # Bottom actions: [ ✔ 保存并应用 ]   [ ✖ 取消返回 ]
                act_y = dialog_y + dialog_h - 2
                btn_save_txt = " [ ▶ ✔ 保存并应用 ◀ ] " if focused_idx == 6 else " [   ✔ 保存并应用   ] "
                btn_cancel_txt = " [ ▶ ✖ 取消返回 ◀ ] " if focused_idx == 7 else " [   ✖ 取消返回   ] "
                c_save = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_idx == 6 else curses.color_pair(self.BUTTON_NORMAL)
                c_cancel = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_idx == 7 else curses.color_pair(self.BUTTON_NORMAL)

                tot_act_w = str_display_width(btn_save_txt) + 4 + str_display_width(btn_cancel_txt)
                save_x = dialog_x + max(2, (dialog_w - tot_act_w) // 2)
                cancel_x = save_x + str_display_width(btn_save_txt) + 4

                self.stdscr.addstr(act_y, save_x, btn_save_txt, c_save)
                self.stdscr.addstr(act_y, cancel_x, btn_cancel_txt, c_cancel)

                self.stdscr.noutrefresh()
                curses.doupdate()

                key = self.stdscr.getch()
                if key in [curses.KEY_RIGHT, 9]:  # Tab or Right
                    focused_idx = (focused_idx + 1) % 8
                elif key in [curses.KEY_LEFT, curses.KEY_BTAB]:  # Shift-Tab or Left
                    focused_idx = (focused_idx - 1) % 8
                elif key in [curses.KEY_DOWN]:
                    if focused_idx in [0, 1]:
                        focused_idx = 3
                    elif focused_idx in [2, 3, 4, 5]:
                        focused_idx = 6
                elif key in [curses.KEY_UP]:
                    if focused_idx in [6, 7]:
                        focused_idx = 3
                    elif focused_idx in [2, 3, 4, 5]:
                        focused_idx = 0
                elif key in [ord('+'), ord('=')]:
                    temp_val = min(3600, temp_val + 1)
                elif key in [ord('-'), ord('_')]:
                    temp_val = max(0, temp_val - 1)
                elif key in [10, 13, curses.KEY_ENTER, ord(' ')]:
                    if focused_idx == 0:
                        temp_val = max(0, temp_val - 1)
                    elif focused_idx == 1:
                        temp_val = min(3600, temp_val + 1)
                    elif focused_idx == 2:
                        temp_val = 0
                    elif focused_idx == 3:
                        temp_val = 3
                    elif focused_idx == 4:
                        temp_val = 5
                    elif focused_idx == 5:
                        temp_val = 10
                    elif focused_idx == 6:
                        if initial_val is not None:
                            return temp_val
                        output, rc = BACKEND.set_timeout(temp_val)
                        if rc == 0:
                            self.set_message(f"引导倒计时超时已成功设置为 {temp_val} 秒。", "success")
                        else:
                            self.set_message(f"设置失败: {output.strip()}", "error")
                        self.refresh(keep_message=True)
                        return temp_val
                    elif focused_idx == 7:
                        return None
                elif key in [27, ord('q'), ord('Q')]:
                    return None
                elif key == curses.KEY_MOUSE:
                    try:
                        _, mx, my, _, bstate = curses.getmouse()
                        if bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED):
                            if my == stepper_y:
                                if m_x <= mx < m_x + str_display_width(btn_minus_txt):
                                    temp_val = max(0, temp_val - 1)
                                    focused_idx = 0
                                elif p_x <= mx < p_x + str_display_width(btn_plus_txt):
                                    temp_val = min(3600, temp_val + 1)
                                    focused_idx = 1
                            elif my == preset_y:
                                for p_idx, p_x1, p_x2, _, p_val in preset_boxes:
                                    if p_x1 <= mx < p_x2:
                                        temp_val = p_val
                                        focused_idx = p_idx
                                        break
                            elif my == act_y:
                                if save_x <= mx < save_x + str_display_width(btn_save_txt):
                                    if initial_val is not None:
                                        return temp_val
                                    output, rc = BACKEND.set_timeout(temp_val)
                                    if rc == 0:
                                        self.set_message(f"引导倒计时超时已成功设置为 {temp_val} 秒。", "success")
                                    else:
                                        self.set_message(f"设置失败: {output.strip()}", "error")
                                    self.refresh(keep_message=True)
                                    return temp_val
                                elif cancel_x <= mx < cancel_x + str_display_width(btn_cancel_txt):
                                    return None
                    except Exception:
                        pass
            except curses.error:
                break

    def _confirm(self, question: str) -> bool:
        """Show an interactive 2-button confirmation dialog."""
        dialog_w = min(max(str_display_width(question) + 12, 48), self.max_x - 4)
        dialog_h = 7
        dialog_y = (self.max_y - dialog_h) // 2
        dialog_x = (self.max_x - dialog_w) // 2

        focused_btn = 0  # 0: 确认, 1: 取消

        while True:
            try:
                self.stdscr.erase()
                # Draw dialog box
                for y in range(dialog_h):
                    if y == 0:
                        self.stdscr.addstr(dialog_y + y, dialog_x,
                                           "┌" + "─" * (dialog_w - 2) + "┐",
                                           curses.color_pair(self.HEADER))
                    elif y == dialog_h - 1:
                        self.stdscr.addstr(dialog_y + y, dialog_x,
                                           "└" + "─" * (dialog_w - 2) + "┘",
                                           curses.color_pair(self.HEADER))
                    else:
                        self.stdscr.addstr(dialog_y + y, dialog_x,
                                           "│" + " " * (dialog_w - 2) + "│",
                                           curses.color_pair(self.HEADER))

                # Question text (centered)
                q_disp = pad_to_width(question, dialog_w - 6)
                q_x = dialog_x + max(2, (dialog_w - str_display_width(q_disp)) // 2)
                self.stdscr.addstr(dialog_y + 2, q_x, q_disp, curses.color_pair(self.TITLE) | curses.A_BOLD)

                # Two interactive buttons
                btn_yes_str = " [ ▶ ✔ 确认执行 ◀ ] " if focused_btn == 0 else " [   ✔ 确认执行   ] "
                btn_no_str = " [ ▶ ✖ 取消返回 ◀ ] " if focused_btn == 1 else " [   ✖ 取消返回   ] "
                btn_yes_col = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 0 else curses.color_pair(self.BUTTON_NORMAL)
                btn_no_col = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 1 else curses.color_pair(self.BUTTON_NORMAL)

                b_spacing = 4
                total_b_w = str_display_width(btn_yes_str) + b_spacing + str_display_width(btn_no_str)
                btn_start_x = dialog_x + max(2, (dialog_w - total_b_w) // 2)
                btn_yes_x = btn_start_x
                btn_no_x = btn_yes_x + str_display_width(btn_yes_str) + b_spacing
                btn_y = dialog_y + 4

                self.stdscr.addstr(btn_y, btn_yes_x, btn_yes_str, btn_yes_col)
                self.stdscr.addstr(btn_y, btn_no_x, btn_no_str, btn_no_col)

                self.stdscr.noutrefresh()
                curses.doupdate()

                key = self.stdscr.getch()
                if key in [curses.KEY_LEFT, curses.KEY_RIGHT, 9]:  # Tab
                    focused_btn = 1 - focused_btn
                elif key in [10, 13, curses.KEY_ENTER, ord(' ')]:
                    return focused_btn == 0
                elif key in [ord('y'), ord('Y')]:
                    return True
                elif key in [ord('n'), ord('N'), 27, ord('q'), ord('Q')]:
                    return False
                elif key == curses.KEY_MOUSE:
                    try:
                        _, mx, my, _, bstate = curses.getmouse()
                        if bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED):
                            if my == btn_y:
                                if btn_yes_x <= mx < btn_yes_x + str_display_width(btn_yes_str):
                                    return True
                                elif btn_no_x <= mx < btn_no_x + str_display_width(btn_no_str):
                                    return False
                    except Exception:
                        pass
            except curses.error:
                return False

    def _show_info_popup(self, lines: List[str], title: str):
        """Show an information popup with scroll support."""
        content_width = max(str_display_width(l) for l in lines) if lines else 40
        popup_w = min(content_width + 8, self.max_x - 4)
        popup_h = min(len(lines) + 4, self.max_y - 4)

        popup_y = (self.max_y - popup_h) // 2
        popup_x = (self.max_x - popup_w) // 2

        scroll_offset = 0
        max_scroll = max(0, len(lines) - (popup_h - 3))

        while True:
            try:
                self.stdscr.erase()
                # Draw border
                for y in range(popup_h):
                    if y == 0:
                        self.stdscr.addstr(popup_y + y, popup_x,
                                           "┌" + "─" * (popup_w - 2) + "┐",
                                           curses.color_pair(self.HEADER))
                    elif y == popup_h - 1:
                        self.stdscr.addstr(popup_y + y, popup_x,
                                           "└" + "─" * (popup_w - 2) + "┘",
                                           curses.color_pair(self.HEADER))
                    else:
                        self.stdscr.addstr(popup_y + y, popup_x,
                                           "│" + " " * (popup_w - 2) + "│",
                                           curses.color_pair(self.HEADER))

                # Draw title
                if title:
                    title_str = f" {title} "
                    title_x = popup_x + max(2, (popup_w - str_display_width(title_str)) // 2)
                    self.stdscr.addstr(popup_y, title_x,
                                       title_str, curses.color_pair(self.TITLE) | curses.A_BOLD)

                # Draw content with scrolling
                visible_lines = lines[scroll_offset:scroll_offset + popup_h - 3]
                for i, line in enumerate(visible_lines):
                    display_line = pad_to_width(line, popup_w - 4)
                    color = curses.color_pair(self.ACTIVE) if i == 0 and scroll_offset == 0 else curses.A_NORMAL
                    self.stdscr.addstr(popup_y + 1 + i, popup_x + 2, display_line, color)

                # Bottom border with return button
                close_btn_txt = " [ 🔙 返回主列表 (Esc / 点击关闭) ] "
                cb_w = str_display_width(close_btn_txt)
                if cb_w < popup_w - 4:
                    cb_x = popup_x + (popup_w - cb_w) // 2
                    left_d = max(0, cb_x - popup_x - 1)
                    right_d = max(0, popup_w - 2 - left_d - cb_w)
                    self.stdscr.addstr(popup_y + popup_h - 1, popup_x,
                                       "└" + "─" * left_d,
                                       curses.color_pair(self.HEADER))
                    self.stdscr.addstr(popup_y + popup_h - 1, popup_x + 1 + left_d,
                                       close_btn_txt,
                                       curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD)
                    self.stdscr.addstr(popup_y + popup_h - 1, popup_x + 1 + left_d + cb_w,
                                       "─" * right_d + "┘",
                                       curses.color_pair(self.HEADER))
                else:
                    self.stdscr.addstr(popup_y + popup_h - 1, popup_x,
                                       "└" + "─" * (popup_w - 2) + "┘",
                                       curses.color_pair(self.HEADER))

                # Scroll indicator
                if max_scroll > 0 and popup_w > cb_w + 30:
                    scroll_info = f" [第 {scroll_offset + 1}-{scroll_offset + len(visible_lines)} 行 / 共 {len(lines)} 行] "
                    try:
                        self.stdscr.addstr(popup_y + popup_h - 1, popup_x + 2,
                                           scroll_info, curses.color_pair(self.HELP))
                    except curses.error:
                        pass

                self.stdscr.noutrefresh()
                curses.doupdate()

                key = self.stdscr.getch()
                if key in [ord('q'), ord('Q'), 27, ord('\n'), ord(' ')]:
                    break
                elif key in [curses.KEY_DOWN, ord('j')] and scroll_offset < max_scroll:
                    scroll_offset += 1
                elif key in [curses.KEY_UP, ord('k')] and scroll_offset > 0:
                    scroll_offset -= 1
                elif key == curses.KEY_NPAGE:  # Page Down
                    scroll_offset = min(scroll_offset + popup_h - 3, max_scroll)
                elif key == curses.KEY_PPAGE:  # Page Up
                    scroll_offset = max(scroll_offset - (popup_h - 3), 0)
                elif key == curses.KEY_HOME:
                    scroll_offset = 0
                elif key == curses.KEY_END:
                    scroll_offset = max_scroll
                elif key == curses.KEY_MOUSE:
                    try:
                        _, mx, my, _, bstate = curses.getmouse()
                        if bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED):
                            # Clicked bottom line or outside popup dismisses it
                            if my >= popup_y + popup_h - 2 or mx < popup_x or mx >= popup_x + popup_w or my < popup_y:
                                break
                        # Wheel scrolling
                        if bstate & getattr(curses, 'BUTTON4_PRESSED', 0x10000):
                            if scroll_offset > 0:
                                scroll_offset = max(0, scroll_offset - 2)
                        elif bstate & getattr(curses, 'BUTTON5_PRESSED', 0x200000):
                            if scroll_offset < max_scroll:
                                scroll_offset = min(max_scroll, scroll_offset + 2)
                    except Exception:
                        pass

            except curses.error:
                break

    def _show_action_popup(self, lines: List[str], title: str) -> int:
        """Show an action dialog where options are interactive buttons selectable via mouse or arrow keys."""
        content_width = max(str_display_width(l) for l in lines) if lines else 40
        popup_w = min(max(content_width + 8, 56), self.max_x - 4)
        popup_h = min(len(lines) + 4, self.max_y - 4)

        popup_y = (self.max_y - popup_h) // 2
        popup_x = (self.max_x - popup_w) // 2

        # Detect actionable lines: pattern like "[1] ...", "[q / Esc] ..."
        action_pattern = re.compile(r'^\s*\[([a-zA-Z0-9\?]+(?:/[a-zA-Z0-9]+)*)\]\s*(.*)$')
        actions = []  # (line_index, primary_key, badge_str, label_str)
        for idx, line in enumerate(lines):
            m = action_pattern.match(line)
            if m:
                badge = m.group(1).strip()
                label = m.group(2).strip()
                b_lower = badge.lower()
                if "q" in b_lower or "esc" in b_lower:
                    pk = 'q'
                elif "/" in badge:
                    pk = badge.split("/")[0].strip()
                else:
                    pk = badge
                actions.append((idx, pk, badge, label))

        action_line_indices = [a[0] for a in actions]
        focused_act_idx = 0 if actions else -1

        scroll_offset = 0
        max_scroll = max(0, len(lines) - (popup_h - 3))

        while True:
            try:
                self.stdscr.erase()
                for y in range(popup_h):
                    if y == 0:
                        self.stdscr.addstr(popup_y + y, popup_x,
                                           "┌" + "─" * (popup_w - 2) + "┐",
                                           curses.color_pair(self.HEADER))
                    elif y == popup_h - 1:
                        self.stdscr.addstr(popup_y + y, popup_x,
                                           "└" + "─" * (popup_w - 2) + "┘",
                                           curses.color_pair(self.HEADER))
                    else:
                        self.stdscr.addstr(popup_y + y, popup_x,
                                           "│" + " " * (popup_w - 2) + "│",
                                           curses.color_pair(self.HEADER))

                if title:
                    title_str = f" {title} "
                    title_x = popup_x + max(2, (popup_w - str_display_width(title_str)) // 2)
                    self.stdscr.addstr(popup_y, title_x,
                                       title_str, curses.color_pair(self.TITLE) | curses.A_BOLD)

                visible_lines = lines[scroll_offset:scroll_offset + popup_h - 3]
                action_hitboxes = []

                for i, line in enumerate(visible_lines):
                    abs_line_idx = scroll_offset + i
                    screen_y = popup_y + 1 + i

                    if abs_line_idx in action_line_indices:
                        act_pos = action_line_indices.index(abs_line_idx)
                        _, pk, badge, label = actions[act_pos]
                        is_focused = (act_pos == focused_act_idx)

                        if is_focused:
                            btn_text = f" ▶ [{badge}] {label} ◀ "
                            color = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD
                        else:
                            btn_text = f"   [{badge}] {label}   "
                            color = curses.color_pair(self.BUTTON_NORMAL)

                        padded_btn = pad_to_width(btn_text, popup_w - 4)
                        self.stdscr.addstr(screen_y, popup_x + 2, padded_btn, color)
                        action_hitboxes.append((screen_y, popup_x + 2, popup_x + popup_w - 2, pk))
                    else:
                        display_line = pad_to_width(line, popup_w - 4)
                        color = curses.color_pair(self.ACTIVE) if i == 0 and scroll_offset == 0 else curses.A_NORMAL
                        self.stdscr.addstr(screen_y, popup_x + 2, display_line, color)

                if max_scroll > 0:
                    scroll_info = f" [第 {scroll_offset + 1}-{scroll_offset + len(visible_lines)} 行 / 共 {len(lines)} 行] "
                    try:
                        self.stdscr.addstr(popup_y + popup_h - 1, popup_x + popup_w - str_display_width(scroll_info) - 2,
                                           scroll_info, curses.color_pair(self.HELP))
                    except curses.error:
                        pass

                self.stdscr.noutrefresh()
                curses.doupdate()

                key = self.stdscr.getch()

                # Hotkeys match directly
                if key in [ord('q'), ord('Q'), 27]:
                    return ord('q')
                for _, pk, _, _ in actions:
                    if len(pk) == 1 and (key == ord(pk.lower()) or key == ord(pk.upper())):
                        return ord(pk[0])

                if key in [curses.KEY_DOWN, ord('j')]:
                    if actions:
                        if focused_act_idx < len(actions) - 1:
                            focused_act_idx += 1
                            target_l = actions[focused_act_idx][0]
                            if target_l >= scroll_offset + popup_h - 3:
                                scroll_offset = min(max_scroll, target_l - (popup_h - 4))
                    elif scroll_offset < max_scroll:
                        scroll_offset += 1

                elif key in [curses.KEY_UP, ord('k')]:
                    if actions:
                        if focused_act_idx > 0:
                            focused_act_idx -= 1
                            target_l = actions[focused_act_idx][0]
                            if target_l < scroll_offset:
                                scroll_offset = max(0, target_l)
                    elif scroll_offset > 0:
                        scroll_offset -= 1

                elif key in [10, 13, curses.KEY_ENTER, ord(' ')]:
                    if actions and 0 <= focused_act_idx < len(actions):
                        pk = actions[focused_act_idx][1]
                        return ord('q') if pk == 'q' else ord(pk[0])

                elif key == curses.KEY_NPAGE:
                    scroll_offset = min(scroll_offset + popup_h - 3, max_scroll)
                elif key == curses.KEY_PPAGE:
                    scroll_offset = max(scroll_offset - (popup_h - 3), 0)

                elif key == curses.KEY_MOUSE:
                    try:
                        _, mx, my, _, bstate = curses.getmouse()
                        if bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED):
                            for sy, x1, x2, pk in action_hitboxes:
                                if my == sy and x1 <= mx <= x2:
                                    return ord('q') if pk == 'q' else ord(pk[0])
                    except Exception:
                        pass

            except curses.error:
                return ord('q')

    def _show_diff_confirm_dialog(self, diff_lines: List[str], title: str = "Diff 变更审核") -> bool:
        """Show unified diff with syntax highlighting and ask for user consent with interactive buttons."""
        content_width = max(str_display_width(l) for l in diff_lines) if diff_lines else 60
        popup_w = min(max(content_width + 8, 72), self.max_x - 4)
        popup_h = min(len(diff_lines) + 7, self.max_y - 4)

        popup_y = (self.max_y - popup_h) // 2
        popup_x = (self.max_x - popup_w) // 2

        scroll_offset = 0
        content_h = popup_h - 5
        max_scroll = max(0, len(diff_lines) - content_h)

        focused_btn = 0  # 0: 授权写入系统, 1: 放弃并返回

        while True:
            try:
                self.stdscr.erase()
                for y in range(popup_h):
                    if y == 0:
                        self.stdscr.addstr(popup_y + y, popup_x,
                                           "┌" + "─" * (popup_w - 2) + "┐",
                                           curses.color_pair(self.HEADER))
                    elif y == popup_h - 1:
                        self.stdscr.addstr(popup_y + y, popup_x,
                                           "└" + "─" * (popup_w - 2) + "┘",
                                           curses.color_pair(self.HEADER))
                    elif y == popup_h - 3:
                        self.stdscr.addstr(popup_y + y, popup_x,
                                           "├" + "─" * (popup_w - 2) + "┤",
                                           curses.color_pair(self.HEADER))
                    else:
                        self.stdscr.addstr(popup_y + y, popup_x,
                                           "│" + " " * (popup_w - 2) + "│",
                                           curses.color_pair(self.HEADER))

                if title:
                    title_str = f" [Diff 审核] {title} "
                    title_x = popup_x + max(2, (popup_w - str_display_width(title_str)) // 2)
                    self.stdscr.addstr(popup_y, title_x,
                                       title_str, curses.color_pair(self.TITLE) | curses.A_BOLD)

                visible = diff_lines[scroll_offset:scroll_offset + content_h]
                for i, line in enumerate(visible):
                    y_pos = popup_y + 1 + i
                    display_line = pad_to_width(line, popup_w - 4)

                    if line.startswith("+++") or line.startswith("---"):
                        color = curses.color_pair(self.TITLE) | curses.A_BOLD
                    elif line.startswith("+"):
                        color = curses.color_pair(self.ACTIVE) | curses.A_BOLD
                    elif line.startswith("-"):
                        color = curses.color_pair(self.INACTIVE) | curses.A_BOLD
                    elif line.startswith("@@"):
                        color = curses.color_pair(self.CURRENT) | curses.A_BOLD
                    elif line.startswith("###") or line.startswith("==="):
                        color = curses.color_pair(self.HIGHLIGHT) | curses.A_BOLD
                    else:
                        color = curses.A_NORMAL

                    self.stdscr.addstr(y_pos, popup_x + 2, display_line[:popup_w - 4], color)

                if max_scroll > 0:
                    scroll_info = f" [↑↓/PgDn 滚动 {scroll_offset + 1}/{max_scroll + 1}] "
                    self.stdscr.addstr(popup_y + popup_h - 3, popup_x + popup_w - str_display_width(scroll_info) - 2,
                                       scroll_info, curses.color_pair(self.HELP))

                # Interactive buttons at bottom
                btn_y = popup_y + popup_h - 2
                btn_agree = " [ ▶ ✔ 授权并写入系统 ◀ ] " if focused_btn == 0 else " [   ✔ 授权并写入系统   ] "
                btn_cancel = " [ ▶ ✖ 放弃并返回 ◀ ] " if focused_btn == 1 else " [   ✖ 放弃并返回   ] "
                col_agree = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 0 else curses.color_pair(self.BUTTON_NORMAL)
                col_cancel = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 1 else curses.color_pair(self.BUTTON_NORMAL)

                tot_btn_w = str_display_width(btn_agree) + 4 + str_display_width(btn_cancel)
                agree_x = popup_x + max(2, (popup_w - tot_btn_w) // 2)
                cancel_x = agree_x + str_display_width(btn_agree) + 4

                self.stdscr.addstr(btn_y, agree_x, btn_agree, col_agree)
                self.stdscr.addstr(btn_y, cancel_x, btn_cancel, col_cancel)

                self.stdscr.noutrefresh()
                curses.doupdate()

                key = self.stdscr.getch()
                if key in [curses.KEY_LEFT, curses.KEY_RIGHT, 9]:  # Tab
                    focused_btn = 1 - focused_btn
                elif key in [10, 13, curses.KEY_ENTER, ord(' ')]:
                    return focused_btn == 0
                elif key in [ord('y'), ord('Y')]:
                    return True
                elif key in [ord('n'), ord('N'), ord('q'), ord('Q'), 27]:
                    return False
                elif key in [curses.KEY_DOWN, ord('j')] and scroll_offset < max_scroll:
                    scroll_offset += 1
                elif key in [curses.KEY_UP, ord('k')] and scroll_offset > 0:
                    scroll_offset -= 1
                elif key == curses.KEY_NPAGE:
                    scroll_offset = min(scroll_offset + content_h, max_scroll)
                elif key == curses.KEY_PPAGE:
                    scroll_offset = max(0, scroll_offset - content_h)
                elif key == curses.KEY_MOUSE:
                    try:
                        _, mx, my, _, bstate = curses.getmouse()
                        if bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED):
                            if my == btn_y:
                                if agree_x <= mx < agree_x + str_display_width(btn_agree):
                                    return True
                                elif cancel_x <= mx < cancel_x + str_display_width(btn_cancel):
                                    return False
                    except Exception:
                        pass

            except curses.error:
                return False

    def show_bootloader_dialog(self):
        """Show and manage Linux bootloaders (GRUB & systemd-boot) with a dedicated visual workstation."""
        from src.bootloader import GrubConfigManager, SystemdBootManager, SystemdBootEntry
        grub_mgr = GrubConfigManager()
        sdboot_mgr = SystemdBootManager()

        has_grub = grub_mgr.exists()
        has_sdboot = sdboot_mgr.exists()
        if not has_grub and not has_sdboot:
            self._show_info_popup([
                "未在当前系统中检测到标准的 GRUB 2 或 systemd-boot 引导加载器。",
                "提示：如为多系统且引导位于 ESP 分区，可按 [e] 查看 ESP 分区。"
            ], "Linux 引导配置")
            return

        selected_b_idx = 0
        focused_btn = 0
        b_msg = ""
        b_msg_type = "info"

        while True:
            self.max_y, self.max_x = self.stdscr.getmaxyx()
            self.stdscr.erase()

            has_grub = grub_mgr.exists()
            has_sdboot = sdboot_mgr.exists()

            if has_sdboot:
                entries = sdboot_mgr.list_entries()
                loader_name = "systemd-boot"
            elif has_grub:
                entries = grub_mgr.list_entries()
                loader_name = "GRUB 2"
            else:
                break

            if not entries:
                entries = [SystemdBootEntry(filename="default", title="默认系统引导", is_default=True)]

            selected_b_idx = max(0, min(selected_b_idx, len(entries) - 1))
            target = entries[selected_b_idx]

            w = self.max_x - 2

            try:
                # 1. Header (Lines 0..2)
                title = " Linux 引导加载器配置工作台 "
                mode = f" [{loader_name}] "
                self.stdscr.addstr(0, 0, "┌" + "─" * (self.max_x - 2) + "┐", curses.color_pair(self.HEADER))
                self.stdscr.addstr(1, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(1, 2, title, curses.color_pair(self.TITLE) | curses.A_BOLD)
                mode_w = str_display_width(mode)
                self.stdscr.addstr(1, max(str_display_width(title) + 4, self.max_x - 1 - mode_w), mode, curses.color_pair(self.CURRENT))
                self.stdscr.addstr(1, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(2, 0, "├" + "─" * (self.max_x - 2) + "┤", curses.color_pair(self.HEADER))

                # 2. Status Bar (Line 3)
                esp_dir = sdboot_mgr.esp_root or "/boot" if has_sdboot else "/etc/default/grub"
                timeout_val = "0"
                def_val = "(未指定)"
                if has_sdboot:
                    sd_conf = sdboot_mgr.read_loader_conf()
                    timeout_val = sd_conf.get("timeout", "0")
                    def_val = sd_conf.get("default", "(未指定)")
                elif has_grub:
                    g_conf = grub_mgr.read_config()
                    timeout_val = g_conf.get("GRUB_TIMEOUT", "5")
                    def_val = g_conf.get("GRUB_DEFAULT", "0")

                status_bar_txt = f" 引导引擎: {loader_name} │ ESP路径: {esp_dir} │ 默认引导: {def_val} │ 超时倒计时: {timeout_val}秒 │ 条目数: {len(entries)} 项 "
                self.stdscr.addstr(3, 0, pad_to_width(status_bar_txt, self.max_x), curses.color_pair(self.STATUS_BAR))

                # 3. Entry List Header (Line 5)
                self.stdscr.addstr(4, 0, "│" + " " * w + "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(5, 0, "│", curses.color_pair(self.HEADER))
                sec_title = " 【引导加载器条目列表】(按 ↑/↓ 切换条目，或直接鼠标点击选择):"
                self.stdscr.addstr(5, 2, pad_to_width(sec_title, w - 2), curses.color_pair(self.HEADER) | curses.A_BOLD)
                self.stdscr.addstr(5, self.max_x - 1, "│", curses.color_pair(self.HEADER))

                # 4. Entry Cards (Lines 6..)
                ent_hitboxes = []
                cur_line = 6
                max_show_count = min(len(entries), max(1, self.max_y - 20))
                for i in range(max_show_count):
                    ent = entries[i]
                    is_sel = (i == selected_b_idx)
                    radio = "(*)" if is_sel else "( )"
                    badges = []
                    if ent.is_default:
                        badges.append("[默认启动]")
                    if ent.is_selected:
                        badges.append("[当前系统]")
                    badges.append(f"[{ent.type_label}]")

                    badges_str = " ".join(badges)
                    title_display = ent.title or ent.filename
                    row_text = f" {radio} [{i+1}] {title_display}  ({ent.filename})  {badges_str}"
                    padded_row = pad_to_width(row_text, w - 2)

                    self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                    if is_sel:
                        self.stdscr.addstr(cur_line, 2, padded_row, curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD)
                    else:
                        self.stdscr.addstr(cur_line, 2, padded_row, curses.color_pair(self.BUTTON_NORMAL))
                    self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))

                    ent_hitboxes.append((cur_line, 2, self.max_x - 2, i))
                    cur_line += 1

                # 5. Divider
                self.stdscr.addstr(cur_line, 0, "├" + "─" * (self.max_x - 2) + "┤", curses.color_pair(self.HEADER))
                cur_line += 1

                # 6. Selected Entry Live Inspection Card
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                card_title = f" 【选中条目深度解析与实时体检】[{target.title or target.filename}]"
                self.stdscr.addstr(cur_line, 2, pad_to_width(card_title, w - 2), curses.color_pair(self.TITLE) | curses.A_BOLD)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                def_state = "是 (Default)" if target.is_default else "否"
                info_l1 = f" 属性概况: 条目标识: {target.filename} │ 引导格式: {target.type_label} │ 默认项: {def_state}"
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(info_l1, w - 2), curses.A_NORMAL)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                core_img = target.linux or target.efi or "(固件内建启动)"
                initrd_str = target.initrd or "(无)"
                info_l2 = f" 核心镜像: 引导文件: {core_img} │ 内存盘: {initrd_str}"
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(info_l2, w - 2), curses.A_NORMAL)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                cmd_str = target.options or "(使用 UEFI 默认参数 / 固件预设)"
                info_l3 = f" 启动参数: {cmd_str}"
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(info_l3, w - 2), curses.A_NORMAL)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                if target.is_default:
                    diag_str = " 状态诊断: ✔ 该条目已设为引导器默认项，开机倒计时结束将全自动启动进入此条目。"
                    diag_color = curses.color_pair(self.ACTIVE) | curses.A_BOLD
                else:
                    diag_str = " 状态诊断: ✔ 条目配置健康就绪，可随时点击下方【设为默认引导】将其置为开机首选项。"
                    diag_color = curses.color_pair(self.CURRENT) | curses.A_BOLD

                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(diag_str, w - 2), diag_color)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                # 7. Divider before Buttons
                self.stdscr.addstr(cur_line, 0, "├" + "─" * (self.max_x - 2) + "┤", curses.color_pair(self.HEADER))
                cur_line += 1

                # 8. Interactive Action Buttons
                btn_sec_title = " 【可视化快捷操作按钮】(鼠标直接点击，或按 Tab / ← / → 切换焦点后回车执行):"
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(btn_sec_title, w - 2), curses.color_pair(self.HEADER) | curses.A_BOLD)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                # Button Row 1: 0 (设为默认), 1 (修改倒计时), 2 (注入Win11)
                b0_txt = " [ ▶ ★ 设为默认引导 ◀ ] " if focused_btn == 0 else " [   ★ 设为默认引导   ] "
                b1_txt = " [ ▶ ⏱ 调整菜单倒计时 ◀ ] " if focused_btn == 1 else " [   ⏱ 调整菜单倒计时   ] "
                b2_txt = " [ ▶ 🛠 一键注入 Win11 ◀ ] " if focused_btn == 2 else " [   🛠 一键注入 Win11   ] "

                c0 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 0 else curses.color_pair(self.BUTTON_NORMAL)
                c1 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 1 else curses.color_pair(self.BUTTON_NORMAL)
                c2 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 2 else curses.color_pair(self.BUTTON_NORMAL)

                tot_r1_w = str_display_width(b0_txt) + 3 + str_display_width(b1_txt) + 3 + str_display_width(b2_txt)
                r1_start_x = max(2, (self.max_x - tot_r1_w) // 2)
                b0_x = r1_start_x
                b1_x = b0_x + str_display_width(b0_txt) + 3
                b2_x = b1_x + str_display_width(b1_txt) + 3
                r1_y = cur_line

                self.stdscr.addstr(r1_y, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(r1_y, 1, " " * w)
                self.stdscr.addstr(r1_y, b0_x, b0_txt, c0)
                self.stdscr.addstr(r1_y, b1_x, b1_txt, c1)
                self.stdscr.addstr(r1_y, b2_x, b2_txt, c2)
                self.stdscr.addstr(r1_y, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                # Button Row 2: 3 (切换内核), 4 (编辑参数), 5 (重载生效)
                b3_txt = " [ ▶ 🔄 切换启动内核 ◀ ] " if focused_btn == 3 else " [   🔄 切换启动内核   ] "
                b4_txt = " [ ▶ ✏️ 编辑内核启动参数 ◀ ] " if focused_btn == 4 else " [   ✏️ 编辑内核启动参数   ] "
                b5_txt = " [ ▶ ⚡ 立即重载生效 ◀ ] " if focused_btn == 5 else " [   ⚡ 立即重载生效   ] "

                c3 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 3 else curses.color_pair(self.BUTTON_NORMAL)
                c4 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 4 else curses.color_pair(self.BUTTON_NORMAL)
                c5 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 5 else curses.color_pair(self.BUTTON_NORMAL)

                tot_r2_w = str_display_width(b3_txt) + 3 + str_display_width(b4_txt) + 3 + str_display_width(b5_txt)
                r2_start_x = max(2, (self.max_x - tot_r2_w) // 2)
                b3_x = r2_start_x
                b4_x = b3_x + str_display_width(b3_txt) + 3
                b5_x = b4_x + str_display_width(b4_txt) + 3
                r2_y = cur_line

                self.stdscr.addstr(r2_y, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(r2_y, 1, " " * w)
                self.stdscr.addstr(r2_y, b3_x, b3_txt, c3)
                self.stdscr.addstr(r2_y, b4_x, b4_txt, c4)
                self.stdscr.addstr(r2_y, b5_x, b5_txt, c5)
                self.stdscr.addstr(r2_y, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                # Button Row 3: 6 (返回主列表)
                b6_txt = " [ ▶ 🔙 返回主列表 ◀ ] " if focused_btn == 6 else " [   🔙 返回主列表   ] "
                c6 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 6 else curses.color_pair(self.BUTTON_NORMAL)
                b6_x = max(2, (self.max_x - str_display_width(b6_txt)) // 2)
                r3_y = cur_line

                self.stdscr.addstr(r3_y, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(r3_y, 1, " " * w)
                self.stdscr.addstr(r3_y, b6_x, b6_txt, c6)
                self.stdscr.addstr(r3_y, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                btn_hitboxes = [
                    (0, r1_y, b0_x, b0_x + str_display_width(b0_txt)),
                    (1, r1_y, b1_x, b1_x + str_display_width(b1_txt)),
                    (2, r1_y, b2_x, b2_x + str_display_width(b2_txt)),
                    (3, r2_y, b3_x, b3_x + str_display_width(b3_txt)),
                    (4, r2_y, b4_x, b4_x + str_display_width(b4_txt)),
                    (5, r2_y, b5_x, b5_x + str_display_width(b5_txt)),
                    (6, r3_y, b6_x, b6_x + str_display_width(b6_txt)),
                ]

                # Fill remaining rows up to bottom
                while cur_line < self.max_y - 1:
                    self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                    self.stdscr.addstr(cur_line, 1, " " * w)
                    self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                    cur_line += 1

                # 9. Bottom Border with integrated Status Message Badge
                bottom_y = self.max_y - 1
                if not b_msg:
                    self.stdscr.addstr(bottom_y, 0, "└" + "─" * (self.max_x - 2) + "┘", curses.color_pair(self.HEADER))
                else:
                    b_color = curses.color_pair(self.ACTIVE) | curses.A_BOLD if b_msg_type == "success" else (curses.color_pair(self.INACTIVE) | curses.A_BOLD if b_msg_type == "error" else curses.color_pair(self.CURRENT) | curses.A_BOLD)
                    b_icon = "✔" if b_msg_type == "success" else ("✖" if b_msg_type == "error" else "ℹ")
                    badge_str = f" [ {b_icon} {b_msg} ] "
                    b_w = str_display_width(badge_str)
                    left_d = 2
                    right_d = max(0, self.max_x - 2 - left_d - b_w)
                    self.stdscr.addstr(bottom_y, 0, "└" + "─" * left_d, curses.color_pair(self.HEADER))
                    self.stdscr.addstr(bottom_y, 1 + left_d, badge_str, b_color)
                    self.stdscr.addstr(bottom_y, 1 + left_d + b_w, "─" * right_d + "┘", curses.color_pair(self.HEADER))

                self.stdscr.noutrefresh()
                curses.doupdate()

                key = self.stdscr.getch()

                def execute_action(action_idx: int):
                    nonlocal target, b_msg, b_msg_type
                    if action_idx == 0:
                        # 设为默认引导
                        if has_sdboot:
                            ok, msg = sdboot_mgr.set_default(target.filename)
                            b_msg = f"默认引导项已成功更新为: {target.title or target.filename}"
                            b_msg_type = "success"
                            self.set_message(b_msg, "success")
                        elif has_grub:
                            ok, msg = grub_mgr.update_settings({"GRUB_DEFAULT": target.title or target.filename})
                            grub_mgr.regenerate_grub()
                            b_msg = f"GRUB 默认引导项已设为: {target.title or target.filename}"
                            b_msg_type = "success"
                            self.set_message(b_msg, "success")
                    elif action_idx == 1:
                        # 调整菜单倒计时
                        curr_t = 3
                        if has_sdboot:
                            try:
                                curr_t = int(sdboot_mgr.read_loader_conf().get("timeout", "3"))
                            except Exception:
                                curr_t = 3
                        elif has_grub:
                            try:
                                curr_t = int(grub_mgr.read_config().get("GRUB_TIMEOUT", "5"))
                            except Exception:
                                curr_t = 5

                        new_t = self.set_timeout_dialog(initial_val=curr_t, title="引导菜单倒计时设置 (Timeout)")
                        if new_t is not None:
                            if has_sdboot:
                                sdboot_mgr.update_loader_conf({"timeout": str(new_t)})
                                sdboot_mgr.update_bootctl()
                            if has_grub:
                                grub_mgr.update_settings({"GRUB_TIMEOUT": str(new_t)})
                                grub_mgr.regenerate_grub()
                            b_msg = f"引导菜单倒计时已成功设置为 {new_t} 秒并更新生效！"
                            b_msg_type = "success"
                            self.set_message(b_msg, "success")
                    elif action_idx == 2:
                        # 一键注入 Win11
                        if has_sdboot:
                            plan = sdboot_mgr.generate_windows_entry_diff()
                        elif has_grub:
                            plan = grub_mgr.generate_windows_entry_diff()
                        else:
                            b_msg = "未检测到支持的引导器"
                            b_msg_type = "error"
                            return

                        agreed = self._show_diff_confirm_dialog(
                            plan["diff_lines"],
                            "为 Linux 引导加载器注入 Win11 启动项"
                        )
                        if not agreed:
                            b_msg = "操作已取消，未对系统进行修改。"
                            b_msg_type = "info"
                            return

                        if has_sdboot:
                            ok, msg = sdboot_mgr.apply_windows_entry(plan)
                        elif has_grub:
                            ok, msg = grub_mgr.add_windows_boot_entry()

                        b_msg = msg.splitlines()[0]
                        b_msg_type = "success" if ok else "error"
                        self.set_message(b_msg, b_msg_type)
                    elif action_idx == 3:
                        # 切换启动内核
                        self.show_kernel_dialog()
                    elif action_idx == 4:
                        # 编辑内核启动参数
                        if has_sdboot:
                            if target.filename.endswith(".conf"):
                                res = self._input_dialog(
                                    f"编辑 [{target.filename}] 内核引导参数 (options):",
                                    target.options
                                )
                                if res is not None:
                                    ok, msg = sdboot_mgr.update_entry_options(target.filename, res.strip())
                                    b_msg = msg.splitlines()[0]
                                    b_msg_type = "success" if ok else "error"
                                    self.set_message(b_msg, b_msg_type)
                            else:
                                b_msg = f"条目 '{target.title or target.filename}' 属于 UKI 统一内核或固件条目，启动参数已固化内嵌。"
                                b_msg_type = "info"
                        elif has_grub:
                            curr_cmd = grub_mgr.read_config().get("GRUB_CMDLINE_LINUX_DEFAULT", "")
                            res = self._input_dialog("编辑 GRUB 内核启动参数 (GRUB_CMDLINE_LINUX_DEFAULT):", curr_cmd)
                            if res is not None:
                                ok, msg = grub_mgr.update_settings({"GRUB_CMDLINE_LINUX_DEFAULT": res.strip()})
                                grub_mgr.regenerate_grub()
                                b_msg = "GRUB 内核引导参数已更新并重载生效！"
                                b_msg_type = "success"
                                self.set_message(b_msg, b_msg_type)
                    elif action_idx == 5:
                        # 立即重载生效
                        if has_sdboot:
                            ok, msg = sdboot_mgr.update_bootctl()
                        elif has_grub:
                            ok, msg = grub_mgr.regenerate_grub()
                        b_msg = msg.splitlines()[0]
                        b_msg_type = "success" if ok else "error"
                        self.set_message(b_msg, b_msg_type)
                    elif action_idx == 6:
                        # 返回主列表
                        return "RETURN"

                if key in [ord('q'), ord('Q'), 27]:
                    break
                elif key in [curses.KEY_UP, ord('k')]:
                    if selected_b_idx > 0:
                        selected_b_idx -= 1
                elif key in [curses.KEY_DOWN, ord('j')]:
                    if selected_b_idx < len(entries) - 1:
                        selected_b_idx += 1
                elif key in [curses.KEY_RIGHT, 9]:  # Tab or Right
                    focused_btn = (focused_btn + 1) % 7
                elif key in [curses.KEY_LEFT, curses.KEY_BTAB]:  # Shift-Tab or Left
                    focused_btn = (focused_btn - 1) % 7
                elif key in [10, 13, curses.KEY_ENTER, ord(' ')]:
                    ret = execute_action(focused_btn)
                    if ret == "RETURN":
                        break
                elif key == ord('1'):
                    execute_action(0)
                elif key in [ord('2'), ord('t'), ord('T')]:
                    execute_action(1)
                elif key in [ord('3'), ord('w'), ord('W')]:
                    execute_action(2)
                elif key in [ord('4'), ord('k'), ord('K')]:
                    execute_action(3)
                elif key in [ord('5'), ord('c'), ord('C')]:
                    execute_action(4)
                elif key in [ord('6'), ord('u'), ord('U')]:
                    execute_action(5)
                elif key == curses.KEY_MOUSE:
                    try:
                        _, mx, my, _, bstate = curses.getmouse()
                        if bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED):
                            m_now = time.time()
                            for ry, x1, x2, bi in ent_hitboxes:
                                if my == ry and x1 <= mx <= x2:
                                    last_b_t = getattr(self, "_last_b_click_time", 0.0)
                                    last_b_i = getattr(self, "_last_b_clicked_i", None)
                                    if last_b_i == bi and 0.04 <= (m_now - last_b_t) <= 0.55:
                                        self._last_b_click_time = 0.0
                                        self._last_b_clicked_i = None
                                        selected_b_idx = bi
                                        execute_action(0)  # 双击直接设为默认引导
                                    else:
                                        self._last_b_click_time = m_now
                                        self._last_b_clicked_i = bi
                                        selected_b_idx = bi
                                    break
                            for bi, by, x1, x2 in btn_hitboxes:
                                if my == by and x1 <= mx <= x2:
                                    focused_btn = bi
                                    ret = execute_action(bi)
                                    if ret == "RETURN":
                                        return
                                    break
                    except Exception:
                        pass
            except curses.error:
                break

    def show_kernel_dialog(self):
        """Show and manage Linux kernel switching with a dedicated visual button workstation."""
        from src.kernel import KernelManager, KernelInfo

        kernels = KernelManager.detect_kernels()
        if not kernels:
            self._show_info_popup(["未在当前系统中检测到可用的 Linux 内核。"], "Linux 内核管理")
            return

        selected_k_idx = 0
        for i, k in enumerate(kernels):
            if k.is_current or k.is_default:
                selected_k_idx = i
                break

        focused_btn = 0  # 0: 设为默认, 1: 单次启动, 2: 一键生成, 3: 查看详情, 4: 返回
        k_msg = ""
        k_msg_type = "info"

        while True:
            self.max_y, self.max_x = self.stdscr.getmaxyx()
            self.stdscr.erase()  # Clean canvas, zero ghosting!

            kernels = KernelManager.detect_kernels()
            if not kernels:
                break
            selected_k_idx = min(selected_k_idx, len(kernels) - 1)
            target = kernels[selected_k_idx]

            w = self.max_x - 2

            try:
                # 1. Header (Lines 0..2)
                title = " Linux 多内核可视化切换与引导工作台 "
                mode = f" [{target.entry_type_label}] "
                self.stdscr.addstr(0, 0, "┌" + "─" * (self.max_x - 2) + "┐", curses.color_pair(self.HEADER))
                self.stdscr.addstr(1, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(1, 2, title, curses.color_pair(self.TITLE) | curses.A_BOLD)
                mode_w = str_display_width(mode)
                self.stdscr.addstr(1, max(str_display_width(title) + 4, self.max_x - 1 - mode_w), mode, curses.color_pair(self.CURRENT))
                self.stdscr.addstr(1, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(2, 0, "├" + "─" * (self.max_x - 2) + "┤", curses.color_pair(self.HEADER))

                # 2. Status Bar (Line 3)
                esp_dir = KernelManager.get_esp_root() or "/boot"
                status_bar_txt = f" 当前运行: {os.uname().release} │ 引导格式: {target.entry_type_label} │ ESP目录: {esp_dir} │ 已安装内核: {len(kernels)} 项 "
                self.stdscr.addstr(3, 0, pad_to_width(status_bar_txt, self.max_x), curses.color_pair(self.STATUS_BAR))

                # 3. Kernel List Header (Line 5)
                self.stdscr.addstr(4, 0, "│" + " " * w + "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(5, 0, "│", curses.color_pair(self.HEADER))
                k_sec_title = " 【已安装内核列表】(鼠标点击或使用 ↑/↓ 切换目标内核):"
                self.stdscr.addstr(5, 2, pad_to_width(k_sec_title, w - 2), curses.color_pair(self.HEADER) | curses.A_BOLD)
                self.stdscr.addstr(5, self.max_x - 1, "│", curses.color_pair(self.HEADER))

                # 4. Kernel Cards (Lines 6..)
                k_hitboxes = []
                cur_line = 6
                for i, k in enumerate(kernels):
                    is_sel = (i == selected_k_idx)
                    radio = "(*)" if is_sel else "( )"
                    badges = []
                    if k.is_current:
                        badges.append("[当前运行]")
                    if k.is_default:
                        badges.append("[默认启动]")
                    elif k.is_oneshot:
                        badges.append("[单次启动]")
                    if k.has_boot_entry:
                        badges.append(f"[条目: {k.entry_id}]")
                    else:
                        badges.append("[未配置条目]")

                    badges_str = " ".join(badges)
                    k_row_text = f" {radio} [{i+1}] {k.display_name}  ({k.release})  {badges_str}"
                    padded_row = pad_to_width(k_row_text, w - 2)

                    self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                    if is_sel:
                        self.stdscr.addstr(cur_line, 2, padded_row, curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD)
                    else:
                        self.stdscr.addstr(cur_line, 2, padded_row, curses.color_pair(self.BUTTON_NORMAL))
                    self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))

                    k_hitboxes.append((cur_line, 2, self.max_x - 2, i))
                    cur_line += 1

                # 5. Divider
                self.stdscr.addstr(cur_line, 0, "├" + "─" * (self.max_x - 2) + "┤", curses.color_pair(self.HEADER))
                cur_line += 1

                # 6. Selected Kernel Live Inspection Card
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                card_title = f" 【选中内核深度解析与实时诊断】[{target.display_name}]"
                self.stdscr.addstr(cur_line, 2, pad_to_width(card_title, w - 2), curses.color_pair(self.TITLE) | curses.A_BOLD)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                entry_status_str = f"已就绪 ({target.entry_id})" if target.has_boot_entry else "⚠️ 缺失条目 (可一键生成)"
                info_l1 = f" 属性概况: 版本 {target.release} │ 软件包: {target.package_name} │ 引导条目: {entry_status_str}"
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(info_l1, w - 2), curses.A_NORMAL)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                info_l2 = f" 镜像文件: 内核 {target.vmlinuz_path} │ 内存盘: {target.initramfs_path}"
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(info_l2, w - 2), curses.A_NORMAL)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                if target.has_boot_entry:
                    if target.is_default:
                        diag_str = " 状态研判: ✔ 引导条目完整且已设为默认启动内核，开机将全自动进入此内核。"
                        diag_color = curses.color_pair(self.ACTIVE) | curses.A_BOLD
                    elif target.is_oneshot:
                        diag_str = " 状态研判: 🚀 已设置为下次单次启动 (One-Shot)，下次开机仅运行此内核一次。"
                        diag_color = curses.color_pair(self.CURRENT) | curses.A_BOLD
                    else:
                        diag_str = " 状态研判: ✔ 引导条目已就绪，可随时点击下方按钮设为默认启动或单次测试。"
                        diag_color = curses.color_pair(self.ACTIVE) | curses.A_BOLD
                else:
                    diag_str = " 状态研判: ⚠️ 检测到内核镜像已安装，但未配置引导项。点击下方【一键生成条目】即可自动补齐！"
                    diag_color = curses.color_pair(self.HIGHLIGHT) | curses.A_BOLD

                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(diag_str, w - 2), diag_color)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                # 7. Divider before Buttons
                self.stdscr.addstr(cur_line, 0, "├" + "─" * (self.max_x - 2) + "┤", curses.color_pair(self.HEADER))
                cur_line += 1

                # 8. Interactive Action Buttons
                btn_sec_title = " 【可视化快捷操作按钮】(鼠标直接点击，或按 Tab / ← / → 切换焦点后回车执行):"
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(btn_sec_title, w - 2), curses.color_pair(self.HEADER) | curses.A_BOLD)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                # Button Row 1: 0 (设为默认), 1 (单次启动), 2 (生成条目)
                b0_txt = " [ ▶ 设为永久默认 ◀ ] " if focused_btn == 0 else " [   设为永久默认   ] "
                b1_txt = " [ ▶ 仅单次启动 ◀ ] " if focused_btn == 1 else " [   仅单次启动   ] "
                b2_txt = " [ ▶ 一键生成条目 ◀ ] " if focused_btn == 2 else " [   一键生成条目   ] "

                c0 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 0 else curses.color_pair(self.BUTTON_NORMAL)
                c1 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 1 else curses.color_pair(self.BUTTON_NORMAL)
                c2 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 2 else curses.color_pair(self.BUTTON_NORMAL)

                tot_r1_w = str_display_width(b0_txt) + 4 + str_display_width(b1_txt) + 4 + str_display_width(b2_txt)
                r1_start_x = max(2, (self.max_x - tot_r1_w) // 2)
                b0_x = r1_start_x
                b1_x = b0_x + str_display_width(b0_txt) + 4
                b2_x = b1_x + str_display_width(b1_txt) + 4
                r1_y = cur_line

                self.stdscr.addstr(r1_y, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(r1_y, 1, " " * w)
                self.stdscr.addstr(r1_y, b0_x, b0_txt, c0)
                self.stdscr.addstr(r1_y, b1_x, b1_txt, c1)
                self.stdscr.addstr(r1_y, b2_x, b2_txt, c2)
                self.stdscr.addstr(r1_y, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                # Button Row 2: 3 (查看详情), 4 (返回主列表)
                b3_txt = " [ ▶ 查看完整参数 ◀ ] " if focused_btn == 3 else " [   查看完整参数   ] "
                b4_txt = " [ ▶ 返回主列表 ◀ ] " if focused_btn == 4 else " [   返回主列表   ] "

                c3 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 3 else curses.color_pair(self.BUTTON_NORMAL)
                c4 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 4 else curses.color_pair(self.BUTTON_NORMAL)

                tot_r2_w = str_display_width(b3_txt) + 6 + str_display_width(b4_txt)
                r2_start_x = max(2, (self.max_x - tot_r2_w) // 2)
                b3_x = r2_start_x
                b4_x = b3_x + str_display_width(b3_txt) + 6
                r2_y = cur_line

                self.stdscr.addstr(r2_y, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(r2_y, 1, " " * w)
                self.stdscr.addstr(r2_y, b3_x, b3_txt, c3)
                self.stdscr.addstr(r2_y, b4_x, b4_txt, c4)
                self.stdscr.addstr(r2_y, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                btn_hitboxes = [
                    (0, r1_y, b0_x, b0_x + str_display_width(b0_txt)),
                    (1, r1_y, b1_x, b1_x + str_display_width(b1_txt)),
                    (2, r1_y, b2_x, b2_x + str_display_width(b2_txt)),
                    (3, r2_y, b3_x, b3_x + str_display_width(b3_txt)),
                    (4, r2_y, b4_x, b4_x + str_display_width(b4_txt)),
                ]

                # Fill remaining rows up to bottom
                while cur_line < self.max_y - 1:
                    self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                    self.stdscr.addstr(cur_line, 1, " " * w)
                    self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                    cur_line += 1

                # 9. Bottom Border with integrated Status Message Badge
                bottom_y = self.max_y - 1
                if not k_msg:
                    self.stdscr.addstr(bottom_y, 0, "└" + "─" * (self.max_x - 2) + "┘", curses.color_pair(self.HEADER))
                else:
                    b_color = curses.color_pair(self.ACTIVE) | curses.A_BOLD if k_msg_type == "success" else (curses.color_pair(self.INACTIVE) | curses.A_BOLD if k_msg_type == "error" else curses.color_pair(self.CURRENT) | curses.A_BOLD)
                    b_icon = "✔" if k_msg_type == "success" else ("✖" if k_msg_type == "error" else "ℹ")
                    badge_str = f" [ {b_icon} {k_msg} ] "
                    b_w = str_display_width(badge_str)
                    left_d = 2
                    right_d = max(0, self.max_x - 2 - left_d - b_w)
                    self.stdscr.addstr(bottom_y, 0, "└" + "─" * left_d, curses.color_pair(self.HEADER))
                    self.stdscr.addstr(bottom_y, 1 + left_d, badge_str, b_color)
                    self.stdscr.addstr(bottom_y, 1 + left_d + b_w, "─" * right_d + "┘", curses.color_pair(self.HEADER))

                self.stdscr.noutrefresh()
                curses.doupdate()

                key = self.stdscr.getch()

                def execute_action(action_idx: int):
                    nonlocal target, k_msg, k_msg_type
                    if action_idx == 0:
                        # 设为永久默认
                        if not target.has_boot_entry:
                            plan = KernelManager.generate_entry_plan(target)
                            agreed = self._show_diff_confirm_dialog(
                                plan["diff_lines"],
                                f"生成条目并切换默认内核: {target.display_name}"
                            )
                            if not agreed:
                                k_msg = "操作已取消，未做任何修改。"
                                k_msg_type = "info"
                                return
                        ok, msg = KernelManager.switch_default_kernel(target)
                        k_msg = msg.splitlines()[0]
                        k_msg_type = "success" if ok else "error"
                        self.set_message(k_msg, k_msg_type)
                    elif action_idx == 1:
                        # 设为单次启动
                        if not target.has_boot_entry:
                            plan = KernelManager.generate_entry_plan(target)
                            agreed = self._show_diff_confirm_dialog(
                                plan["diff_lines"],
                                f"生成条目并设置单次启动: {target.display_name}"
                            )
                            if not agreed:
                                k_msg = "操作已取消，未做任何修改。"
                                k_msg_type = "info"
                                return
                        ok, msg = KernelManager.switch_oneshot_kernel(target)
                        k_msg = msg.splitlines()[0]
                        k_msg_type = "success" if ok else "error"
                        self.set_message(k_msg, k_msg_type)
                    elif action_idx == 2:
                        # 一键生成条目
                        if target.has_boot_entry:
                            k_msg = f"内核 '{target.display_name}' 引导条目已存在，无需重复生成。"
                            k_msg_type = "info"
                            return
                        plan = KernelManager.generate_entry_plan(target)
                        agreed = self._show_diff_confirm_dialog(
                            plan["diff_lines"],
                            f"生成条目配置: {target.display_name}"
                        )
                        if not agreed:
                            k_msg = "操作已取消，未做任何修改。"
                            k_msg_type = "info"
                            return
                        ok, msg = KernelManager.create_boot_entry(target)
                        k_msg = msg.splitlines()[0]
                        k_msg_type = "success" if ok else "error"
                        self.set_message(k_msg, k_msg_type)
                    elif action_idx == 3:
                        # 详情
                        detail_lines = [
                            f"内核名称: {target.display_name}",
                            f"软件包名: {target.package_name}",
                            f"发布版本: {target.release}",
                            f"当前运行: {'是 (Active Running)' if target.is_current else '否'}",
                            f"默认启动: {'是 (Default)' if target.is_default else '否'}",
                            f"单次引导: {'是 (One-Shot)' if target.is_oneshot else '否'}",
                            f"引导条目: {target.entry_id or '未生成'}",
                            f"引导格式: {target.entry_type_label}",
                            f"内核镜像: {target.vmlinuz_path}",
                            f"初始化盘: {target.initramfs_path}",
                            f"CPU 微码: {target.ucode_path or '无'}",
                            f"启动参数: {target.cmdline}",
                        ]
                        self._show_info_popup(detail_lines, f"内核详情: {target.display_name}")
                    elif action_idx == 4:
                        return "RETURN"

                if key in [ord('q'), ord('Q'), 27]:
                    break
                elif key in [curses.KEY_UP, ord('k')]:
                    if selected_k_idx > 0:
                        selected_k_idx -= 1
                elif key in [curses.KEY_DOWN, ord('j')]:
                    if selected_k_idx < len(kernels) - 1:
                        selected_k_idx += 1
                elif key in [curses.KEY_RIGHT, 9]:  # Tab or Right
                    focused_btn = (focused_btn + 1) % 5
                elif key in [curses.KEY_LEFT, curses.KEY_BTAB]:  # Shift-Tab or Left
                    focused_btn = (focused_btn - 1) % 5
                elif key in [10, 13, curses.KEY_ENTER, ord(' ')]:
                    ret = execute_action(focused_btn)
                    if ret == "RETURN":
                        break
                elif key == ord('1'):
                    execute_action(0)
                elif key == ord('2'):
                    execute_action(1)
                elif key == ord('3'):
                    execute_action(2)
                elif key in [ord('4'), ord('i'), ord('I')]:
                    execute_action(3)
                elif key == curses.KEY_MOUSE:
                    try:
                        _, mx, my, _, bstate = curses.getmouse()
                        if bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED):
                            m_now = time.time()
                            for ry, x1, x2, ki in k_hitboxes:
                                if my == ry and x1 <= mx <= x2:
                                    last_k_t = getattr(self, "_last_k_click_time", 0.0)
                                    last_k_i = getattr(self, "_last_k_clicked_i", None)
                                    if last_k_i == ki and 0.04 <= (m_now - last_k_t) <= 0.55:
                                        self._last_k_click_time = 0.0
                                        self._last_k_clicked_i = None
                                        selected_k_idx = ki
                                        execute_action(3)  # 双击直接查看内核详情
                                    else:
                                        self._last_k_click_time = m_now
                                        self._last_k_clicked_i = ki
                                        selected_k_idx = ki
                                    break
                            for bi, by, x1, x2 in btn_hitboxes:
                                if my == by and x1 <= mx <= x2:
                                    focused_btn = bi
                                    ret = execute_action(bi)
                                    if ret == "RETURN":
                                        return
                                    break
                    except Exception:
                        pass
            except curses.error:
                break

    def show_repair_dialog(self):
        """Dedicated visual workstation for boot diagnostics and one-click recovery."""
        from src.repair import diagnose_boot_system, repair_loader, reinstall_system_bootloader

        focused_btn = 0
        r_msg = ""
        r_msg_type = "info"

        while True:
            self.max_y, self.max_x = self.stdscr.getmaxyx()
            self.stdscr.erase()

            report = diagnose_boot_system()
            w = self.max_x - 2

            try:
                # 1. Header (Lines 0..2)
                title = " UEFI 固件与 Linux 引导深度诊断修复工作台 "
                mode = " [引导体检修复] "
                self.stdscr.addstr(0, 0, "┌" + "─" * (self.max_x - 2) + "┐", curses.color_pair(self.HEADER))
                self.stdscr.addstr(1, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(1, 2, title, curses.color_pair(self.TITLE) | curses.A_BOLD)
                mode_w = str_display_width(mode)
                self.stdscr.addstr(1, max(str_display_width(title) + 4, self.max_x - 1 - mode_w), mode, curses.color_pair(self.CURRENT))
                self.stdscr.addstr(1, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(2, 0, "├" + "─" * (self.max_x - 2) + "┤", curses.color_pair(self.HEADER))

                # 2. Status Bar (Line 3)
                first_lbl = f"[{report.first_boot_entry.boot_num}] {report.first_boot_entry.label}" if report.first_boot_entry else "无有效条目"
                nvram_cnt = len(report.boot_config.entries if report.boot_config else [])
                esp_cnt = len(report.esp_partitions)
                missing_cnt = len(report.missing_loaders)
                status_txt = f" 首选启动: {first_lbl} │ NVRAM条目: {nvram_cnt} 项 │ ESP分区: {esp_cnt} 个 │ 缺失条目: {missing_cnt} 个 "
                self.stdscr.addstr(3, 0, pad_to_width(status_txt, self.max_x), curses.color_pair(self.STATUS_BAR))

                # 3. Diagnostic Report Card
                cur_line = 5
                self.stdscr.addstr(4, 0, "│" + " " * w + "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                sec_title = " 【全盘引导文件与固件健康体检报告】:"
                self.stdscr.addstr(cur_line, 2, pad_to_width(sec_title, w - 2), curses.color_pair(self.HEADER) | curses.A_BOLD)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                diag_l1 = f" 固件状态: 第一优先启动项为 {first_lbl}，主板 NVRAM 数据交互正常"
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(diag_l1, w - 2), curses.A_NORMAL)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                diag_l2 = f" 分区探测: 发现 {esp_cnt} 个物理 ESP 分区，已扫描底层 EFI 二进制签名"
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(diag_l2, w - 2), curses.A_NORMAL)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                if not report.missing_loaders:
                    diag_str = " 诊断研判: ✔ 未发现丢失条目，全盘物理 EFI 引导文件与主板 NVRAM 完美匹配，引导链路健康！"
                    diag_col = curses.color_pair(self.ACTIVE) | curses.A_BOLD
                    self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                    self.stdscr.addstr(cur_line, 2, pad_to_width(diag_str, w - 2), diag_col)
                    self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                    cur_line += 1
                else:
                    diag_str = f" 诊断研判: ⚠ 警告：检测到 {len(report.missing_loaders)} 个物理存在但 NVRAM 缺失的 EFI 引导项！"
                    diag_col = curses.color_pair(self.HIGHLIGHT) | curses.A_BOLD
                    self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                    self.stdscr.addstr(cur_line, 2, pad_to_width(diag_str, w - 2), diag_col)
                    self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                    cur_line += 1

                    for idx, m in enumerate(report.missing_loaders[:3], start=1):
                        m_txt = f"   ★ 缺失条目 #{idx}: {m.recommended_label} -> {m.efi_rel_path}"
                        self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                        self.stdscr.addstr(cur_line, 2, pad_to_width(m_txt, w - 2), curses.color_pair(self.CURRENT))
                        self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                        cur_line += 1

                # 4. Divider before Buttons
                self.stdscr.addstr(cur_line, 0, "├" + "─" * (self.max_x - 2) + "┤", curses.color_pair(self.HEADER))
                cur_line += 1

                # 5. Buttons Header
                btn_sec_title = " 【可视化快捷操作按钮】(鼠标直接点击，或按 Tab / ← / → 切换焦点后回车执行):"
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(btn_sec_title, w - 2), curses.color_pair(self.HEADER) | curses.A_BOLD)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                btn_hitboxes = []
                if report.missing_loaders:
                    num_btns = 3
                    b0_txt = " [ ▶ 🛠 一键恢复所有缺失引导项并置顶 ◀ ] " if focused_btn == 0 else " [   🛠 一键恢复所有缺失引导项并置顶   ] "
                    b1_txt = " [ ▶ 📦 引导加载器深度重装 ◀ ] " if focused_btn == 1 else " [   📦 引导加载器深度重装   ] "
                    b2_txt = " [ ▶ 🔙 返回主列表 ◀ ] " if focused_btn == 2 else " [   🔙 返回主列表   ] "

                    c0 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 0 else curses.color_pair(self.BUTTON_NORMAL)
                    c1 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 1 else curses.color_pair(self.BUTTON_NORMAL)
                    c2 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 2 else curses.color_pair(self.BUTTON_NORMAL)

                    tot_w = str_display_width(b0_txt) + 3 + str_display_width(b1_txt) + 3 + str_display_width(b2_txt)
                    r_x = max(2, (self.max_x - tot_w) // 2)
                    b0_x = r_x
                    b1_x = b0_x + str_display_width(b0_txt) + 3
                    b2_x = b1_x + str_display_width(b1_txt) + 3
                    r_y = cur_line

                    self.stdscr.addstr(r_y, 0, "│", curses.color_pair(self.HEADER))
                    self.stdscr.addstr(r_y, 1, " " * w)
                    self.stdscr.addstr(r_y, b0_x, b0_txt, c0)
                    self.stdscr.addstr(r_y, b1_x, b1_txt, c1)
                    self.stdscr.addstr(r_y, b2_x, b2_txt, c2)
                    self.stdscr.addstr(r_y, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                    cur_line += 1

                    btn_hitboxes = [
                        (0, r_y, b0_x, b0_x + str_display_width(b0_txt)),
                        (1, r_y, b1_x, b1_x + str_display_width(b1_txt)),
                        (2, r_y, b2_x, b2_x + str_display_width(b2_txt)),
                    ]
                else:
                    num_btns = 2
                    b0_txt = " [ ▶ 📦 引导加载器深度重装 (bootctl / grub) ◀ ] " if focused_btn == 0 else " [   📦 引导加载器深度重装 (bootctl / grub)   ] "
                    b1_txt = " [ ▶ 🔙 返回主列表 ◀ ] " if focused_btn == 1 else " [   🔙 返回主列表   ] "

                    c0 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 0 else curses.color_pair(self.BUTTON_NORMAL)
                    c1 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 1 else curses.color_pair(self.BUTTON_NORMAL)

                    tot_w = str_display_width(b0_txt) + 4 + str_display_width(b1_txt)
                    r_x = max(2, (self.max_x - tot_w) // 2)
                    b0_x = r_x
                    b1_x = b0_x + str_display_width(b0_txt) + 4
                    r_y = cur_line

                    self.stdscr.addstr(r_y, 0, "│", curses.color_pair(self.HEADER))
                    self.stdscr.addstr(r_y, 1, " " * w)
                    self.stdscr.addstr(r_y, b0_x, b0_txt, c0)
                    self.stdscr.addstr(r_y, b1_x, b1_txt, c1)
                    self.stdscr.addstr(r_y, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                    cur_line += 1

                    btn_hitboxes = [
                        (0, r_y, b0_x, b0_x + str_display_width(b0_txt)),
                        (1, r_y, b1_x, b1_x + str_display_width(b1_txt)),
                    ]

                # Fill remaining rows up to bottom
                while cur_line < self.max_y - 1:
                    self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                    self.stdscr.addstr(cur_line, 1, " " * w)
                    self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                    cur_line += 1

                # 6. Bottom Border with integrated Status Message Badge
                bottom_y = self.max_y - 1
                if not r_msg:
                    self.stdscr.addstr(bottom_y, 0, "└" + "─" * (self.max_x - 2) + "┘", curses.color_pair(self.HEADER))
                else:
                    b_color = curses.color_pair(self.ACTIVE) | curses.A_BOLD if r_msg_type == "success" else (curses.color_pair(self.INACTIVE) | curses.A_BOLD if r_msg_type == "error" else curses.color_pair(self.CURRENT) | curses.A_BOLD)
                    b_icon = "✔" if r_msg_type == "success" else ("✖" if r_msg_type == "error" else "ℹ")
                    badge_str = f" [ {b_icon} {r_msg} ] "
                    b_w = str_display_width(badge_str)
                    left_d = 2
                    right_d = max(0, self.max_x - 2 - left_d - b_w)
                    self.stdscr.addstr(bottom_y, 0, "└" + "─" * left_d, curses.color_pair(self.HEADER))
                    self.stdscr.addstr(bottom_y, 1 + left_d, badge_str, b_color)
                    self.stdscr.addstr(bottom_y, 1 + left_d + b_w, "─" * right_d + "┘", curses.color_pair(self.HEADER))

                self.stdscr.noutrefresh()
                curses.doupdate()

                key = self.stdscr.getch()

                def do_repair_action(action_idx: int):
                    nonlocal r_msg, r_msg_type
                    if report.missing_loaders:
                        if action_idx == 0:
                            restored = 0
                            for m in report.missing_loaders:
                                ok, res_msg = repair_loader(m, set_first=True)
                                if ok:
                                    restored += 1
                            r_msg = f"成功将 {restored} 个缺失的引导项重新注册到 NVRAM！"
                            r_msg_type = "success"
                            self.set_message(r_msg, "success")
                            self.refresh(keep_message=True)
                        elif action_idx == 1:
                            ok, msg = reinstall_system_bootloader()
                            r_msg = msg.splitlines()[0]
                            r_msg_type = "success" if ok else "error"
                            self.set_message(r_msg, r_msg_type)
                        elif action_idx == 2:
                            return "RETURN"
                    else:
                        if action_idx == 0:
                            ok, msg = reinstall_system_bootloader()
                            r_msg = msg.splitlines()[0]
                            r_msg_type = "success" if ok else "error"
                            self.set_message(r_msg, r_msg_type)
                        elif action_idx == 1:
                            return "RETURN"

                if key in [ord('q'), ord('Q'), 27]:
                    break
                elif key in [curses.KEY_RIGHT, 9]:  # Tab or Right
                    focused_btn = (focused_btn + 1) % num_btns
                elif key in [curses.KEY_LEFT, curses.KEY_BTAB]:  # Shift-Tab or Left
                    focused_btn = (focused_btn - 1) % num_btns
                elif key in [10, 13, curses.KEY_ENTER, ord(' ')]:
                    ret = do_repair_action(focused_btn)
                    if ret == "RETURN":
                        break
                elif key == ord('1'):
                    do_repair_action(0)
                elif key == ord('2'):
                    do_repair_action(1)
                elif key == curses.KEY_MOUSE:
                    try:
                        _, mx, my, _, bstate = curses.getmouse()
                        if bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED):
                            for bi, by, x1, x2 in btn_hitboxes:
                                if my == by and x1 <= mx <= x2:
                                    focused_btn = bi
                                    ret = do_repair_action(bi)
                                    if ret == "RETURN":
                                        return
                                    break
                    except Exception:
                        pass
            except curses.error:
                break

    def show_esp_dialog(self):
        """Dedicated visual workstation for inspecting physical ESP partitions and boot files."""
        from src.esp import find_all_esp_partitions
        esp_list = find_all_esp_partitions()

        selected_esp_idx = 0
        focused_btn = 0
        esp_msg = ""
        esp_msg_type = "info"

        while True:
            self.max_y, self.max_x = self.stdscr.getmaxyx()
            self.stdscr.erase()

            if not esp_list:
                esp_list = find_all_esp_partitions()

            if not esp_list:
                self._show_info_popup(["未在当前系统中检测到标准的 ESP 分区。"], "ESP 物理分区")
                return

            selected_esp_idx = max(0, min(selected_esp_idx, len(esp_list) - 1))
            target = esp_list[selected_esp_idx]
            w = self.max_x - 2

            try:
                # 1. Header (Lines 0..2)
                title = " UEFI 物理 ESP 分区与引导文件架构体检工作台 "
                mode = f" [发现 {len(esp_list)} 个 ESP 分区] "
                self.stdscr.addstr(0, 0, "┌" + "─" * (self.max_x - 2) + "┐", curses.color_pair(self.HEADER))
                self.stdscr.addstr(1, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(1, 2, title, curses.color_pair(self.TITLE) | curses.A_BOLD)
                mode_w = str_display_width(mode)
                self.stdscr.addstr(1, max(str_display_width(title) + 4, self.max_x - 1 - mode_w), mode, curses.color_pair(self.CURRENT))
                self.stdscr.addstr(1, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(2, 0, "├" + "─" * (self.max_x - 2) + "┤", curses.color_pair(self.HEADER))

                # 2. Status Bar (Line 3)
                mounted_cnt = sum(1 for e in esp_list if e.mount_point)
                total_efi = sum(len(e.efi_files) for e in esp_list)
                arch_tag = "双 ESP 物理隔离架构 (高稳定性)" if len(esp_list) >= 2 else "单 ESP 共享引导架构"
                status_txt = f" ESP分区: {len(esp_list)} 个 │ 已挂载: {mounted_cnt} 个 │ EFI引导二进制: {total_efi} 个 │ 架构: {arch_tag} "
                self.stdscr.addstr(3, 0, pad_to_width(status_txt, self.max_x), curses.color_pair(self.STATUS_BAR))

                # 3. Section 1: ESP 分区卡片列表 (Lines 5..)
                cur_line = 5
                self.stdscr.addstr(4, 0, "│" + " " * w + "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                sec_title = " 【全盘物理 ESP 分区列表】(按 ↑/↓ 切换选择分区，或直接鼠标点击选择):"
                self.stdscr.addstr(cur_line, 2, pad_to_width(sec_title, w - 2), curses.color_pair(self.HEADER) | curses.A_BOLD)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                esp_hitboxes = []
                for i, esp in enumerate(esp_list):
                    is_sel = (i == selected_esp_idx)
                    radio = "(*)" if is_sel else "( )"
                    mnt_str = f"挂载点: {esp.mount_point}" if esp.mount_point else "未挂载 (独立物理隐藏引导分区)"
                    badges = []
                    if esp.mount_point in ("/boot", "/boot/efi", "/efi"):
                        badges.append("[当前系统 ESP]")
                    if any("microsoft" in f[1].lower() for f in esp.efi_files):
                        badges.append("[Windows 11]")
                    if any("systemd" in f[1].lower() or "linux" in f[1].lower() or "grub" in f[1].lower() for f in esp.efi_files):
                        badges.append("[Linux 引导]")
                    badge_str = " ".join(badges)

                    row_txt = f" {radio} [ESP #{i+1}] {esp.device_path} (分区 {esp.part_num}) │ {esp.size_mb:.0f} MB │ {mnt_str}  {badge_str}"
                    padded_row = pad_to_width(row_txt, w - 2)

                    self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                    if is_sel:
                        self.stdscr.addstr(cur_line, 2, padded_row, curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD)
                    else:
                        self.stdscr.addstr(cur_line, 2, padded_row, curses.color_pair(self.BUTTON_NORMAL))
                    self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))

                    esp_hitboxes.append((cur_line, 2, self.max_x - 2, i))
                    cur_line += 1

                # 4. Divider
                self.stdscr.addstr(cur_line, 0, "├" + "─" * (self.max_x - 2) + "┤", curses.color_pair(self.HEADER))
                cur_line += 1

                # 5. Inspection Card for target ESP
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                card_title = f" 【选中分区底层引导结构深度解析】[{target.device_path}]"
                self.stdscr.addstr(cur_line, 2, pad_to_width(card_title, w - 2), curses.color_pair(self.TITLE) | curses.A_BOLD)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                t_mnt = target.mount_point or "(未直接挂载至根文件系统，通过 UEFI 固件硬件寻址)"
                info_l1 = f" 物理设备: {target.device_path} (分区 #{target.part_num}) │ 容量: {target.size_mb:.1f} MB │ 挂载: {t_mnt}"
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(info_l1, w - 2), curses.A_NORMAL)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                if target.part_uuid:
                    info_uuid = f" 硬件标识: PARTUUID: {target.part_uuid} │ 磁盘节点: {target.disk}"
                    self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                    self.stdscr.addstr(cur_line, 2, pad_to_width(info_uuid, w - 2), curses.A_NORMAL)
                    self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                    cur_line += 1

                # EFI files
                if target.efi_files:
                    efi_names = [f[1] for f in target.efi_files]
                    efi_str = " 引导二进制: " + ", ".join(efi_names[:4])
                    if len(efi_names) > 4:
                        efi_str += f" 等共 {len(efi_names)} 个引导程序"
                else:
                    efi_str = " 引导二进制: (未发现或当前挂载点未读出 .efi 文件)"
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(efi_str, w - 2), curses.color_pair(self.ACTIVE))
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                # Cfg files
                if target.cfg_files:
                    cfg_names = [c[1] for c in target.cfg_files]
                    cfg_str = " 发现配置文件: " + ", ".join(cfg_names[:3])
                else:
                    cfg_str = " 发现配置文件: (无独立 .conf / .cfg 配置文件)"
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(cfg_str, w - 2), curses.A_NORMAL)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                # Diagnosis
                has_win = any("microsoft" in f[1].lower() for f in target.efi_files)
                has_linux = any("systemd" in f[1].lower() or "linux" in f[1].lower() or "grub" in f[1].lower() for f in target.efi_files)
                if has_win and has_linux:
                    diag_str = " 架构研判: ✔ 单 ESP 混合引导架构，Windows 与 Linux 共享此分区，引导互通配置良好。"
                elif has_win:
                    diag_str = " 架构研判: ✔ Windows 11 专属独立 ESP 分区，Windows 更新无法破坏 Linux 引导，安全性极高。"
                elif has_linux:
                    diag_str = " 架构研判: ✔ Linux 核心 ESP 引导分区，systemd-boot/内核配置健全，为系统提供主要引导能力。"
                else:
                    diag_str = " 架构研判: ℹ 标准 UEFI ESP FAT32 分区，符合主板固件开机引导标准。"

                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(diag_str, w - 2), curses.color_pair(self.CURRENT) | curses.A_BOLD)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                # 6. Divider before buttons
                self.stdscr.addstr(cur_line, 0, "├" + "─" * (self.max_x - 2) + "┤", curses.color_pair(self.HEADER))
                cur_line += 1

                # 7. Action buttons
                btn_sec_title = " 【可视化快捷操作按钮】(鼠标直接点击，或按 Tab / ← / → 切换焦点后回车执行):"
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(btn_sec_title, w - 2), curses.color_pair(self.HEADER) | curses.A_BOLD)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                # Row 1: 0 (重新全盘扫描), 1 (双系统引导互通), 2 (Linux引导配置)
                b0_txt = " [ ▶ 🔍 重新全盘扫描 ◀ ] " if focused_btn == 0 else " [   🔍 重新全盘扫描   ] "
                b1_txt = " [ ▶ 🛠 双系统引导互通 ◀ ] " if focused_btn == 1 else " [   🛠 双系统引导互通   ] "
                b2_txt = " [ ▶ ⚙️ Linux 引导配置 ◀ ] " if focused_btn == 2 else " [   ⚙️ Linux 引导配置   ] "

                c0 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 0 else curses.color_pair(self.BUTTON_NORMAL)
                c1 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 1 else curses.color_pair(self.BUTTON_NORMAL)
                c2 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 2 else curses.color_pair(self.BUTTON_NORMAL)

                tot_r1_w = str_display_width(b0_txt) + 3 + str_display_width(b1_txt) + 3 + str_display_width(b2_txt)
                r1_start_x = max(2, (self.max_x - tot_r1_w) // 2)
                b0_x = r1_start_x
                b1_x = b0_x + str_display_width(b0_txt) + 3
                b2_x = b1_x + str_display_width(b1_txt) + 3
                r1_y = cur_line

                self.stdscr.addstr(r1_y, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(r1_y, 1, " " * w)
                self.stdscr.addstr(r1_y, b0_x, b0_txt, c0)
                self.stdscr.addstr(r1_y, b1_x, b1_txt, c1)
                self.stdscr.addstr(r1_y, b2_x, b2_txt, c2)
                self.stdscr.addstr(r1_y, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                # Row 2: 3 (固件诊断与修复), 4 (返回主列表)
                b3_txt = " [ ▶ 🩺 固件诊断与修复 ◀ ] " if focused_btn == 3 else " [   🩺 固件诊断与修复   ] "
                b4_txt = " [ ▶ 🔙 返回主列表 ◀ ] " if focused_btn == 4 else " [   🔙 返回主列表   ] "

                c3 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 3 else curses.color_pair(self.BUTTON_NORMAL)
                c4 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 4 else curses.color_pair(self.BUTTON_NORMAL)

                tot_r2_w = str_display_width(b3_txt) + 4 + str_display_width(b4_txt)
                r2_start_x = max(2, (self.max_x - tot_r2_w) // 2)
                b3_x = r2_start_x
                b4_x = b3_x + str_display_width(b3_txt) + 4
                r2_y = cur_line

                self.stdscr.addstr(r2_y, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(r2_y, 1, " " * w)
                self.stdscr.addstr(r2_y, b3_x, b3_txt, c3)
                self.stdscr.addstr(r2_y, b4_x, b4_txt, c4)
                self.stdscr.addstr(r2_y, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                btn_hitboxes = [
                    (0, r1_y, b0_x, b0_x + str_display_width(b0_txt)),
                    (1, r1_y, b1_x, b1_x + str_display_width(b1_txt)),
                    (2, r1_y, b2_x, b2_x + str_display_width(b2_txt)),
                    (3, r2_y, b3_x, b3_x + str_display_width(b3_txt)),
                    (4, r2_y, b4_x, b4_x + str_display_width(b4_txt)),
                ]

                # Fill remaining space
                while cur_line < self.max_y - 1:
                    self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                    self.stdscr.addstr(cur_line, 1, " " * w)
                    self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                    cur_line += 1

                # 8. Bottom Border with integrated Status Message Badge
                bottom_y = self.max_y - 1
                if not esp_msg:
                    self.stdscr.addstr(bottom_y, 0, "└" + "─" * (self.max_x - 2) + "┘", curses.color_pair(self.HEADER))
                else:
                    b_color = curses.color_pair(self.ACTIVE) | curses.A_BOLD if esp_msg_type == "success" else (curses.color_pair(self.INACTIVE) | curses.A_BOLD if esp_msg_type == "error" else curses.color_pair(self.CURRENT) | curses.A_BOLD)
                    b_icon = "✔" if esp_msg_type == "success" else ("✖" if esp_msg_type == "error" else "ℹ")
                    badge_str = f" [ {b_icon} {esp_msg} ] "
                    b_w = str_display_width(badge_str)
                    left_d = 2
                    right_d = max(0, self.max_x - 2 - left_d - b_w)
                    self.stdscr.addstr(bottom_y, 0, "└" + "─" * left_d, curses.color_pair(self.HEADER))
                    self.stdscr.addstr(bottom_y, 1 + left_d, badge_str, b_color)
                    self.stdscr.addstr(bottom_y, 1 + left_d + b_w, "─" * right_d + "┘", curses.color_pair(self.HEADER))

                self.stdscr.noutrefresh()
                curses.doupdate()

                key = self.stdscr.getch()

                def do_esp_action(action_idx: int):
                    nonlocal esp_list, esp_msg, esp_msg_type
                    if action_idx == 0:
                        esp_list = find_all_esp_partitions()
                        esp_msg = f"全盘 ESP 重新扫描完成，检测到 {len(esp_list)} 个 ESP 分区。"
                        esp_msg_type = "success"
                    elif action_idx == 1:
                        self.show_win11_dialog()
                    elif action_idx == 2:
                        self.show_bootloader_dialog()
                    elif action_idx == 3:
                        self.show_repair_dialog()
                    elif action_idx == 4:
                        return "RETURN"

                if key in [ord('q'), ord('Q'), 27]:
                    break
                elif key in [curses.KEY_RIGHT, 9]:  # Tab or Right
                    focused_btn = (focused_btn + 1) % 5
                elif key in [curses.KEY_LEFT, curses.KEY_BTAB]:  # Shift-Tab or Left
                    focused_btn = (focused_btn - 1) % 5
                elif key in [curses.KEY_DOWN, ord('j')]:
                    if selected_esp_idx < len(esp_list) - 1:
                        selected_esp_idx += 1
                elif key in [curses.KEY_UP, ord('k')]:
                    if selected_esp_idx > 0:
                        selected_esp_idx -= 1
                elif key in [10, 13, curses.KEY_ENTER, ord(' ')]:
                    ret = do_esp_action(focused_btn)
                    if ret == "RETURN":
                        break
                elif key in [ord('r'), ord('R')]:
                    do_esp_action(0)
                elif key in [ord('w'), ord('W')]:
                    do_esp_action(1)
                elif key in [ord('b'), ord('B')]:
                    do_esp_action(2)
                elif key in [ord('c'), ord('C'), ord('f'), ord('F')]:
                    do_esp_action(3)
                elif key == curses.KEY_MOUSE:
                    try:
                        _, mx, my, _, bstate = curses.getmouse()
                        if bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED):
                            # Check ESP partition click
                            for by, x1, x2, esp_i in esp_hitboxes:
                                if my == by and x1 <= mx <= x2:
                                    selected_esp_idx = esp_i
                                    break
                            # Check buttons
                            for bi, by, x1, x2 in btn_hitboxes:
                                if my == by and x1 <= mx <= x2:
                                    focused_btn = bi
                                    ret = do_esp_action(bi)
                                    if ret == "RETURN":
                                        return
                                    break
                    except Exception:
                        pass
            except curses.error:
                break

    def show_win11_dialog(self):
        """Dedicated visual workstation for Windows 11 boot analysis and dual-boot interop."""
        from src.win11 import Win11Analyzer, Win11ActionHandler, format_win11_report_text

        focused_btn = 0
        w_msg = ""
        w_msg_type = "info"
        scroll_offset = 0

        while True:
            self.max_y, self.max_x = self.stdscr.getmaxyx()
            self.stdscr.erase()

            diag = Win11Analyzer.analyze()
            report_lines = format_win11_report_text(diag)

            w = self.max_x - 2

            try:
                # 1. Header (Lines 0..2)
                title = " Windows 11 引导深度智能分析与双系统互通工作台 "
                mode = f" [Boot{diag.boot_num or '未检出'}] "
                self.stdscr.addstr(0, 0, "┌" + "─" * (self.max_x - 2) + "┐", curses.color_pair(self.HEADER))
                self.stdscr.addstr(1, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(1, 2, title, curses.color_pair(self.TITLE) | curses.A_BOLD)
                mode_w = str_display_width(mode)
                self.stdscr.addstr(1, max(str_display_width(title) + 4, self.max_x - 1 - mode_w), mode, curses.color_pair(self.CURRENT))
                self.stdscr.addstr(1, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(2, 0, "├" + "─" * (self.max_x - 2) + "┤", curses.color_pair(self.HEADER))

                # 2. Status Bar (Line 3)
                act_str = "已启用" if diag.is_active else "已禁用"
                rank_str = f"第 {diag.order_index + 1} 位" if diag.order_index >= 0 else "未加入启动顺序"
                tpm_str = "TPM 2.0 ✔" if diag.tpm_present else "TPM 未检出"
                sb_str = "安全启动: 开启" if diag.secure_boot_enabled else "安全启动: 未开启"
                status_txt = f" Win11条目: Boot{diag.boot_num} ({act_str}) │ 启动顺位: {rank_str} │ 架构: {diag.summary_label} │ {tpm_str} │ {sb_str} "
                self.stdscr.addstr(3, 0, pad_to_width(status_txt, self.max_x), curses.color_pair(self.STATUS_BAR))

                # 3. Report Card Title (Line 4)
                self.stdscr.addstr(4, 0, "│", curses.color_pair(self.HEADER))
                r_title = " 【Windows 11 深度体检研判报告】(按 ↑/↓/PgUp/PgDn 可上下翻滚查看详细参数):"
                self.stdscr.addstr(4, 2, pad_to_width(r_title, w - 2), curses.color_pair(self.HEADER) | curses.A_BOLD)
                self.stdscr.addstr(4, self.max_x - 1, "│", curses.color_pair(self.HEADER))

                # 4. Visible Report Body (Lines 5..max_y - 8)
                content_top = 5
                content_bottom = max(content_top + 4, self.max_y - 8)
                avail_h = content_bottom - content_top
                max_scroll = max(0, len(report_lines) - avail_h)
                scroll_offset = min(scroll_offset, max_scroll)

                for i in range(avail_h):
                    cur_y = content_top + i
                    l_idx = scroll_offset + i
                    self.stdscr.addstr(cur_y, 0, "│", curses.color_pair(self.HEADER))
                    if l_idx < len(report_lines):
                        line_txt = report_lines[l_idx]
                        if line_txt.startswith("🪟") or line_txt.startswith("💾") or line_txt.startswith("🛡️") or line_txt.startswith("⚡"):
                            c = curses.color_pair(self.TITLE) | curses.A_BOLD
                        elif "[✔]" in line_txt or "【已启用】" in line_txt:
                            c = curses.color_pair(self.ACTIVE) | curses.A_BOLD
                        elif "[★" in line_txt:
                            c = curses.color_pair(self.HIGHLIGHT) | curses.A_BOLD
                        elif "[ℹ" in line_txt:
                            c = curses.color_pair(self.CURRENT)
                        elif "─" in line_txt or "═" in line_txt:
                            c = curses.color_pair(self.HEADER)
                        else:
                            c = curses.A_NORMAL
                        self.stdscr.addstr(cur_y, 2, pad_to_width(line_txt, w - 2), c)
                    else:
                        self.stdscr.addstr(cur_y, 2, " " * (w - 2))
                    self.stdscr.addstr(cur_y, self.max_x - 1, "│", curses.color_pair(self.HEADER))

                cur_line = content_bottom

                # 5. Divider before Buttons
                self.stdscr.addstr(cur_line, 0, "├" + "─" * (self.max_x - 2) + "┤", curses.color_pair(self.HEADER))
                cur_line += 1

                # 6. Buttons Header
                btn_sec_title = " 【可视化快捷操作按钮】(鼠标直接点击，或按 Tab / ← / → 切换焦点后回车执行):"
                self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(cur_line, 2, pad_to_width(btn_sec_title, w - 2), curses.color_pair(self.HEADER) | curses.A_BOLD)
                self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                # Button Row 1: 0 (单次启动), 1 (设为第一项), 2 (同步到ESP)
                b0_txt = " [ ▶ 🚀 仅单次启动 Win11 ◀ ] " if focused_btn == 0 else " [   🚀 仅单次启动 Win11   ] "
                b1_txt = " [ ▶ ★ 设为默认第一项 ◀ ] " if focused_btn == 1 else " [   ★ 设为默认第一项   ] "
                b2_txt = " [ ▶ 🛠 同步引导至 Linux ESP ◀ ] " if focused_btn == 2 else " [   🛠 同步引导至 Linux ESP   ] "

                c0 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 0 else curses.color_pair(self.BUTTON_NORMAL)
                c1 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 1 else curses.color_pair(self.BUTTON_NORMAL)
                c2 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 2 else curses.color_pair(self.BUTTON_NORMAL)

                tot_r1_w = str_display_width(b0_txt) + 3 + str_display_width(b1_txt) + 3 + str_display_width(b2_txt)
                r1_start_x = max(2, (self.max_x - tot_r1_w) // 2)
                b0_x = r1_start_x
                b1_x = b0_x + str_display_width(b0_txt) + 3
                b2_x = b1_x + str_display_width(b1_txt) + 3
                r1_y = cur_line

                self.stdscr.addstr(r1_y, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(r1_y, 1, " " * w)
                self.stdscr.addstr(r1_y, b0_x, b0_txt, c0)
                self.stdscr.addstr(r1_y, b1_x, b1_txt, c1)
                self.stdscr.addstr(r1_y, b2_x, b2_txt, c2)
                self.stdscr.addstr(r1_y, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                # Button Row 2: 3 (重建NVRAM), 4 (启禁切换), 5 (返回)
                b3_txt = " [ ▶ 🔧 重建 NVRAM 条目 ◀ ] " if focused_btn == 3 else " [   🔧 重建 NVRAM 条目   ] "
                b4_txt = " [ ▶ 🔄 切换启用/禁用状态 ◀ ] " if focused_btn == 4 else " [   🔄 切换启用/禁用状态   ] "
                b5_txt = " [ ▶ 🔙 返回主列表 ◀ ] " if focused_btn == 5 else " [   🔙 返回主列表   ] "

                c3 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 3 else curses.color_pair(self.BUTTON_NORMAL)
                c4 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 4 else curses.color_pair(self.BUTTON_NORMAL)
                c5 = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 5 else curses.color_pair(self.BUTTON_NORMAL)

                tot_r2_w = str_display_width(b3_txt) + 3 + str_display_width(b4_txt) + 3 + str_display_width(b5_txt)
                r2_start_x = max(2, (self.max_x - tot_r2_w) // 2)
                b3_x = r2_start_x
                b4_x = b3_x + str_display_width(b3_txt) + 3
                b5_x = b4_x + str_display_width(b4_txt) + 3
                r2_y = cur_line

                self.stdscr.addstr(r2_y, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(r2_y, 1, " " * w)
                self.stdscr.addstr(r2_y, b3_x, b3_txt, c3)
                self.stdscr.addstr(r2_y, b4_x, b4_txt, c4)
                self.stdscr.addstr(r2_y, b5_x, b5_txt, c5)
                self.stdscr.addstr(r2_y, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                cur_line += 1

                btn_hitboxes = [
                    (0, r1_y, b0_x, b0_x + str_display_width(b0_txt)),
                    (1, r1_y, b1_x, b1_x + str_display_width(b1_txt)),
                    (2, r1_y, b2_x, b2_x + str_display_width(b2_txt)),
                    (3, r2_y, b3_x, b3_x + str_display_width(b3_txt)),
                    (4, r2_y, b4_x, b4_x + str_display_width(b4_txt)),
                    (5, r2_y, b5_x, b5_x + str_display_width(b5_txt)),
                ]

                # Fill remaining rows up to bottom
                while cur_line < self.max_y - 1:
                    self.stdscr.addstr(cur_line, 0, "│", curses.color_pair(self.HEADER))
                    self.stdscr.addstr(cur_line, 1, " " * w)
                    self.stdscr.addstr(cur_line, self.max_x - 1, "│", curses.color_pair(self.HEADER))
                    cur_line += 1

                # 7. Bottom Border with integrated Status Message Badge
                bottom_y = self.max_y - 1
                if not w_msg:
                    self.stdscr.addstr(bottom_y, 0, "└" + "─" * (self.max_x - 2) + "┘", curses.color_pair(self.HEADER))
                else:
                    b_color = curses.color_pair(self.ACTIVE) | curses.A_BOLD if w_msg_type == "success" else (curses.color_pair(self.INACTIVE) | curses.A_BOLD if w_msg_type == "error" else curses.color_pair(self.CURRENT) | curses.A_BOLD)
                    b_icon = "✔" if w_msg_type == "success" else ("✖" if w_msg_type == "error" else "ℹ")
                    badge_str = f" [ {b_icon} {w_msg} ] "
                    b_w = str_display_width(badge_str)
                    left_d = 2
                    right_d = max(0, self.max_x - 2 - left_d - b_w)
                    self.stdscr.addstr(bottom_y, 0, "└" + "─" * left_d, curses.color_pair(self.HEADER))
                    self.stdscr.addstr(bottom_y, 1 + left_d, badge_str, b_color)
                    self.stdscr.addstr(bottom_y, 1 + left_d + b_w, "─" * right_d + "┘", curses.color_pair(self.HEADER))

                self.stdscr.noutrefresh()
                curses.doupdate()

                key = self.stdscr.getch()

                def execute_win_action(action_idx: int):
                    nonlocal w_msg, w_msg_type
                    if action_idx == 0:
                        ok, msg = Win11ActionHandler.set_boot_next_win11(diag)
                        w_msg = msg
                        w_msg_type = "success" if ok else "error"
                        self.set_message(msg, w_msg_type)
                        if ok:
                            self.refresh()
                    elif action_idx == 1:
                        if diag.order_index == 0:
                            if self._confirm("Windows 11 当前已是第一启动项，是否恢复 Linux 为第一启动项？"):
                                ok, msg = Win11ActionHandler.restore_linux_as_first_boot(diag)
                                w_msg = msg
                                w_msg_type = "success" if ok else "error"
                                self.set_message(msg, w_msg_type)
                                if ok:
                                    self.refresh()
                        else:
                            if self._confirm(f"是否将 Windows 11 [{diag.boot_num}] 设为第一优先启动项？"):
                                ok, msg = Win11ActionHandler.set_win11_as_first_boot(diag)
                                w_msg = msg
                                w_msg_type = "success" if ok else "error"
                                self.set_message(msg, w_msg_type)
                                if ok:
                                    self.refresh()
                    elif action_idx == 2:
                        from src.bootloader import SystemdBootManager
                        sd_mgr = SystemdBootManager()
                        plan = sd_mgr.generate_windows_entry_diff()
                        agreed = self._show_diff_confirm_dialog(
                            plan["diff_lines"],
                            "跨 ESP 镜像并为 Linux 引导增加 Win11"
                        )
                        if not agreed:
                            w_msg = "用户已取消变更，未对系统进行修改。"
                            w_msg_type = "info"
                            return
                        ok, msg = sd_mgr.apply_windows_entry(plan)
                        w_msg = msg.splitlines()[0]
                        w_msg_type = "success" if ok else "error"
                        self.set_message(w_msg, w_msg_type)
                    elif action_idx == 3:
                        if self._confirm("是否重新创建 Windows Boot Manager 的 UEFI NVRAM 启动项？"):
                            ok, msg = Win11ActionHandler.restore_win11_nvram_entry(diag)
                            w_msg = msg
                            w_msg_type = "success" if ok else "error"
                            self.set_message(msg, w_msg_type)
                            if ok:
                                self.refresh()
                    elif action_idx == 4:
                        ok, msg = Win11ActionHandler.toggle_win11_active(diag)
                        w_msg = msg
                        w_msg_type = "success" if ok else "error"
                        self.set_message(msg, w_msg_type)
                        if ok:
                            self.refresh()
                    elif action_idx == 5:
                        return "RETURN"

                if key in [ord('q'), ord('Q'), 27]:
                    break
                elif key in [curses.KEY_UP, ord('k')]:
                    if scroll_offset > 0:
                        scroll_offset -= 1
                elif key in [curses.KEY_DOWN, ord('j')]:
                    if scroll_offset < max_scroll:
                        scroll_offset += 1
                elif key == curses.KEY_PPAGE:
                    scroll_offset = max(0, scroll_offset - avail_h)
                elif key == curses.KEY_NPAGE:
                    scroll_offset = min(max_scroll, scroll_offset + avail_h)
                elif key in [curses.KEY_RIGHT, 9]:  # Tab or Right
                    focused_btn = (focused_btn + 1) % 6
                elif key in [curses.KEY_LEFT, curses.KEY_BTAB]:  # Shift-Tab or Left
                    focused_btn = (focused_btn - 1) % 6
                elif key in [10, 13, curses.KEY_ENTER, ord(' ')]:
                    ret = execute_win_action(focused_btn)
                    if ret == "RETURN":
                        break
                elif key == ord('1'):
                    execute_win_action(0)
                elif key == ord('2'):
                    execute_win_action(1)
                elif key == ord('3'):
                    execute_win_action(2)
                elif key == ord('4'):
                    execute_win_action(3)
                elif key == ord('5'):
                    execute_win_action(4)
                elif key == curses.KEY_MOUSE:
                    try:
                        _, mx, my, _, bstate = curses.getmouse()
                        if bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED):
                            for bi, by, x1, x2 in btn_hitboxes:
                                if my == by and x1 <= mx <= x2:
                                    focused_btn = bi
                                    ret = execute_win_action(bi)
                                    if ret == "RETURN":
                                        return
                                    break
                    except Exception:
                        pass
            except curses.error:
                break

    def _input_dialog(self, prompt: str, default: str = "") -> Optional[str]:
        """Show an input dialog with visual buttons and mouse support. Returns input or None."""
        dialog_w = min(max(str_display_width(prompt) + 12, 54), self.max_x - 4)
        dialog_h = 8
        dialog_y = (self.max_y - dialog_h) // 2
        dialog_x = (self.max_x - dialog_w) // 2

        result = list(default)
        focused_btn = -1  # -1: input field, 0: 确认保存, 1: 取消返回

        while True:
            try:
                self.stdscr.erase()
                for y in range(dialog_h):
                    if y == 0:
                        self.stdscr.addstr(dialog_y + y, dialog_x,
                                           "┌" + "─" * (dialog_w - 2) + "┐",
                                           curses.color_pair(self.HEADER))
                    elif y == dialog_h - 1:
                        self.stdscr.addstr(dialog_y + y, dialog_x,
                                           "└" + "─" * (dialog_w - 2) + "┘",
                                           curses.color_pair(self.HEADER))
                    elif y == dialog_h - 3:
                        self.stdscr.addstr(dialog_y + y, dialog_x,
                                           "├" + "─" * (dialog_w - 2) + "┤",
                                           curses.color_pair(self.HEADER))
                    else:
                        self.stdscr.addstr(dialog_y + y, dialog_x,
                                           "│" + " " * (dialog_w - 2) + "│",
                                           curses.color_pair(self.HEADER))

                # Draw prompt
                prompt_display = pad_to_width(prompt, dialog_w - 6)
                self.stdscr.addstr(dialog_y + 1, dialog_x + 2, prompt_display,
                                   curses.color_pair(self.HIGHLIGHT))

                # Draw input field
                input_text = "".join(result)
                if len(input_text) > dialog_w - 6:
                    input_text = input_text[-(dialog_w - 6):]

                self.stdscr.addstr(dialog_y + 3, dialog_x + 2,
                                   " " * (dialog_w - 4))
                in_col = curses.color_pair(self.CURRENT) | curses.A_BOLD if focused_btn == -1 else curses.color_pair(self.CURRENT)
                self.stdscr.addstr(dialog_y + 3, dialog_x + 2, input_text, in_col)

                # Two visual buttons at bottom
                btn_y = dialog_y + dialog_h - 2
                btn_ok = " [ ▶ ✔ 确认保存 ◀ ] " if focused_btn == 0 else " [   ✔ 确认保存   ] "
                btn_cancel = " [ ▶ ✖ 取消返回 ◀ ] " if focused_btn == 1 else " [   ✖ 取消返回   ] "
                c_ok = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 0 else curses.color_pair(self.BUTTON_NORMAL)
                c_can = curses.color_pair(self.BUTTON_ACTIVE) | curses.A_BOLD if focused_btn == 1 else curses.color_pair(self.BUTTON_NORMAL)

                tot_btn_w = str_display_width(btn_ok) + 4 + str_display_width(btn_cancel)
                ok_x = dialog_x + max(2, (dialog_w - tot_btn_w) // 2)
                can_x = ok_x + str_display_width(btn_ok) + 4

                self.stdscr.addstr(btn_y, ok_x, btn_ok, c_ok)
                self.stdscr.addstr(btn_y, can_x, btn_cancel, c_can)

                self.stdscr.noutrefresh()
                curses.doupdate()

                key = self.stdscr.getch()
                if key == 27:
                    return None
                elif key == 9:  # Tab
                    if focused_btn == -1:
                        focused_btn = 0
                    elif focused_btn == 0:
                        focused_btn = 1
                    else:
                        focused_btn = -1
                elif key in [10, 13, curses.KEY_ENTER]:
                    if focused_btn == 1:
                        return None
                    return "".join(result)
                elif key in [curses.KEY_BACKSPACE, 127, 8]:
                    if result:
                        result.pop()
                elif key == curses.KEY_MOUSE:
                    try:
                        _, mx, my, _, bstate = curses.getmouse()
                        if bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED):
                            if my == btn_y:
                                if ok_x <= mx < ok_x + str_display_width(btn_ok):
                                    return "".join(result)
                                elif can_x <= mx < can_x + str_display_width(btn_cancel):
                                    return None
                            elif my == dialog_y + 3:
                                focused_btn = -1
                    except Exception:
                        pass
                elif 32 <= key <= 126 and focused_btn == -1:
                    if len(result) < 80:
                        result.append(chr(key))
            except curses.error:
                break
        return None

    def _dispatch_key_action(self, key_str: str) -> Optional[str]:
        """Dispatch action based on button string or key code."""
        k = key_str.lower().strip()
        if key_str in ["↑↓"]:
            pass
        elif key_str in ["回车", "\n"] or k in ["enter", "详情", "详细参数"]:
            self.show_detail()
        elif key_str in ["1", "置顶", "置顶首选"]:
            self.set_as_first()
        elif key_str in ["m/m", "m", "调序", "调整顺序"]:
            self.move_entry(-1)
        elif key_str in ["空格", " "] or k in ["space", "启禁"]:
            self.toggle_active()
        elif k in ["x", "x/右键", "菜单", "操作菜单"]:
            self.show_context_menu(self.selected_idx)
        elif k in ["v", "去重", "去重视图"]:
            self.dedup_mode = not self.dedup_mode
            self.refresh()
        elif k in ["c", "修复", "清理", "清理冗余", "修复/清理"]:
            entry = self.ordered_entries[self.selected_idx] if self.ordered_entries else None
            if isinstance(entry, DeduplicatedItem) and entry.has_duplicates:
                self.cleanup_duplicate_entries(entry)
            else:
                self.show_repair_dialog()
        elif k in ["t", "超时"]:
            self.set_timeout_dialog()
        elif k in ["k", "内核", "内核切换"]:
            self.show_kernel_dialog()
        elif k in ["b", "引导", "linux引导"]:
            self.show_bootloader_dialog()
        elif k in ["w", "win11", "win11分析"]:
            self.show_win11_dialog()
        elif k in ["e", "esp", "esp分区"]:
            self.show_esp_dialog()
        elif k in ["?", "帮助"]:
            self.show_help()
        elif k in ["q", "退出"]:
            return "QUIT"
        return None

    # ─── Main Loop ──────────────────────────────────────────────────────────

    def run(self):
        """Main application loop."""
        curses.curs_set(0)  # Hide cursor
        self.stdscr.nodelay(False)
        self.stdscr.keypad(True)

        while True:
            self.draw()

            key = self.stdscr.getch()
            if key == curses.ERR:
                # No input available (e.g. stdin at EOF); avoid a busy redraw loop
                time.sleep(0.1)
                continue

            # Mouse handling
            if key == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, bstate = curses.getmouse()
                    now = time.time()

                    # 1. Mouse wheel scrolling
                    if bstate & (getattr(curses, 'BUTTON4_PRESSED', 0) | 0x10000):
                        if self.selected_idx > 0:
                            self.selected_idx -= 1
                        continue
                    elif bstate & (getattr(curses, 'BUTTON5_PRESSED', 0) | 0x200000):
                        if self.selected_idx < len(self.ordered_entries) - 1:
                            self.selected_idx += 1
                        continue

                    # 2. Right click handling (BUTTON3 in curses: 0x400 | 0x800 | 0x1000 | 0x2000)
                    right_mask = (getattr(curses, "BUTTON3_CLICKED", 0x1000) | 
                                  getattr(curses, "BUTTON3_PRESSED", 0x800) | 
                                  getattr(curses, "BUTTON3_RELEASED", 0x400) | 
                                  getattr(curses, "BUTTON3_DOUBLE_CLICKED", 0x2000) | 0x3c00)
                    if bstate & right_mask:
                        last_rclick_t = getattr(self, "_last_rclick_time", 0.0)
                        if (now - last_rclick_t) < 0.15:
                            continue
                        self._last_rclick_time = now

                        clicked_idx = None
                        for row_y, abs_idx in getattr(self, "_table_row_hitboxes", []):
                            if my == row_y:
                                clicked_idx = abs_idx
                                break
                        if clicked_idx is not None and 0 <= clicked_idx < len(self.ordered_entries):
                            self.selected_idx = clicked_idx
                        self.show_context_menu(self.selected_idx, mx, my)
                        continue

                    # 3. Filter for left click events
                    click_mask = (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | 
                                  curses.BUTTON1_RELEASED | getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0))
                    if not (bstate & click_mask):
                        continue

                    # Debounce duplicate press/release events of same click (within 40ms)
                    last_event_t = getattr(self, "_last_mouse_event_time", 0.0)
                    if (now - last_event_t) < 0.04:
                        continue
                    self._last_mouse_event_time = now

                    # Check footer buttons
                    handled = False
                    for btn in getattr(self, "_footer_buttons", []):
                        if btn["y"] == my and btn["x_start"] <= mx <= btn["x_end"]:
                            act_res = self._dispatch_key_action(btn["key"])
                            if act_res == "QUIT":
                                return
                            handled = True
                            break

                    if not handled:
                        # Check main table row click & double click
                        clicked_idx = None
                        for row_y, abs_idx in getattr(self, "_table_row_hitboxes", []):
                            if my == row_y:
                                clicked_idx = abs_idx
                                break

                        if clicked_idx is not None and 0 <= clicked_idx < len(self.ordered_entries):
                            last_click_t = getattr(self, "_last_row_click_time", 0.0)
                            last_clicked_row = getattr(self, "_last_clicked_row", None)
                            time_diff = now - last_click_t

                            is_double = (
                                bool(bstate & getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0)) or
                                (last_clicked_row == clicked_idx and 0.02 <= time_diff <= 0.65)
                            )

                            if is_double:
                                self._last_row_click_time = 0.0
                                self._last_clicked_row = None
                                self.selected_idx = clicked_idx
                                self.show_detail()
                            else:
                                self._last_row_click_time = now
                                self._last_clicked_row = clicked_idx
                                self.selected_idx = clicked_idx
                            handled = True

                    if not handled:
                        # Check inspection card action buttons / double click
                        card_hb = getattr(self, "_card_hitbox", None)
                        if card_hb:
                            c_top, c_bottom = card_hb
                            if c_top <= my <= c_bottom:
                                action_clicked = False
                                for act_hb in getattr(self, "_card_action_hitboxes", []):
                                    if act_hb["y"] == my and act_hb["x1"] <= mx <= act_hb["x2"]:
                                        self._dispatch_key_action(act_hb["action"])
                                        action_clicked = True
                                        break
                                if not action_clicked:
                                    last_card_t = getattr(self, "_last_card_click_time", 0.0)
                                    if 0.02 <= (now - last_card_t) <= 0.65:
                                        self._last_card_click_time = 0.0
                                        self.show_detail()
                                    else:
                                        self._last_card_click_time = now
                except Exception:
                    pass
                continue

            # Navigation
            if key in [curses.KEY_DOWN, ord('j')]:
                if self.selected_idx < len(self.ordered_entries) - 1:
                    self.selected_idx += 1

            elif key == curses.KEY_UP:
                if self.selected_idx > 0:
                    self.selected_idx -= 1

            elif key == curses.KEY_NPAGE:  # Page Down
                page_size = (self.max_y - 12)
                self.selected_idx = min(self.selected_idx + page_size,
                                        max(0, len(self.ordered_entries) - 1))

            elif key == curses.KEY_PPAGE:  # Page Up
                page_size = (self.max_y - 12)
                self.selected_idx = max(self.selected_idx - page_size, 0)

            elif key == curses.KEY_HOME:
                self.selected_idx = 0

            elif key == curses.KEY_END:
                self.selected_idx = max(0, len(self.ordered_entries) - 1)

            # Actions
            elif key in [ord('m'), ord('M')]:
                direction = -1 if key == ord('m') else 1
                self.move_entry(direction)

            elif key == ord('1'):
                self.set_as_first()

            elif key in [ord('x'), ord('X')]:
                self.show_context_menu(self.selected_idx)

            elif key in [ord('v'), ord('V')]:
                self.dedup_mode = not self.dedup_mode
                self.refresh()

            elif key == ord('n'):
                self.set_next_boot()

            elif key == ord(' '):
                self.toggle_active()

            elif key == ord('d'):
                self.delete_entry()

            elif key == ord('t'):
                self.set_timeout_dialog()

            elif key == ord('\n') or key == curses.KEY_ENTER:
                self.show_detail()

            elif key in [ord('b'), ord('B')]:
                self.show_bootloader_dialog()

            elif key in [ord('k'), ord('K')]:
                self.show_kernel_dialog()

            elif key in [ord('w'), ord('W')]:
                self.show_win11_dialog()

            elif key in [ord('c'), ord('C')]:
                entry = self.ordered_entries[self.selected_idx] if self.ordered_entries else None
                if isinstance(entry, DeduplicatedItem) and entry.has_duplicates:
                    self.cleanup_duplicate_entries(entry)
                else:
                    self.show_repair_dialog()

            elif key in [ord('f'), ord('F')]:
                self.show_repair_dialog()

            elif key in [ord('e'), ord('E')]:
                self.show_esp_dialog()

            elif key == ord('?'):
                self.show_help()

            elif key == ord('r'):
                self.refresh()

            elif key in [ord('q'), ord('Q')]:
                break

            # Resize handling
            elif key == curses.KEY_RESIZE:
                self.max_y, self.max_x = self.stdscr.getmaxyx()


# ─── Entry Point ───────────────────────────────────────────────────────────────

def _check_privileges():
    """Require elevation on Windows; warn (but continue) without root on Linux."""
    if IS_WINDOWS:
        try:
            import ctypes
            if ctypes.windll.shell32.IsUserAnAdmin():
                return
            # Attempt automatic UAC elevation
            if os.path.exists(__file__):
                script_path = os.path.abspath(__file__)
                params = subprocess.list2cmdline([script_path] + sys.argv[1:])
                ret = ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", sys.executable, params, None, 1
                )
                if ret > 32:
                    sys.exit(0)
        except Exception:
            pass
        print("╔════════════════════════════════════════════════════╗")
        print("║  ✗  需要管理员权限 (Administrator)                ║")
        print("║  bcdedit 必须具备系统管理员权限方可读写 BCD 数据。 ║")
        print("║  请右键点击终端，选择「以管理员身份运行」后再启动。 ║")
        print("╚════════════════════════════════════════════════════╝")
        sys.exit(1)
    elif os.geteuid() != 0:
        if shutil.which("sudo") and sys.stdin.isatty():
            try:
                os.execvp("sudo", ["sudo", "-E", sys.executable] + sys.argv)
            except Exception:
                pass
        # 若 sudo 不可用或非 tty 环境，以普通用户继续运行 (只读查看)


def main():
    """Application entry point."""
    if curses is None:
        if IS_WINDOWS:
            print("错误: Windows 环境下需要安装 'windows-curses' 扩展包。",
                  file=sys.stderr)
            print("请执行命令安装:  pip install windows-curses", file=sys.stderr)
        else:
            print(f"错误: 无法加载 curses 终端图形库: {_CURSES_IMPORT_ERROR}",
                  file=sys.stderr)
        sys.exit(1)

    _check_privileges()

    # PDCurses reads the console directly, so no /dev/tty rebind is needed on
    # Windows even when stdin is piped.
    if not IS_WINDOWS and not sys.stdin.isatty():
        try:
            # Rebind fd 0 to the controlling terminal; replacing the sys.stdin
            # object alone does not change what ncurses reads from.
            tty_fd = os.open("/dev/tty", os.O_RDONLY)
            os.dup2(tty_fd, 0)
            os.close(tty_fd)
            sys.stdin = open("/dev/tty")
        except OSError:
            print("Error: No terminal available for interactive input.", file=sys.stderr)
            sys.exit(1)

    try:
        curses.wrapper(lambda stdscr: BootOrderTUI(stdscr).run())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()