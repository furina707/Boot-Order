//! UEFI 启动项与 NVRAM 管理器 —— GTK4 主界面
//!
//! 遵循《项目准则》：
//!   - 原生 GTK4 控件，原生 HiDPI 物理点阵渲染，绝无 DWM 位图拉伸模糊；
//!   - miuix 风格卡片式布局：圆角卡片、低饱和色彩、清晰状态药丸徽章；
//!   - 零 Mock、零模拟数据：所有显示与变更直接与主板 NVRAM 实时同步；
//!   - 智能诊断：自动检测未加入 BootOrder 的引导项（如 Linux Boot Manager），提供一键置顶修复；
//!   - 默认安全：删除危险操作配备模态对话框二次防误触确认。

const std = @import("std");
const gtk = @import("gtk.zig");
const c = gtk.c;
const nvram = @import("nvram.zig");
const sys = @import("sys.zig");
const sdboot = @import("loader.zig");

pub const meta = struct {
    pub const app_id = "org.arch.BootOrder";
    pub const title = "UEFI 启动项与引导管理";
    pub const version = "2.0.0";
};

pub const AppState = struct {
    io: std.Io,
    gpa: std.mem.Allocator,

    application: ?*c.GtkApplication = null,
    window: ?*c.GtkWindow = null,
    list_box: ?*c.GtkListBox = null,
    summary_label: ?*c.GtkLabel = null,
    btn_clear_next: ?*c.GtkWidget = null,
    warning_box: ?*c.GtkBox = null,
    warning_label: ?*c.GtkLabel = null,
    warning_button: ?*c.GtkWidget = null,
    status_label: ?*c.GtkLabel = null,

    cfg: ?nvram.BootConfig = null,
    orphan_ident: ?[4]u8 = null,

    // 缓存每行对应条目的 ID
    row_idents: std.ArrayList([4]u8),

    pub fn init(io: std.Io, gpa: std.mem.Allocator) AppState {
        return .{
            .io = io,
            .gpa = gpa,
            .row_idents = std.ArrayList([4]u8).empty,
        };
    }

    pub fn deinit(self: *AppState) void {
        self.row_idents.deinit(self.gpa);
        if (self.cfg) |*cfg| {
            cfg.deinit(self.gpa);
            self.cfg = null;
        }
    }
};

var state: AppState = undefined;

pub fn run(init: std.process.Init) !u8 {
    state = AppState.init(init.io, init.gpa);
    defer state.deinit();

    const application = c.gtk_application_new(
        gtk.s(meta.app_id),
        @intCast(c.G_APPLICATION_DEFAULT_FLAGS),
    );
    if (application == null) return error.GtkInitFailed;
    state.application = @ptrCast(application);

    _ = c.g_signal_connect_data(
        @ptrCast(application),
        gtk.s("activate"),
        @ptrCast(&onActivate),
        null,
        null,
        0,
    );

    const status = c.g_application_run(@ptrCast(application), 0, null);
    gtk.unref(application);
    return @intCast(status);
}

// ============================================================================
// CSS 样式 (miuix 风格)
// ============================================================================

const CSS_TEXT =
    \\ .card {
    \\   background-color: alpha(@theme_bg_color, 0.7);
    \\   border: 1px solid alpha(@borders, 0.5);
    \\   border-radius: 12px;
    \\   padding: 10px 14px;
    \\   margin-bottom: 6px;
    \\ }
    \\ .card-first {
    \\   background-color: alpha(@accent_color, 0.12);
    \\   border: 1.5px solid @accent_color;
    \\   border-radius: 12px;
    \\   padding: 10px 14px;
    \\   margin-bottom: 6px;
    \\ }
    \\ .card-warning {
    \\   background-color: alpha(#e5a50a, 0.15);
    \\   border: 1px solid #e5a50a;
    \\   border-radius: 12px;
    \\   padding: 10px 14px;
    \\   margin-bottom: 8px;
    \\ }
    \\ .card-loader {
    \\   background-color: alpha(@theme_bg_color, 0.6);
    \\   border: 1px solid alpha(#3584e4, 0.35);
    \\   border-radius: 12px;
    \\   padding: 12px 14px;
    \\   margin-bottom: 8px;
    \\ }
    \\ .card-banner {
    \\   background-color: alpha(#3584e4, 0.12);
    \\   border: 1.5px solid alpha(#3584e4, 0.5);
    \\   border-radius: 10px;
    \\   padding: 10px 14px;
    \\   margin-bottom: 10px;
    \\ }
    \\ .pill-badge {
    \\   border-radius: 6px;
    \\   padding: 3px 8px;
    \\   font-weight: bold;
    \\   font-size: 11px;
    \\ }
    \\ .badge-green {
    \\   background-color: #2ec27e;
    \\   color: white;
    \\ }
    \\ .badge-blue {
    \\   background-color: #3584e4;
    \\   color: white;
    \\ }
    \\ .badge-purple {
    \\   background-color: #9141ac;
    \\   color: white;
    \\ }
    \\ .badge-amber {
    \\   background-color: #e5a50a;
    \\   color: black;
    \\ }
    \\ .badge-gray {
    \\   background-color: #77767b;
    \\   color: white;
    \\ }
    \\ .entry-title {
    \\   font-size: 14px;
    \\   font-weight: 700;
    \\ }
    \\ .entry-sub {
    \\   font-size: 11px;
    \\   opacity: 0.75;
    \\   font-family: monospace;
    \\ }
;

fn installCss() void {
    const provider = c.gtk_css_provider_new();
    c.gtk_css_provider_load_from_string(provider, gtk.s(CSS_TEXT));

    const display = c.gdk_display_get_default();
    if (display) |d| {
        c.gtk_style_context_add_provider_for_display(
            d,
            @ptrCast(provider),
            @intCast(c.GTK_STYLE_PROVIDER_PRIORITY_APPLICATION),
        );
    }
    gtk.unref(provider);
}

// ============================================================================
// 界面初始化与主框架
// ============================================================================

fn onActivate(application: *c.GtkApplication, _: ?*anyopaque) callconv(.c) void {
    installCss();

    const window = c.gtk_application_window_new(application);
    state.window = @ptrCast(window);
    c.gtk_window_set_title(@ptrCast(window), gtk.s(meta.title));
    c.gtk_window_set_default_size(@ptrCast(window), 980, 680);

    // 顶部标题栏 HeaderBar
    const header = buildHeaderBar();
    c.gtk_window_set_titlebar(@ptrCast(window), header);

    // 主容器 (垂直 Box)
    const root_box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_VERTICAL), 10);
    c.gtk_widget_set_margin_start(@ptrCast(root_box), 16);
    c.gtk_widget_set_margin_end(@ptrCast(root_box), 16);
    c.gtk_widget_set_margin_top(@ptrCast(root_box), 12);
    c.gtk_widget_set_margin_bottom(@ptrCast(root_box), 12);

    // 1. 顶部全局概览卡片
    const summary_box = buildSummaryCard();
    c.gtk_box_append(@ptrCast(root_box), summary_box);

    // 2. 诊断警告栏（若有条目未加入 BootOrder，自动显示）
    const warn_box = buildWarningBanner();
    state.warning_box = @ptrCast(warn_box);
    c.gtk_box_append(@ptrCast(root_box), warn_box);

    // 3. 中间可滚动列表
    const scroll = c.gtk_scrolled_window_new();
    c.gtk_widget_set_vexpand(@ptrCast(scroll), 1);
    c.gtk_widget_set_hexpand(@ptrCast(scroll), 1);

    const list_box = c.gtk_list_box_new();
    state.list_box = @ptrCast(list_box);
    c.gtk_list_box_set_selection_mode(@ptrCast(list_box), @intCast(c.GTK_SELECTION_NONE));
    c.gtk_scrolled_window_set_child(@ptrCast(scroll), @ptrCast(list_box));
    c.gtk_box_append(@ptrCast(root_box), @ptrCast(scroll));

    // 4. 底部状态栏
    const status_box = buildStatusBar();
    c.gtk_box_append(@ptrCast(root_box), status_box);

    c.gtk_window_set_child(@ptrCast(window), @ptrCast(root_box));
    c.gtk_window_present(@ptrCast(window));

    // 加载数据并刷新 UI
    refreshData();
}

fn buildHeaderBar() *c.GtkWidget {
    const header = c.gtk_header_bar_new();

    // 刷新按钮
    const btn_refresh = c.gtk_button_new_with_label(gtk.s("⟳ 刷新 NVRAM"));
    _ = c.g_signal_connect_data(
        @ptrCast(btn_refresh),
        gtk.s("clicked"),
        @ptrCast(&onRefreshClicked),
        null,
        null,
        0,
    );
    c.gtk_header_bar_pack_start(@ptrCast(header), btn_refresh);

    // 超时设置按钮
    const btn_timeout = c.gtk_button_new_with_label(gtk.s("⏱ 设置超时"));
    _ = c.g_signal_connect_data(
        @ptrCast(btn_timeout),
        gtk.s("clicked"),
        @ptrCast(&onSetTimeoutClicked),
        null,
        null,
        0,
    );
    c.gtk_header_bar_pack_start(@ptrCast(header), btn_timeout);

    // 清理重复项按钮
    const btn_dedup = c.gtk_button_new_with_label(gtk.s("🧹 清理重复项"));
    _ = c.g_signal_connect_data(
        @ptrCast(btn_dedup),
        gtk.s("clicked"),
        @ptrCast(&onDedupClicked),
        null,
        null,
        0,
    );
    c.gtk_header_bar_pack_end(@ptrCast(header), btn_dedup);

    // 添加新启动项按钮
    const btn_add = c.gtk_button_new_with_label(gtk.s("➕ 注册新条目"));
    _ = c.g_signal_connect_data(
        @ptrCast(btn_add),
        gtk.s("clicked"),
        @ptrCast(&onAddEntryClicked),
        null,
        null,
        0,
    );
    c.gtk_header_bar_pack_end(@ptrCast(header), btn_add);

    return @ptrCast(header);
}

fn buildSummaryCard() *c.GtkWidget {
    const card = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 16);
    c.gtk_widget_add_css_class(@ptrCast(card), gtk.s("card"));

    const lbl = c.gtk_label_new(gtk.s("正在读取 UEFI NVRAM 固件数据…"));
    c.gtk_label_set_wrap(@ptrCast(lbl), 1);
    c.gtk_label_set_xalign(@ptrCast(lbl), 0.0);
    c.gtk_widget_set_hexpand(@ptrCast(lbl), 1);
    state.summary_label = @ptrCast(lbl);
    c.gtk_box_append(@ptrCast(card), @ptrCast(lbl));

    const btn_clear = c.gtk_button_new_with_label(gtk.s("✖ 取消 BootNext"));
    c.gtk_widget_add_css_class(@ptrCast(btn_clear), gtk.s("pill-badge"));
    c.gtk_widget_set_valign(@ptrCast(btn_clear), @intCast(c.GTK_ALIGN_CENTER));
    _ = c.g_signal_connect_data(
        @ptrCast(btn_clear),
        gtk.s("clicked"),
        @ptrCast(&onClearBootNextClicked),
        null,
        null,
        0,
    );
    state.btn_clear_next = btn_clear;
    c.gtk_widget_set_visible(@ptrCast(btn_clear), 0);
    c.gtk_box_append(@ptrCast(card), btn_clear);

    return @ptrCast(card);
}

fn buildWarningBanner() *c.GtkWidget {
    const banner = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 12);
    c.gtk_widget_add_css_class(@ptrCast(banner), gtk.s("card-warning"));

    const icon_lbl = c.gtk_label_new(gtk.s("⚠️"));
    c.gtk_box_append(@ptrCast(banner), @ptrCast(icon_lbl));

    const warn_lbl = c.gtk_label_new(gtk.s("检测到未加入启动顺位的系统引导项"));
    c.gtk_label_set_xalign(@ptrCast(warn_lbl), 0.0);
    c.gtk_widget_set_hexpand(@ptrCast(warn_lbl), 1);
    state.warning_label = @ptrCast(warn_lbl);
    c.gtk_box_append(@ptrCast(banner), @ptrCast(warn_lbl));

    const fix_btn = c.gtk_button_new_with_label(gtk.s("👑 设为第一优先启动项"));
    c.gtk_widget_add_css_class(@ptrCast(fix_btn), gtk.s("suggested-action"));
    _ = c.g_signal_connect_data(
        @ptrCast(fix_btn),
        gtk.s("clicked"),
        @ptrCast(&onFixWarningClicked),
        null,
        null,
        0,
    );
    state.warning_button = fix_btn;
    c.gtk_box_append(@ptrCast(banner), fix_btn);

    // 默认隐藏，在 refreshData 时根据诊断按需呈现
    c.gtk_widget_set_visible(@ptrCast(banner), 0);
    return @ptrCast(banner);
}

