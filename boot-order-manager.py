#!/usr/bin/env python3
"""
Boot Order Manager - TUI for managing UEFI boot entries
Requires: efibootmgr, Python 3, root privileges for write operations

One-click launch:
  curl -sL https://raw.githubusercontent.com/furina707/Boot-Order/master/boot-order-manager.py | sudo python3 -
"""

import curses
import subprocess
import re
import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ─── Data Models ──────────────────────────────────────────────────────────────

@dataclass
class BootEntry:
    """Represents a single UEFI boot entry."""
    boot_num: str          # e.g., "0000"
    label: str             # e.g., "Windows Boot Manager"
    active: bool           # active (has '*') or not
    is_current: bool = False
    is_next: bool = False


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
        entry_map = {e.boot_num: e for e in self.entries}
        ordered = []
        for num in self.boot_order:
            if num in entry_map:
                ordered.append(entry_map[num])
        # Add entries not in boot_order at the end
        for e in self.entries:
            if e.boot_num not in self.boot_order:
                ordered.append(e)
        return ordered


# ─── EFI Operations ────────────────────────────────────────────────────────────

EFIBOOTMGR = "/usr/sbin/efibootmgr"


def run_efibootmgr(args: List[str]) -> Tuple[str, str, int]:
    """Run efibootmgr and return (stdout, stderr, returncode)."""
    cmd = [EFIBOOTMGR] + args
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


def get_boot_config() -> Optional[BootConfig]:
    """Fetch and parse current boot configuration."""
    stdout, stderr, rc = run_efibootmgr(["-v"])
    if rc != 0:
        return None
    return parse_boot_config(stdout)


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
            )
            config.entries.append(entry)

    return config


def set_boot_order(order: List[str]) -> Tuple[str, int]:
    """Set the boot order. Returns (output, returncode)."""
    args = ["-o"] + [",".join(order)]
    stdout, stderr, rc = run_efibootmgr(args)
    return stdout + stderr, rc


def set_boot_next(boot_num: str) -> Tuple[str, int]:
    """Set the boot entry for next boot only."""
    args = ["-n", boot_num]
    stdout, stderr, rc = run_efibootmgr(args)
    return stdout + stderr, rc


def set_active(boot_num: str, active: bool) -> Tuple[str, int]:
    """Enable or disable a boot entry."""
    flag = "-a" if active else "-A"
    stdout, stderr, rc = run_efibootmgr([flag, boot_num])
    return stdout + stderr, rc


def delete_boot_entry(boot_num: str) -> Tuple[str, int]:
    """Delete a boot entry."""
    stdout, stderr, rc = run_efibootmgr(["-b", boot_num, "-B"])
    return stdout + stderr, rc


