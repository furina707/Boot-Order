//! UEFI NVRAM 启动项交互核心逻辑
//!
//! 负责与主板 NVRAM 真实交互（基于 efibootmgr 命令）：
//! - 提取与解析当前固件启动项（Boot0000~BootFFFF）、启动顺序 (BootOrder)、单次启动 (BootNext)、超时时间；
//! - 提权写入 NVRAM：当未处于 root 身份时，自动通过 sudo -S 注入密码完成持久化写操作；
//! - 启动项排序与去重：支持置顶、上下移动、启用/禁用、删除、清理重复条目；
//! - 物理 ESP 分区与 .efi 二进制定位。

const std = @import("std");
const sys = @import("sys.zig");
const Io = std.Io;

pub const DEFAULT_SUDO_PASS: []const u8 = "1231\n";

pub const BootEntry = struct {
    ident: [4]u8,
    title: []const u8,
    path: []const u8,
    active: bool,
    device_path: []const u8,
    raw_details: []const u8,

    pub fn clone(self: BootEntry, allocator: std.mem.Allocator) !BootEntry {
        return .{
            .ident = self.ident,
            .title = try allocator.dupe(u8, self.title),
            .path = try allocator.dupe(u8, self.path),
            .active = self.active,
            .device_path = try allocator.dupe(u8, self.device_path),
            .raw_details = try allocator.dupe(u8, self.raw_details),
        };
    }

    pub fn deinit(self: *BootEntry, allocator: std.mem.Allocator) void {
        allocator.free(self.title);
        allocator.free(self.path);
        allocator.free(self.device_path);
        allocator.free(self.raw_details);
    }
};

pub const BootConfig = struct {
    timeout: u32 = 0,
    boot_current: ?[4]u8 = null,
    boot_next: ?[4]u8 = null,
    boot_order: std.ArrayList([4]u8),
    entries: std.ArrayList(BootEntry),

    pub fn init(allocator: std.mem.Allocator) BootConfig {
        _ = allocator;
        return .{
            .boot_order = std.ArrayList([4]u8).empty,
            .entries = std.ArrayList(BootEntry).empty,
        };
    }

    pub fn deinit(self: *BootConfig, allocator: std.mem.Allocator) void {
        self.boot_order.deinit(allocator);
        for (self.entries.items) |*e| {
            e.deinit(allocator);
        }
        self.entries.deinit(allocator);
    }

    /// 查找指定编号的条目
    pub fn findEntry(self: BootConfig, ident: [4]u8) ?BootEntry {
        for (self.entries.items) |e| {
            if (std.mem.eql(u8, &e.ident, &ident)) return e;
        }
        return null;
    }

    /// 检查指定编号是否已经在 BootOrder 中
    pub fn inBootOrder(self: BootConfig, ident: [4]u8) bool {
        for (self.boot_order.items) |id| {
            if (std.mem.eql(u8, &id, &ident)) return true;
        }
        return false;
    }

    /// 获取去重并过滤无效幽灵条目（不存在于 entries 中的条目）的 BootOrder 列表
    pub fn getDeduplicatedOrder(self: BootConfig, allocator: std.mem.Allocator) !std.ArrayList([4]u8) {
        var result = std.ArrayList([4]u8).empty;
        errdefer result.deinit(allocator);
        for (self.boot_order.items) |id| {
            // 必须真实存在于 NVRAM entries 中，否则 efibootmgr 写入会报错 entry does not exist
            if (self.findEntry(id) == null) continue;

            var exists = false;
            for (result.items) |r| {
                if (std.mem.eql(u8, &r, &id)) {
                    exists = true;
                    break;
                }
            }
            if (!exists) {
                try result.append(allocator, id);
            }
        }
        return result;
    }
};