fn buildStatusBar() *c.GtkWidget {
    const bar = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 8);
    c.gtk_widget_set_margin_top(@ptrCast(bar), 4);

    const lbl = c.gtk_label_new(gtk.s("就绪。所有写入操作均基于 sudo 自动提权真实写入主板 NVRAM。"));
    c.gtk_label_set_xalign(@ptrCast(lbl), 0.0);
    state.status_label = @ptrCast(lbl);
    c.gtk_box_append(@ptrCast(bar), @ptrCast(lbl));

    return @ptrCast(bar);
}

// ============================================================================
// 数据装载与列表重建
// ============================================================================

fn refreshData() void {
    if (state.cfg) |*old_cfg| {
        old_cfg.deinit(state.gpa);
        state.cfg = null;
    }

    state.cfg = nvram.loadConfig(state.io, state.gpa) catch |err| {
        showStatus("❌ 无法从主板读取 UEFI NVRAM 启动项", true);
        std.debug.print("[-] 读取 NVRAM 失败: {s}\n", .{@errorName(err)});
        return;
    };

    const cfg = state.cfg.?;

    // 1. 更新概览文本
    var first_title: []const u8 = "无";
    var first_id: []const u8 = "----";
    if (cfg.boot_order.items.len > 0) {
        const fid = cfg.boot_order.items[0];
        first_id = &fid;
        if (cfg.findEntry(fid)) |e| {
            first_title = e.title;
        }
    }

    const next_str = if (cfg.boot_next) |bn| bn[0..4] else "无";
    const is_elevated = sys.isRoot();
    const sum_text = gtk.zfmt(
        "👑 <b>首选启动项</b>: [{s}] {s}   │   🚀 <b>单次启动 (BootNext)</b>: {s}   │   ⏱ <b>倒计时</b>: {d} 秒   │   🛡 <b>权限</b>: {s}",
        .{
            first_id,
            first_title,
            next_str,
            cfg.timeout,
            if (is_elevated) "Root 原生" else "sudo 认证已就绪 (1231)",
        },
    );
    c.gtk_label_set_markup(@ptrCast(state.summary_label.?), gtk.s(sum_text));

    // 取消 BootNext 按钮状态
    if (cfg.boot_next != null) {
        if (state.btn_clear_next) |btn| c.gtk_widget_set_visible(@ptrCast(btn), 1);
    } else {
        if (state.btn_clear_next) |btn| c.gtk_widget_set_visible(@ptrCast(btn), 0);
    }

    // 2. 诊断未排序条目 (例如 Linux Boot Manager 在 NVRAM 存在但不在 BootOrder 里)
    state.orphan_ident = null;
    var found_orphan = false;
    for (cfg.entries.items) |e| {
        if (!cfg.inBootOrder(e.ident)) {
            state.orphan_ident = e.ident;
            const wtext = gtk.zfmt(
                "条目 [Boot{s}] <b>{s}</b> 存在于主板 NVRAM，但<b>尚未加入启动顺序 (BootOrder)</b>！",
                .{ e.ident, e.title },
            );
            c.gtk_label_set_markup(@ptrCast(state.warning_label.?), gtk.s(wtext));
            c.gtk_widget_set_visible(@ptrCast(state.warning_box.?), 1);
            found_orphan = true;
            break;
        }
    }
    if (!found_orphan) {
        c.gtk_widget_set_visible(@ptrCast(state.warning_box.?), 0);
    }

    // 3. 重建列表
    state.row_idents.clearRetainingCapacity();

    // 清空 list_box 中的所有旧子控件
    while (c.gtk_widget_get_first_child(gtk.w(state.list_box.?))) |child| {
        c.gtk_list_box_remove(@ptrCast(state.list_box.?), child);
    }

    // 首先按 BootOrder 顺序添加
    for (cfg.boot_order.items, 0..) |id, idx| {
        state.row_idents.append(state.gpa, id) catch {};
        const entry = cfg.findEntry(id);
        const card = createEntryCard(id, entry, idx, true, idx == 0, idx == cfg.boot_order.items.len - 1);
        c.gtk_list_box_append(@ptrCast(state.list_box.?), card);
    }

    // 其次添加不在 BootOrder 中的其他条目 (未排序项)
    for (cfg.entries.items) |e| {
        if (!cfg.inBootOrder(e.ident)) {
            state.row_idents.append(state.gpa, e.ident) catch {};
            const card = createEntryCard(e.ident, e, 9999, false, false, false);
            c.gtk_list_box_append(@ptrCast(state.list_box.?), card);
        }
    }
}

