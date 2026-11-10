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

    const is_sd_boot = if (maybe_entry) |e|
        (std.mem.indexOf(u8, e.path, "systemd-boot") != null or
            std.mem.indexOf(u8, e.title, "Linux Boot Manager") != null or
            std.mem.indexOf(u8, e.title, "Arch Linux") != null)
    else
        false;

    // 若为 systemd-boot，在主列表卡片内直接呈现「启动项显示」概览
    if (is_sd_boot) {
        const inner_box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_VERTICAL), 3);
        c.gtk_widget_add_css_class(@ptrCast(inner_box), gtk.s("card-loader"));
        c.gtk_widget_set_margin_top(@ptrCast(inner_box), 6);

        if (sdboot.loadConfig(state.io, state.gpa)) |lcfg_val| {
            var lcfg = lcfg_val;
            defer lcfg.deinit(state.gpa);
            for (lcfg.entries.items) |e| {
                const item_row = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 6);
                if (e.is_default) {
                    const def_b = c.gtk_label_new(gtk.s("★ 首选"));
                    c.gtk_widget_add_css_class(@ptrCast(def_b), gtk.s("pill-badge"));
                    c.gtk_widget_add_css_class(@ptrCast(def_b), gtk.s("badge-green"));
                    c.gtk_box_append(@ptrCast(item_row), @ptrCast(def_b));
                } else {
                    const dot_b = c.gtk_label_new(gtk.s("• 启动项:"));
                    c.gtk_widget_add_css_class(@ptrCast(dot_b), gtk.s("entry-sub"));
                    c.gtk_box_append(@ptrCast(item_row), @ptrCast(dot_b));
                }

                const item_title = c.gtk_label_new(gtk.s(gtk.zstr(e.title)));
                c.gtk_widget_add_css_class(@ptrCast(item_title), gtk.s("entry-sub"));
                c.gtk_box_append(@ptrCast(item_row), @ptrCast(item_title));

                if (e.options.len > 0) {
                    const opt_preview = c.gtk_label_new(gtk.s(gtk.zfmt("({s})", .{e.options})));
                    c.gtk_label_set_ellipsize(@ptrCast(opt_preview), @intCast(c.PANGO_ELLIPSIZE_END));
                    c.gtk_widget_add_css_class(@ptrCast(opt_preview), gtk.s("entry-sub"));
                    c.gtk_box_append(@ptrCast(item_row), @ptrCast(opt_preview));
                }

                c.gtk_box_append(@ptrCast(inner_box), @ptrCast(item_row));
            }
        } else |_| {}

        c.gtk_box_append(@ptrCast(mid_box), @ptrCast(inner_box));
    }

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

    // 6. 编辑条目 (对于 systemd-boot 显示为 "✏ 编辑启动项")
    const edit_lbl = if (is_sd_boot) "✏ 编辑启动项" else "✏ 编辑";
    const btn_edit = c.gtk_button_new_with_label(gtk.s(edit_lbl));
    if (is_sd_boot) {
        c.gtk_widget_add_css_class(@ptrCast(btn_edit), gtk.s("suggested-action"));
    }
    _ = c.g_signal_connect_data(@ptrCast(btn_edit), gtk.s("clicked"), @ptrCast(&onEditClicked), user_data, null, 0);
    c.gtk_box_append(@ptrCast(btn_box), btn_edit);

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
// 对话框：启动项显示与内层引导项管理 (用于 Boot0002 / Linux Boot Manager)
// ============================================================================

var loader_entries_dialog: ?*c.GtkWindow = null;
var single_edit_dialog: ?*c.GtkWindow = null;
var single_title_entry: ?*c.GtkEntry = null;
var single_options_entry: ?*c.GtkEntry = null;
var single_sort_key_entry: ?*c.GtkEntry = null;
var single_entry_id_buf: [64]u8 = undefined;
var single_entry_id_len: usize = 0;