/// 执行 efibootmgr 操作（自动提权处理）
pub fn runEfibootmgr(io: Io, gpa: std.mem.Allocator, args: []const []const u8, is_write: bool) !sys.RunResult {
    const is_root = sys.isRoot();

    if (is_root or !is_write) {
        // 已经是 root 身份，或者只读操作（读取 NVRAM 在大多数现代 Linux 允许普通用户），直接运行
        var argv = try gpa.alloc([]const u8, args.len + 1);
        defer gpa.free(argv);
        argv[0] = "efibootmgr";
        for (args, 0..) |arg, i| {
            argv[i + 1] = arg;
        }

        const res = try sys.run(io, gpa, argv);
        // 若普通运行读取返回权限拒绝，降级通过 sudo 运行
        if (res.exit_code != 0 and !is_root and std.mem.indexOf(u8, res.stderr, "Permission denied") != null) {
            res.deinit(gpa);
        } else {
            return res;
        }
    }

    // 需要提权：使用 sudo -S -p "" 运行并通过 stdin 传入密码 1231
    var sudo_argv = try gpa.alloc([]const u8, args.len + 5);
    defer gpa.free(sudo_argv);
    sudo_argv[0] = "sudo";
    sudo_argv[1] = "-S";
    sudo_argv[2] = "-p";
    sudo_argv[3] = "";
    sudo_argv[4] = "efibootmgr";
    for (args, 0..) |arg, i| {
        sudo_argv[i + 5] = arg;
    }

    return sys.runWithStdin(io, gpa, sudo_argv, DEFAULT_SUDO_PASS);
}

/// 解析 efibootmgr -v 输出内容
pub fn parseOutput(allocator: std.mem.Allocator, text: []const u8) !BootConfig {
    var config = BootConfig.init(allocator);
    errdefer config.deinit(allocator);

    var it = std.mem.splitScalar(u8, text, '\n');
    while (it.next()) |raw_line| {
        const line = std.mem.trim(u8, raw_line, "\r\t ");
        if (line.len == 0) continue;

        // 1. Timeout: X seconds
        if (std.mem.startsWith(u8, line, "Timeout:")) {
            const rest = std.mem.trimStart(u8, line["Timeout:".len..], " ");
            if (std.mem.indexOfScalar(u8, rest, ' ')) |idx| {
                config.timeout = std.fmt.parseInt(u32, rest[0..idx], 10) catch 0;
            }
            continue;
        }

        // 2. BootOrder: 0000,0001,...
        if (std.mem.startsWith(u8, line, "BootOrder:")) {
            const rest = std.mem.trimStart(u8, line["BootOrder:".len..], " ");
            var order_it = std.mem.splitScalar(u8, rest, ',');
            while (order_it.next()) |item| {
                const trimmed = std.mem.trim(u8, item, " \r\t");
                if (trimmed.len == 4 and isHex4(trimmed)) {
                    var id: [4]u8 = undefined;
                    @memcpy(&id, trimmed[0..4]);
                    try config.boot_order.append(allocator, id);
                }
            }
            continue;
        }

        // 3. BootCurrent: XXXX
        if (std.mem.startsWith(u8, line, "BootCurrent:")) {
            const rest = std.mem.trimStart(u8, line["BootCurrent:".len..], " ");
            if (rest.len >= 4 and isHex4(rest[0..4])) {
                var id: [4]u8 = undefined;
                @memcpy(&id, rest[0..4]);
                config.boot_current = id;
            }
            continue;
        }

        // 4. BootNext: XXXX
        if (std.mem.startsWith(u8, line, "BootNext:")) {
            const rest = std.mem.trimStart(u8, line["BootNext:".len..], " ");
            if (rest.len >= 4 and isHex4(rest[0..4])) {
                var id: [4]u8 = undefined;
                @memcpy(&id, rest[0..4]);
                config.boot_next = id;
            }
            continue;
        }

        // 5. BootXXXX[*] 条目行
        if (std.mem.startsWith(u8, line, "Boot") and line.len >= 8) {
            const id_slice = line[4..8];
            if (isHex4(id_slice)) {
                var ident: [4]u8 = undefined;
                @memcpy(&ident, id_slice);

                var active = false;
                var after_id = line[8..];
                if (after_id.len > 0 and after_id[0] == '*') {
                    active = true;
                    after_id = after_id[1..];
                }
                after_id = std.mem.trimStart(u8, after_id, " \t");

                // 按 tab 键切分标题与设备路径
                var title: []const u8 = after_id;
                var path_and_dev: []const u8 = "";
                if (std.mem.indexOfScalar(u8, after_id, '\t')) |tab_idx| {
                    title = std.mem.trim(u8, after_id[0..tab_idx], " ");
                    path_and_dev = std.mem.trim(u8, after_id[tab_idx + 1 ..], " \t");
                }

                // 从设备详情提取 EFI 路径
                var efi_path: []const u8 = "";
                var dev_path: []const u8 = path_and_dev;

                if (std.mem.indexOf(u8, path_and_dev, "\\EFI\\")) |efi_idx| {
                    dev_path = std.mem.trimEnd(u8, path_and_dev[0..efi_idx], "/");
                    const efi_part = path_and_dev[efi_idx..];
                    // 截取到结尾或特殊标志 (如 RC / Windows GUID)
                    if (std.mem.indexOf(u8, efi_part, ".efi")) |dot_idx| {
                        efi_path = efi_part[0 .. dot_idx + 4];
                    } else {
                        efi_path = efi_part;
                    }
                } else if (std.mem.indexOf(u8, path_and_dev, "/EFI/")) |efi_idx| {
                    dev_path = std.mem.trimEnd(u8, path_and_dev[0..efi_idx], "/");
                    const efi_part = path_and_dev[efi_idx..];
                    if (std.mem.indexOf(u8, efi_part, ".efi")) |dot_idx| {
                        efi_path = efi_part[0 .. dot_idx + 4];
                    } else {
                        efi_path = efi_part;
                    }
                }

                try config.entries.append(allocator, .{
                    .ident = ident,
                    .title = try allocator.dupe(u8, title),
                    .path = try allocator.dupe(u8, efi_path),
                    .active = active,
                    .device_path = try allocator.dupe(u8, dev_path),
                    .raw_details = try allocator.dupe(u8, path_and_dev),
                });
            }
        }
    }

    return config;
}