fn createEntryCard(
    ident: [4]u8,
    maybe_entry: ?nvram.BootEntry,
    seq_idx: usize,
    in_order: bool,
    is_first: bool,
    is_last: bool,
) *c.GtkWidget {
    const card = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 12);
    if (is_first) {
        c.gtk_widget_add_css_class(@ptrCast(card), gtk.s("card-first"));
    } else {
        c.gtk_widget_add_css_class(@ptrCast(card), gtk.s("card"));
    }

    // ── 左侧：序号与识别标志 ──
    const left_box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_VERTICAL), 4);
    c.gtk_widget_set_size_request(@ptrCast(left_box), 90, -1);

    const badge_lbl = if (in_order)
        if (is_first)
            gtk.zstr("★ 顺位 #1")
        else
            gtk.zfmt("顺位 #{d}", .{seq_idx + 1})
    else
        gtk.zstr("未在顺序中");

    const badge = c.gtk_label_new(gtk.s(badge_lbl));
    c.gtk_widget_add_css_class(@ptrCast(badge), gtk.s("pill-badge"));
    if (is_first) {
        c.gtk_widget_add_css_class(@ptrCast(badge), gtk.s("badge-blue"));
    } else if (in_order) {
        c.gtk_widget_add_css_class(@ptrCast(badge), gtk.s("badge-gray"));
    } else {
        c.gtk_widget_add_css_class(@ptrCast(badge), gtk.s("badge-amber"));
    }
    c.gtk_box_append(@ptrCast(left_box), @ptrCast(badge));

    const id_lbl = c.gtk_label_new(gtk.s(gtk.zfmt("Boot{s}", .{ident})));
    c.gtk_widget_add_css_class(@ptrCast(id_lbl), gtk.s("entry-sub"));
    c.gtk_box_append(@ptrCast(left_box), @ptrCast(id_lbl));

    c.gtk_box_append(@ptrCast(card), @ptrCast(left_box));

    // ── 中间：条目标题与详情 ──
    const mid_box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_VERTICAL), 3);
    c.gtk_widget_set_hexpand(@ptrCast(mid_box), 1);

    const title_row = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 8);

    const title_str = if (maybe_entry) |e| e.title else "(未知启动项)";
    const title_lbl = c.gtk_label_new(gtk.s(gtk.zstr(title_str)));
    c.gtk_label_set_xalign(@ptrCast(title_lbl), 0.0);
    c.gtk_widget_add_css_class(@ptrCast(title_lbl), gtk.s("entry-title"));
    c.gtk_box_append(@ptrCast(title_row), @ptrCast(title_lbl));

    // 激活状态小药丸
    const is_active = if (maybe_entry) |e| e.active else false;
    const act_lbl = c.gtk_label_new(if (is_active) gtk.s("已启用 ✔") else gtk.s("已禁用 ✖"));
    c.gtk_widget_add_css_class(@ptrCast(act_lbl), gtk.s("pill-badge"));
    c.gtk_widget_add_css_class(@ptrCast(act_lbl), if (is_active) gtk.s("badge-green") else gtk.s("badge-gray"));
    c.gtk_box_append(@ptrCast(title_row), @ptrCast(act_lbl));

    // 单次启动标记
    if (state.cfg) |cfg| {
        if (cfg.boot_next) |bn| {
            if (std.mem.eql(u8, &bn, &ident)) {
                const next_lbl = c.gtk_label_new(gtk.s("下次单次启动项 🚀"));
                c.gtk_widget_add_css_class(@ptrCast(next_lbl), gtk.s("pill-badge"));
                c.gtk_widget_add_css_class(@ptrCast(next_lbl), gtk.s("badge-amber"));
                c.gtk_box_append(@ptrCast(title_row), @ptrCast(next_lbl));
            }
        }
    }

    c.gtk_box_append(@ptrCast(mid_box), @ptrCast(title_row));

    // 路径与设备信息
    const path_str = if (maybe_entry) |e|
        if (e.path.len > 0) e.path else e.raw_details
    else
        "固件预置硬件条目";
    const path_lbl = c.gtk_label_new(gtk.s(gtk.zfmt("路径: {s}", .{path_str})));
    c.gtk_label_set_xalign(@ptrCast(path_lbl), 0.0);
    c.gtk_label_set_ellipsize(@ptrCast(path_lbl), @intCast(c.PANGO_ELLIPSIZE_END));
    c.gtk_widget_add_css_class(@ptrCast(path_lbl), gtk.s("entry-sub"));
    c.gtk_box_append(@ptrCast(mid_box), @ptrCast(path_lbl));

    c.gtk_box_append(@ptrCast(card), @ptrCast(mid_box));

    // ── 右侧：操作按钮组 ──
    const btn_box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 6);
    c.gtk_widget_set_valign(@ptrCast(btn_box), @intCast(c.GTK_ALIGN_CENTER));

    // 传递条目 ID 作为按钮参数
    const user_data = packIdent(ident);

    // 1. 置顶第一项
    if (!is_first) {
        const btn_top = c.gtk_button_new_with_label(gtk.s("👑 置顶首选"));
        _ = c.g_signal_connect_data(@ptrCast(btn_top), gtk.s("clicked"), @ptrCast(&onSetDefaultClicked), user_data, null, 0);
        c.gtk_box_append(@ptrCast(btn_box), btn_top);
    }

    // 2. 单次启动 BootNext
    const btn_next = c.gtk_button_new_with_label(gtk.s("🚀 单次启动"));
    _ = c.g_signal_connect_data(@ptrCast(btn_next), gtk.s("clicked"), @ptrCast(&onSetBootNextClicked), user_data, null, 0);
    c.gtk_box_append(@ptrCast(btn_box), btn_next);

    // 3. 上移
    if (in_order and !is_first) {
        const btn_up = c.gtk_button_new_with_label(gtk.s("▲"));
        _ = c.g_signal_connect_data(@ptrCast(btn_up), gtk.s("clicked"), @ptrCast(&onMoveUpClicked), user_data, null, 0);
        c.gtk_box_append(@ptrCast(btn_box), btn_up);
    }

    // 4. 下移
    if (in_order and !is_last) {
        const btn_down = c.gtk_button_new_with_label(gtk.s("▼"));
        _ = c.g_signal_connect_data(@ptrCast(btn_down), gtk.s("clicked"), @ptrCast(&onMoveDownClicked), user_data, null, 0);
        c.gtk_box_append(@ptrCast(btn_box), btn_down);
    }

    // 5. 启停状态切换
    const toggle_text = if (is_active) "禁用" else "启用";
    const btn_toggle = c.gtk_button_new_with_label(gtk.s(gtk.zstr(toggle_text)));
    _ = c.g_signal_connect_data(@ptrCast(btn_toggle), gtk.s("clicked"), @ptrCast(&onToggleActiveClicked), user_data, null, 0);
    c.gtk_box_append(@ptrCast(btn_box), btn_toggle);

    // 6. 编辑条目 (修改显示名称、分区号、加载器路径与状态)
    const btn_edit = c.gtk_button_new_with_label(gtk.s("✏ 编辑"));
    _ = c.g_signal_connect_data(@ptrCast(btn_edit), gtk.s("clicked"), @ptrCast(&onEditClicked), user_data, null, 0);
    c.gtk_box_append(@ptrCast(btn_box), btn_edit);

    // 6.1 若该条目为 Linux 引导加载器 (systemd-boot)，直接呈现「⚙ 内层引导」快捷入口
    const is_sd_boot = if (maybe_entry) |e|
        (std.mem.indexOf(u8, e.path, "systemd-boot") != null or
            std.mem.indexOf(u8, e.title, "Linux Boot Manager") != null or
            std.mem.indexOf(u8, e.title, "Arch Linux") != null)
    else
        false;

    if (is_sd_boot) {
        const btn_loader = c.gtk_button_new_with_label(gtk.s("⚙ 内层引导"));
        c.gtk_widget_add_css_class(@ptrCast(btn_loader), gtk.s("suggested-action"));
        _ = c.g_signal_connect_data(@ptrCast(btn_loader), gtk.s("clicked"), @ptrCast(&onOpenLoaderConfig), null, null, 0);
        c.gtk_box_append(@ptrCast(btn_box), btn_loader);
    }

    // 7. 删除（安全模态确认防误触）
    const btn_del = c.gtk_button_new_with_label(gtk.s("🗑"));
    c.gtk_widget_add_css_class(@ptrCast(btn_del), gtk.s("destructive-action"));
    _ = c.g_signal_connect_data(@ptrCast(btn_del), gtk.s("clicked"), @ptrCast(&onDeleteClicked), user_data, null, 0);
    c.gtk_box_append(@ptrCast(btn_box), btn_del);

    c.gtk_box_append(@ptrCast(card), @ptrCast(btn_box));

    return @ptrCast(card);
}

// ============================================================================
// 按钮回调与 NVRAM 写入分发
// ============================================================================

fn packIdent(ident: [4]u8) ?*anyopaque {
    const val = @as(usize, ident[0]) |
        (@as(usize, ident[1]) << 8) |
        (@as(usize, ident[2]) << 16) |
        (@as(usize, ident[3]) << 24);
    return @ptrFromInt(val);
}

fn unpackIdent(data: ?*anyopaque) [4]u8 {
    const val = @intFromPtr(data);
    return .{
        @intCast(val & 0xFF),
        @intCast((val >> 8) & 0xFF),
        @intCast((val >> 16) & 0xFF),
        @intCast((val >> 24) & 0xFF),
    };
}

fn showStatus(msg: []const u8, is_error: bool) void {
    if (state.status_label) |lbl| {
        const color = if (is_error) "#e01b24" else "#2ec27e";
        const formatted = gtk.zfmt("<span color=\"{s}\">{s}</span>", .{ color, msg });
        c.gtk_label_set_markup(@ptrCast(lbl), gtk.s(formatted));
    }
}

fn onRefreshClicked(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    refreshData();
    showStatus("✔ NVRAM 数据已刷新至最新状态", false);
}

fn onClearBootNextClicked(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    const res = nvram.clearBootNext(state.io, state.gpa) catch return;
    defer res.deinit(state.gpa);
    if (res.ok()) {
        refreshData();
        showStatus("✔ 已成功清除单次启动项 (BootNext 已重置)", false);
    } else {
        showStatus(res.stderr, true);
    }
}

fn onSetDefaultClicked(_: *c.GtkButton, user_data: ?*anyopaque) callconv(.c) void {
    const ident = unpackIdent(user_data);
    if (state.cfg) |cfg| {
        const res = nvram.setAsDefault(state.io, state.gpa, cfg, ident) catch |err| {
            showStatus("❌ 写入 NVRAM 失败", true);
            std.debug.print("[-] 置顶失败: {s}\n", .{@errorName(err)});
            return;
        };
        defer res.deinit(state.gpa);

        if (res.ok()) {
            refreshData();
            const msg = gtk.zfmt("✔ 成功将 Boot{s} 设为主板第一优先启动项！(NVRAM 已持久化写入)", .{ident});
            showStatus(msg, false);
        } else {
            showStatus(res.stderr, true);
        }
    }
}

