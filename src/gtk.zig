//! GTK4 C 绑定薄封装层
//!
//! 提供对 translate-c 产物的安全封装，统一处理 NUL 结尾 C 字符串、
//! 控件指针类型转换与 GObject 生命周期管理。

const std = @import("std");

/// translate-c 生成的原始绑定。模块名由 build.zig 注入为 "gtk"。
pub const c = @import("gtk");

/// 将 Zig 0 结尾字符串切片转为 GTK 接受的 C 字符串指针。
/// 严格要求调用方必须传入 `[:0]const u8`，杜绝未以 0 截断导致的乱码与内存越界。
pub inline fn s(text: [:0]const u8) [*c]const u8 {
    return @ptrCast(text.ptr);
}

/// 将具体 GTK 控件对象指针转换为基类 `[*c]GtkWidget` 指针。
pub inline fn w(ptr: anytype) [*c]c.GtkWidget {
    return @ptrCast(@alignCast(ptr));
}

/// glib 的 `g_object_unref` 安全封装。
pub inline fn unref(obj: anytype) void {
    c.g_object_unref(@ptrCast(obj));
}

/// 环形缓冲快速将任意 `[]const u8` 转为 NUL 结尾切片 `[:0]const u8`
pub fn zstr(text: []const u8) [:0]const u8 {
    const Slot = struct {
        var bufs: [16][2048]u8 = undefined;
        var next: usize = 0;
    };
    const n = @min(text.len, Slot.bufs[0].len - 1);
    const i = Slot.next;
    Slot.next = (Slot.next + 1) % Slot.bufs.len;
    @memcpy(Slot.bufs[i][0..n], text[0..n]);
    Slot.bufs[i][n] = 0;
    return Slot.bufs[i][0..n :0];
}

/// 环形缓冲格式化为 NUL 结尾切片 `[:0]const u8`
pub fn zfmt(comptime format: []const u8, args: anytype) [:0]const u8 {
    const Slot = struct {
        var bufs: [16][2048]u8 = undefined;
        var next: usize = 0;
    };
    const i = Slot.next;
    Slot.next = (Slot.next + 1) % Slot.bufs.len;
    const slice = std.fmt.bufPrint(Slot.bufs[i][0 .. Slot.bufs[i].len - 1], format, args) catch "fmt_err";
    Slot.bufs[i][slice.len] = 0;
    return Slot.bufs[i][0..slice.len :0];
}