const LoaderEntrySnapshot = struct {
    id_buf: [64]u8,
    id_len: usize,
    title_buf: [128]u8,
    title_len: usize,
    options_buf: [256]u8,
    options_len: usize,
    sort_key_buf: [64]u8,
    sort_key_len: usize,
    is_conf_file: bool,
};
var cached_entries: [16]LoaderEntrySnapshot = undefined;
var cached_entries_count: usize = 0;

fn onEditClicked(_: *c.GtkButton, user_data: ?*anyopaque) callconv(.c) void {
    const ident = unpackIdent(user_data);
    edit_ident = ident;

    var is_sd_boot = false;
    if (state.cfg) |cfg| {
        if (cfg.findEntry(ident)) |e| {
            if (std.mem.indexOf(u8, e.path, "systemd-boot") != null or
                std.mem.indexOf(u8, e.title, "Linux Boot Manager") != null or
                std.mem.indexOf(u8, e.title, "Arch Linux") != null)
            {
                is_sd_boot = true;
            }
        }
    }

    if (is_sd_boot) {
        // 直接呈现真实的启动项列表显示，而非枯燥的硬件设置表单！
        openLoaderEntriesDisplay();
    } else {
        openHardwareNvramEditDialog(ident);
    }
}

fn openLoaderEntriesDisplay() void {
    if (loader_entries_dialog) |d| {
        c.gtk_window_destroy(d);
        loader_entries_dialog = null;
    }

    var lcfg = sdboot.loadConfig(state.io, state.gpa) catch |err| {
        showStatus(gtk.zfmt("[-] 读取系统启动项失败: {s}", .{@errorName(err)}), true);
        return;
    };
    defer lcfg.deinit(state.gpa);

    // 缓存快照
    cached_entries_count = 0;
    for (lcfg.entries.items) |e| {
        if (cached_entries_count >= cached_entries.len) break;
        var snap = &cached_entries[cached_entries_count];

        const id_l = @min(e.id.len, snap.id_buf.len);
        @memcpy(snap.id_buf[0..id_l], e.id[0..id_l]);
        snap.id_len = id_l;

        const t_l = @min(e.title.len, snap.title_buf.len);
        @memcpy(snap.title_buf[0..t_l], e.title[0..t_l]);
        snap.title_len = t_l;

        const o_l = @min(e.options.len, snap.options_buf.len);
        @memcpy(snap.options_buf[0..o_l], e.options[0..o_l]);
        snap.options_len = o_l;

        const sk_l = @min(e.sort_key.len, snap.sort_key_buf.len);
        @memcpy(snap.sort_key_buf[0..sk_l], e.sort_key[0..sk_l]);
        snap.sort_key_len = sk_l;

        snap.is_conf_file = e.is_conf_file;
        cached_entries_count += 1;
    }

    var reorderable_count: usize = 0;
    for (lcfg.entries.items) |e| {
        if (!std.mem.startsWith(u8, e.id, "auto-")) {
            reorderable_count += 1;
        }
    }

    const dialog = c.gtk_window_new();
    loader_entries_dialog = @ptrCast(dialog);

    c.gtk_window_set_title(@ptrCast(dialog), gtk.s("Linux Boot Manager (Boot0002) 内部启动项"));
    c.gtk_window_set_transient_for(@ptrCast(dialog), state.window);
    c.gtk_window_set_modal(@ptrCast(dialog), 1);
    c.gtk_window_set_default_size(@ptrCast(dialog), 760, 520);

    const scrolled = c.gtk_scrolled_window_new();
    c.gtk_scrolled_window_set_policy(@ptrCast(scrolled), @intCast(c.GTK_POLICY_NEVER), @intCast(c.GTK_POLICY_AUTOMATIC));

    const content_box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_VERTICAL), 10);
    c.gtk_widget_set_margin_start(@ptrCast(content_box), 20);
    c.gtk_widget_set_margin_end(@ptrCast(content_box), 20);
    c.gtk_widget_set_margin_top(@ptrCast(content_box), 16);
    c.gtk_widget_set_margin_bottom(@ptrCast(content_box), 16);

    // ── 顶部说明卡片 ──
    const top_card = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_VERTICAL), 4);
    c.gtk_widget_add_css_class(@ptrCast(top_card), gtk.s("card-banner"));

    const head_lbl = c.gtk_label_new(gtk.s("🐧 Linux Boot Manager (systemd-boot) 启动项列表"));
    c.gtk_label_set_xalign(@ptrCast(head_lbl), 0.0);
    c.gtk_widget_add_css_class(@ptrCast(head_lbl), gtk.s("entry-title"));
    c.gtk_box_append(@ptrCast(top_card), @ptrCast(head_lbl));

    const desc_lbl = c.gtk_label_new(gtk.s("以下为当前主板固件 Boot0002 启动时呈现的系统引导条目，支持自由切换顺位(▲/▼)、置顶首选项或编辑内核参数："));
    c.gtk_label_set_xalign(@ptrCast(desc_lbl), 0.0);
    c.gtk_widget_add_css_class(@ptrCast(desc_lbl), gtk.s("entry-sub"));
    c.gtk_box_append(@ptrCast(top_card), @ptrCast(desc_lbl));

    c.gtk_box_append(@ptrCast(content_box), @ptrCast(top_card));

    // ── 启动项卡片列表 ──
    for (lcfg.entries.items, 0..) |e, idx| {
        const card = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 12);
        if (e.is_default) {
            c.gtk_widget_add_css_class(@ptrCast(card), gtk.s("card-first"));
        } else {
            c.gtk_widget_add_css_class(@ptrCast(card), gtk.s("card"));
        }

        // 1. 左侧：序号与识别标志
        const left_box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_VERTICAL), 4);
        c.gtk_widget_set_size_request(@ptrCast(left_box), 100, -1);

        const badge_text = if (e.is_default) gtk.zstr("★ 首选默认") else gtk.zfmt("顺位 #{d}", .{idx + 1});
        const badge = c.gtk_label_new(gtk.s(badge_text));
        c.gtk_widget_add_css_class(@ptrCast(badge), gtk.s("pill-badge"));
        if (e.is_default) {
            c.gtk_widget_add_css_class(@ptrCast(badge), gtk.s("badge-green"));
        } else {
            c.gtk_widget_add_css_class(@ptrCast(badge), gtk.s("badge-gray"));
        }
        c.gtk_box_append(@ptrCast(left_box), @ptrCast(badge));

        const id_lbl = c.gtk_label_new(gtk.s(gtk.zstr(e.id)));
        c.gtk_label_set_ellipsize(@ptrCast(id_lbl), @intCast(c.PANGO_ELLIPSIZE_MIDDLE));
        c.gtk_widget_add_css_class(@ptrCast(id_lbl), gtk.s("entry-sub"));
        c.gtk_box_append(@ptrCast(left_box), @ptrCast(id_lbl));
        c.gtk_box_append(@ptrCast(card), @ptrCast(left_box));

        // 2. 中间：标题与详细参数
        const mid_box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_VERTICAL), 4);
        c.gtk_widget_set_hexpand(@ptrCast(mid_box), 1);

        // 标题行
        const tr = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 8);
        const title_lbl = c.gtk_label_new(gtk.s(gtk.zstr(e.title)));
        c.gtk_label_set_xalign(@ptrCast(title_lbl), 0.0);
        c.gtk_widget_add_css_class(@ptrCast(title_lbl), gtk.s("entry-title"));
        c.gtk_box_append(@ptrCast(tr), @ptrCast(title_lbl));

        // 类型药丸
        const type_str = if (std.mem.endsWith(u8, e.id, ".efi"))
            "UKI 镜像"
        else if (std.mem.startsWith(u8, e.id, "auto-"))
            "固件工具"
        else if (e.linux.len > 0)
            "Linux 内核"
        else if (e.efi.len > 0)
            "Windows EFI"
        else
            "系统引导";
        const type_lbl = c.gtk_label_new(gtk.s(type_str));
        c.gtk_widget_add_css_class(@ptrCast(type_lbl), gtk.s("pill-badge"));
        if (std.mem.endsWith(u8, e.id, ".efi")) {
            c.gtk_widget_add_css_class(@ptrCast(type_lbl), gtk.s("badge-amber"));
        } else if (std.mem.startsWith(u8, e.id, "auto-")) {
            c.gtk_widget_add_css_class(@ptrCast(type_lbl), gtk.s("badge-gray"));
        } else if (e.linux.len > 0) {
            c.gtk_widget_add_css_class(@ptrCast(type_lbl), gtk.s("badge-blue"));
        } else if (e.efi.len > 0) {
            c.gtk_widget_add_css_class(@ptrCast(type_lbl), gtk.s("badge-purple"));
        } else {
            c.gtk_widget_add_css_class(@ptrCast(type_lbl), gtk.s("badge-gray"));
        }
        c.gtk_box_append(@ptrCast(tr), @ptrCast(type_lbl));
        c.gtk_box_append(@ptrCast(mid_box), @ptrCast(tr));

        // 路径详情
        if (e.linux.len > 0) {
            const p_lbl = c.gtk_label_new(gtk.s(gtk.zfmt("内核: {s}", .{e.linux})));
            c.gtk_label_set_xalign(@ptrCast(p_lbl), 0.0);
            c.gtk_widget_add_css_class(@ptrCast(p_lbl), gtk.s("entry-sub"));
            c.gtk_box_append(@ptrCast(mid_box), @ptrCast(p_lbl));
        } else if (e.efi.len > 0) {
            const p_lbl = c.gtk_label_new(gtk.s(gtk.zfmt("EFI 路径: {s}", .{e.efi})));
            c.gtk_label_set_xalign(@ptrCast(p_lbl), 0.0);
            c.gtk_widget_add_css_class(@ptrCast(p_lbl), gtk.s("entry-sub"));
            c.gtk_box_append(@ptrCast(mid_box), @ptrCast(p_lbl));
        }

        // 命令行参数详情
        if (e.options.len > 0) {
            const opt_lbl = c.gtk_label_new(gtk.s(gtk.zfmt("参数: {s}", .{e.options})));
            c.gtk_label_set_xalign(@ptrCast(opt_lbl), 0.0);
            c.gtk_label_set_ellipsize(@ptrCast(opt_lbl), @intCast(c.PANGO_ELLIPSIZE_END));
            c.gtk_widget_add_css_class(@ptrCast(opt_lbl), gtk.s("entry-sub"));
            c.gtk_box_append(@ptrCast(mid_box), @ptrCast(opt_lbl));
        }

        c.gtk_box_append(@ptrCast(card), @ptrCast(mid_box));

        // 3. 右侧按钮组
        const btn_box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 6);
        c.gtk_widget_set_valign(@ptrCast(btn_box), @intCast(c.GTK_ALIGN_CENTER));

        const user_data: ?*anyopaque = @ptrFromInt(idx);

        if (!e.is_default) {
            const btn_top = c.gtk_button_new_with_label(gtk.s("👑 置顶首选"));
            _ = c.g_signal_connect_data(@ptrCast(btn_top), gtk.s("clicked"), @ptrCast(&onSetLoaderDefaultClicked), user_data, null, 0);
            c.gtk_box_append(@ptrCast(btn_box), btn_top);
        }

        if (idx < reorderable_count and idx > 0) {
            const btn_up = c.gtk_button_new_with_label(gtk.s("▲"));
            _ = c.g_signal_connect_data(@ptrCast(btn_up), gtk.s("clicked"), @ptrCast(&onMoveLoaderEntryUpClicked), user_data, null, 0);
            c.gtk_box_append(@ptrCast(btn_box), btn_up);
        }

        if (idx < reorderable_count and idx < reorderable_count - 1) {
            const btn_down = c.gtk_button_new_with_label(gtk.s("▼"));
            _ = c.g_signal_connect_data(@ptrCast(btn_down), gtk.s("clicked"), @ptrCast(&onMoveLoaderEntryDownClicked), user_data, null, 0);
            c.gtk_box_append(@ptrCast(btn_box), btn_down);
        }

        if (e.is_conf_file) {
            const btn_edit = c.gtk_button_new_with_label(gtk.s("✏ 编辑条目"));
            _ = c.g_signal_connect_data(@ptrCast(btn_edit), gtk.s("clicked"), @ptrCast(&onEditSingleEntryClicked), user_data, null, 0);
            c.gtk_box_append(@ptrCast(btn_box), btn_edit);
        }

        c.gtk_box_append(@ptrCast(card), @ptrCast(btn_box));
        c.gtk_box_append(@ptrCast(content_box), @ptrCast(card));
    }

    // ── 底部操作栏 ──
    const btm_row = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 10);
    c.gtk_widget_set_margin_top(@ptrCast(btm_row), 10);

    const btn_hw = c.gtk_button_new_with_label(gtk.s("🔧 底层 NVRAM 硬件参数"));
    _ = c.g_signal_connect_data(@ptrCast(btn_hw), gtk.s("clicked"), @ptrCast(&onSwitchToHardwareEditDialog), null, null, 0);
    c.gtk_box_append(@ptrCast(btm_row), btn_hw);

    const spacer = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 0);
    c.gtk_widget_set_hexpand(@ptrCast(spacer), 1);
    c.gtk_box_append(@ptrCast(btm_row), spacer);

    const btn_close = c.gtk_button_new_with_label(gtk.s("关闭"));
    _ = c.g_signal_connect_data(@ptrCast(btn_close), gtk.s("clicked"), @ptrCast(&onCloseLoaderEntriesDialog), null, null, 0);
    c.gtk_box_append(@ptrCast(btm_row), btn_close);

    c.gtk_box_append(@ptrCast(content_box), @ptrCast(btm_row));

    c.gtk_scrolled_window_set_child(@ptrCast(scrolled), @ptrCast(content_box));
    c.gtk_window_set_child(@ptrCast(dialog), @ptrCast(scrolled));
    c.gtk_window_present(@ptrCast(dialog));
}

