//! UEFI 启动项管理器 (Boot Order Manager) - 原生 Zig 核心入口
//!
//! 职责划分：
//!   1. 分发 `selftest` 子命令（准则第 38-40 条：内建全链路自检，无需图形环境）；
//!   2. 提供免交互 CLI 命令路由 (--list, --set-default, --set-next, --set-timeout, --dedup)；
//!   3. 默认启动 GTK4 原生交互界面（`ui.run`）。

const std = @import("std");
const nvram = @import("nvram.zig");
const ui = @import("ui.zig");
pub const selftest = @import("selftest.zig");

pub fn main(init: std.process.Init) !u8 {
    const argv = try init.minimal.args.toSlice(init.arena.allocator());
    const allocator = init.gpa;
    const io = init.io;

    if (argv.len > 1) {
        const sub = argv[1];

        // 1. 内建自检
        if (std.mem.eql(u8, sub, "selftest")) {
            return selftest.run(init);
        }

        // 2. 帮助信息
        if (std.mem.eql(u8, sub, "--help") or std.mem.eql(u8, sub, "-h") or std.mem.eql(u8, sub, "help")) {
            printHelp();
            return 0;
        }

        // 3. 命令行列表输出 (--list / -l)
        if (std.mem.eql(u8, sub, "--list") or std.mem.eql(u8, sub, "-l") or std.mem.eql(u8, sub, "list")) {
            var cfg = try nvram.loadConfig(io, allocator);
            defer cfg.deinit(allocator);

            std.debug.print("================================================================================\n", .{});
            std.debug.print("              UEFI NVRAM 主板固件启动项与顺序 (efibootmgr)\n", .{});
            std.debug.print("================================================================================\n", .{});
            std.debug.print("固件超时: {d} 秒  │  单次启动 (BootNext): {s}\n", .{
                cfg.timeout,
                if (cfg.boot_next) |bn| &bn else "无",
            });

            const order_str = try nvram.formatOrderString(allocator, cfg.boot_order.items);
            defer allocator.free(order_str);
            std.debug.print("启动顺序 (BootOrder): {s}\n", .{order_str});
            std.debug.print("--------------------------------------------------------------------------------\n", .{});

            // 先按顺序输出
            for (cfg.boot_order.items, 0..) |id, idx| {
                const entry = cfg.findEntry(id);
                const title = if (entry) |e| e.title else "(未知)";
                const active = if (entry) |e| (if (e.active) "启用" else "禁用") else "未知";
                const path = if (entry) |e| e.path else "";
                std.debug.print("  #{d:<2} [Boot{s}] {s:<32} [{s}] {s}\n", .{
                    idx + 1,
                    id,
                    title,
                    active,
                    path,
                });
            }

            // 输出不在顺序中的条目
            var orphan_count: usize = 0;
            for (cfg.entries.items) |e| {
                if (!cfg.inBootOrder(e.ident)) {
                    if (orphan_count == 0) {
                        std.debug.print("\n未包含在 BootOrder 中的条目:\n", .{});
                    }
                    orphan_count += 1;
                    std.debug.print("  [!] [Boot{s}] {s:<32} [{s}] {s}\n", .{
                        e.ident,
                        e.title,
                        if (e.active) "启用" else "禁用",
                        e.path,
                    });
                }
            }
            std.debug.print("================================================================================\n", .{});
            return 0;
        }

        // 4. 设置默认启动项 (--set-default <ID>)
        if (std.mem.eql(u8, sub, "--set-default") and argv.len > 2) {
            const ident_arg = argv[2];
            if (ident_arg.len != 4) {
                std.debug.print("[-] 错误: 条目编号必须为 4 位十六进制，例如 0005\n", .{});
                return 1;
            }
            var id: [4]u8 = undefined;
            @memcpy(&id, ident_arg[0..4]);

            var cfg = try nvram.loadConfig(io, allocator);
            defer cfg.deinit(allocator);

            const res = try nvram.setAsDefault(io, allocator, cfg, id);
            defer res.deinit(allocator);

            if (res.ok()) {
                std.debug.print("[✔] 成功将 Boot{s} 设为主板第一优先启动项！(NVRAM 写入完成)\n", .{id});
                return 0;
            } else {
                std.debug.print("[-] 失败: {s}\n", .{res.stderr});
                return 1;
            }
        }

        // 5. 设置单次启动 (--set-next <ID>)
        if (std.mem.eql(u8, sub, "--set-next") and argv.len > 2) {
            const ident_arg = argv[2];
            if (ident_arg.len != 4) {
                std.debug.print("[-] 错误: 条目编号必须为 4 位十六进制，例如 0005\n", .{});
                return 1;
            }
            var id: [4]u8 = undefined;
            @memcpy(&id, ident_arg[0..4]);

            const res = try nvram.setBootNext(io, allocator, id);
            defer res.deinit(allocator);

            if (res.ok()) {
                std.debug.print("[✔] 成功设置下次单次启动项为 Boot{s}！(BootNext 已写入)\n", .{id});
                return 0;
            } else {
                std.debug.print("[-] 失败: {s}\n", .{res.stderr});
                return 1;
            }
        }

        // 6. 清理重复项 (--dedup)
        if (std.mem.eql(u8, sub, "--dedup")) {
            var cfg = try nvram.loadConfig(io, allocator);
            defer cfg.deinit(allocator);

            const res = try nvram.deduplicateBootOrder(io, allocator, cfg);
            defer res.deinit(allocator);

            if (res.ok()) {
                std.debug.print("[✔] 成功清理 BootOrder 重复项并持久化写入 NVRAM！\n", .{});
                return 0;
            } else {
                std.debug.print("[-] 失败: {s}\n", .{res.stderr});
                return 1;
            }
        }

        // 7. 设置超时 (--set-timeout <SEC>)
        if (std.mem.eql(u8, sub, "--set-timeout") and argv.len > 2) {
            const sec = std.fmt.parseInt(u32, argv[2], 10) catch {
                std.debug.print("[-] 错误: 超时时间必须为数字 (秒)\n", .{});
                return 1;
            };
            const res = try nvram.setTimeout(io, allocator, sec);
            defer res.deinit(allocator);
            if (res.ok()) {
                std.debug.print("[✔] 成功将固件倒计时超时设置为 {d} 秒！\n", .{sec});
                return 0;
            } else {
                std.debug.print("[-] 失败: {s}\n", .{res.stderr});
                return 1;
            }
        }

        // 8. 编辑启动项 (--edit <ID> [--label <NAME>] [--loader <PATH>] [--disk <DEV>] [--part <NUM>])
        if (std.mem.eql(u8, sub, "--edit") and argv.len > 2) {
            const ident_arg = argv[2];
            if (ident_arg.len != 4) {
                std.debug.print("[-] 错误: 条目编号必须为 4 位十六进制，例如 0002\n", .{});
                return 1;
            }
            var id: [4]u8 = undefined;
            @memcpy(&id, ident_arg[0..4]);

            var cfg = try nvram.loadConfig(io, allocator);
            defer cfg.deinit(allocator);

            const existing = cfg.findEntry(id);
            if (existing == null) {
                std.debug.print("[-] 错误: 未在 NVRAM 中找到条目 Boot{s}\n", .{id});
                return 1;
            }

            var label: []const u8 = existing.?.title;
            var loader: []const u8 = existing.?.path;
            var disk: []const u8 = "/dev/nvme0n1";
            var part: u32 = nvram.parsePartition(existing.?.device_path);
            var active: bool = existing.?.active;

            var i: usize = 3;
            while (i < argv.len) : (i += 1) {
                if (std.mem.eql(u8, argv[i], "--label") and i + 1 < argv.len) {
                    i += 1;
                    label = argv[i];
                } else if (std.mem.eql(u8, argv[i], "--loader") and i + 1 < argv.len) {
                    i += 1;
                    loader = argv[i];
                } else if (std.mem.eql(u8, argv[i], "--disk") and i + 1 < argv.len) {
                    i += 1;
                    disk = argv[i];
                } else if (std.mem.eql(u8, argv[i], "--part") and i + 1 < argv.len) {
                    i += 1;
                    part = std.fmt.parseInt(u32, argv[i], 10) catch part;
                } else if (std.mem.eql(u8, argv[i], "--active") and i + 1 < argv.len) {
                    i += 1;
                    active = std.mem.eql(u8, argv[i], "true") or std.mem.eql(u8, argv[i], "1");
                }
            }

            const res = try nvram.editEntry(io, allocator, cfg, id, disk, part, label, loader, active);
            defer res.deinit(allocator);

            if (res.ok()) {
                std.debug.print("[✔] 成功更新启动项 Boot{s}！(标签: '{s}', 路径: '{s}', 分区: {d})\n", .{ id, label, loader, part });
                return 0;
            } else {
                std.debug.print("[-] 失败: {s}\n", .{res.stderr});
                return 1;
            }
        }
    }

    // 默认：启动原生 GTK4 图形界面
    return ui.run(init);
}

