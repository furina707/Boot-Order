//! systemd-boot / Linux 内层系统引导项管理器
//!
//! 负责解析与维护 ESP 分区中的内层引导配置：
//! - 全局配置文件: /boot/loader/loader.conf (default, timeout, console-mode)
//! - 条目配置文件: /boot/loader/entries/*.conf (title, linux, initrd, options, efi)
//! - 通过 sudo 管道安全读写更新

const std = @import("std");
const sys = @import("sys.zig");
const Io = std.Io;

pub const DEFAULT_SUDO_PASS: []const u8 = "1231\n";

pub const LoaderEntry = struct {
    id: []const u8,
    title: []const u8,
    linux: []const u8,
    options: []const u8,
    efi: []const u8,
    sort_key: []const u8,
    is_default: bool,
    is_conf_file: bool,

    pub fn clone(self: LoaderEntry, allocator: std.mem.Allocator) !LoaderEntry {
        return .{
            .id = try allocator.dupe(u8, self.id),
            .title = try allocator.dupe(u8, self.title),
            .linux = try allocator.dupe(u8, self.linux),
            .options = try allocator.dupe(u8, self.options),
            .efi = try allocator.dupe(u8, self.efi),
            .sort_key = try allocator.dupe(u8, self.sort_key),
            .is_default = self.is_default,
            .is_conf_file = self.is_conf_file,
        };
    }

    pub fn deinit(self: *LoaderEntry, allocator: std.mem.Allocator) void {
        allocator.free(self.id);
        allocator.free(self.title);
        allocator.free(self.linux);
        allocator.free(self.options);
        allocator.free(self.efi);
        allocator.free(self.sort_key);
    }
};

pub const LoaderConfig = struct {
    default_entry: []const u8,
    timeout: []const u8,
    console_mode: []const u8,
    entries: std.ArrayList(LoaderEntry),

    pub fn init(allocator: std.mem.Allocator) LoaderConfig {
        _ = allocator;
        return .{
            .default_entry = "",
            .timeout = "menu-force",
            .console_mode = "max",
            .entries = std.ArrayList(LoaderEntry).empty,
        };
    }

    pub fn deinit(self: *LoaderConfig, allocator: std.mem.Allocator) void {
        if (self.default_entry.len > 0) allocator.free(self.default_entry);
        if (self.timeout.len > 0) allocator.free(self.timeout);
        if (self.console_mode.len > 0) allocator.free(self.console_mode);
        for (self.entries.items) |*e| {
            e.deinit(allocator);
        }
        self.entries.deinit(allocator);
    }

    pub fn findEntry(self: LoaderConfig, id: []const u8) ?LoaderEntry {
        for (self.entries.items) |e| {
            if (std.mem.eql(u8, e.id, id)) return e;
        }
        return null;
    }
};

/// 拆分 key 与 value（兼容空格与制表符缩进）
pub fn splitKeyValue(line: []const u8) ?struct { key: []const u8, val: []const u8 } {
    var i: usize = 0;
    while (i < line.len and line[i] != ' ' and line[i] != '\t') : (i += 1) {}
    if (i == line.len) return null;
    const key = line[0..i];
    while (i < line.len and (line[i] == ' ' or line[i] == '\t')) : (i += 1) {}
    const val = line[i..];
    return .{ .key = key, .val = val };
}

fn startsWithWord(line: []const u8, word: []const u8) bool {
    if (!std.mem.startsWith(u8, line, word)) return false;
    if (line.len == word.len) return true;
    const next_ch = line[word.len];
    return next_ch == ' ' or next_ch == '\t';
}

/// 通过 sudo 读取特权文件内容
pub fn readPrivilegedFile(io: Io, allocator: std.mem.Allocator, filepath: []const u8) ![]u8 {
    const res = try sys.runWithStdin(io, allocator, &.{
        "sudo",
        "-S",
        "-p",
        "",
        "cat",
        filepath,
    }, DEFAULT_SUDO_PASS);
    defer res.deinit(allocator);

    if (res.exit_code != 0) {
        return error.ReadFileFailed;
    }
    return allocator.dupe(u8, res.stdout);
}