fn onSetBootNextClicked(_: *c.GtkButton, user_data: ?*anyopaque) callconv(.c) void {
    const ident = unpackIdent(user_data);
    const res = nvram.setBootNext(state.io, state.gpa, ident) catch |err| {
        showStatus("❌ 设置 BootNext 失败", true);
        std.debug.print("[-] 设置 BootNext 失败: {s}\n", .{@errorName(err)});
        return;
    };
    defer res.deinit(state.gpa);

    if (res.ok()) {
        refreshData();
        const msg = gtk.zfmt("✔ 成功设置下次单次启动为 Boot{s}！(不影响默认顺序)", .{ident});
        showStatus(msg, false);
    } else {
        showStatus(res.stderr, true);
    }
}

fn onMoveUpClicked(_: *c.GtkButton, user_data: ?*anyopaque) callconv(.c) void {
    const ident = unpackIdent(user_data);
    if (state.cfg) |cfg| {
        for (cfg.boot_order.items, 0..) |id, idx| {
            if (std.mem.eql(u8, &id, &ident) and idx > 0) {
                const res = nvram.moveOrder(state.io, state.gpa, cfg, idx, idx - 1) catch return;
                defer res.deinit(state.gpa);
                if (res.ok()) {
                    refreshData();
                    showStatus("✔ 启动顺位上移成功", false);
                } else {
                    showStatus(res.stderr, true);
                }
                break;
            }
        }
    }
}

fn onMoveDownClicked(_: *c.GtkButton, user_data: ?*anyopaque) callconv(.c) void {
    const ident = unpackIdent(user_data);
    if (state.cfg) |cfg| {
        for (cfg.boot_order.items, 0..) |id, idx| {
            if (std.mem.eql(u8, &id, &ident) and idx + 1 < cfg.boot_order.items.len) {
                const res = nvram.moveOrder(state.io, state.gpa, cfg, idx, idx + 1) catch return;
                defer res.deinit(state.gpa);
                if (res.ok()) {
                    refreshData();
                    showStatus("✔ 启动顺位下移成功", false);
                } else {
                    showStatus(res.stderr, true);
                }
                break;
            }
        }
    }
}

fn onToggleActiveClicked(_: *c.GtkButton, user_data: ?*anyopaque) callconv(.c) void {
    const ident = unpackIdent(user_data);
    if (state.cfg) |cfg| {
        if (cfg.findEntry(ident)) |e| {
            const target_state = !e.active;
            const res = nvram.setActive(state.io, state.gpa, ident, target_state) catch return;
            defer res.deinit(state.gpa);
            if (res.ok()) {
                refreshData();
                showStatus(if (target_state) "✔ 条目已启用" else "✔ 条目已禁用", false);
            } else {
                showStatus(res.stderr, true);
            }
        }
    }
}

// ── 模态安全防误触删除对话框 ──
var delete_dialog: ?*c.GtkWindow = null;
var pending_delete_id: [4]u8 = undefined;

fn onDeleteClicked(_: *c.GtkButton, user_data: ?*anyopaque) callconv(.c) void {
    pending_delete_id = unpackIdent(user_data);

    const dialog = c.gtk_window_new();
    delete_dialog = @ptrCast(dialog);
    c.gtk_window_set_title(@ptrCast(dialog), gtk.s("⚠️ 危险操作确认"));
    c.gtk_window_set_transient_for(@ptrCast(dialog), state.window);
    c.gtk_window_set_modal(@ptrCast(dialog), 1);
    c.gtk_window_set_default_size(@ptrCast(dialog), 400, 180);

    const box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_VERTICAL), 14);
    c.gtk_widget_set_margin_start(@ptrCast(box), 20);
    c.gtk_widget_set_margin_end(@ptrCast(box), 20);
    c.gtk_widget_set_margin_top(@ptrCast(box), 16);
    c.gtk_widget_set_margin_bottom(@ptrCast(box), 16);

    const title_text = if (state.cfg) |cfg|
        if (cfg.findEntry(pending_delete_id)) |e| e.title else "未知条目"
    else
        "条目";

    const prompt_lbl = c.gtk_label_new(gtk.s(gtk.zfmt(
        "确定要从主板 NVRAM 中<b>永久删除</b>启动项 [Boot{s}] 吗？\n\n条目标题: <b>{s}</b>\n\n<span color=\"#e01b24\">警告：删除后主板固件将无法引导该项，除非重新注册。</span>",
        .{ pending_delete_id, title_text },
    )));
    c.gtk_label_set_use_markup(@ptrCast(prompt_lbl), 1);
    c.gtk_label_set_wrap(@ptrCast(prompt_lbl), 1);
    c.gtk_box_append(@ptrCast(box), @ptrCast(prompt_lbl));

    const btn_row = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 10);
    c.gtk_widget_set_halign(@ptrCast(btn_row), @intCast(c.GTK_ALIGN_END));

    const btn_cancel = c.gtk_button_new_with_label(gtk.s("取消"));
    _ = c.g_signal_connect_data(@ptrCast(btn_cancel), gtk.s("clicked"), @ptrCast(&onCloseDeleteDialog), null, null, 0);
    c.gtk_box_append(@ptrCast(btn_row), btn_cancel);

    const btn_confirm = c.gtk_button_new_with_label(gtk.s("🗑 确认删除"));
    c.gtk_widget_add_css_class(@ptrCast(btn_confirm), gtk.s("destructive-action"));
    _ = c.g_signal_connect_data(@ptrCast(btn_confirm), gtk.s("clicked"), @ptrCast(&onConfirmDelete), null, null, 0);
    c.gtk_box_append(@ptrCast(btn_row), btn_confirm);

    c.gtk_box_append(@ptrCast(box), @ptrCast(btn_row));
    c.gtk_window_set_child(@ptrCast(dialog), @ptrCast(box));
    c.gtk_window_present(@ptrCast(dialog));
}

fn onCloseDeleteDialog(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (delete_dialog) |d| {
        c.gtk_window_destroy(d);
        delete_dialog = null;
    }
}

fn onConfirmDelete(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    const res = nvram.deleteEntry(state.io, state.gpa, pending_delete_id) catch return;
    defer res.deinit(state.gpa);
    if (res.ok()) {
        refreshData();
        const msg = gtk.zfmt("✔ 成功从主板 NVRAM 删除条目 Boot{s}", .{pending_delete_id});
        showStatus(msg, false);
    } else {
        showStatus(res.stderr, true);
    }
    onCloseDeleteDialog(@ptrCast(c.gtk_button_new()), null);
}

fn onDedupClicked(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (state.cfg) |cfg| {
        const res = nvram.deduplicateBootOrder(state.io, state.gpa, cfg) catch return;
        defer res.deinit(state.gpa);
        if (res.ok()) {
            refreshData();
            showStatus("✔ 已成功清理 BootOrder 重复项并持久化写入 NVRAM！", false);
        } else {
            showStatus(res.stderr, true);
        }
    }
}

fn onFixWarningClicked(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (state.orphan_ident) |ident| {
        if (state.cfg) |cfg| {
            const res = nvram.setAsDefault(state.io, state.gpa, cfg, ident) catch return;
            defer res.deinit(state.gpa);
            if (res.ok()) {
                refreshData();
                const msg = gtk.zfmt("✔ 成功将 Boot{s} 加入启动顺序并设为首选项！", .{ident});
                showStatus(msg, false);
            } else {
                showStatus(res.stderr, true);
            }
        }
    }
}

// ============================================================================
// 对话框：超时设置与条目注册
// ============================================================================

var timeout_spin: ?*c.GtkSpinButton = null;
var timeout_dialog: ?*c.GtkWindow = null;

fn onSetTimeoutClicked(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    const dialog = c.gtk_window_new();
    timeout_dialog = @ptrCast(dialog);
    c.gtk_window_set_title(@ptrCast(dialog), gtk.s("设置 UEFI 启动超时时间"));
    c.gtk_window_set_transient_for(@ptrCast(dialog), state.window);
    c.gtk_window_set_modal(@ptrCast(dialog), 1);
    c.gtk_window_set_default_size(@ptrCast(dialog), 360, 160);

    const box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_VERTICAL), 12);
    c.gtk_widget_set_margin_start(@ptrCast(box), 20);
    c.gtk_widget_set_margin_end(@ptrCast(box), 20);
    c.gtk_widget_set_margin_top(@ptrCast(box), 16);
    c.gtk_widget_set_margin_bottom(@ptrCast(box), 16);

    const lbl = c.gtk_label_new(gtk.s("固件引导倒计时等待时间 (秒):"));
    c.gtk_label_set_xalign(@ptrCast(lbl), 0.0);
    c.gtk_box_append(@ptrCast(box), @ptrCast(lbl));

    const cur_timeout: f64 = if (state.cfg) |cfg| @floatFromInt(cfg.timeout) else 0.0;
    const adj = c.gtk_adjustment_new(cur_timeout, 0.0, 3600.0, 1.0, 5.0, 0.0);
    const spin = c.gtk_spin_button_new(adj, 1.0, 0);
    timeout_spin = @ptrCast(spin);
    c.gtk_box_append(@ptrCast(box), @ptrCast(spin));

    const btn_row = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 10);
    c.gtk_widget_set_halign(@ptrCast(btn_row), @intCast(c.GTK_ALIGN_END));

    const btn_cancel = c.gtk_button_new_with_label(gtk.s("取消"));
    _ = c.g_signal_connect_data(@ptrCast(btn_cancel), gtk.s("clicked"), @ptrCast(&onCloseTimeoutDialog), null, null, 0);
    c.gtk_box_append(@ptrCast(btn_row), btn_cancel);

    const btn_save = c.gtk_button_new_with_label(gtk.s("确认写入 NVRAM"));
    c.gtk_widget_add_css_class(@ptrCast(btn_save), gtk.s("suggested-action"));
    _ = c.g_signal_connect_data(@ptrCast(btn_save), gtk.s("clicked"), @ptrCast(&onSaveTimeout), null, null, 0);
    c.gtk_box_append(@ptrCast(btn_row), btn_save);

    c.gtk_box_append(@ptrCast(box), @ptrCast(btn_row));
    c.gtk_window_set_child(@ptrCast(dialog), @ptrCast(box));
    c.gtk_window_present(@ptrCast(dialog));
}

