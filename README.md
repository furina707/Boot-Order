# Boot Order Manager

UEFI 启动项与多系统引导综合管理系统，提供终端交互 TUI、Linux 引导加载器配置（GRUB 2 / systemd-boot）、引导健康诊断与一键修复重建、以及物理 ESP 分区跨平台挂载探测。

- **UEFI 固件启动顺序**：
  - Linux：基于 `efibootmgr` 真实交互主板 NVRAM
  - Windows：基于 `bcdedit` 真实交互 `{fwbootmgr}` 固件启动数据库
- **全可视化按钮化交互（零指令输入要求）**：
  - **完全摒弃文本指令输入**：所有操作均通过可视化交互按钮（Buttons）、单选卡片（Radio Cards）、数字步进器（Steppers）完成
  - **全方位鼠标支持**：完美支持鼠标点击底栏按钮、表格数据行、弹窗功能选项与预设卡片
  - **双重导航模式**：同时支持鼠标即点即用与键盘 `↑ / ↓ / ← / → / Tab / Enter / Space`，Diff 变更审核配备独立 `[ ✔ 授权写入 ]` / `[ ✖ 放弃返回 ]` 按钮
- **Linux 引导加载器配置与多内核切换**：
  - **多内核深度探测与一键切换**：自动扫描系统已安装的所有 Linux 内核（标准内核、Zen、LTS、Hardened 等），一键切换永久默认内核或下次单次启动（One-Shot），并可为未配置引导条目的内核自动生成 entry 配置文件与 Diff 预览审核
  - **GRUB 2**：修改 `/etc/default/grub` 中的超时时间 (`GRUB_TIMEOUT`)、默认启动项 (`GRUB_DEFAULT`)、内核启动参数 (`GRUB_CMDLINE_LINUX_DEFAULT`)，并自动执行 `update-grub` / `grub-mkconfig`
  - **systemd-boot**：管理 `$ESP/loader/loader.conf` 与 `entries/*.conf`，支持内核参数与超时调整，并自动调用 `bootctl update`
- **引导深度诊断与一键修复**：
  - 自动扫描全盘 ESP 分区中的物理 `.efi` 引导文件（shimx64, grubx64, systemd-bootx64, bootmgfw 等）
  - 交叉比对主板 NVRAM 条目，检出因 Windows 更新、BIOS 重置或误删导致的丢失条目，支持一键重新注册写回 NVRAM 并自动置顶
- **跨平台 ESP 探测与挂载**：
  - Windows：基于 PowerShell 动态分配空闲盘符挂载、扫描文件、安全卸载
  - Linux：基于 `lsblk`、`efibootmgr -v` 与 `/proc/mounts` 智能定位物理 ESP 分区
- **Windows 11 引导深度智能分析与双系统互通**：
  - **NVRAM 深度体检**：分析 `Windows Boot Manager` 顺位、激活状态、PARTUUID、EFI 二进制路径与 BCDOBJECT GUID
  - **双系统架构研判**：自动诊断「双 ESP 独立分区隔离」架构（彻底解决 systemd-boot 菜单中找不到 Win11 的问题）
  - **组件与安全基线**：探测 Windows C: 盘、BitLocker 卷头、WinRE 恢复分区、TPM 2.0 状态与 Secure Boot 安全启动
  - **一键切换与修复**：支持设定 UEFI `BootNext` 单次直入 Win11、第一启动项顺位切换、以及跨 ESP 一键同步引导到 systemd-boot

---

## 统一启动器 (run.py)

项目严格遵循规范，**严禁使用 .bat / .sh / .ps1 脚本**，统一使用唯一的 Python 启动器 `run.py`：

```bash
# 直接运行：一步秒进全屏交互 TUI（双平台自动提权，零中间步骤）
python run.py             # Linux / Windows 通用

# 直接进入各项子功能 CLI / 路由：
python run.py --tui         # 启动 UEFI 启动项排序 curses TUI
python run.py --kernel      # 进入 Linux 多内核探测与一键切换工作台
python run.py --bootloader  # 进入 Linux 引导加载器配置 (GRUB / systemd-boot)
python run.py --win11       # 进入 Windows 11 引导智能分析与双系统互通
python run.py --repair      # 运行引导深度诊断与一键修复恢复
python run.py --esp         # 真实探测并挂载分析 ESP 分区
python run.py --abbr        # 查看 UEFI 与引导体系核心专业缩写速查表

# 快速设置命令行（免交互）：
python run.py --set-default 0005        # 快速将条目 0005 设为主板第一优先启动项
python run.py --set-kernel zen          # 快速切换默认启动内核为 Zen 内核
python run.py --set-kernel-oneshot zen  # 设定仅下次开机单次启动 Zen 内核
python run.py --set-timeout 5           # 快速将固件引导超时设置为 5 秒
python run.py --clear-auth              # 清除本地保存的 sudo 密码凭据 (恢复提示)
```

---

## 依赖说明

### Linux
- `efibootmgr`
- `python3`（标准库，无需额外 pip 包）
- 管理员权限（`sudo`，写 NVRAM、挂载分区或更新引导时必需）

```bash
# Arch Linux
sudo pacman -S efibootmgr

# Ubuntu / Debian
sudo apt install efibootmgr

# Fedora
sudo dnf install efibootmgr
```