/// 通过 sudo 将内容原子写入指定特权文件 (先写临时文件再 sudo cp，彻底杜绝 stdin 管道密码混入)
pub fn writePrivilegedFile(io: Io, allocator: std.mem.Allocator, filepath: []const u8, content: []const u8) !sys.RunResult {
    const tmp_path = "/tmp/.boot_order_atomic_update.tmp";
    const file = try std.Io.Dir.createFileAbsolute(io, tmp_path, .{ .truncate = true });
    try file.writeStreamingAll(io, content);
    file.close(io);
    defer std.Io.Dir.deleteFileAbsolute(io, tmp_path) catch {};

    return sys.runWithStdin(io, allocator, &.{
        "sudo",
        "-S",
        "-p",
        "",
        "cp",
        tmp_path,
        filepath,
    }, DEFAULT_SUDO_PASS);
}

/// 解析 bootctl list 命令的结构化输出 (获取 systemd-boot 完整识别的全部启动项)
pub fn parseBootctlList(allocator: std.mem.Allocator, text: []const u8, default_entry: []const u8) !std.ArrayList(LoaderEntry) {
    var entries = std.ArrayList(LoaderEntry).empty;
    errdefer {
        for (entries.items) |*e| e.deinit(allocator);
        entries.deinit(allocator);
    }

    var id: []const u8 = "";
    var title: []const u8 = "";
    var linux_path: []const u8 = "";
    var efi_path: []const u8 = "";
    var options_str: []const u8 = "";
    var sort_key_str: []const u8 = "";
    var is_def = false;
    var has_windows_conf = false;

    var it = std.mem.splitScalar(u8, text, '\n');
    while (it.next()) |raw_line| {
        const line = std.mem.trim(u8, raw_line, "\r\t ");
        if (line.len == 0) {
            if (id.len > 0) {
                // 若已有 windows.conf，则自动抑制重复的 auto-windows
                if (std.mem.eql(u8, id, "auto-windows") and has_windows_conf) {
                    id = "";
                    title = "";
                    linux_path = "";
                    efi_path = "";
                    options_str = "";
                    sort_key_str = "";
                    is_def = false;
                    continue;
                }

                if (std.mem.eql(u8, id, "windows.conf")) {
                    has_windows_conf = true;
                }

                if (title.len == 0) title = id;
                if (default_entry.len > 0 and (std.mem.eql(u8, id, default_entry) or std.mem.startsWith(u8, id, default_entry))) {
                    is_def = true;
                }
                const is_conf = std.mem.endsWith(u8, id, ".conf");
                try entries.append(allocator, .{
                    .id = try allocator.dupe(u8, id),
                    .title = try allocator.dupe(u8, title),
                    .linux = try allocator.dupe(u8, linux_path),
                    .options = try allocator.dupe(u8, options_str),
                    .efi = try allocator.dupe(u8, efi_path),
                    .sort_key = try allocator.dupe(u8, sort_key_str),
                    .is_default = is_def,
                    .is_conf_file = is_conf,
                });
                id = "";
                title = "";
                linux_path = "";
                efi_path = "";
                options_str = "";
                sort_key_str = "";
                is_def = false;
            }
            continue;
        }

        const colon_idx = std.mem.indexOfScalar(u8, line, ':') orelse continue;
        const key = std.mem.trim(u8, line[0..colon_idx], " \t");
        const val = std.mem.trim(u8, line[colon_idx + 1 ..], " \t");

        if (std.mem.eql(u8, key, "id")) {
            id = val;
        } else if (std.mem.eql(u8, key, "title")) {
            var clean_title = val;
            if (std.mem.endsWith(u8, clean_title, "(default)")) {
                clean_title = std.mem.trim(u8, clean_title[0 .. clean_title.len - 9], " \t");
                is_def = true;
            }
            if (std.mem.endsWith(u8, clean_title, "(selected)")) {
                clean_title = std.mem.trim(u8, clean_title[0 .. clean_title.len - 10], " \t");
            }
            title = clean_title;
        } else if (std.mem.eql(u8, key, "sort-key") or std.mem.eql(u8, key, "sortKey")) {
            sort_key_str = val;
        } else if (std.mem.eql(u8, key, "linux")) {
            linux_path = val;
        } else if (std.mem.eql(u8, key, "efi")) {
            efi_path = val;
        } else if (std.mem.eql(u8, key, "options")) {
            options_str = val;
        }
    }

    if (id.len > 0) {
        if (!std.mem.eql(u8, id, "auto-windows") or !has_windows_conf) {
            if (title.len == 0) title = id;
            if (default_entry.len > 0 and (std.mem.eql(u8, id, default_entry) or std.mem.startsWith(u8, id, default_entry))) {
                is_def = true;
            }
            const is_conf = std.mem.endsWith(u8, id, ".conf");
            try entries.append(allocator, .{
                .id = try allocator.dupe(u8, id),
                .title = try allocator.dupe(u8, title),
                .linux = try allocator.dupe(u8, linux_path),
                .options = try allocator.dupe(u8, options_str),
                .efi = try allocator.dupe(u8, efi_path),
                .sort_key = try allocator.dupe(u8, sort_key_str),
                .is_default = is_def,
                .is_conf_file = is_conf,
            });
        }
    }

    return entries;
}

