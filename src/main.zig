//! UEFI 启动项管理器 (Boot Order Manager) - 原生 Zig 核心入口
//!
//! 职责划分：
//!   1. 分发 `selftest` 子命令（准则第 38-40 条：内建全链路自检，无需图形环境）；
//!   2. 提供免交互 CLI 命令路由 (--list, --set-default, --set-next, --set-timeout, --dedup)；
//!   3. 默认启动 GTK4 原生交互界面（`ui.run`）。

const std = @import("std");
const nvram = @import("nvram.zig");
const loader = @import("loader.zig");
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
            const timeout_info = if (cfg.timeout == 0)
                "0 秒 (取消倒计时直入)"
            else
                try std.fmt.allocPrint(allocator, "{d} 秒 (倒计时自动进入)", .{cfg.timeout});
            defer if (cfg.timeout != 0) allocator.free(timeout_info);
            std.debug.print("固件超时: {s}  │  单次启动 (BootNext): {s}\n", .{
                timeout_info,
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
            var loader_path: []const u8 = existing.?.path;
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
                    loader_path = argv[i];
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

            const res = try nvram.editEntry(io, allocator, cfg, id, disk, part, label, loader_path, active);
            defer res.deinit(allocator);

            if (res.ok()) {
                std.debug.print("[✔] 成功更新启动项 Boot{s}！(标签: '{s}', 路径: '{s}', 分区: {d})\n", .{ id, label, loader_path, part });
                return 0;
            } else {
                std.debug.print("[-] 失败: {s}\n", .{res.stderr});
                return 1;
            }
        }

        // 9. 查看 systemd-boot 内层引导配置与条目 (--loader-list / -ll)
        if (std.mem.eql(u8, sub, "--loader-list") or std.mem.eql(u8, sub, "-ll")) {
            var lcfg = try loader.loadConfig(io, allocator);
            defer lcfg.deinit(allocator);

            std.debug.print("================================================================================\n", .{});
            std.debug.print("           Linux Boot Manager (systemd-boot) 内层引导与内核配置\n", .{});
            std.debug.print("================================================================================\n", .{});
            std.debug.print("默认条目 (default):     {s}\n", .{lcfg.default_entry});
            const timeout_display = if (std.mem.eql(u8, lcfg.timeout, "0"))
                "0 秒 (取消倒计时直接进入)"
            else if (std.mem.eql(u8, lcfg.timeout, "menu-force"))
                "menu-force (强制停留菜单，不自动进入)"
            else
                lcfg.timeout;
            std.debug.print("菜单倒计时 (timeout):   {s}\n", .{timeout_display});
            std.debug.print("控制台模式 (console):   {s}\n", .{lcfg.console_mode});
            std.debug.print("--------------------------------------------------------------------------------\n", .{});

            for (lcfg.entries.items, 0..) |e, idx| {
                const def_tag = if (e.is_default) " [★ 默认项]" else "";
                std.debug.print("  #{d:<2} 条目: {s:<24}{s}\n", .{ idx + 1, e.id, def_tag });
                std.debug.print("      标题: {s}\n", .{e.title});
                if (e.sort_key.len > 0) std.debug.print("      排序: {s} (sort-key)\n", .{e.sort_key});
                if (e.linux.len > 0) std.debug.print("      内核: {s}\n", .{e.linux});
                if (e.efi.len > 0) std.debug.print("      EFI : {s}\n", .{e.efi});
                if (e.options.len > 0) std.debug.print("      参数: {s}\n", .{e.options});
                std.debug.print("\n", .{});
            }
            std.debug.print("================================================================================\n", .{});
            return 0;
        }

        // 10. 设置内层默认启动项 (--set-loader-default <ENTRY_ID>)
        if (std.mem.eql(u8, sub, "--set-loader-default") and argv.len > 2) {
            const entry_id = argv[2];
            var lcfg = try loader.loadConfig(io, allocator);
            defer lcfg.deinit(allocator);

            const res = try loader.saveGlobalConf(io, allocator, entry_id, lcfg.timeout, lcfg.console_mode);
            defer res.deinit(allocator);

            if (res.ok()) {
                std.debug.print("[✔] 成功将 systemd-boot 默认启动项设置为 '{s}'！(已写入 /boot/loader/loader.conf)\n", .{entry_id});
                return 0;
            } else {
                std.debug.print("[-] 失败: {s}\n", .{res.stderr});
                return 1;
            }
        }

        // 11. 设置内层超时时间 (--set-loader-timeout <VAL>)
        if (std.mem.eql(u8, sub, "--set-loader-timeout") and argv.len > 2) {
            const timeout_val = argv[2];
            var lcfg = try loader.loadConfig(io, allocator);
            defer lcfg.deinit(allocator);

            const res = try loader.saveGlobalConf(io, allocator, lcfg.default_entry, timeout_val, lcfg.console_mode);
            defer res.deinit(allocator);

            if (res.ok()) {
                std.debug.print("[✔] 成功将 systemd-boot 引导倒计时设置为 '{s}'！\n", .{timeout_val});
                return 0;
            } else {
                std.debug.print("[-] 失败: {s}\n", .{res.stderr});
                return 1;
            }
        }

        // 12. 设置内核启动参数 (--set-options <ENTRY_ID> "<OPTIONS>")
        if (std.mem.eql(u8, sub, "--set-options") and argv.len > 3) {
            const entry_id = argv[2];
            const new_options = argv[3];

            var lcfg = try loader.loadConfig(io, allocator);
            defer lcfg.deinit(allocator);

            const existing = lcfg.findEntry(entry_id);
            const title = if (existing) |e| e.title else entry_id;

            const res = try loader.saveEntryConfig(io, allocator, entry_id, title, new_options, null);
            defer res.deinit(allocator);

            if (res.ok()) {
                std.debug.print("[✔] 成功更新条目 '{s}' 的内核命令行参数！\n", .{entry_id});
                return 0;
            } else {
                std.debug.print("[-] 失败: {s}\n", .{res.stderr});
                return 1;
            }
        }

        // 13. 编辑内层条目标题与参数 (--loader-edit <ENTRY_ID> [--title <TITLE>] [--options <OPTIONS>] [--sort-key <KEY>])
        if (std.mem.eql(u8, sub, "--loader-edit") and argv.len > 2) {
            const entry_id = argv[2];
            var lcfg = try loader.loadConfig(io, allocator);
            defer lcfg.deinit(allocator);

            const existing = lcfg.findEntry(entry_id);
            var title: []const u8 = if (existing) |e| e.title else entry_id;
            var options: []const u8 = if (existing) |e| e.options else "";
            var sort_key: ?[]const u8 = null;

            var i: usize = 3;
            while (i < argv.len) : (i += 1) {
                if (std.mem.eql(u8, argv[i], "--title") and i + 1 < argv.len) {
                    i += 1;
                    title = argv[i];
                } else if (std.mem.eql(u8, argv[i], "--options") and i + 1 < argv.len) {
                    i += 1;
                    options = argv[i];
                } else if (std.mem.eql(u8, argv[i], "--sort-key") and i + 1 < argv.len) {
                    i += 1;
                    sort_key = argv[i];
                }
            }

            const res = try loader.saveEntryConfig(io, allocator, entry_id, title, options, sort_key);
            defer res.deinit(allocator);

            if (res.ok()) {
                std.debug.print("[✔] 成功更新内层条目 '{s}'！(标题: '{s}')\n", .{ entry_id, title });
                return 0;
            } else {
                std.debug.print("[-] 失败: {s}\n", .{res.stderr});
                return 1;
            }
        }

        // 14. 调整内层条目排位顺位 (--loader-move <FROM_IDX> <TO_IDX>)
        if (std.mem.eql(u8, sub, "--loader-move") and argv.len > 3) {
            const from_idx = std.fmt.parseInt(usize, argv[2], 10) catch {
                std.debug.print("[-] 错误: 源顺位必须为有效正整数 (例如 1)\n", .{});
                return 1;
            };
            const to_idx = std.fmt.parseInt(usize, argv[3], 10) catch {
                std.debug.print("[-] 错误: 目标顺位必须为有效正整数 (例如 2)\n", .{});
                return 1;
            };
            if (from_idx == 0 or to_idx == 0) {
                std.debug.print("[-] 错误: 顺位编号必须从 1 开始\n", .{});
                return 1;
            }

            var lcfg = try loader.loadConfig(io, allocator);
            defer lcfg.deinit(allocator);

            const res = try loader.moveLoaderEntry(io, allocator, lcfg, from_idx - 1, to_idx - 1);
            defer res.deinit(allocator);

            if (res.ok()) {
                std.debug.print("[✔] 成功将内层条目从顺位 #{d} 移动到顺位 #{d}！(已更新 sort-key 菜单排序)\n", .{ from_idx, to_idx });
                return 0;
            } else {
                std.debug.print("[-] 失败: {s}\n", .{res.stderr});
                return 1;
            }
        }

        // 15. 设置内层条目排序权重 (--loader-sort-key <ENTRY_ID> <KEY>)
        if (std.mem.eql(u8, sub, "--loader-sort-key") and argv.len > 3) {
            const entry_id = argv[2];
            const sort_key = argv[3];

            const res = try loader.setEntrySortKey(io, allocator, entry_id, sort_key);
            defer res.deinit(allocator);

            if (res.ok()) {
                std.debug.print("[✔] 成功为条目 '{s}' 设置 sort-key 为 '{s}'！\n", .{ entry_id, sort_key });
                return 0;
            } else {
                std.debug.print("[-] 失败: {s}\n", .{res.stderr});
                return 1;
            }
        }

        // 16. 删除内层启动条目 (--loader-delete <ENTRY_ID>)
        if (std.mem.eql(u8, sub, "--loader-delete") and argv.len > 2) {
            const entry_id = argv[2];

            const res = try loader.deleteEntry(io, allocator, entry_id);
            defer res.deinit(allocator);

            if (res.ok()) {
                std.debug.print("[✔] 成功删除内层条目 '{s}'！(已从 ESP 分区移除)\n", .{entry_id});
                return 0;
            } else {
                std.debug.print("[-] 失败: {s}\n", .{res.stderr});
                return 1;
            }
        }

        // 17. 一键全速秒进模式 (--auto-boot / --no-timeout / --direct-boot)
        if (std.mem.eql(u8, sub, "--auto-boot") or std.mem.eql(u8, sub, "--no-timeout") or std.mem.eql(u8, sub, "--direct-boot")) {
            const nv_res = try nvram.setTimeout(io, allocator, 0);
            defer nv_res.deinit(allocator);
            if (!nv_res.ok()) {
                std.debug.print("[-] 写入 NVRAM 失败: {s}\n", .{nv_res.stderr});
                return 1;
            }

            const ld_res = try loader.setLoaderTimeout(io, allocator, "0");
            defer ld_res.deinit(allocator);
            if (!ld_res.ok()) {
                std.debug.print("[-] 写入 loader.conf 失败: {s}\n", .{ld_res.stderr});
                return 1;
            }

            std.debug.print("==================================================\n", .{});
            std.debug.print("✔ 全速直接自动进入模式已成功开启！\n", .{});
            std.debug.print("  - 主板固件 NVRAM 倒计时: 0 秒 (取消等待)\n", .{});
            std.debug.print("  - systemd-boot 菜单倒计时: 0 秒 (取消倒计时直接进入)\n", .{});
            std.debug.print("  - 开机无需任何按键，闪电直达默认系统！\n", .{});
            std.debug.print("==================================================\n", .{});
            return 0;
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
        \\  python run.py                     启动原生 GTK4 高清图形界面 (默认)
        \\  python run.py selftest            运行内建全链路功能自检
        \\  python run.py --list              查看当前 NVRAM 全部启动项与顺序 (CLI)
        \\  python run.py --set-default <ID>  快速将指定条目设为第一启动项 (例如 0002)
        \\  python run.py --set-next <ID>     设置下次开机单次启动项 (BootNext)
        \\  python run.py --edit <ID> [...]   编辑现有启动项 (支持修改标签/路径/分区/状态)
        \\  python run.py --set-timeout <秒>  设置固件引导倒计时超时时间
        \\  python run.py --dedup             自动清理 BootOrder 重复项
        \\  python run.py --loader-list       查看 systemd-boot 内层引导项与内核配置
        \\  python run.py --set-loader-default <ID> 设置内层默认启动条目 (例如 arch-linux-zen.conf)
        \\  python run.py --set-loader-timeout <秒> 设置内层菜单倒计时 (例如 menu-force 或 3)
        \\  python run.py --set-options <ID> "<参数>" 设置指定内核条目的启动参数 (cmdline)
        \\  python run.py --loader-edit <ID> [...] 编辑内层条目标题与参数 (支持 --title/--options/--sort-key)
        \\  python run.py --loader-move <源顺位> <目标顺位> 切换调整内层启动条目排位顺序 (更新 sort-key)
        \\  python run.py --loader-sort-key <ID> <KEY> 直接为内层条目设置自定义 sort-key 排序键
        \\  python run.py --loader-delete <ID> 删除指定的内层引导条目文件 (安全移除)
        \\  python run.py --auto-boot         一键开启全速秒进 (主板与引导器均取消倒计时，0秒直入)
        \\  python run.py -h, --help          显示本帮助
        \\
        \\编辑参数 (配合 --edit 使用):
        \\  --label <名称>     设置启动项显示名称 (例如 "Linux Boot Manager")
        \\  --loader <路径>    设置 EFI 引导文件路径 (例如 "\\EFI\\systemd\\systemd-bootx64.efi")
        \\  --part <分区号>    设置目标 ESP 分区号 (例如 5)
        \\  --disk <磁盘设备>  设置磁盘设备路径 (默认 /dev/nvme0n1)
        \\  --active <0|1>     启用或禁用该条目
        \\
        \\内层参数 (配合 --loader-edit 使用):
        \\  --title <标题>     设置引导项显示标题
        \\  --options <参数>   设置内核启动参数 (cmdline)
        \\  --sort-key <键名>  设置引导项排序权重键 (例如 01-windows, 02-zen)
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