fn onSetLoaderDefaultClicked(_: *c.GtkButton, user_data: ?*anyopaque) callconv(.c) void {
    const idx = @intFromPtr(user_data);
    if (idx >= cached_entries_count) return;

    const snap = &cached_entries[idx];
    const entry_id = snap.id_buf[0..snap.id_len];
    const title = snap.title_buf[0..snap.title_len];

    var lcfg = sdboot.loadConfig(state.io, state.gpa) catch return;
    defer lcfg.deinit(state.gpa);

    const res = sdboot.saveGlobalConf(state.io, state.gpa, entry_id, lcfg.timeout, lcfg.console_mode) catch return;
    defer res.deinit(state.gpa);

    if (res.ok()) {
        refreshData();
        openLoaderEntriesDisplay();
        showStatus(gtk.zfmt("✔ 成功将 [{s}] 设为第一首选启动项！(已更新 loader.conf)", .{title}), false);
    } else {
        showStatus(res.stderr, true);
    }
}

fn onMoveLoaderEntryUpClicked(_: *c.GtkButton, user_data: ?*anyopaque) callconv(.c) void {
    const idx = @intFromPtr(user_data);
    if (idx == 0 or idx >= cached_entries_count) return;

    var lcfg = sdboot.loadConfig(state.io, state.gpa) catch return;
    defer lcfg.deinit(state.gpa);

    const res = sdboot.moveLoaderEntry(state.io, state.gpa, lcfg, idx, idx - 1) catch |err| {
        showStatus(gtk.zfmt("[-] 调整启动项顺位失败: {s}", .{@errorName(err)}), true);
        return;
    };
    defer res.deinit(state.gpa);

    if (res.ok()) {
        refreshData();
        openLoaderEntriesDisplay();
        showStatus(gtk.zfmt("✔ 启动项顺位已上移至 #{d} (已更新 systemd-boot 菜单次序)", .{idx}), false);
    } else {
        showStatus(res.stderr, true);
    }
}