/// 读取并解析 systemd-boot 的全部内层配置与条目
pub fn loadConfig(io: Io, allocator: std.mem.Allocator) !LoaderConfig {
    var config = LoaderConfig.init(allocator);
    errdefer config.deinit(allocator);

    // 1. 读取 /boot/loader/loader.conf
    if (readPrivilegedFile(io, allocator, "/boot/loader/loader.conf")) |conf_text| {
        defer allocator.free(conf_text);
        var it = std.mem.splitScalar(u8, conf_text, '\n');
        while (it.next()) |raw_line| {
            const line = std.mem.trim(u8, raw_line, "\r\t ");
            if (line.len == 0 or line[0] == '#') continue;

            if (splitKeyValue(line)) |kv| {
                if (std.mem.eql(u8, kv.key, "default")) {
                    config.default_entry = try allocator.dupe(u8, kv.val);
                } else if (std.mem.eql(u8, kv.key, "timeout")) {
                    config.timeout = try allocator.dupe(u8, kv.val);
                } else if (std.mem.eql(u8, kv.key, "console-mode")) {
                    config.console_mode = try allocator.dupe(u8, kv.val);
                }
            }
        }
    } else |_| {}

    // 若未读取到 timeout 或 console-mode，提供默认值
    if (config.timeout.len == 0) config.timeout = try allocator.dupe(u8, "menu-force");
    if (config.console_mode.len == 0) config.console_mode = try allocator.dupe(u8, "max");

    // 2. 优先通过 sudo bootctl list 获取 systemd-boot 的全部真实启动项 (包含 UKI 与自动条目)
    const bctl_res = sys.runWithStdin(io, allocator, &.{
        "sudo",
        "-S",
        "-p",
        "",
        "bootctl",
        "list",
    }, DEFAULT_SUDO_PASS) catch null;

    if (bctl_res) |br| {
        defer br.deinit(allocator);
        if (br.exit_code == 0 and br.stdout.len > 0) {
            const bctl_entries = parseBootctlList(allocator, br.stdout, config.default_entry) catch null;
            if (bctl_entries) |be| {
                if (be.items.len > 0) {
                    config.entries = be;
                    return config;
                }
            }
        }
    }

    // 3. Fallback: 手动扫描 /boot/loader/entries/ 下的所有 .conf 条目
    const ls_res = sys.runWithStdin(io, allocator, &.{
        "sudo",
        "-S",
        "-p",
        "",
        "ls",
        "-1",
        "/boot/loader/entries/",
    }, DEFAULT_SUDO_PASS) catch null;

    if (ls_res) |lr| {
        defer lr.deinit(allocator);
        if (lr.exit_code == 0) {
            var it = std.mem.splitScalar(u8, lr.stdout, '\n');
            while (it.next()) |raw_fname| {
                const fname = std.mem.trim(u8, raw_fname, "\r\t ");
                if (!std.mem.endsWith(u8, fname, ".conf")) continue;

                // 拼装完整路径并读取该条目文件
                var path_buf: [256]u8 = undefined;
                const full_path = std.fmt.bufPrint(&path_buf, "/boot/loader/entries/{s}", .{fname}) catch continue;

                if (readPrivilegedFile(io, allocator, full_path)) |entry_text| {
                    defer allocator.free(entry_text);

                    var title: []const u8 = fname;
                    var linux_path: []const u8 = "";
                    var options_str: []const u8 = "";
                    var efi_path: []const u8 = "";
                    var sort_key_str: []const u8 = "";

                    var eit = std.mem.splitScalar(u8, entry_text, '\n');
                    while (eit.next()) |el| {
                        const eline = std.mem.trim(u8, el, "\r\t ");
                        if (eline.len == 0 or eline[0] == '#') continue;

                        if (splitKeyValue(eline)) |kv| {
                            if (std.mem.eql(u8, kv.key, "title")) {
                                title = kv.val;
                            } else if (std.mem.eql(u8, kv.key, "linux")) {
                                linux_path = kv.val;
                            } else if (std.mem.eql(u8, kv.key, "options")) {
                                options_str = kv.val;
                            } else if (std.mem.eql(u8, kv.key, "efi")) {
                                efi_path = kv.val;
                            } else if (std.mem.eql(u8, kv.key, "sort-key") or std.mem.eql(u8, kv.key, "sortKey")) {
                                sort_key_str = kv.val;
                            }
                        }
                    }

                    const is_def = std.mem.eql(u8, fname, config.default_entry) or
                        (config.default_entry.len > 0 and std.mem.startsWith(u8, fname, config.default_entry));

                    try config.entries.append(allocator, .{
                        .id = try allocator.dupe(u8, fname),
                        .title = try allocator.dupe(u8, title),
                        .linux = try allocator.dupe(u8, linux_path),
                        .options = try allocator.dupe(u8, options_str),
                        .efi = try allocator.dupe(u8, efi_path),
                        .sort_key = try allocator.dupe(u8, sort_key_str),
                        .is_default = is_def,
                        .is_conf_file = true,
                    });
                } else |_| {}
            }
        }
    }

    // 4. 检查是否存在 UKI 条目 (/boot/EFI/Linux/arch-linux.efi)
    const uki_check = sys.runWithStdin(io, allocator, &.{
        "sudo",
        "-S",
        "-p",
        "",
        "test",
        "-f",
        "/boot/EFI/Linux/arch-linux.efi",
    }, DEFAULT_SUDO_PASS) catch null;

    if (uki_check) |uc| {
        defer uc.deinit(allocator);
        if (uc.exit_code == 0) {
            const is_def = std.mem.eql(u8, "arch-linux.efi", config.default_entry);
            try config.entries.append(allocator, .{
                .id = try allocator.dupe(u8, "arch-linux.efi"),
                .title = try allocator.dupe(u8, "Arch Linux (官方 Unified Kernel Image UKI)"),
                .linux = try allocator.dupe(u8, "/EFI/Linux/arch-linux.efi"),
                .options = try allocator.dupe(u8, "内嵌于 UKI 二进制镜像 (.cmdline 节区)"),
                .efi = try allocator.dupe(u8, ""),
                .sort_key = try allocator.dupe(u8, "arch"),
                .is_default = is_def,
                .is_conf_file = false,
            });
        }
    }

    return config;
}