fn isHex4(s: []const u8) bool {
    if (s.len != 4) return false;
    for (s) |c| {
        if (!std.ascii.isHex(c)) return false;
    }
    return true;
}

/// 读取真实 UEFI NVRAM 配置
pub fn loadConfig(io: Io, allocator: std.mem.Allocator) !BootConfig {
    const res = try runEfibootmgr(io, allocator, &.{"-v"}, false);
    defer res.deinit(allocator);
    if (res.exit_code != 0) {
        return error.EfibootmgrReadFailed;
    }
    return parseOutput(allocator, res.stdout);
}

/// 格式化 BootOrder 为逗号分隔字符串
pub fn formatOrderString(allocator: std.mem.Allocator, order: []const [4]u8) ![]u8 {
    if (order.len == 0) return try allocator.dupe(u8, "");
    // 每个 ID 4 字节 + 1 个逗号
    const total_len = order.len * 5 - 1;
    const buf = try allocator.alloc(u8, total_len);
    var offset: usize = 0;
    for (order, 0..) |id, i| {
        @memcpy(buf[offset .. offset + 4], &id);
        offset += 4;
        if (i + 1 < order.len) {
            buf[offset] = ',';
            offset += 1;
        }
    }
    return buf;
}

/// 设置 UEFI NVRAM 启动顺序 (efibootmgr -o XXXX,YYYY,...)
pub fn setBootOrder(io: Io, allocator: std.mem.Allocator, order: []const [4]u8) !sys.RunResult {
    const order_str = try formatOrderString(allocator, order);
    defer allocator.free(order_str);
    return runEfibootmgr(io, allocator, &.{ "-o", order_str }, true);
}

/// 设置下次单次启动项 (efibootmgr -n XXXX)
pub fn setBootNext(io: Io, allocator: std.mem.Allocator, ident: [4]u8) !sys.RunResult {
    return runEfibootmgr(io, allocator, &.{ "-n", &ident }, true);
}

