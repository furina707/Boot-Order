# Boot Order Manager

UEFI 启动项管理 TUI 工具，提供直观的终端界面来管理 UEFI 启动顺序。

- **Linux**：基于 `efibootmgr`
- **Windows**：基于 `bcdedit`（固件启动项，即 `{fwbootmgr}`）

## 一键启动

Linux：

```bash
curl -sL https://raw.githubusercontent.com/furina707/Boot-Order/master/boot-order-manager.py | sudo python3 -
```

Windows（管理员 PowerShell）：

```powershell
irm https://raw.githubusercontent.com/furina707/Boot-Order/master/boot-order-manager.py | python -
```

## 依赖

Linux：

- `efibootmgr`
- `python3`（标准库，无需额外安装）
- root 权限（写入操作需要）

```bash
# Arch Linux
sudo pacman -S efibootmgr

# Ubuntu / Debian
sudo apt install efibootmgr

# Fedora
sudo dnf install efibootmgr
```

Windows：

- Python 3.8+（建议加入 PATH）
- `windows-curses`：`pip install windows-curses`
- 管理员权限（`bcdedit` 读写 BCD 需要，非管理员启动会直接提示）

## 本地运行

```bash
# Linux（root）
sudo python3 boot-order-manager.py
```

```powershell
# Windows（管理员终端）
python boot-order-manager.py
```

## 快捷键

| 按键 | 功能 |
|------|------|
| `↑` / `k` | 上移光标 |
| `↓` / `j` | 下移光标 |
| `PgUp` / `PgDn` | 上/下翻页 |
| `Home` / `End` | 跳到首项/末项 |
| `m` | 上移启动项 |
| `M` | 下移启动项 |
| `1` | 设为第一启动项 |
| `n` | 设为下次一次性启动项（BootNext） |
| `Space` | 切换启用/禁用（仅 Linux） |
| `d` | 删除启动项 |
| `t` | 设置超时时间 |
| `Enter` | 查看详情 |
| `r` | 刷新配置 |
| `?` | 帮助 |
| `q` | 退出 |

## 功能

- 查看 UEFI 启动项及顺序
- 调整启动顺序（上移/下移/置顶）
- 启用/禁用启动项（仅 Linux）
- 删除启动项
- 设置 Boot Next 和 Timeout
- 查看启动项详细信息（含 EFI 加载路径）

## Windows 说明

- 启动项 ID 显示 GUID 的前 8 位（如 `9BD2F12C`），完整 GUID 用于底层命令。
- `bcdedit` 无法启用/禁用固件启动项，`Space` 在 Windows 下会提示不支持。
- 为防手滑，「Windows Boot Manager」（`{bootmgr}`）条目禁止删除。
- 建议在 Windows Terminal / conhost 下运行；Windows 10/11 自带终端均可正常渲染。

## 许可证

MIT