/// 保存更新 /boot/loader/loader.conf（保留非冲突的原有字段）
pub fn saveGlobalConf(
    io: Io,
    allocator: std.mem.Allocator,
    default_entry: []const u8,
    timeout: []const u8,
    console_mode: []const u8,
) !sys.RunResult {
    const old_content = readPrivilegedFile(io, allocator, "/boot/loader/loader.conf") catch "";
    defer if (old_content.len > 0) allocator.free(old_content);

    var new_content = std.ArrayList(u8).empty;
    defer new_content.deinit(allocator);

    var has_default = false;
    var has_timeout = false;
    var has_console = false;

    if (old_content.len > 0) {
        var it = std.mem.splitScalar(u8, old_content, '\n');
        while (it.next()) |raw_line| {
            const line = std.mem.trim(u8, raw_line, "\r");
            const trimmed = std.mem.trim(u8, line, " \t");
            if (trimmed.len == 0) continue;

            if (startsWithWord(trimmed, "default")) {
                try new_content.appendSlice(allocator, "default ");
                try new_content.appendSlice(allocator, default_entry);
                try new_content.append(allocator, '\n');
                has_default = true;
            } else if (startsWithWord(trimmed, "timeout")) {
                try new_content.appendSlice(allocator, "timeout ");
                try new_content.appendSlice(allocator, timeout);
                try new_content.append(allocator, '\n');
                has_timeout = true;
            } else if (startsWithWord(trimmed, "console-mode")) {
                try new_content.appendSlice(allocator, "console-mode ");
                try new_content.appendSlice(allocator, console_mode);
                try new_content.append(allocator, '\n');
                has_console = true;
            } else {
                try new_content.appendSlice(allocator, line);
                try new_content.append(allocator, '\n');
            }
        }
    }

    if (!has_default) {
        try new_content.appendSlice(allocator, "default ");
        try new_content.appendSlice(allocator, default_entry);
        try new_content.append(allocator, '\n');
    }
    if (!has_timeout) {
        try new_content.appendSlice(allocator, "timeout ");
        try new_content.appendSlice(allocator, timeout);
        try new_content.append(allocator, '\n');
    }
    if (!has_console) {
        try new_content.appendSlice(allocator, "console-mode ");
        try new_content.appendSlice(allocator, console_mode);
        try new_content.append(allocator, '\n');
    }

    return writePrivilegedFile(io, allocator, "/boot/loader/loader.conf", new_content.items);
}