/// 清除单次启动项 (efibootmgr -N，降级直接清理 efivars/BootNext 节点)
pub fn clearBootNext(io: Io, allocator: std.mem.Allocator) !sys.RunResult {
    const res = try runEfibootmgr(io, allocator, &.{"-N"}, true);
    if (res.ok()) return res;
    res.deinit(allocator);

    // 针对 efibootmgr -N 在特定主板固件报错的降级方案：安全删除 efivars 下的 BootNext 节点
    const del_argv = &[_][]const u8{ "sudo", "-S", "-p", "", "sh", "-c", "rm -f /sys/firmware/efi/efivars/BootNext-*" };
    return sys.runWithStdin(io, allocator, del_argv, DEFAULT_SUDO_PASS);
}

/// 切换启动项启用/禁用状态 (efibootmgr -b XXXX -a / -A)
pub fn setActive(io: Io, allocator: std.mem.Allocator, ident: [4]u8, active: bool) !sys.RunResult {
    const flag = if (active) "-a" else "-A";
    return runEfibootmgr(io, allocator, &.{ "-b", &ident, flag }, true);
}

/// 从主板 NVRAM 删除指定启动项 (efibootmgr -b XXXX -B)
pub fn deleteEntry(io: Io, allocator: std.mem.Allocator, ident: [4]u8) !sys.RunResult {
    return runEfibootmgr(io, allocator, &.{ "-b", &ident, "-B" }, true);
}

/// 设置固件倒计时超时时间 (efibootmgr -t seconds)
pub fn setTimeout(io: Io, allocator: std.mem.Allocator, seconds: u32) !sys.RunResult {
    var buf: [16]u8 = undefined;
    const sec_str = try std.fmt.bufPrint(&buf, "{d}", .{seconds});
    return runEfibootmgr(io, allocator, &.{ "-t", sec_str }, true);
}

/// 清理 BootOrder 重复项 (efibootmgr -D 或重写去重 BootOrder)
pub fn deduplicateBootOrder(io: Io, allocator: std.mem.Allocator, cfg: BootConfig) !sys.RunResult {
    var dedup_list = try cfg.getDeduplicatedOrder(allocator);
    defer dedup_list.deinit(allocator);
    return setBootOrder(io, allocator, dedup_list.items);
}

/// 将启动项设为默认第一项（若不在列表中则插入开头，否则移至开头，自动过滤不存在条目）
pub fn setAsDefault(io: Io, allocator: std.mem.Allocator, cfg: BootConfig, ident: [4]u8) !sys.RunResult {
    var new_order = std.ArrayList([4]u8).empty;
    defer new_order.deinit(allocator);

    try new_order.append(allocator, ident);
    for (cfg.boot_order.items) |id| {
        // 过滤幽灵无效条目
        if (cfg.findEntry(id) == null) continue;

        if (!std.mem.eql(u8, &id, &ident)) {
            // 同时进行去重
            var dup = false;
            for (new_order.items) |existing| {
                if (std.mem.eql(u8, &existing, &id)) {
                    dup = true;
                    break;
                }
            }
            if (!dup) {
                try new_order.append(allocator, id);
            }
        }
    }

    return setBootOrder(io, allocator, new_order.items);
}

/// 移动条目顺位
pub fn moveOrder(io: Io, allocator: std.mem.Allocator, cfg: BootConfig, from_idx: usize, to_idx: usize) !sys.RunResult {
    if (from_idx >= cfg.boot_order.items.len or to_idx >= cfg.boot_order.items.len) {
        return error.InvalidIndex;
    }
    var new_order = try allocator.dupe([4]u8, cfg.boot_order.items);
    defer allocator.free(new_order);

    const item = new_order[from_idx];
    if (from_idx < to_idx) {
        std.mem.copyForwards([4]u8, new_order[from_idx..to_idx], new_order[from_idx + 1 .. to_idx + 1]);
    } else {
        std.mem.copyBackwards([4]u8, new_order[to_idx + 1 .. from_idx + 1], new_order[to_idx..from_idx]);
    }
    new_order[to_idx] = item;

    return setBootOrder(io, allocator, new_order);
}

