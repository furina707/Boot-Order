//! 系统与底层进程交互薄封装
//!
//! 统一提供子进程调用（含 stdin 密码管道注入）、文件读取与权限状态查询。

const std = @import("std");
const Io = std.Io;

pub const RunResult = struct {
    stdout: []u8,
    stderr: []u8,
    exit_code: u8,

    pub fn deinit(self: RunResult, gpa: std.mem.Allocator) void {
        gpa.free(self.stdout);
        gpa.free(self.stderr);
    }

    pub fn ok(self: RunResult) bool {
        return self.exit_code == 0;
    }
};

extern "c" fn getuid() u32;

/// 检查当前进程是否具有 root 管理员权限 (uid == 0)
pub fn isRoot() bool {
    return getuid() == 0;
}

/// 执行普通命令并完整收集 stdout 与 stderr
pub fn run(io: Io, gpa: std.mem.Allocator, argv: []const []const u8) !RunResult {
    const res = try std.process.run(gpa, io, .{
        .argv = argv,
        .stdout_limit = .limited(16 * 1024 * 1024),
        .stderr_limit = .limited(4 * 1024 * 1024),
    });
    return .{
        .stdout = res.stdout,
        .stderr = res.stderr,
        .exit_code = switch (res.term) {
            .exited => |c| c,
            else => 255,
        },
    };
}

/// 执行命令并通过 stdin 写入数据（用于向 sudo -S 注入密码）
pub fn runWithStdin(io: Io, gpa: std.mem.Allocator, argv: []const []const u8, stdin_text: []const u8) !RunResult {
    var child = try std.process.spawn(io, .{
        .argv = argv,
        .stdin = .pipe,
        .stdout = .pipe,
        .stderr = .pipe,
    });
    defer child.kill(io);

    if (child.stdin) |f| {
        f.writeStreamingAll(io, stdin_text) catch {};
        f.close(io);
        child.stdin = null;
    }

    var mr_buf: Io.File.MultiReader.Buffer(2) = undefined;
    var mr: Io.File.MultiReader = undefined;
    mr.init(gpa, io, mr_buf.toStreams(), &.{ child.stdout.?, child.stderr.? });
    defer mr.deinit();

    while (mr.fill(64, .none)) |_| {} else |err| switch (err) {
        error.EndOfStream => {},
        else => return err,
    }
    try mr.checkAnyError();

    const term = try child.wait(io);
    const out = try mr.toOwnedSlice(0);
    errdefer gpa.free(out);
    const err_out = try mr.toOwnedSlice(1);

    return .{
        .stdout = out,
        .stderr = err_out,
        .exit_code = switch (term) {
            .exited => |c| c,
            else => 255,
        },
    };
}