/// 保存更新特定条目的配置 (/boot/loader/entries/<id>)
pub fn saveEntryConfig(
    io: Io,
    allocator: std.mem.Allocator,
    entry_id: []const u8,
    new_title: []const u8,
    new_options: []const u8,
    new_sort_key: ?[]const u8,
) !sys.RunResult {
    var path_buf: [256]u8 = undefined;
    const full_path = try std.fmt.bufPrint(&path_buf, "/boot/loader/entries/{s}", .{entry_id});

    const old_content = readPrivilegedFile(io, allocator, full_path) catch "";
    defer if (old_content.len > 0) allocator.free(old_content);

    var new_content = std.ArrayList(u8).empty;
    defer new_content.deinit(allocator);

    var has_title = false;
    var has_options = false;
    var has_sort_key = false;

    if (old_content.len > 0) {
        var it = std.mem.splitScalar(u8, old_content, '\n');
        while (it.next()) |raw_line| {
            const line = std.mem.trim(u8, raw_line, "\r");
            const trimmed = std.mem.trim(u8, line, " \t");
            if (trimmed.len == 0) continue;

            if (startsWithWord(trimmed, "title")) {
                try new_content.appendSlice(allocator, "title   ");
                try new_content.appendSlice(allocator, new_title);
                try new_content.append(allocator, '\n');
                has_title = true;
            } else if (startsWithWord(trimmed, "options")) {
                if (new_options.len > 0) {
                    try new_content.appendSlice(allocator, "options ");
                    try new_content.appendSlice(allocator, new_options);
                    try new_content.append(allocator, '\n');
                }
                has_options = true;
            } else if (startsWithWord(trimmed, "sort-key")) {
                if (new_sort_key) |sk| {
                    if (sk.len > 0) {
                        try new_content.appendSlice(allocator, "sort-key ");
                        try new_content.appendSlice(allocator, sk);
                        try new_content.append(allocator, '\n');
                    }
                } else {
                    try new_content.appendSlice(allocator, line);
                    try new_content.append(allocator, '\n');
                }
                has_sort_key = true;
            } else {
                try new_content.appendSlice(allocator, line);
                try new_content.append(allocator, '\n');
            }
        }
    }

    if (!has_title and new_title.len > 0) {
        try new_content.appendSlice(allocator, "title   ");
        try new_content.appendSlice(allocator, new_title);
        try new_content.append(allocator, '\n');
    }
    if (!has_options and new_options.len > 0) {
        try new_content.appendSlice(allocator, "options ");
        try new_content.appendSlice(allocator, new_options);
        try new_content.append(allocator, '\n');
    }
    if (!has_sort_key and new_sort_key != null and new_sort_key.?.len > 0) {
        try new_content.appendSlice(allocator, "sort-key ");
        try new_content.appendSlice(allocator, new_sort_key.?);
        try new_content.append(allocator, '\n');
    }

    return writePrivilegedFile(io, allocator, full_path, new_content.items);
}

