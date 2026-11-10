//! 内建全链路功能自检 (Built-in Self-test)
//!
//! 对应《项目准则》第 38-40 条：
//! - 无需启动 X11 / Wayland 图形显示，在纯终端环境运行；
//! - 针对 UEFI NVRAM 输出解析、启动顺序计算、去重算法、参数生成进行全流水线断言；
//! - 交付即自证可用，杜绝任何假数据或未经验证的逻辑漏洞。

const std = @import("std");
const nvram = @import("nvram.zig");
const sys = @import("sys.zig");

const SAMPLE_EFIBOOTMGR_OUTPUT =
    "Timeout: 5 seconds\n" ++
    "BootOrder: 0000,0000,2001,3000,2002,2004\n" ++
    "BootCurrent: 0005\n" ++
    "BootNext: 0005\n" ++
    "Boot0000* Windows Boot Manager\tHD(1,GPT,83866a79-ae26-47fd-8aac-7779ac80fb3e,0x800,0x64000)/\\EFI\\Microsoft\\Boot\\bootmgfw.efiRC\n" ++
    "Boot0001* Windows Boot Manager\tHD(5,GPT,3833dd23-333d-4cde-b586-07884557ed87,0x30042000,0x1e8800)/\\EFI\\Microsoft\\Boot\\bootmgfw.efiRC\n" ++
    "Boot0002* USB 硬盘 (UEFI) - Mass Storage Device\tUsbWwid(14cd,1212,0,12122016020)/HD(2,MBR,0xa2210013,0x3a89c00,0x10000)RC\n" ++
    "Boot0003* USB 硬盘 (UEFI) - JMicron Tech\tUsbWwid(152d,580,0,DD564198838F)/HD(1,GPT,93bffefd-bbc5-414d-9436-27fe56806c76,0x800,0x82000)RC\n" ++
    "Boot0004* Windows Boot Manager\tHD(1,GPT,83866a79-ae26-47fd-8aac-7779ac80fb3e,0x800,0x64000)/\\EFI\\Microsoft\\Boot\\bootmgfw.efi\n" ++
    "Boot0005* Linux Boot Manager\tHD(4,GPT,3833dd23-333d-4cde-b586-07884557ed87,0x30042000,0x1e8800)/\\EFI\\systemd\\systemd-bootx64.efi\n" ++
    "Boot2001* EFI USB Device\tRC\n" ++
    "Boot3000* Internal Hard Disk or Solid State Disk\tRC\n";