def set_timeout(seconds: int) -> Tuple[str, int]:
    """Set the boot manager timeout."""
    stdout, stderr, rc = run_efibootmgr(["-t", str(seconds)])
    return stdout + stderr, rc


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
        curses.init_pair(self.STATUS_BAR, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(self.TITLE, curses.COLOR_YELLOW, -1)
        curses.init_pair(self.WARNING, curses.COLOR_RED, curses.COLOR_YELLOW)

    def refresh(self):
        """Reload boot configuration."""
        self.config = get_boot_config()
        if self.config:
            self.ordered_entries = self.config.get_ordered_entries()
            self.selected_idx = min(self.selected_idx, max(0, len(self.ordered_entries) - 1))
            self.message = f"Configuration loaded ({len(self.ordered_entries)} entries)"
            self.message_type = "success"
        else:
            self.ordered_entries = []
            self.message = "Failed to load boot configuration. Try running with sudo."
            self.message_type = "error"

    # ─── Drawing Methods ────────────────────────────────────────────────────

    def draw(self):
        """Main draw method."""
        self.max_y, self.max_x = self.stdscr.getmaxyx()
        self.stdscr.clear()

        if self.max_y < 10 or self.max_x < 50:
            self._draw_min_size_warning()
            return

        self._draw_header()
        self._draw_info_bar()
        self._draw_table()
        self._draw_footer()
        self._draw_message()
        self.stdscr.noutrefresh()

    def _draw_min_size_warning(self):
        """Draw warning when terminal is too small."""
        warn = "Terminal too small. Minimum 50x10 required."
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

            # Mode indicator
            if self.config:
                self.stdscr.addstr(1, self.max_x - len(mode) - 3, mode,
                                   curses.color_pair(self.CURRENT))
            else:
                self.stdscr.addstr(1, self.max_x - len(mode) - 3, mode,
                                   curses.color_pair(self.INACTIVE))

            self.stdscr.addstr(1, self.max_x - 2, "│", curses.color_pair(self.HEADER))
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
        timeout = f" Timeout: {self.config.timeout or 'N/A'}s "
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
                self.stdscr.addstr(6, 2, "Try running: sudo " + os.path.basename(sys.argv[0]),
                                   curses.color_pair(self.HELP))
            except curses.error:
                pass
            return

        # Table dimensions
        table_top = 5
        header_height = 2
        available_height = self.max_y - table_top - header_height - 4  # -4 for footer and message

        num_col = 8  # column for '#'
        num_width = 6  # width for boot number column
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
            self.stdscr.addstr(table_top + 1, self.max_x - 2, "│",
                               curses.color_pair(self.HEADER))
            self.stdscr.addstr(table_top + 2, 0, f"│{'─' * (self.max_x - 4)}│",
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
                self.stdscr.addstr(row, 1, " " * (self.max_x - 2))
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
            ("Space", "Toggle"),
            ("d", "Delete"),
            ("t", "Timeout"),
            ("r", "Refresh"),
            ("q", "Quit"),
        ]

        try:
            self.stdscr.addstr(footer_y, 0, "┌" + "─" * (self.max_x - 2) + "┐",
                               curses.color_pair(self.HEADER))

            footer_text = "│ "
            x = 2
            for key, action in controls:
                piece = f"[{key}] {action} "
                try:
                    self.stdscr.addstr(footer_y + 1, x, "│ ", curses.color_pair(self.HEADER))
                    x += 2
                    self.stdscr.addstr(footer_y + 1, x, f"[{key}]",
                                       curses.color_pair(self.HIGHLIGHT) | curses.A_BOLD)
                    x += len(key) + 2
                    self.stdscr.addstr(footer_y + 1, x, f" {action} ")
                    x += len(action) + 2
                except curses.error:
                    break

            try:
                self.stdscr.addstr(footer_y + 1, self.max_x - 2, "│",
                                   curses.color_pair(self.HEADER))
            except curses.error:
                pass

            self.stdscr.addstr(footer_y + 2, 0, "└" + "─" * (self.max_x - 2) + "┘",
                               curses.color_pair(self.HEADER))
        except curses.error:
            pass

    def _draw_message(self):
        """Draw status message at the bottom."""
        if not self.message:
            return

        msg_y = self.max_y - 1
        color = curses.color_pair(self.CURRENT) | curses.A_BOLD
        if self.message_type == "error":
            color = curses.color_pair(self.INACTIVE) | curses.A_BOLD
        elif self.message_type == "success":
            color = curses.color_pair(self.ACTIVE) | curses.A_BOLD

        msg = f" {self.message} "
        try:
            self.stdscr.addstr(msg_y, 0, " " * (self.max_x - 1))
            self.stdscr.addstr(msg_y, 0, msg[:self.max_x - 1], color)
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
        if entry.boot_num in self.config.boot_order:
            old_pos = self.config.boot_order.index(entry.boot_num)
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
            self.config.boot_order.insert(0, entry.boot_num)

        # Apply changes
        output, rc = set_boot_order(self.config.boot_order)
        if rc == 0:
            self.selected_idx = new_idx
            self.set_message(f"Moved '{entry.label}' {'up' if direction < 0 else 'down'}.", "success")
        else:
            self.set_message(f"Failed to change boot order: {output.strip()}", "error")

        self.refresh()

    def set_as_first(self):
        """Set the selected entry as the first boot entry."""
        if not self.config or not self.ordered_entries:
            return

        entry = self.ordered_entries[self.selected_idx]
        if not entry.active:
            self.set_message(f"Entry '{entry.label}' is inactive. Activate it first.", "error")
            return

        # Move to front of boot_order
        if entry.boot_num in self.config.boot_order:
            self.config.boot_order.remove(entry.boot_num)
        self.config.boot_order.insert(0, entry.boot_num)

        output, rc = set_boot_order(self.config.boot_order)
        if rc == 0:
            self.set_message(f"Set '{entry.label}' as the first boot entry.", "success")
        else:
            self.set_message(f"Failed: {output.strip()}", "error")

        self.refresh()

    def toggle_active(self):
        """Toggle active/inactive state of the selected entry."""
        if not self.config or not self.ordered_entries:
            return

        entry = self.ordered_entries[self.selected_idx]
        new_active = not entry.active

        output, rc = set_active(entry.boot_num, new_active)
        if rc == 0:
            status = "activated" if new_active else "deactivated"
            self.set_message(f"{status.capitalize()} '{entry.label}'.", "success")
        else:
            self.set_message(f"Failed: {output.strip()}", "error")

        self.refresh()

    def delete_entry(self):
        """Delete the selected boot entry after confirmation."""
        if not self.config or not self.ordered_entries:
            return

        entry = self.ordered_entries[self.selected_idx]
        if self._confirm(f"Delete boot entry '{entry.label}' ({entry.boot_num})?"):
            output, rc = delete_boot_entry(entry.boot_num)
            if rc == 0:
                self.set_message(f"Deleted '{entry.label}'.", "success")
            else:
                self.set_message(f"Failed: {output.strip()}", "error")
            self.refresh()

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

        # Try to get verbose info
        stdout, stderr, rc = run_efibootmgr(["-v", "-b", entry.boot_num])
        if rc == 0:
            for line in stdout.splitlines():
                if entry.boot_num in line:
                    # Extract path info
                    parts = line.split(entry.boot_num, 1)
                    if len(parts) > 1:
                        details.append(f"Path:         {parts[1].strip()}")

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
            "Note: Write operations require root privileges.",
            "      Run with: sudo python3 boot-order-manager.py",
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

                output, rc = set_timeout(seconds)
                if rc == 0:
                    self.set_message(f"Timeout set to {seconds} seconds.", "success")
                else:
                    self.set_message(f"Failed: {output.strip()}", "error")
                self.refresh()
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
            self.stdscr.refresh()

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

                self.stdscr.refresh()

                key = self.stdscr.getch()
                if key in [ord('q'), ord('Q'), 27, ord('\n'), ord(' ')]:  # q, ESC, Enter, Space
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

                self.stdscr.refresh()

                key = self.stdscr.getch()
                if key == 27:  # ESC
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
            self.stdscr.refresh()

            key = self.stdscr.getch()

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

def main():
    """Application entry point."""
    if not sys.stdin.isatty():
        try:
            sys.stdin = open("/dev/tty")
        except OSError:
            print("Error: No terminal available for interactive input.", file=sys.stderr)
            sys.exit(1)

    if os.geteuid() != 0:
        print("╔════════════════════════════════════════════════════╗")
        print("║  ⚠  Note: Running without root privileges        ║")
        print("║  Write operations (reorder, delete, etc.)        ║")
        print("║  require root access. Use: sudo                   ║")
        print("╚════════════════════════════════════════════════════╝")
        print()

    try:
        curses.wrapper(lambda stdscr: BootOrderTUI(stdscr).run())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()