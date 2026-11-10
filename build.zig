const std = @import("std");

/// Boot Order Manager —— UEFI 固件启动项与 NVRAM 综合管理器 (GTK4 + Zig 原生)
///
/// 设计要点（严格遵循《项目准则》）：
///   - Zig 原生、Zero Runtime：编译产物为单一独立 ELF，冷启动毫秒级；
///   - UI 采用系统 GTK4 原生控件（现代卡片式布局、原生 HiDPI 支持，绝无位图拉伸发虚）；
///   - run.py 为唯一定义启动器，build.zig 专注纯粹构建与自检；
///   - 零 Mock、零假数据：直接真实交互主板 UEFI NVRAM (efibootmgr)；
///   - 内建全链路自检 (Built-in Self-test): 支持无头终端环境回归验证。
pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});

    // GTK4 的 C 头文件翻译为 Zig 模块 (Zig 0.17 要求构建期 translate-c)
    const gtk_tc = b.addTranslateC(.{
        .root_source_file = b.path("src/gtk_c.h"),
        .target = target,
        .optimize = optimize,
        .link_libc = true,
    });
    for (gtk_include_dirs) |dir| {
        gtk_tc.addSystemIncludePath(.{ .cwd_relative = dir });
    }

    const exe = b.addExecutable(.{
        .name = "boot-order",
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/main.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
        }),
    });
    exe.root_module.addImport("gtk", gtk_tc.createModule());
    for (gtk_libs) |lib| {
        exe.root_module.linkSystemLibrary(lib, .{});
    }

    b.installArtifact(exe);

    // zig build run
    const run_cmd = b.addRunArtifact(exe);
    run_cmd.step.dependOn(b.getInstallStep());
    const run_step = b.step("run", "启动 UEFI 启动项管理器 (GTK4)");
    run_step.dependOn(&run_cmd.step);

    // zig build selftest: 无头终端自动化全链路断言自检
    const selftest_cmd = b.addRunArtifact(exe);
    selftest_cmd.step.dependOn(b.getInstallStep());
    selftest_cmd.addArg("selftest");
    const selftest_step = b.step("selftest", "执行内建全链路功能自检");
    selftest_step.dependOn(&selftest_cmd.step);
}

/// GTK4 C 头文件搜索路径 (标准 Arch Linux 布局)
const gtk_include_dirs = [_][]const u8{
    "/usr/include/gtk-4.0",
    "/usr/include/pango-1.0",
    "/usr/include/fribidi",
    "/usr/include/harfbuzz",
    "/usr/include/gdk-pixbuf-2.0",
    "/usr/include/glycin-2",
    "/usr/include/cairo",
    "/usr/include/freetype2",
    "/usr/include/libpng16",
    "/usr/include/pixman-1",
    "/usr/include/graphene-1.0",
    "/usr/lib/graphene-1.0/include",
    "/usr/include/glib-2.0",
    "/usr/lib/glib-2.0/include",
    "/usr/include/libmount",
    "/usr/include/blkid",
    "/usr/include/sysprof-6",
};

/// 链接库
const gtk_libs = [_][]const u8{
    "gtk-4",
    "gobject-2.0",
    "glib-2.0",
    "gio-2.0",
    "pangocairo-1.0",
    "pango-1.0",
    "harfbuzz",
    "gdk_pixbuf-2.0",
    "cairo-gobject",
    "cairo",
    "graphene-1.0",
};