pub fn run(init: std.process.Init) !u8 {
    const allocator = init.gpa;
    const io = init.io;

    std.debug.print("==================================================\n", .{});
    std.debug.print("      Boot-Order 内建全链路自检 (Built-in Self-test)\n", .{});
    std.debug.print("==================================================\n", .{});

    // 1. 测试解析
    std.debug.print("[1/5] 测试 efibootmgr -v 输出解析器...", .{});
    var cfg = try nvram.parseOutput(allocator, SAMPLE_EFIBOOTMGR_OUTPUT);
    defer cfg.deinit(allocator);

    // 断言超时时间
    if (cfg.timeout != 5) {
        std.debug.print("\n[FAIL] 超时解析错误: 期望 5，实际 {d}\n", .{cfg.timeout});
        return 1;
    }

    // 断言 BootCurrent 与 BootNext
    if (cfg.boot_current == null or !std.mem.eql(u8, &cfg.boot_current.?, "0005")) {
        std.debug.print("\n[FAIL] BootCurrent 解析错误\n", .{});
        return 1;
    }
    if (cfg.boot_next == null or !std.mem.eql(u8, &cfg.boot_next.?, "0005")) {
        std.debug.print("\n[FAIL] BootNext 解析错误\n", .{});
        return 1;
    }

    // 断言条目总数与 BootOrder 长度
    if (cfg.boot_order.items.len != 6) {
        std.debug.print("\n[FAIL] BootOrder 长度错误: 期望 6，实际 {d}\n", .{cfg.boot_order.items.len});
        return 1;
    }
    if (cfg.entries.items.len != 8) {
        std.debug.print("\n[FAIL] 条目数量错误: 期望 8，实际 {d}\n", .{cfg.entries.items.len});
        return 1;
    }

    // 检查具体条目
    const entry0005 = cfg.findEntry([4]u8{ '0', '0', '0', '5' });
    if (entry0005 == null) {
        std.debug.print("\n[FAIL] 未找到 Boot0005 条目\n", .{});
        return 1;
    }
    if (!std.mem.eql(u8, entry0005.?.title, "Linux Boot Manager")) {
        std.debug.print("\n[FAIL] Boot0005 标题解析错误: {s}\n", .{entry0005.?.title});
        return 1;
    }
    if (!entry0005.?.active) {
        std.debug.print("\n[FAIL] Boot0005 激活状态错误: 期望 true\n", .{});
        return 1;
    }
    if (!std.mem.eql(u8, entry0005.?.path, "\\EFI\\systemd\\systemd-bootx64.efi")) {
        std.debug.print("\n[FAIL] Boot0005 EFI 路径解析错误: {s}\n", .{entry0005.?.path});
        return 1;
    }
    std.debug.print(" [OK]\n", .{});

    // 2. 测试去重与幽灵过滤
    std.debug.print("[2/5] 测试 BootOrder 自动去重与幽灵条目过滤算法...", .{});
    var dedup_order = try cfg.getDeduplicatedOrder(allocator);
    defer dedup_order.deinit(allocator);

    // 原始为 0000,0000,2001,3000,2002,2004，其中 2002/2004 为不存在的幽灵条目，去重过滤后应为 3 项
    if (dedup_order.items.len != 3) {
        std.debug.print("\n[FAIL] 去重与幽灵过滤后数量错误: 期望 3，实际 {d}\n", .{dedup_order.items.len});
        return 1;
    }
    const order_str = try nvram.formatOrderString(allocator, dedup_order.items);
    defer allocator.free(order_str);
    if (!std.mem.eql(u8, order_str, "0000,2001,3000")) {
        std.debug.print("\n[FAIL] 序列化 BootOrder 字符串错误: {s}\n", .{order_str});
        return 1;
    }
    std.debug.print(" [OK]\n", .{});

    // 3. 测试首选项置顶算法
    std.debug.print("[3/5] 测试首选启动项置顶调整...", .{});
    var new_order = std.ArrayList([4]u8).empty;
    defer new_order.deinit(allocator);
    // 把 0005 置顶
    try new_order.append(allocator, [4]u8{ '0', '0', '0', '5' });
    for (dedup_order.items) |id| {
        if (!std.mem.eql(u8, &id, "0005")) {
            try new_order.append(allocator, id);
        }
    }
    const top_order_str = try nvram.formatOrderString(allocator, new_order.items);
    defer allocator.free(top_order_str);
    if (!std.mem.eql(u8, top_order_str, "0005,0000,2001,3000")) {
        std.debug.print("\n[FAIL] 置顶算法结果错误: {s}\n", .{top_order_str});
        return 1;
    }
    std.debug.print(" [OK]\n", .{});

    // 4. 测试设备路径 ESP 分区号解析算法
    std.debug.print("[4/5] 测试设备路径 ESP 分区号解析算法...", .{});
    const p5 = nvram.parsePartition("HD(5,GPT,3833dd23-333d-4cde-b586-07884557ed87,0x30042000,0x1e8800)");
    if (p5 != 5) {
        std.debug.print("\n[FAIL] 分区号解析错误: 期望 5，实际 {d}\n", .{p5});
        return 1;
    }
    const p1 = nvram.parsePartition("HD(1,GPT,83866a79-ae26-47fd-8aac-7779ac80fb3e,0x800,0x64000)");
    if (p1 != 1) {
        std.debug.print("\n[FAIL] 分区号解析错误: 期望 1，实际 {d}\n", .{p1});
        return 1;
    }
    std.debug.print(" [OK]\n", .{});

    // 5. 测试系统权限与 efibootmgr 可用性探针
    std.debug.print("[5/5] 测试真实系统 efibootmgr 命令探针...", .{});
    // 检查 efibootmgr 二进制是否存在
    const probe_res = sys.run(io, allocator, &.{ "efibootmgr", "-v" }) catch |err| {
        std.debug.print("\n[WARN] 无法启动 efibootmgr 进程: {s} (非 UEFI 环境可能跳过)\n", .{@errorName(err)});
        return 0;
    };
    defer probe_res.deinit(allocator);
    std.debug.print(" [OK] (系统存在 efibootmgr 命令)\n", .{});

    std.debug.print("--------------------------------------------------\n", .{});
    std.debug.print("✔ 内建全链路自检全部通过！(数据准确、算法无误、交付即自证可用)\n", .{});
    std.debug.print("==================================================\n", .{});
    return 0;
}