fn onMoveLoaderEntryDownClicked(_: *c.GtkButton, user_data: ?*anyopaque) callconv(.c) void {
    const idx = @intFromPtr(user_data);
    if (idx + 1 >= cached_entries_count) return;

    var lcfg = sdboot.loadConfig(state.io, state.gpa) catch return;
    defer lcfg.deinit(state.gpa);

    const res = sdboot.moveLoaderEntry(state.io, state.gpa, lcfg, idx, idx + 1) catch |err| {
        showStatus(gtk.zfmt("[-] 调整启动项顺位失败: {s}", .{@errorName(err)}), true);
        return;
    };
    defer res.deinit(state.gpa);

    if (res.ok()) {
        refreshData();
        openLoaderEntriesDisplay();
        showStatus(gtk.zfmt("✔ 启动项顺位已下移至 #{d} (已更新 systemd-boot 菜单次序)", .{idx + 2}), false);
    } else {
        showStatus(res.stderr, true);
    }
}

fn onEditSingleEntryClicked(_: *c.GtkButton, user_data: ?*anyopaque) callconv(.c) void {
    const idx = @intFromPtr(user_data);
    if (idx >= cached_entries_count) return;

    const snap = &cached_entries[idx];
    @memcpy(single_entry_id_buf[0..snap.id_len], snap.id_buf[0..snap.id_len]);
    single_entry_id_len = snap.id_len;

    const title = snap.title_buf[0..snap.title_len];
    const options = snap.options_buf[0..snap.options_len];

    const dialog = c.gtk_window_new();
    single_edit_dialog = @ptrCast(dialog);

    c.gtk_window_set_title(@ptrCast(dialog), gtk.s(gtk.zfmt("编辑启动项: {s}", .{title})));
    c.gtk_window_set_transient_for(@ptrCast(dialog), @ptrCast(loader_entries_dialog));
    c.gtk_window_set_modal(@ptrCast(dialog), 1);
    c.gtk_window_set_default_size(@ptrCast(dialog), 600, 320);

    const box = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_VERTICAL), 10);
    c.gtk_widget_set_margin_start(@ptrCast(box), 20);
    c.gtk_widget_set_margin_end(@ptrCast(box), 20);
    c.gtk_widget_set_margin_top(@ptrCast(box), 16);
    c.gtk_widget_set_margin_bottom(@ptrCast(box), 16);

    const f_title = createFormField("启动项显示标题 (Title):", gtk.zstr(title));
    single_title_entry = f_title.entry;
    c.gtk_box_append(@ptrCast(box), f_title.container);

    if (options.len > 0 or snap.is_conf_file) {
        const f_opt = createFormField("内核启动命令行参数 (Options / Cmdline):", gtk.zstr(options));
        single_options_entry = f_opt.entry;
        c.gtk_box_append(@ptrCast(box), f_opt.container);
    } else {
        single_options_entry = null;
    }

    if (snap.is_conf_file) {
        const sk = snap.sort_key_buf[0..snap.sort_key_len];
        const f_sk = createFormField("引导排位排序键 (sort-key, 决定启动菜单排位):", gtk.zstr(sk));
        single_sort_key_entry = f_sk.entry;
        c.gtk_box_append(@ptrCast(box), f_sk.container);
    } else {
        single_sort_key_entry = null;
    }

    const btn_row = c.gtk_box_new(@intCast(c.GTK_ORIENTATION_HORIZONTAL), 10);
    c.gtk_widget_set_halign(@ptrCast(btn_row), @intCast(c.GTK_ALIGN_END));
    c.gtk_widget_set_margin_top(@ptrCast(btn_row), 10);

    const btn_cancel = c.gtk_button_new_with_label(gtk.s("取消"));
    _ = c.g_signal_connect_data(@ptrCast(btn_cancel), gtk.s("clicked"), @ptrCast(&onCloseSingleEditDialog), null, null, 0);
    c.gtk_box_append(@ptrCast(btn_row), btn_cancel);

    const btn_save = c.gtk_button_new_with_label(gtk.s("💾 确认保存修改"));
    c.gtk_widget_add_css_class(@ptrCast(btn_save), gtk.s("suggested-action"));
    _ = c.g_signal_connect_data(@ptrCast(btn_save), gtk.s("clicked"), @ptrCast(&onSaveSingleEntry), null, null, 0);
    c.gtk_box_append(@ptrCast(btn_row), btn_save);

    c.gtk_box_append(@ptrCast(box), @ptrCast(btn_row));
    c.gtk_window_set_child(@ptrCast(dialog), @ptrCast(box));
    c.gtk_window_present(@ptrCast(dialog));
}