fn onCloseTimeoutDialog(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (timeout_dialog) |d| {
        c.gtk_window_destroy(d);
        timeout_dialog = null;
    }
}

fn onSaveTimeout(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (timeout_spin) |spin| {
        const val: u32 = @intFromFloat(c.gtk_spin_button_get_value(spin));
        const res = nvram.setTimeout(state.io, state.gpa, val) catch return;
        defer res.deinit(state.gpa);
        if (res.ok()) {
            refreshData();
            const msg = gtk.zfmt("✔ 固件超时已成功写入为 {d} 秒！", .{val});
            showStatus(msg, false);
        } else {
            showStatus(res.stderr, true);
        }
    }
    onCloseTimeoutDialog(@ptrCast(c.gtk_button_new()), null);
}

// 注册新条目对话框
var add_dialog: ?*c.GtkWindow = null;
var add_disk_entry: ?*c.GtkEntry = null;
var add_part_entry: ?*c.GtkEntry = null;
var add_label_entry: ?*c.GtkEntry = null;
var add_loader_entry: ?*c.GtkEntry = null;

fn onAddEntryClicked(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    const dialog = c.gtk_window_new();
    add_dialog = @ptrCast(dialog);
    c.gtk_window_set_title(@ptrCast(dialog), gtk.s("向主板 NVRAM 注册新 UEFI 启动项"));
    c.gtk_window_set_transient_for(@ptrCast(dialog), state.window);
    c.gtk_window_set_modal(@ptrCast(dialog), 1);
    c.gtk_window_set_default_size(@ptrCast(dialog), 500, 340);

    const box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_VERTICAL), 10);
    c.gtk_widget_set_margin_start(@ptrCast(box), 20);
    c.gtk_widget_set_margin_end(@ptrCast(box), 20);
    c.gtk_widget_set_margin_top(@ptrCast(box), 16);
    c.gtk_widget_set_margin_bottom(@ptrCast(box), 16);

    // 预设快速填入按钮行
    const preset_box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 6);
    const p1 = c.gtk_button_new_with_label(gtk.s("预设: systemd-boot"));
    _ = c.g_signal_connect_data(@ptrCast(p1), gtk.s("clicked"), @ptrCast(&onPresetSdBoot), null, null, 0);
    c.gtk_box_append(@ptrCast(preset_box), p1);

    const p2 = c.gtk_button_new_with_label(gtk.s("预设: Windows"));
    _ = c.g_signal_connect_data(@ptrCast(p2), gtk.s("clicked"), @ptrCast(&onPresetWin), null, null, 0);
    c.gtk_box_append(@ptrCast(preset_box), p2);

    const p3 = c.gtk_button_new_with_label(gtk.s("预设: GRUB"));
    _ = c.g_signal_connect_data(@ptrCast(p3), gtk.s("clicked"), @ptrCast(&onPresetGrub), null, null, 0);
    c.gtk_box_append(@ptrCast(preset_box), p3);

    c.gtk_box_append(@ptrCast(box), preset_box);

    // 输入字段
    const f1 = createFormField("磁盘设备路径 (例如 /dev/nvme0n1 或 /dev/sda):", "/dev/nvme0n1");
    add_disk_entry = f1.entry;
    c.gtk_box_append(@ptrCast(box), f1.container);

    const f2 = createFormField("ESP 分区号 (例如 5 或 1):", "5");
    add_part_entry = f2.entry;
    c.gtk_box_append(@ptrCast(box), f2.container);

    const f3 = createFormField("固件显示标签 (Label):", "Linux Boot Manager");
    add_label_entry = f3.entry;
    c.gtk_box_append(@ptrCast(box), f3.container);

    const f4 = createFormField("EFI 加载器路径 (Loader):", "\\EFI\\systemd\\systemd-bootx64.efi");
    add_loader_entry = f4.entry;
    c.gtk_box_append(@ptrCast(box), f4.container);

    const btn_row = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 10);
    c.gtk_widget_set_halign(@ptrCast(btn_row), @intCast(c.GTK_ALIGN_END));

    const btn_cancel = c.gtk_button_new_with_label(gtk.s("取消"));
    _ = c.g_signal_connect_data(@ptrCast(btn_cancel), gtk.s("clicked"), @ptrCast(&onCloseAddDialog), null, null, 0);
    c.gtk_box_append(@ptrCast(btn_row), btn_cancel);

    const btn_save = c.gtk_button_new_with_label(gtk.s("写入并注册 (efibootmgr -c)"));
    c.gtk_widget_add_css_class(@ptrCast(btn_save), gtk.s("suggested-action"));
    _ = c.g_signal_connect_data(@ptrCast(btn_save), gtk.s("clicked"), @ptrCast(&onSaveNewEntry), null, null, 0);
    c.gtk_box_append(@ptrCast(btn_row), btn_save);

    c.gtk_box_append(@ptrCast(box), @ptrCast(btn_row));
    c.gtk_window_set_child(@ptrCast(dialog), @ptrCast(box));
    c.gtk_window_present(@ptrCast(dialog));
}

const FormField = struct {
    container: *c.GtkWidget,
    entry: *c.GtkEntry,
};

fn createFormField(label_text: [:0]const u8, default_val: [:0]const u8) FormField {
    const box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_VERTICAL), 3);
    const lbl = c.gtk_label_new(gtk.s(label_text));
    c.gtk_label_set_xalign(@ptrCast(lbl), 0.0);
    c.gtk_widget_add_css_class(@ptrCast(lbl), gtk.s("entry-sub"));
    c.gtk_box_append(@ptrCast(box), @ptrCast(lbl));

    const entry = c.gtk_entry_new();
    c.gtk_editable_set_text(@ptrCast(entry), gtk.s(default_val));
    c.gtk_box_append(@ptrCast(box), @ptrCast(entry));

    return .{ .container = @ptrCast(box), .entry = @ptrCast(entry) };
}

fn onPresetSdBoot(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (add_disk_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("/dev/nvme0n1"));
    if (add_part_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("5"));
    if (add_label_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("Linux Boot Manager"));
    if (add_loader_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("\\EFI\\systemd\\systemd-bootx64.efi"));
}

fn onPresetWin(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (add_disk_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("/dev/nvme0n1"));
    if (add_part_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("1"));
    if (add_label_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("Windows Boot Manager"));
    if (add_loader_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("\\EFI\\Microsoft\\Boot\\bootmgfw.efi"));
}

fn onPresetGrub(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (add_disk_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("/dev/nvme0n1"));
    if (add_part_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("5"));
    if (add_label_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("GRUB"));
    if (add_loader_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("\\EFI\\GRUB\\grubx64.efi"));
}

fn onCloseAddDialog(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (add_dialog) |d| {
        c.gtk_window_destroy(d);
        add_dialog = null;
    }
}

fn onSaveNewEntry(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    const disk = std.mem.span(c.gtk_editable_get_text(@ptrCast(add_disk_entry.?)));
    const part_str = std.mem.span(c.gtk_editable_get_text(@ptrCast(add_part_entry.?)));
    const label = std.mem.span(c.gtk_editable_get_text(@ptrCast(add_label_entry.?)));
    const loader = std.mem.span(c.gtk_editable_get_text(@ptrCast(add_loader_entry.?)));

    const part = std.fmt.parseInt(u32, part_str, 10) catch 1;

    const res = nvram.createEntry(state.io, state.gpa, disk, part, label, loader) catch return;
    defer res.deinit(state.gpa);

    if (res.ok()) {
        refreshData();
        showStatus("✔ 成功向主板 NVRAM 注册新 UEFI 启动项！", false);
    } else {
        showStatus(res.stderr, true);
    }

    onCloseAddDialog(@ptrCast(c.gtk_button_new()), null);
}

// ============================================================================
// 对话框：编辑现有启动项 (支持 Boot0002 等任意条目)
// ============================================================================

var edit_dialog: ?*c.GtkWindow = null;
var edit_ident: [4]u8 = undefined;
var edit_disk_entry: ?*c.GtkEntry = null;
var edit_part_entry: ?*c.GtkEntry = null;
var edit_label_entry: ?*c.GtkEntry = null;
var edit_loader_entry: ?*c.GtkEntry = null;
var edit_active_switch: ?*c.GtkSwitch = null;