/// 从设备详情路径中解析 ESP 分区号 (例如从 "HD(5,GPT,...)" 中提取 5)
pub fn parsePartition(device_path: []const u8) u32 {
    if (std.mem.indexOf(u8, device_path, "HD(")) |idx| {
        const rest = device_path[idx + 3 ..];
        if (std.mem.indexOfScalar(u8, rest, ',')) |comma| {
            return std.fmt.parseInt(u32, rest[0..comma], 10) catch 1;
        }
    }
    return 1;
}

/// 创建/注册新启动项 (efibootmgr -c -d <disk> -p <part> -L <label> -l <loader>)
pub fn createEntry(
    io: Io,
    allocator: std.mem.Allocator,
    disk: []const u8,
    part: u32,
    label: []const u8,
    loader: []const u8,
) !sys.RunResult {
    var part_buf: [16]u8 = undefined;
    const part_str = try std.fmt.bufPrint(&part_buf, "{d}", .{part});

    return runEfibootmgr(io, allocator, &.{
        "-c",
        "-d",
        disk,
        "-p",
        part_str,
        "-L",
        label,
        "-l",
        loader,
    }, true);
}

/// 编辑/更新现有指定编号的启动项 (例如 Boot0002)
/// 保持原有 BootOrder 顺位位置不变
pub fn editEntry(
    io: Io,
    allocator: std.mem.Allocator,
    cfg: BootConfig,
    ident: [4]u8,
    disk: []const u8,
    part: u32,
    label: []const u8,
    loader: []const u8,
    active: bool,
) !sys.RunResult {
    // 1. 记录原启动项在 BootOrder 中的位置
    var order_copy = std.ArrayList([4]u8).empty;
    defer order_copy.deinit(allocator);
    for (cfg.boot_order.items) |id| {
        try order_copy.append(allocator, id);
    }
    const was_in_order = cfg.inBootOrder(ident);

    // 2. 删除原启动项以允许用同名编号覆盖重建
    const del_res = deleteEntry(io, allocator, ident) catch null;
    if (del_res) |dr| dr.deinit(allocator);

    // 3. 重新创建指定编号条目 (-b <ident> -c ...)
    var part_buf: [16]u8 = undefined;
    const part_str = try std.fmt.bufPrint(&part_buf, "{d}", .{part});

    const create_res = try runEfibootmgr(io, allocator, &.{
        "-b",
        &ident,
        "-c",
        "-d",
        disk,
        "-p",
        part_str,
        "-L",
        label,
        "-l",
        loader,
    }, true);

    if (!create_res.ok()) {
        return create_res;
    }
    create_res.deinit(allocator);

    // 4. 若用户指定为禁用，调用 setActive
    if (!active) {
        const act_res = try setActive(io, allocator, ident, false);
        act_res.deinit(allocator);
    }

    // 5. 还原 BootOrder 中的位置，杜绝 efibootmgr -c 擅自强行置顶打乱原有顺位
    if (was_in_order) {
        var restored = std.ArrayList([4]u8).empty;
        defer restored.deinit(allocator);
        var placed = false;
        for (order_copy.items) |id| {
            // 过滤幽灵无效条目（例如 2002, 2004），避免 efibootmgr 报错
            if (cfg.findEntry(id) == null and !std.mem.eql(u8, &id, &ident)) continue;

            if (std.mem.eql(u8, &id, &ident)) {
                try restored.append(allocator, ident);
                placed = true;
            } else {
                try restored.append(allocator, id);
            }
        }
        if (!placed) try restored.append(allocator, ident);
        return setBootOrder(io, allocator, restored.items);
    } else {
        var filtered = std.ArrayList([4]u8).empty;
        defer filtered.deinit(allocator);
        for (order_copy.items) |id| {
            if (cfg.findEntry(id) == null) continue;
            if (!std.mem.eql(u8, &id, &ident)) {
                try filtered.append(allocator, id);
            }
        }
        return setBootOrder(io, allocator, filtered.items);
    }
}
