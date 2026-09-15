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

IS_WINDOWS = os.name == "nt"


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
        """Return entries sorted by boot order."""
        entry_map = {(e.ident or e.boot_num): e for e in self.entries}
        ordered = []
        for num in self.boot_order:
            if num in entry_map:
                ordered.append(entry_map[num])
        # Add entries not in boot_order at the end
        for e in self.entries:
            if (e.ident or e.boot_num) not in self.boot_order:
                ordered.append(e)
        return ordered


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
        self.ordered_entries: List[BootEntry] = []
        self.selected_idx = 0
        self.scroll_offset = 0
        self.message = ""
        self.message_type = "info"  # "info", "error", "success"

        # Terminal dimensions
        self.max_y, self.max_x = stdscr.getmaxyx()

        # Initialize colors
        self._init_colors()

        # Load initial data
        self.refresh()

    def _init_colors(self):
        """Initialize color pairs."""
        curses.start_color()
        curses.use_default_colors()

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

    def refresh(self, keep_message: bool = False):
        """Reload boot configuration.

        keep_message=True preserves any pending action feedback (success/error)
        that was set before the reload.
        """
        self.config = BACKEND.get_boot_config()
        if self.config:
            self.ordered_entries = self.config.get_ordered_entries()
            self.selected_idx = min(self.selected_idx, max(0, len(self.ordered_entries) - 1))
            if not keep_message:
                self.message = f"Configuration loaded ({len(self.ordered_entries)} entries)"
                self.message_type = "success"
        else:
            self.ordered_entries = []
            if IS_WINDOWS:
                self.message = "Failed to load boot configuration (run as Administrator)."
            else:
                self.message = "Failed to load boot configuration. Try running with sudo."
            self.message_type = "error"

    # ─── Drawing Methods ────────────────────────────────────────────────────

    def draw(self):
        """Main draw method."""
        self.max_y, self.max_x = self.stdscr.getmaxyx()
        self.stdscr.erase()

        if self.max_y < 13 or self.max_x < 50:
            self._draw_min_size_warning()
            return

        self._draw_header()
        self._draw_info_bar()
        self._draw_table()
        self._draw_footer()
        self._draw_message()
        self.stdscr.noutrefresh()
        curses.doupdate()

    def _draw_min_size_warning(self):
        """Draw warning when terminal is too small."""
        warn = "Terminal too small. Minimum 50x13 required."
        try:
            self.stdscr.addstr(0, 0, warn, curses.color_pair(self.WARNING) | curses.A_BOLD)
        except curses.error:
            pass

    def _draw_header(self):
        """Draw the title bar."""
        title = " UEFI Boot Order Manager "
        mode = " [UEFI 64-bit] " if self.config else " [No EFI Data] "

        try:
            self.stdscr.addstr(0, 0, "┌" + "─" * (self.max_x - 2) + "┐",
                               curses.color_pair(self.HEADER))
            self.stdscr.addstr(1, 0, "│", curses.color_pair(self.HEADER))
            self.stdscr.addstr(1, 2, title, curses.color_pair(self.TITLE) | curses.A_BOLD)

            # Mode indicator, flush against the right border
            mode_color = curses.color_pair(self.CURRENT) if self.config \
                else curses.color_pair(self.INACTIVE)
            self.stdscr.addstr(1, self.max_x - 1 - len(mode), mode, mode_color)

            self.stdscr.addstr(1, self.max_x - 1, "│", curses.color_pair(self.HEADER))
            self.stdscr.addstr(2, 0, "└" + "─" * (self.max_x - 2) + "┘",
                               curses.color_pair(self.HEADER))
        except curses.error:
            pass

    def _draw_info_bar(self):
        """Draw system info bar."""
        if not self.config:
            return

        boot_current = f" Current: {self.config.boot_current or 'N/A'} "
        boot_next = f" Next: {self.config.boot_next or 'N/A'} "
        if self.config.timeout is not None:
            timeout = f" Timeout: {self.config.timeout}s "
        else:
            timeout = " Timeout: N/A "
        entry_count = f" Entries: {len(self.config.entries)} "

        info = f" {boot_current}│{boot_next}│{timeout}│{entry_count} "

        try:
            self.stdscr.addstr(3, 0, " " * (self.max_x - 1),
                               curses.color_pair(self.STATUS_BAR))
            self.stdscr.addstr(3, 0, info[:self.max_x - 1],
                               curses.color_pair(self.STATUS_BAR))
        except curses.error:
            pass

    def _draw_table(self):
        """Draw the boot entries table."""
        if not self.ordered_entries:
            try:
                self.stdscr.addstr(5, 2, "No boot entries found or unable to read EFI variables.",
                                   curses.color_pair(self.INACTIVE))
                hint = ("Run the terminal as Administrator." if IS_WINDOWS
                        else "Try running: sudo " + os.path.basename(sys.argv[0]))
                self.stdscr.addstr(6, 2, hint, curses.color_pair(self.HELP))
            except curses.error:
                pass
            return

        # Table dimensions
        table_top = 5
        header_height = 2
        available_height = self.max_y - table_top - header_height - 5  # table bottom border + footer + message

        num_col = 8  # column for '#'
        # Windows entries are shown by the first 8 GUID chars, so widen as needed
        num_width = max(6, max((len(e.boot_num) for e in self.ordered_entries),
                               default=5) + 1)
        active_width = 8  # width for active column

        # Calculate label width
        label_width = self.max_x - num_col - num_width - active_width - 12  # padding

        if label_width < 10:
            label_width = 10

        # Draw table header
        header = (f"  {'#':>3} │ {'Boot Num':<{num_width}} │ {'Label':<{label_width}} │ {'Active':<{active_width}} │ {'Status':<{10}}  ")

        try:
            self.stdscr.addstr(table_top, 0, f"┌{'─' * (self.max_x - 2)}┐",
                               curses.color_pair(self.HEADER))
            self.stdscr.addstr(table_top + 1, 0, f"│", curses.color_pair(self.HEADER))
            self.stdscr.addstr(table_top + 1, 1, header[:self.max_x - 2],
                               curses.color_pair(self.HEADER) | curses.A_BOLD)
            self.stdscr.addstr(table_top + 1, self.max_x - 1, "│",
                               curses.color_pair(self.HEADER))
            self.stdscr.addstr(table_top + 2, 0, f"│{'─' * (self.max_x - 2)}│",
                               curses.color_pair(self.HEADER))
        except curses.error:
            pass

        # Adjust scroll offset
        if self.selected_idx < self.scroll_offset:
            self.scroll_offset = self.selected_idx
        elif self.selected_idx >= self.scroll_offset + available_height:
            self.scroll_offset = self.selected_idx - available_height + 1

        # Draw entries
        display_entries = self.ordered_entries[self.scroll_offset:self.scroll_offset + available_height]

        for i, entry in enumerate(display_entries):
            row = table_top + 3 + i
            abs_idx = self.scroll_offset + i
            is_selected = (abs_idx == self.selected_idx)

            # Determine entry color
            if is_selected:
                entry_color = curses.color_pair(self.SELECTED)
            elif entry.is_current:
                entry_color = curses.color_pair(self.CURRENT)
            elif entry.active:
                entry_color = curses.color_pair(self.ACTIVE)
            else:
                entry_color = curses.color_pair(self.INACTIVE)

            # Boot number display
            boot_num_str = f"{'*' if entry.active else ' '}{entry.boot_num}"

            # Status indicators
            status_parts = []
            if entry.is_current:
                status_parts.append("CURRENT")
            if entry.is_next:
                status_parts.append("NEXT")
            status = ",".join(status_parts) if status_parts else ""

            # Truncate label if needed
            label = entry.label[:label_width]

            row_str = (
                f"  {abs_idx + 1:>3} │ {boot_num_str:<{num_width}} │ "
                f"{label:<{label_width}} │ "
                f"{'Yes' if entry.active else 'No':<{active_width}} │ "
                f"{status:<{10}}  "
            )

            # Row borders
            try:
                self.stdscr.addstr(row, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(row, self.max_x - 1, "│", curses.color_pair(self.HEADER))
            except curses.error:
                pass

            # Highlight current boot entry
            if is_selected:
                # Clear line with highlight background
                try:
                    self.stdscr.addstr(row, 1, " " * (self.max_x - 2), entry_color)
                    self.stdscr.addstr(row, 1, row_str[:self.max_x - 2], entry_color)
                except curses.error:
                    pass
            else:
                try:
                    self.stdscr.addstr(row, 1, row_str[:self.max_x - 2], entry_color)
                except curses.error:
                    pass

        # Clear remaining lines
        for i in range(len(display_entries), available_height):
            row = table_top + 3 + i
            try:
                self.stdscr.addstr(row, 0, "│", curses.color_pair(self.HEADER))
                self.stdscr.addstr(row, 1, " " * (self.max_x - 2))
                self.stdscr.addstr(row, self.max_x - 1, "│", curses.color_pair(self.HEADER))
            except curses.error:
                pass

        # Table bottom border
        try:
            self.stdscr.addstr(table_top + 3 + available_height, 0,
                               "└" + "─" * (self.max_x - 2) + "┘",
                               curses.color_pair(self.HEADER))
        except curses.error:
            pass

    def _draw_footer(self):
        """Draw the footer with key bindings."""
        footer_y = self.max_y - 3

        controls = [
            ("↑↓", "Nav"),
            ("Enter", "Detail"),
            ("m/M", "Move"),
            ("1", "First"),
            ("n", "Next"),
            ("Space", "Toggle"),
            ("d", "Delete"),
            ("t", "Timeout"),
            ("r", "Refresh"),
            ("q", "Quit"),
        ]

        try:
            self.stdscr.addstr(footer_y, 0, "┌" + "─" * (self.max_x - 2) + "┐",
                               curses.color_pair(self.HEADER))
            self.stdscr.addstr(footer_y + 1, 0, "│", curses.color_pair(self.HEADER))

            x = 2
            for i, (key, action) in enumerate(controls):
                try:
                    if i > 0:
                        self.stdscr.addstr(footer_y + 1, x, "│ ",
                                           curses.color_pair(self.HEADER))
                        x += 2
                    self.stdscr.addstr(footer_y + 1, x, f"[{key}]",
                                       curses.color_pair(self.HIGHLIGHT) | curses.A_BOLD)
                    x += len(key) + 2
                    self.stdscr.addstr(footer_y + 1, x, f" {action} ")
                    x += len(action) + 2
                except curses.error:
                    break

            self.stdscr.addstr(footer_y + 1, self.max_x - 1, "│",
                               curses.color_pair(self.HEADER))

            self.stdscr.addstr(footer_y + 2, 0, "└" + "─" * (self.max_x - 2) + "┘",
                               curses.color_pair(self.HEADER))
        except curses.error:
            pass

    def _draw_message(self):
        """Draw status message at the bottom."""
        if not self.message:
            return

        color = curses.color_pair(self.CURRENT) | curses.A_BOLD
        if self.message_type == "error":
            color = curses.color_pair(self.INACTIVE) | curses.A_BOLD
        elif self.message_type == "success":
            color = curses.color_pair(self.ACTIVE) | curses.A_BOLD

        # Full-width colored strip along the bottom edge; it overlays the
        # footer box's bottom border row, acting as the status bar.
        msg = f" {self.message} "[:self.max_x - 1].ljust(self.max_x - 1)
        try:
            self.stdscr.addstr(self.max_y - 1, 0, msg, color)
        except curses.error:
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
            self.set_message("Already at the edge of the list.", "info")
            return

        entry = self.ordered_entries[idx]
        # Swap in boot_order list
        if entry.ident in self.config.boot_order:
            old_pos = self.config.boot_order.index(entry.ident)
            new_pos = old_pos + direction
            if new_pos < 0 or new_pos >= len(self.config.boot_order):
                self.set_message("Cannot move this entry further.", "info")
                return
            # Swap the two entries in boot_order
            other_num = self.config.boot_order[new_pos]
            self.config.boot_order[new_pos] = self.config.boot_order[old_pos]
            self.config.boot_order[old_pos] = other_num
        else:
            # Entry not in boot order, add it
            self.config.boot_order.insert(0, entry.ident)

        # Apply changes
        output, rc = BACKEND.set_boot_order(self.config.boot_order)
        if rc == 0:
            self.selected_idx = new_idx
            self.set_message(f"Moved '{entry.label}' {'up' if direction < 0 else 'down'}.", "success")
        else:
            self.set_message(f"Failed to change boot order: {output.strip()}", "error")

        self.refresh(keep_message=True)

    def set_as_first(self):
        """Set the selected entry as the first boot entry."""
        if not self.config or not self.ordered_entries:
            return

        entry = self.ordered_entries[self.selected_idx]
        if not entry.active:
            self.set_message(f"Entry '{entry.label}' is inactive. Activate it first.", "error")
            return

        # Move to front of boot_order
        if entry.ident in self.config.boot_order:
            self.config.boot_order.remove(entry.ident)
        self.config.boot_order.insert(0, entry.ident)

        output, rc = BACKEND.set_boot_order(self.config.boot_order)
        if rc == 0:
            self.set_message(f"Set '{entry.label}' as the first boot entry.", "success")
        else:
            self.set_message(f"Failed: {output.strip()}", "error")

        self.refresh(keep_message=True)

    def toggle_active(self):
        """Toggle active/inactive state of the selected entry."""
        if not self.config or not self.ordered_entries:
            return

        if not BACKEND.supports_active_toggle:
            self.set_message("Not supported on this platform: bcdedit cannot "
                             "enable/disable firmware boot entries.", "error")
            return

        entry = self.ordered_entries[self.selected_idx]
        new_active = not entry.active

        output, rc = BACKEND.set_active(entry.ident, new_active)
        if rc == 0:
            status = "activated" if new_active else "deactivated"
            self.set_message(f"{status.capitalize()} '{entry.label}'.", "success")
        else:
            self.set_message(f"Failed: {output.strip()}", "error")

        self.refresh(keep_message=True)

    def delete_entry(self):
        """Delete the selected boot entry after confirmation."""
        if not self.config or not self.ordered_entries:
            return

        entry = self.ordered_entries[self.selected_idx]
        if self._confirm(f"Delete boot entry '{entry.label}' ({entry.boot_num})?"):
            output, rc = BACKEND.delete_boot_entry(entry.ident)
            if rc == 0:
                self.set_message(f"Deleted '{entry.label}'.", "success")
            else:
                self.set_message(f"Failed: {output.strip()}", "error")
            self.refresh(keep_message=True)

    def set_next_boot(self):
        """Set the selected entry as the one-time boot entry for next restart."""
        if not self.config or not self.ordered_entries:
            return

        entry = self.ordered_entries[self.selected_idx]
        output, rc = BACKEND.set_boot_next(entry.ident)
        if rc == 0:
            self.set_message(f"'{entry.label}' will boot once on next restart.", "success")
        else:
            self.set_message(f"Failed: {output.strip()}", "error")

        self.refresh(keep_message=True)

    def show_detail(self):
        """Show detailed information about the selected entry."""
        if not self.ordered_entries:
            return

        entry = self.ordered_entries[self.selected_idx]
        details = [
            f"Boot Entry Detail",
            f"{'─' * 40}",
            f"Boot Number:  {entry.boot_num}",
            f"Label:        {entry.label}",
            f"Active:       {'Yes' if entry.active else 'No'}",
            f"Current Boot: {'Yes' if entry.is_current else 'No'}",
            f"Next Boot:    {'Yes' if entry.is_next else 'No'}",
        ]

        if entry.path:
            details.append(f"Path:         {entry.path}")

        self._show_info_popup(details, f"Boot Entry: {entry.label}")

    def show_help(self):
        """Show help screen."""
        help_lines = [
            "UEFI Boot Order Manager - Help",
            "",
            "Navigation:",
            "  ↑/↓  or  k/j    Navigate through boot entries",
            "  PgUp/PgDn        Scroll faster",
            "  Home/End         Jump to first/last entry",
            "",
            "Operations:",
            "  m               Move entry UP in boot order",
            "  M               Move entry DOWN in boot order",
            "  1               Set entry as first boot device",
            "  n               Boot entry once on next restart",
            "  Space           Toggle entry active/inactive",
            "  d               Delete boot entry (with confirmation)",
            "  t               Set boot menu timeout",
            "  Enter           Show detailed entry information",
            "",
            "Other:",
            "  r               Refresh configuration",
            "  ?               Show this help screen",
            "  q               Quit application",
            "",
            "Note: Elevated privileges are required to write:",
            "      Linux:   sudo python3 boot-order-manager.py",
            "      Windows: run the terminal as Administrator",
        ]
        self._show_info_popup(help_lines, "Help")

    def set_timeout_dialog(self):
        """Interactive dialog to set boot timeout."""
        if not self.config:
            return

        current = self.config.timeout or 0
        result = self._input_dialog(
            f"Set timeout in seconds (current: {current}s):",
            str(current)
        )

        if result is not None:
            try:
                seconds = int(result)
                if seconds < 0:
                    self.set_message("Timeout must be >= 0.", "error")
                    return
                if seconds > 3600:
                    self.set_message("Timeout too large (max 3600).", "error")
                    return

                output, rc = BACKEND.set_timeout(seconds)
                if rc == 0:
                    self.set_message(f"Timeout set to {seconds} seconds.", "success")
                else:
                    self.set_message(f"Failed: {output.strip()}", "error")
                self.refresh(keep_message=True)
            except ValueError:
                self.set_message("Invalid number.", "error")

    def _confirm(self, question: str) -> bool:
        """Show a confirmation dialog. Returns True if confirmed."""
        dialog_h = 5
        dialog_w = min(len(question) + 12, self.max_x - 4)
        dialog_y = (self.max_y - dialog_h) // 2
        dialog_x = (self.max_x - dialog_w) // 2

        # Calculate the text that fits
        text = f" {question} (y/N) "
        if len(text) > dialog_w - 2:
            text = text[:dialog_w - 6] + "..."

        try:
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

            # Draw text
            self.stdscr.addstr(dialog_y + dialog_h // 2, dialog_x + 2,
                               text, curses.color_pair(self.HIGHLIGHT) | curses.A_BOLD)
            self.stdscr.noutrefresh()
            curses.doupdate()

            # Wait for keypress
            key = self.stdscr.getch()
            return key in [ord('y'), ord('Y')]
        except curses.error:
            return False

    def _show_info_popup(self, lines: List[str], title: str):
        """Show an information popup with scroll support."""
        content_width = max(len(l) for l in lines) if lines else 40
        popup_w = min(content_width + 6, self.max_x - 4)
        popup_h = min(len(lines) + 4, self.max_y - 4)

        popup_y = (self.max_y - popup_h) // 2
        popup_x = (self.max_x - popup_w) // 2

        scroll_offset = 0
        max_scroll = max(0, len(lines) - (popup_h - 3))

        while True:
            try:
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
                    self.stdscr.addstr(popup_y, popup_x + max(2, (popup_w - len(title_str)) // 2),
                                       title_str, curses.color_pair(self.TITLE) | curses.A_BOLD)

                # Draw content with scrolling
                visible_lines = lines[scroll_offset:scroll_offset + popup_h - 3]
                for i, line in enumerate(visible_lines):
                    display_line = line[:popup_w - 4]
                    color = curses.color_pair(self.ACTIVE) if i == 0 and scroll_offset == 0 else curses.A_NORMAL
                    self.stdscr.addstr(popup_y + 1 + i, popup_x + 2, display_line, color)

                # Scroll indicator
                if max_scroll > 0:
                    scroll_info = f" [Lines {scroll_offset + 1}-{scroll_offset + len(visible_lines)}/{len(lines)}] "
                    try:
                        self.stdscr.addstr(popup_y + popup_h - 1, popup_x + popup_w - len(scroll_info) - 2,
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

            except curses.error:
                break

    def _input_dialog(self, prompt: str, default: str = "") -> Optional[str]:
        """Show an input dialog. Returns the input string or None if cancelled."""
        dialog_w = min(len(prompt) + 20, self.max_x - 4)
        dialog_h = 7
        dialog_y = (self.max_y - dialog_h) // 2
        dialog_x = (self.max_x - dialog_w) // 2

        result = list(default)

        while True:
            try:
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

                # Draw prompt
                prompt_display = prompt[:dialog_w - 6]
                self.stdscr.addstr(dialog_y + 1, dialog_x + 2, prompt_display,
                                   curses.color_pair(self.HIGHLIGHT))

                # Draw input field
                input_text = "".join(result)
                if len(input_text) > dialog_w - 6:
                    input_text = input_text[-(dialog_w - 6):]

                self.stdscr.addstr(dialog_y + 3, dialog_x + 2,
                                   " " * (dialog_w - 4))
                self.stdscr.addstr(dialog_y + 3, dialog_x + 2,
                                   input_text, curses.color_pair(self.CURRENT) | curses.A_BOLD)

                # Draw hint
                hint = " [Enter] confirm  [Esc] cancel "
                self.stdscr.addstr(dialog_y + 5, dialog_x + 2, hint,
                                   curses.color_pair(self.HELP))

                self.stdscr.noutrefresh()
                curses.doupdate()

                key = self.stdscr.getch()
                if key == 27:
                    return None
                elif key in [10, 13, curses.KEY_ENTER]:  # Enter
                    return "".join(result)
                elif key in [curses.KEY_BACKSPACE, 127, 8]:
                    if result:
                        result.pop()
                elif 32 <= key <= 126:  # Printable characters
                    if len(result) < 20:  # Limit input length
                        result.append(chr(key))

            except curses.error:
                break

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

            # Navigation
            if key in [curses.KEY_DOWN, ord('j')]:
                if self.selected_idx < len(self.ordered_entries) - 1:
                    self.selected_idx += 1

            elif key in [curses.KEY_UP, ord('k')]:
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
        except Exception:
            pass
        print("╔════════════════════════════════════════════════════╗")
        print("║  ✗  Administrator privileges required              ║")
        print("║  bcdedit cannot access the BCD store without them. ║")
        print("║  Re-open the terminal via right-click:             ║")
        print("║  'Run as administrator', then start again.         ║")
        print("╚════════════════════════════════════════════════════╝")
        sys.exit(1)
    elif os.geteuid() != 0:
        print("╔════════════════════════════════════════════════════╗")
        print("║  ⚠  Note: Running without root privileges        ║")
        print("║  Write operations (reorder, delete, etc.)        ║")
        print("║  require root access. Use: sudo                   ║")
        print("╚════════════════════════════════════════════════════╝")
        print()


def main():
    """Application entry point."""
    if curses is None:
        if IS_WINDOWS:
            print("Error: the 'windows-curses' package is required on Windows.",
                  file=sys.stderr)
            print("Install it with:  pip install windows-curses", file=sys.stderr)
        else:
            print(f"Error: unable to import curses: {_CURSES_IMPORT_ERROR}",
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