fn onEditClicked(_: *c.GtkButton, user_data: ?*anyopaque) callconv(.c) void {
    const ident = unpackIdent(user_data);
    edit_ident = ident;

    const dialog = c.gtk_window_new();
    edit_dialog = @ptrCast(dialog);

    const title_text = gtk.zfmt("编辑 UEFI 启动项 [Boot{s}]", .{ident});
    c.gtk_window_set_title(@ptrCast(dialog), gtk.s(title_text));
    c.gtk_window_set_transient_for(@ptrCast(dialog), state.window);
    c.gtk_window_set_modal(@ptrCast(dialog), 1);
    c.gtk_window_set_default_size(@ptrCast(dialog), 540, 420);

    const box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_VERTICAL), 10);
    c.gtk_widget_set_margin_start(@ptrCast(box), 20);
    c.gtk_widget_set_margin_end(@ptrCast(box), 20);
    c.gtk_widget_set_margin_top(@ptrCast(box), 16);
    c.gtk_widget_set_margin_bottom(@ptrCast(box), 16);

    // 获取当前条目已有信息
    var cur_title: []const u8 = "";
    var cur_path: []const u8 = "";
    var cur_part: u32 = 5;
    var cur_active = true;

    if (state.cfg) |cfg| {
        if (cfg.findEntry(ident)) |e| {
            cur_title = e.title;
            cur_path = e.path;
            cur_part = nvram.parsePartition(e.device_path);
            cur_active = e.active;
        }
    }

    // 若当前条目为 systemd-boot / Linux Boot Manager，在弹窗顶部展示醒目的内层引导快捷横幅
    if (std.mem.indexOf(u8, cur_path, "systemd-boot") != null or
        std.mem.indexOf(u8, cur_title, "Linux Boot Manager") != null or
        std.mem.indexOf(u8, cur_title, "Arch Linux") != null)
    {
        const banner = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_VERTICAL), 6);
        c.gtk_widget_add_css_class(@ptrCast(banner), gtk.s("card-banner"));

        const ban_title = c.gtk_label_new(gtk.s("🐧 检测到该条目为 Linux 引导加载器 (systemd-boot)"));
        c.gtk_label_set_xalign(@ptrCast(ban_title), 0.0);
        c.gtk_widget_add_css_class(@ptrCast(ban_title), gtk.s("entry-title"));
        c.gtk_box_append(@ptrCast(banner), @ptrCast(ban_title));

        const ban_desc = c.gtk_label_new(gtk.s("包含内部系统内核选择 (Zen 内核 / UKI)、引导菜单倒计时与 cmdline 内核参数。"));
        c.gtk_label_set_xalign(@ptrCast(ban_desc), 0.0);
        c.gtk_widget_add_css_class(@ptrCast(ban_desc), gtk.s("entry-sub"));
        c.gtk_box_append(@ptrCast(banner), @ptrCast(ban_desc));

        const ban_btn = c.gtk_button_new_with_label(gtk.s("👉 点击直接编辑内层内核与引导项 (loader.conf / cmdline) ➔"));
        c.gtk_widget_add_css_class(@ptrCast(ban_btn), gtk.s("suggested-action"));
        _ = c.g_signal_connect_data(@ptrCast(ban_btn), gtk.s("clicked"), @ptrCast(&onOpenLoaderFromEdit), null, null, 0);
        c.gtk_box_append(@ptrCast(banner), @ptrCast(ban_btn));

        c.gtk_box_append(@ptrCast(box), @ptrCast(banner));
    }

    // 预设快速替换行
    const preset_box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 6);
    const p1 = c.gtk_button_new_with_label(gtk.s("预设: systemd-boot"));
    _ = c.g_signal_connect_data(@ptrCast(p1), gtk.s("clicked"), @ptrCast(&onEditPresetSdBoot), null, null, 0);
    c.gtk_box_append(@ptrCast(preset_box), p1);

    const p2 = c.gtk_button_new_with_label(gtk.s("预设: Windows"));
    _ = c.g_signal_connect_data(@ptrCast(p2), gtk.s("clicked"), @ptrCast(&onEditPresetWin), null, null, 0);
    c.gtk_box_append(@ptrCast(preset_box), p2);

    const p3 = c.gtk_button_new_with_label(gtk.s("预设: GRUB"));
    _ = c.g_signal_connect_data(@ptrCast(p3), gtk.s("clicked"), @ptrCast(&onEditPresetGrub), null, null, 0);
    c.gtk_box_append(@ptrCast(preset_box), p3);

    c.gtk_box_append(@ptrCast(box), preset_box);

    // 输入字段 (带当前值预填充)
    const f1 = createFormField("磁盘设备路径 (例如 /dev/nvme0n1 或 /dev/sda):", "/dev/nvme0n1");
    edit_disk_entry = f1.entry;
    c.gtk_box_append(@ptrCast(box), f1.container);

    const part_str = gtk.zfmt("{d}", .{cur_part});
    const f2 = createFormField("ESP 分区号 (例如 5 或 1):", part_str);
    edit_part_entry = f2.entry;
    c.gtk_box_append(@ptrCast(box), f2.container);

    const title_z = gtk.zstr(cur_title);
    const f3 = createFormField("固件显示标签 (Label):", title_z);
    edit_label_entry = f3.entry;
    c.gtk_box_append(@ptrCast(box), f3.container);

    const path_z = if (cur_path.len > 0) gtk.zstr(cur_path) else gtk.zstr("\\EFI\\systemd\\systemd-bootx64.efi");
    const f4 = createFormField("EFI 加载器路径 (Loader):", path_z);
    edit_loader_entry = f4.entry;
    c.gtk_box_append(@ptrCast(box), f4.container);

    // 激活状态开关
    const act_row = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 10);
    const act_lbl = c.gtk_label_new(gtk.s("是否保持启用该启动项:"));
    c.gtk_box_append(@ptrCast(act_row), @ptrCast(act_lbl));

    const sw = c.gtk_switch_new();
    c.gtk_switch_set_active(@ptrCast(sw), if (cur_active) 1 else 0);
    edit_active_switch = @ptrCast(sw);
    c.gtk_box_append(@ptrCast(act_row), @ptrCast(sw));
    c.gtk_box_append(@ptrCast(box), act_row);

    // 底部按钮栏
    const btn_row = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 10);
    c.gtk_widget_set_halign(@ptrCast(btn_row), @intCast(c.GTK_ALIGN_END));

    const btn_cancel = c.gtk_button_new_with_label(gtk.s("取消"));
    _ = c.g_signal_connect_data(@ptrCast(btn_cancel), gtk.s("clicked"), @ptrCast(&onCloseEditDialog), null, null, 0);
    c.gtk_box_append(@ptrCast(btn_row), btn_cancel);

    const btn_save = c.gtk_button_new_with_label(gtk.s("确认保存修改 (写入 NVRAM)"));
    c.gtk_widget_add_css_class(@ptrCast(btn_save), gtk.s("suggested-action"));
    _ = c.g_signal_connect_data(@ptrCast(btn_save), gtk.s("clicked"), @ptrCast(&onSaveEditEntry), null, null, 0);
    c.gtk_box_append(@ptrCast(btn_row), btn_save);

    c.gtk_box_append(@ptrCast(box), @ptrCast(btn_row));
    c.gtk_window_set_child(@ptrCast(dialog), @ptrCast(box));
    c.gtk_window_present(@ptrCast(dialog));
}

fn onEditPresetSdBoot(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (edit_disk_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("/dev/nvme0n1"));
    if (edit_part_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("5"));
    if (edit_label_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("Linux Boot Manager"));
    if (edit_loader_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("\\EFI\\systemd\\systemd-bootx64.efi"));
}

fn onEditPresetWin(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (edit_disk_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("/dev/nvme0n1"));
    if (edit_part_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("1"));
    if (edit_label_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("Windows Boot Manager"));
    if (edit_loader_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("\\EFI\\Microsoft\\Boot\\bootmgfw.efi"));
}

fn onEditPresetGrub(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (edit_disk_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("/dev/nvme0n1"));
    if (edit_part_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("5"));
    if (edit_label_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("GRUB"));
    if (edit_loader_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("\\EFI\\GRUB\\grubx64.efi"));
}

fn onCloseEditDialog(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (edit_dialog) |d| {
        c.gtk_window_destroy(d);
        edit_dialog = null;
    }
}

fn onSaveEditEntry(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (state.cfg) |cfg| {
        const disk = std.mem.span(c.gtk_editable_get_text(@ptrCast(edit_disk_entry.?)));
        const part_str = std.mem.span(c.gtk_editable_get_text(@ptrCast(edit_part_entry.?)));
        const label = std.mem.span(c.gtk_editable_get_text(@ptrCast(edit_label_entry.?)));
        const loader = std.mem.span(c.gtk_editable_get_text(@ptrCast(edit_loader_entry.?)));
        const active = (c.gtk_switch_get_active(edit_active_switch.?) != 0);

        const part = std.fmt.parseInt(u32, part_str, 10) catch 5;

        const res = nvram.editEntry(state.io, state.gpa, cfg, edit_ident, disk, part, label, loader, active) catch return;
        defer res.deinit(state.gpa);

        if (res.ok()) {
            refreshData();
            const msg = gtk.zfmt("✔ 成功修改并更新启动项 [Boot{s}]！(NVRAM 已持久化)", .{edit_ident});
            showStatus(msg, false);
        } else {
            showStatus(res.stderr, true);
        }
    }
    onCloseEditDialog(@ptrCast(c.gtk_button_new()), null);
}

// ============================================================================
// systemd-boot 内层引导项与内核参数管理界面
// ============================================================================

var loader_dialog: ?*c.GtkWindow = null;
var loader_default_entry: ?*c.GtkEntry = null;
var loader_timeout_entry: ?*c.GtkEntry = null;
var loader_console_entry: ?*c.GtkEntry = null;

const LoaderWidgetPair = struct {
    id_buf: [64]u8,
    id_len: usize,
    title_entry: *c.GtkEntry,
    options_entry: ?*c.GtkEntry,
};
var loader_widget_pairs: [8]LoaderWidgetPair = undefined;
var loader_widget_count: usize = 0;

fn onOpenLoaderFromEdit(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    onCloseEditDialog(@ptrCast(c.gtk_button_new()), null);
    onOpenLoaderConfig(@ptrCast(c.gtk_button_new()), null);
}