fn onCloseSingleEditDialog(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (single_edit_dialog) |d| {
        c.gtk_window_destroy(d);
        single_edit_dialog = null;
    }
}

fn onSaveSingleEntry(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    const entry_id = single_entry_id_buf[0..single_entry_id_len];
    const new_title = std.mem.span(c.gtk_editable_get_text(@ptrCast(single_title_entry.?)));
    const new_options = if (single_options_entry) |oe| std.mem.span(c.gtk_editable_get_text(@ptrCast(oe))) else "";
    const new_sort_key = if (single_sort_key_entry) |ske| std.mem.span(c.gtk_editable_get_text(@ptrCast(ske))) else "";

    const res = sdboot.saveEntryConfig(
        state.io,
        state.gpa,
        entry_id,
        new_title,
        new_options,
        if (new_sort_key.len > 0) new_sort_key else null,
    ) catch |err| {
        showStatus(gtk.zfmt("[-] 保存启动项失败: {s}", .{@errorName(err)}), true);
        return;
    };
    defer res.deinit(state.gpa);

    if (res.ok()) {
        onCloseSingleEditDialog(@ptrCast(c.gtk_button_new()), null);
        openLoaderEntriesDisplay();
        refreshData();
        showStatus(gtk.zfmt("✔ 启动项 [{s}] 修改已成功写入 ESP 分区！", .{new_title}), false);
    } else {
        showStatus(res.stderr, true);
    }
}