### Windows
- Python 3.8+
- `windows-curses`：`pip install windows-curses`
- 管理员权限（`bcdedit` 读写 BCD 需要，`run.py` 会自动触发原生 UAC 申请提权）

---

## 核心功能详解

### 1. UEFI 启动项管理 (TUI 快捷键)

| 按键 | 功能 |
|------|------|
| `↑` / `k` | 上移光标 |
| `↓` / `j` | 下移光标 |
| `PgUp` / `PgDn` | 上/下翻页 |
| `Home` / `End` | 跳到首项/末项 |
| `m` | 上移启动项顺序 |
| `M` | 下移启动项顺序 |
| `1` | 设为第一启动项（置顶） |
| `n` | 设为下次一次性启动项（BootNext） |
| `Space` | 切换启用/禁用（仅 Linux） |
| `d` | 删除指定启动项 |
| `t` | 设置引导超时时间 (Timeout) |
| `k` | Linux 多内核探测与一键切换 (标准 / Zen / LTS 等) |
| `b` | Linux 引导加载器配置 (GRUB / systemd-boot) |
| `w` | Windows 11 引导深度智能分析与双系统互通 |
| `c` / `f` | 引导深度诊断与一键修复恢复 (写回 NVRAM) |
| `e` | 查看物理 ESP 分区与 .efi 文件结构 |
| `Enter` | 查看启动项详细信息（含硬件 EFI 路径） |
| `r` | 刷新当前配置 |
| `?` | 快捷键帮助 |
| `q` | 退出 TUI |

### 2. Linux 引导加载器与多内核配置工作台
- **全新全屏可视化独立工作台**：按 `b` 键或鼠标点击 `[b] Linux引导` 即可秒进专用全屏工作台，彻底根除弹窗背景字符透色与残影问题。
- **条目单选卡片与深度实时体检**：以单选卡片 (`(*)`) 列出所有引导条目（Type #1 独立条目、Type #2 UKI 统一内核、Auto 固件自动发现），点击即可实时查看镜像路径、initrd 内存盘、内核 options 命令行参数及健康研判。
- **纯按钮式零指令操作**：配备 7 个交互式按钮（支持鼠标直击与 Tab/←/→ 焦点切换）：
  - `[ ★ 设为默认引导 ]`：一键将所选条目设为 systemd-boot / GRUB 默认开机首选项
  - `[ ⏱ 调整菜单倒计时 ]`：集成交互式步进器与 `0s/3s/5s/10s` 快速预设，无需输入数字
  - `[ 🛠 一键注入 Win11 ]`：自动探测 Windows ESP 分区并生成配置 Diff 供审核批准
  - `[ 🔄 切换启动内核 ]`：无缝转入多内核可视化切换工作台（标准 / Zen / LTS 等）
  - `[ ✏️ 编辑内核启动参数 ]`：快速编辑 options 或 `GRUB_CMDLINE_LINUX_DEFAULT`
  - `[ ⚡ 立即重载生效 ]`：自动执行 `bootctl update` 或 `grub-mkconfig`
  - `[ 🔙 返回主列表 ]`：一键干净返回主界面
- **多内核深度探测与一键切换**：自动扫描系统已安装的所有 Linux 内核（标准内核、Zen、LTS、Hardened 等），一键切换永久默认内核或下次单次启动（One-Shot），并可为未配置引导条目的内核自动生成 entry 配置文件与 Diff 预览审核。

### 3. 引导诊断与一键修复重建
针对以下高频痛点：
1. **Windows 更新抹掉 Linux 启动项**：Windows 经常在大版本更新时重置固件第一启动项或删除第三方 NVRAM 条目。
2. **BIOS 更新或主板电池掉电**：NVRAM 数据丢失，导致开机直接进 BIOS 或只进 Windows。
3. **误删启动项**：手滑删除了 `Linux Boot Manager` 或 `GRUB`。

**解决方案**：
运行 `python run.py --repair`，程序会自动深度扫描所有 ESP 分区内的物理 `.efi` 文件，交叉对比 NVRAM 条目，一键重写 NVRAM 并恢复置顶。

---

## 项目架构规范

1. **唯一统一入口**：全平台通过 `run.py` 启动，严禁任何外部 shell/bat 包装。
2. **业务分层清晰**：
   - `src/manager.py`：UEFI NVRAM 启动项交互核心与自适应 Split-View TUI 渲染引擎
   - `src/kernel.py`：Linux 多内核深度探测、引导条目生成与一键切换管理器
   - `src/bootloader.py`：GRUB 2 与 systemd-boot 配置文件解析及内核参数管理器
   - `src/win11.py`：Windows 11 引导深度智能体检、双 ESP 架构诊断与跨分区互通
   - `src/repair.py`：引导健康体检、EFI 引导扫描、缺失条目自动恢复
   - `src/esp.py`：跨平台物理 ESP 分区探测与挂载分析
   - `src/ext4.py`：纯 Python 免驱动物理硬盘 ext4 分区解析读取引擎
3. **真实系统交互**：零假代码、零模拟数据，所有操作直接作用于系统底层。

---

## 许可证

MIT License