fn onOpenLoaderConfig(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    var lcfg = sdboot.loadConfig(state.io, state.gpa) catch |err| {
        showStatus(gtk.zfmt("[-] 读取内层引导配置失败: {s}", .{@errorName(err)}), true);
        return;
    };
    defer lcfg.deinit(state.gpa);

    const dialog = c.gtk_window_new();
    loader_dialog = @ptrCast(dialog);

    c.gtk_window_set_title(@ptrCast(dialog), gtk.s("管理 Linux Boot Manager 内层引导项 (systemd-boot)"));
    c.gtk_window_set_transient_for(@ptrCast(dialog), state.window);
    c.gtk_window_set_modal(@ptrCast(dialog), 1);
    c.gtk_window_set_default_size(@ptrCast(dialog), 720, 620);

    const scrolled = c.gtk_scrolled_window_new();
    c.gtk_scrolled_window_set_policy(@ptrCast(scrolled), @intCast(c.GTK_POLICY_NEVER), @intCast(c.GTK_POLICY_AUTOMATIC));

    const content_box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_VERTICAL), 12);
    c.gtk_widget_set_margin_start(@ptrCast(content_box), 22);
    c.gtk_widget_set_margin_end(@ptrCast(content_box), 22);
    c.gtk_widget_set_margin_top(@ptrCast(content_box), 16);
    c.gtk_widget_set_margin_bottom(@ptrCast(content_box), 18);

    // ── 顶部说明卡片 ──
    const top_card = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_VERTICAL), 4);
    c.gtk_widget_add_css_class(@ptrCast(top_card), gtk.s("card-banner"));

    const head_lbl = c.gtk_label_new(gtk.s("🐧 Linux Boot Manager (systemd-boot) 内层配置与内核参数"));
    c.gtk_label_set_xalign(@ptrCast(head_lbl), 0.0);
    c.gtk_widget_add_css_class(@ptrCast(head_lbl), gtk.s("entry-title"));
    c.gtk_box_append(@ptrCast(top_card), @ptrCast(head_lbl));

    const desc_lbl = c.gtk_label_new(gtk.s("直接维护 ESP 分区中的 /boot/loader/loader.conf 与 entries/*.conf 引导文件。"));
    c.gtk_label_set_xalign(@ptrCast(desc_lbl), 0.0);
    c.gtk_widget_add_css_class(@ptrCast(desc_lbl), gtk.s("entry-sub"));
    c.gtk_box_append(@ptrCast(top_card), @ptrCast(desc_lbl));

    c.gtk_box_append(@ptrCast(content_box), @ptrCast(top_card));

    // ── 分组 1: 全局引导配置 (loader.conf) ──
    const global_card = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_VERTICAL), 8);
    c.gtk_widget_add_css_class(@ptrCast(global_card), gtk.s("card-loader"));

    const g_title = c.gtk_label_new(gtk.s("⚙ 全局引导配置 (/boot/loader/loader.conf)"));
    c.gtk_label_set_xalign(@ptrCast(g_title), 0.0);
    c.gtk_widget_add_css_class(@ptrCast(g_title), gtk.s("entry-title"));
    c.gtk_box_append(@ptrCast(global_card), @ptrCast(g_title));

    // 1.1 默认引导项
    const def_box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_VERTICAL), 4);
    const def_lbl = c.gtk_label_new(gtk.s("默认首选启动项 (default):"));
    c.gtk_label_set_xalign(@ptrCast(def_lbl), 0.0);
    c.gtk_widget_add_css_class(@ptrCast(def_lbl), gtk.s("entry-sub"));
    c.gtk_box_append(@ptrCast(def_box), @ptrCast(def_lbl));

    // 预设按钮组
    const def_btn_box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 6);
    const b_zen = c.gtk_button_new_with_label(gtk.s("★ 设为 Zen 内核"));
    _ = c.g_signal_connect_data(@ptrCast(b_zen), gtk.s("clicked"), @ptrCast(&onLoaderPresetZen), null, null, 0);
    c.gtk_box_append(@ptrCast(def_btn_box), b_zen);

    const b_win = c.gtk_button_new_with_label(gtk.s("★ 设为 Windows 11"));
    _ = c.g_signal_connect_data(@ptrCast(b_win), gtk.s("clicked"), @ptrCast(&onLoaderPresetWin), null, null, 0);
    c.gtk_box_append(@ptrCast(def_btn_box), b_win);

    const b_uki = c.gtk_button_new_with_label(gtk.s("★ 设为 UKI 镜像"));
    _ = c.g_signal_connect_data(@ptrCast(b_uki), gtk.s("clicked"), @ptrCast(&onLoaderPresetUki), null, null, 0);
    c.gtk_box_append(@ptrCast(def_btn_box), b_uki);

    c.gtk_box_append(@ptrCast(def_box), @ptrCast(def_btn_box));

    const def_entry = c.gtk_entry_new();
    c.gtk_editable_set_text(@ptrCast(def_entry), gtk.s(gtk.zstr(lcfg.default_entry)));
    loader_default_entry = @ptrCast(def_entry);
    c.gtk_box_append(@ptrCast(def_box), @ptrCast(def_entry));
    c.gtk_box_append(@ptrCast(global_card), @ptrCast(def_box));

    // 1.2 超时时间
    const tm_box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_VERTICAL), 4);
    const tm_lbl = c.gtk_label_new(gtk.s("引导菜单倒计时超时 (timeout):"));
    c.gtk_label_set_xalign(@ptrCast(tm_lbl), 0.0);
    c.gtk_widget_add_css_class(@ptrCast(tm_lbl), gtk.s("entry-sub"));
    c.gtk_box_append(@ptrCast(tm_box), @ptrCast(tm_lbl));

    const tm_btn_box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 6);
    const bt_force = c.gtk_button_new_with_label(gtk.s("常驻菜单 (menu-force)"));
    _ = c.g_signal_connect_data(@ptrCast(bt_force), gtk.s("clicked"), @ptrCast(&onLoaderTimeoutMenuForce), null, null, 0);
    c.gtk_box_append(@ptrCast(tm_btn_box), bt_force);

    const bt_3 = c.gtk_button_new_with_label(gtk.s("3 秒"));
    _ = c.g_signal_connect_data(@ptrCast(bt_3), gtk.s("clicked"), @ptrCast(&onLoaderTimeout3s), null, null, 0);
    c.gtk_box_append(@ptrCast(tm_btn_box), bt_3);

    const bt_5 = c.gtk_button_new_with_label(gtk.s("5 秒"));
    _ = c.g_signal_connect_data(@ptrCast(bt_5), gtk.s("clicked"), @ptrCast(&onLoaderTimeout5s), null, null, 0);
    c.gtk_box_append(@ptrCast(tm_btn_box), bt_5);

    const bt_0 = c.gtk_button_new_with_label(gtk.s("跳过菜单 (0 秒)"));
    _ = c.g_signal_connect_data(@ptrCast(bt_0), gtk.s("clicked"), @ptrCast(&onLoaderTimeout0s), null, null, 0);
    c.gtk_box_append(@ptrCast(tm_btn_box), bt_0);

    c.gtk_box_append(@ptrCast(tm_box), @ptrCast(tm_btn_box));

    const tm_entry = c.gtk_entry_new();
    c.gtk_editable_set_text(@ptrCast(tm_entry), gtk.s(gtk.zstr(lcfg.timeout)));
    loader_timeout_entry = @ptrCast(tm_entry);
    c.gtk_box_append(@ptrCast(tm_box), @ptrCast(tm_entry));
    c.gtk_box_append(@ptrCast(global_card), @ptrCast(tm_box));

    // 1.3 控制台分辨率
    const cs_box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_VERTICAL), 4);
    const cs_lbl = c.gtk_label_new(gtk.s("控制台分辨率模式 (console-mode):"));
    c.gtk_label_set_xalign(@ptrCast(cs_lbl), 0.0);
    c.gtk_widget_add_css_class(@ptrCast(cs_lbl), gtk.s("entry-sub"));
    c.gtk_box_append(@ptrCast(cs_box), @ptrCast(cs_lbl));

    const cs_btn_box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 6);
    const bc_max = c.gtk_button_new_with_label(gtk.s("最高分辨率 (max)"));
    _ = c.g_signal_connect_data(@ptrCast(bc_max), gtk.s("clicked"), @ptrCast(&onLoaderConsoleMax), null, null, 0);
    c.gtk_box_append(@ptrCast(cs_btn_box), bc_max);

    const bc_keep = c.gtk_button_new_with_label(gtk.s("保持固件原样 (keep)"));
    _ = c.g_signal_connect_data(@ptrCast(bc_keep), gtk.s("clicked"), @ptrCast(&onLoaderConsoleKeep), null, null, 0);
    c.gtk_box_append(@ptrCast(cs_btn_box), bc_keep);

    const bc_auto = c.gtk_button_new_with_label(gtk.s("自动检测 (auto)"));
    _ = c.g_signal_connect_data(@ptrCast(bc_auto), gtk.s("clicked"), @ptrCast(&onLoaderConsoleAuto), null, null, 0);
    c.gtk_box_append(@ptrCast(cs_btn_box), bc_auto);

    c.gtk_box_append(@ptrCast(cs_box), @ptrCast(cs_btn_box));

    const cs_entry = c.gtk_entry_new();
    c.gtk_editable_set_text(@ptrCast(cs_entry), gtk.s(gtk.zstr(lcfg.console_mode)));
    loader_console_entry = @ptrCast(cs_entry);
    c.gtk_box_append(@ptrCast(cs_box), @ptrCast(cs_entry));
    c.gtk_box_append(@ptrCast(global_card), @ptrCast(cs_box));

    c.gtk_box_append(@ptrCast(content_box), @ptrCast(global_card));

    // ── 分组 2: 内层条目列表 (/boot/loader/entries/*.conf) ──
    const entries_hdr = c.gtk_label_new(gtk.s("📋 内层条目与内核参数 (/boot/loader/entries/)"));
    c.gtk_label_set_xalign(@ptrCast(entries_hdr), 0.0);
    c.gtk_widget_add_css_class(@ptrCast(entries_hdr), gtk.s("entry-title"));
    c.gtk_box_append(@ptrCast(content_box), @ptrCast(entries_hdr));

    loader_widget_count = 0;

    for (lcfg.entries.items) |e| {
        if (loader_widget_count >= loader_widget_pairs.len) break;

        const entry_card = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_VERTICAL), 6);
        c.gtk_widget_add_css_class(@ptrCast(entry_card), gtk.s("card"));

        // 标题行与徽章
        const tr = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 8);
        const id_badge = c.gtk_label_new(gtk.s(gtk.zfmt("条目: {s}", .{e.id})));
        c.gtk_widget_add_css_class(@ptrCast(id_badge), gtk.s("pill-badge"));
        c.gtk_widget_add_css_class(@ptrCast(id_badge), gtk.s("badge-blue"));
        c.gtk_box_append(@ptrCast(tr), @ptrCast(id_badge));

        if (e.is_default) {
            const def_badge = c.gtk_label_new(gtk.s("当前默认 ★"));
            c.gtk_widget_add_css_class(@ptrCast(def_badge), gtk.s("pill-badge"));
            c.gtk_widget_add_css_class(@ptrCast(def_badge), gtk.s("badge-green"));
            c.gtk_box_append(@ptrCast(tr), @ptrCast(def_badge));
        }

        c.gtk_box_append(@ptrCast(entry_card), @ptrCast(tr));

        // 标题编辑框
        const t_lbl = c.gtk_label_new(gtk.s("引导显示标题 (Title):"));
        c.gtk_label_set_xalign(@ptrCast(t_lbl), 0.0);
        c.gtk_widget_add_css_class(@ptrCast(t_lbl), gtk.s("entry-sub"));
        c.gtk_box_append(@ptrCast(entry_card), @ptrCast(t_lbl));

        const t_entry = c.gtk_entry_new();
        c.gtk_editable_set_text(@ptrCast(t_entry), gtk.s(gtk.zstr(e.title)));
        c.gtk_box_append(@ptrCast(entry_card), @ptrCast(t_entry));

        // 路径详情 (只读提示)
        if (e.linux.len > 0) {
            const lin_lbl = c.gtk_label_new(gtk.s(gtk.zfmt("内核镜像 (linux): {s}", .{e.linux})));
            c.gtk_label_set_xalign(@ptrCast(lin_lbl), 0.0);
            c.gtk_widget_add_css_class(@ptrCast(lin_lbl), gtk.s("entry-sub"));
            c.gtk_box_append(@ptrCast(entry_card), @ptrCast(lin_lbl));
        }
        if (e.efi.len > 0) {
            const efi_lbl = c.gtk_label_new(gtk.s(gtk.zfmt("EFI 镜像路径 (efi): {s}", .{e.efi})));
            c.gtk_label_set_xalign(@ptrCast(efi_lbl), 0.0);
            c.gtk_widget_add_css_class(@ptrCast(efi_lbl), gtk.s("entry-sub"));
            c.gtk_box_append(@ptrCast(entry_card), @ptrCast(efi_lbl));
        }

        var opt_entry_ptr: ?*c.GtkEntry = null;

        if (e.is_conf_file) {
            // 内核命令行参数 (Options / Cmdline)
            const opt_lbl = c.gtk_label_new(gtk.s("内核启动命令行参数 (Options / Cmdline):"));
            c.gtk_label_set_xalign(@ptrCast(opt_lbl), 0.0);
            c.gtk_widget_add_css_class(@ptrCast(opt_lbl), gtk.s("entry-sub"));
            c.gtk_box_append(@ptrCast(entry_card), @ptrCast(opt_lbl));

            const o_entry = c.gtk_entry_new();
            c.gtk_editable_set_text(@ptrCast(o_entry), gtk.s(gtk.zstr(e.options)));
            opt_entry_ptr = @ptrCast(o_entry);
            c.gtk_box_append(@ptrCast(entry_card), @ptrCast(o_entry));
        } else {
            const uki_lbl = c.gtk_label_new(gtk.s("💡 提示: UKI 为一体化 EFI 镜像，内核启动参数与 initramfs 已固化在二进制节区中。"));
            c.gtk_label_set_xalign(@ptrCast(uki_lbl), 0.0);
            c.gtk_widget_add_css_class(@ptrCast(uki_lbl), gtk.s("entry-sub"));
            c.gtk_box_append(@ptrCast(entry_card), @ptrCast(uki_lbl));
        }

        // 记录该条目的控件引用
        var pair = &loader_widget_pairs[loader_widget_count];
        const copy_len = @min(e.id.len, pair.id_buf.len);
        @memcpy(pair.id_buf[0..copy_len], e.id[0..copy_len]);
        pair.id_len = copy_len;
        pair.title_entry = @ptrCast(t_entry);
        pair.options_entry = opt_entry_ptr;
        loader_widget_count += 1;

        c.gtk_box_append(@ptrCast(content_box), @ptrCast(entry_card));
    }

    // ── 底部操作按钮 ──
    const btn_row = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 10);
    c.gtk_widget_set_halign(@ptrCast(btn_row), @intCast(c.GTK_ALIGN_END));
    c.gtk_widget_set_margin_top(@ptrCast(btn_row), 10);

    const btn_cancel = c.gtk_button_new_with_label(gtk.s("取消"));
    _ = c.g_signal_connect_data(@ptrCast(btn_cancel), gtk.s("clicked"), @ptrCast(&onCloseLoaderDialog), null, null, 0);
    c.gtk_box_append(@ptrCast(btn_row), btn_cancel);

    const btn_save = c.gtk_button_new_with_label(gtk.s("💾 确认保存并应用内层配置 (写入 /boot/loader)"));
    c.gtk_widget_add_css_class(@ptrCast(btn_save), gtk.s("suggested-action"));
    _ = c.g_signal_connect_data(@ptrCast(btn_save), gtk.s("clicked"), @ptrCast(&onSaveLoaderConfig), null, null, 0);
    c.gtk_box_append(@ptrCast(btn_row), btn_save);

    c.gtk_box_append(@ptrCast(content_box), @ptrCast(btn_row));

    c.gtk_scrolled_window_set_child(@ptrCast(scrolled), @ptrCast(content_box));
    c.gtk_window_set_child(@ptrCast(dialog), @ptrCast(scrolled));
    c.gtk_window_present(@ptrCast(dialog));
}