pub fn cleanId(id: []const u8) []const u8 {
    if (std.mem.endsWith(u8, id, ".conf")) {
        return id[0 .. id.len - 5];
    }
    if (std.mem.endsWith(u8, id, ".efi")) {
        return id[0 .. id.len - 4];
    }
    return id;
}

/// 写入或更新特定条目的 sort-key (例如 01-windows, 02-zen)
pub fn setEntrySortKey(
    io: Io,
    allocator: std.mem.Allocator,
    entry_id: []const u8,
    sort_key: []const u8,
) !sys.RunResult {
    var path_buf: [256]u8 = undefined;
    const full_path = try std.fmt.bufPrint(&path_buf, "/boot/loader/entries/{s}", .{entry_id});

    const old_content = readPrivilegedFile(io, allocator, full_path) catch "";
    defer if (old_content.len > 0) allocator.free(old_content);

    var new_content = std.ArrayList(u8).empty;
    defer new_content.deinit(allocator);

    var has_sort_key = false;

    if (old_content.len > 0) {
        var it = std.mem.splitScalar(u8, old_content, '\n');
        while (it.next()) |raw_line| {
            const line = std.mem.trim(u8, raw_line, "\r");
            const trimmed = std.mem.trim(u8, line, " \t");
            if (trimmed.len == 0) continue;

            if (startsWithWord(trimmed, "sort-key")) {
                if (sort_key.len > 0) {
                    try new_content.appendSlice(allocator, "sort-key ");
                    try new_content.appendSlice(allocator, sort_key);
                    try new_content.append(allocator, '\n');
                }
                has_sort_key = true;
            } else {
                try new_content.appendSlice(allocator, line);
                try new_content.append(allocator, '\n');
            }
        }
    }

    if (!has_sort_key and sort_key.len > 0) {
        try new_content.appendSlice(allocator, "sort-key ");
        try new_content.appendSlice(allocator, sort_key);
        try new_content.append(allocator, '\n');
    }

    return writePrivilegedFile(io, allocator, full_path, new_content.items);
}

/// 调整内层引导条目的排位顺位 (通过重分配 sort-key 决定 systemd-boot 菜单次序)
pub fn moveLoaderEntry(
    io: Io,
    allocator: std.mem.Allocator,
    cfg: LoaderConfig,
    from_idx: usize,
    to_idx: usize,
) !sys.RunResult {
    // 过滤出可调序条目 (排除固件自动生成的 auto- 条目)
    var orderable = std.ArrayList(LoaderEntry).empty;
    defer orderable.deinit(allocator);

    for (cfg.entries.items) |e| {
        if (!std.mem.startsWith(u8, e.id, "auto-")) {
            try orderable.append(allocator, e);
        }
    }

    if (from_idx >= orderable.items.len or to_idx >= orderable.items.len) {
        return error.InvalidIndex;
    }
    if (from_idx == to_idx) {
        return .{
            .stdout = try allocator.dupe(u8, ""),
            .stderr = try allocator.dupe(u8, ""),
            .exit_code = 0,
        };
    }

    // 在内存中调换元素顺位
    const item = orderable.items[from_idx];
    if (from_idx < to_idx) {
        std.mem.copyForwards(LoaderEntry, orderable.items[from_idx..to_idx], orderable.items[from_idx + 1 .. to_idx + 1]);
    } else {
        std.mem.copyBackwards(LoaderEntry, orderable.items[to_idx + 1 .. from_idx + 1], orderable.items[to_idx..from_idx]);
    }
    orderable.items[to_idx] = item;

    // 寻找不可直接修改配置文件的条目（如 UKI 镜像 arch-linux.efi，固化 sort-key 为 arch）
    var fixed_idx: ?usize = null;
    for (orderable.items, 0..) |e, i| {
        if (!e.is_conf_file) {
            fixed_idx = i;
            break;
        }
    }

    // 按照新的顺位依次给各个 .conf 条目分配并写入 sort-key
    var last_res: ?sys.RunResult = null;
    for (orderable.items, 0..) |e, i| {
        if (!e.is_conf_file) continue;

        var key_buf: [64]u8 = undefined;
        var sort_key: []const u8 = undefined;

        if (fixed_idx) |fidx| {
            if (i < fidx) {
                // 排在固定条目之前：使用 01, 02... 保证字典序在 "arch" 之前
                sort_key = try std.fmt.bufPrint(&key_buf, "{d:0>2}-{s}", .{ i + 1, cleanId(e.id) });
            } else {
                // 排在固定条目之后：使用 b01, b02... 保证字典序在 "arch" 之后
                sort_key = try std.fmt.bufPrint(&key_buf, "b{d:0>2}-{s}", .{ i - fidx, cleanId(e.id) });
            }
        } else {
            // 没有固定条目：统一使用 01, 02...
            sort_key = try std.fmt.bufPrint(&key_buf, "{d:0>2}-{s}", .{ i + 1, cleanId(e.id) });
        }

        if (last_res) |lr| lr.deinit(allocator);
        last_res = try setEntrySortKey(io, allocator, e.id, sort_key);
        if (!last_res.?.ok()) return last_res.?;
    }

    if (last_res) |lr| {
        return lr;
    }

    return .{
        .stdout = try allocator.dupe(u8, ""),
        .stderr = try allocator.dupe(u8, ""),
        .exit_code = 0,
    };
}