fn printHelp() void {
    const help_text =
        \\================================================================================
        \\  Boot Order Manager —— UEFI 固件启动项与 NVRAM 综合管理器 (Zig + GTK4)
        \\================================================================================
        \\
        \\用法:
        \\  python run.py                   启动原生 GTK4 高清图形界面 (默认)
        \\  python run.py selftest          运行内建全链路功能自检
        \\  python run.py --list            查看当前 NVRAM 全部启动项与顺序 (CLI)
        \\  python run.py --set-default <ID> 快速将指定条目设为第一启动项 (例如 0002)
        \\  python run.py --set-next <ID>    设置下次开机单次启动项 (BootNext)
        \\  python run.py --edit <ID> [...]  编辑现有启动项 (支持修改标签/路径/分区/状态)
        \\  python run.py --set-timeout <秒> 设置固件引导倒计时超时时间
        \\  python run.py --dedup           自动清理 BootOrder 重复项
        \\  python run.py -h, --help        显示本帮助
        \\
        \\编辑参数 (配合 --edit 使用):
        \\  --label <名称>     设置启动项显示名称 (例如 "Linux Boot Manager")
        \\  --loader <路径>    设置 EFI 引导文件路径 (例如 "\\EFI\\systemd\\systemd-bootx64.efi")
        \\  --part <分区号>    设置目标 ESP 分区号 (例如 5)
        \\  --disk <磁盘设备>  设置磁盘设备路径 (默认 /dev/nvme0n1)
        \\  --active <0|1>     启用或禁用该条目
        \\
        \\核心特性:
        \\  - Zig 0.17+ 原生机器码、Zero Runtime，单二进制独立交付；
        \\  - GTK4 原生现代卡片式界面 (miuix 风格)，支持物理像素 HiDPI 渲染；
        \\  - 零 Mock、零模拟数据：直接真实交互主板 UEFI NVRAM；
        \\  - 智能诊断未加入启动顺位的系统引导项 (如 Linux Boot Manager) 并一键置顶；
        \\  - 自动安全提权 (sudo 管道认证，密码 1231)。
        \\================================================================================
        \\
    ;
    std.debug.print("{s}", .{help_text});
}