fn onLoaderPresetZen(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (loader_default_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("arch-linux-zen.conf"));
}

fn onLoaderPresetWin(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (loader_default_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("windows.conf"));
}

fn onLoaderPresetUki(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (loader_default_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("arch-linux.efi"));
}

fn onLoaderTimeoutMenuForce(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (loader_timeout_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("menu-force"));
}

fn onLoaderTimeout3s(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (loader_timeout_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("3"));
}

fn onLoaderTimeout5s(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (loader_timeout_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("5"));
}

fn onLoaderTimeout0s(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (loader_timeout_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("0"));
}

fn onLoaderConsoleMax(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (loader_console_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("max"));
}

fn onLoaderConsoleKeep(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (loader_console_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("keep"));
}

fn onLoaderConsoleAuto(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (loader_console_entry) |e| c.gtk_editable_set_text(@ptrCast(e), gtk.s("auto"));
}

fn onCloseLoaderDialog(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (loader_dialog) |d| {
        c.gtk_window_destroy(d);
        loader_dialog = null;
    }
}

fn onSaveLoaderConfig(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    const def_entry = std.mem.span(c.gtk_editable_get_text(@ptrCast(loader_default_entry.?)));
    const timeout_val = std.mem.span(c.gtk_editable_get_text(@ptrCast(loader_timeout_entry.?)));
    const console_val = std.mem.span(c.gtk_editable_get_text(@ptrCast(loader_console_entry.?)));

    const res1 = sdboot.saveGlobalConf(state.io, state.gpa, def_entry, timeout_val, console_val) catch |err| {
        showStatus(gtk.zfmt("[-] 保存全局配置失败: {s}", .{@errorName(err)}), true);
        return;
    };
    defer res1.deinit(state.gpa);

    if (!res1.ok()) {
        showStatus(res1.stderr, true);
        return;
    }

    var i: usize = 0;
    while (i < loader_widget_count) : (i += 1) {
        const pair = &loader_widget_pairs[i];
        const id = pair.id_buf[0..pair.id_len];
        const title = std.mem.span(c.gtk_editable_get_text(@ptrCast(pair.title_entry)));
        const options = if (pair.options_entry) |oe| std.mem.span(c.gtk_editable_get_text(@ptrCast(oe))) else "";

        const res_e = sdboot.saveEntryConfig(state.io, state.gpa, id, title, options) catch continue;
        defer res_e.deinit(state.gpa);
    }

    onCloseLoaderDialog(@ptrCast(c.gtk_button_new()), null);
    refreshData();
    showStatus("✔ 成功保存并应用 systemd-boot 内层引导项与内核参数！", false);
}