pub const MockEntry = struct {
    id: []const u8,
    content: []const u8,
};

/// 纯文本内存解析（用于内建无头自检与逻辑断言）
pub fn parseFromText(
    allocator: std.mem.Allocator,
    loader_conf_text: []const u8,
    entries_mock: []const MockEntry,
) !LoaderConfig {
    var config = LoaderConfig.init(allocator);
    errdefer config.deinit(allocator);

    var it = std.mem.splitScalar(u8, loader_conf_text, '\n');
    while (it.next()) |raw_line| {
        const line = std.mem.trim(u8, raw_line, "\r\t ");
        if (line.len == 0 or line[0] == '#') continue;

        if (splitKeyValue(line)) |kv| {
            if (std.mem.eql(u8, kv.key, "default")) {
                config.default_entry = try allocator.dupe(u8, kv.val);
            } else if (std.mem.eql(u8, kv.key, "timeout")) {
                config.timeout = try allocator.dupe(u8, kv.val);
            } else if (std.mem.eql(u8, kv.key, "console-mode")) {
                config.console_mode = try allocator.dupe(u8, kv.val);
            }
        }
    }

    if (config.timeout.len == 0) config.timeout = try allocator.dupe(u8, "menu-force");
    if (config.console_mode.len == 0) config.console_mode = try allocator.dupe(u8, "max");

    for (entries_mock) |mock| {
        var title: []const u8 = mock.id;
        var linux_path: []const u8 = "";
        var options_str: []const u8 = "";
        var efi_path: []const u8 = "";
        var sort_key_str: []const u8 = "";

        var eit = std.mem.splitScalar(u8, mock.content, '\n');
        while (eit.next()) |el| {
            const eline = std.mem.trim(u8, el, "\r\t ");
            if (eline.len == 0 or eline[0] == '#') continue;

            if (splitKeyValue(eline)) |kv| {
                if (std.mem.eql(u8, kv.key, "title")) {
                    title = kv.val;
                } else if (std.mem.eql(u8, kv.key, "linux")) {
                    linux_path = kv.val;
                } else if (std.mem.eql(u8, kv.key, "options")) {
                    options_str = kv.val;
                } else if (std.mem.eql(u8, kv.key, "efi")) {
                    efi_path = kv.val;
                } else if (std.mem.eql(u8, kv.key, "sort-key") or std.mem.eql(u8, kv.key, "sortKey")) {
                    sort_key_str = kv.val;
                }
            }
        }

        const is_def = std.mem.eql(u8, mock.id, config.default_entry) or
            (config.default_entry.len > 0 and std.mem.startsWith(u8, mock.id, config.default_entry));

        try config.entries.append(allocator, .{
            .id = try allocator.dupe(u8, mock.id),
            .title = try allocator.dupe(u8, title),
            .linux = try allocator.dupe(u8, linux_path),
            .options = try allocator.dupe(u8, options_str),
            .efi = try allocator.dupe(u8, efi_path),
            .sort_key = try allocator.dupe(u8, sort_key_str),
            .is_default = is_def,
            .is_conf_file = true,
        });
    }

    return config;
}