fn onCloseLoaderEntriesDialog(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    if (loader_entries_dialog) |d| {
        c.gtk_window_destroy(d);
        loader_entries_dialog = null;
    }
}

fn onSwitchToHardwareEditDialog(_: *c.GtkButton, _: ?*anyopaque) callconv(.c) void {
    onCloseLoaderEntriesDialog(@ptrCast(c.gtk_button_new()), null);
    openHardwareNvramEditDialog(edit_ident);
}

// ============================================================================
// 对话框：编辑底层 UEFI NVRAM 硬件条目 (磁盘/分区/EFI路径/状态)
// ============================================================================

var edit_dialog: ?*c.GtkWindow = null;
var edit_ident: [4]u8 = undefined;
var edit_disk_entry: ?*c.GtkEntry = null;
var edit_part_entry: ?*c.GtkEntry = null;
var edit_label_entry: ?*c.GtkEntry = null;
var edit_loader_entry: ?*c.GtkEntry = null;
var edit_active_switch: ?*c.GtkSwitch = null;

fn openHardwareNvramEditDialog(ident: [4]u8) void {
    edit_ident = ident;

    const dialog = c.gtk_window_new();
    edit_dialog = @ptrCast(dialog);

    const title_text = gtk.zfmt("编辑 UEFI 硬件条目 [Boot{s}]", .{ident});
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
