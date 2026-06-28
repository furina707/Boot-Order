# Boot Order Manager

UEFI 启动项管理 TUI 工具，基于 `efibootmgr`，提供直观的终端界面来管理 UEFI 启动顺序。

## 一键启动

```bash
curl -sL https://raw.githubusercontent.com/furina707/Boot-Order/master/boot-order-manager.py | sudo python3 -
```

## 依赖

- `efibootmgr`
- `python3`（标准库，无需额外安装）
- root 权限（写入操作需要）

## 安装依赖

```bash
# Arch Linux
sudo pacman -S efibootmgr

# Ubuntu / Debian
sudo apt install efibootmgr

# Fedora
sudo dnf install efibootmgr
```

## 本地运行

```bash
sudo python3 boot-order-manager.py
```

## 快捷键

| 按键 | 功能 |
|------|------|
| `↑` / `k` | 上移光标 |
| `↓` / `j` | 下移光标 |
| `m` | 上移启动项 |
| `M` | 下移启动项 |
| `1` | 设为第一启动项 |
| `Space` | 切换启用/禁用 |
| `d` | 删除启动项 |
| `t` | 设置超时时间 |
| `Enter` | 查看详情 |
| `r` | 刷新配置 |
| `?` | 帮助 |
| `q` | 退出 |

## 功能

- 查看 UEFI 启动项及顺序
- 调整启动顺序（上移/下移/置顶）
- 启用/禁用启动项
- 删除启动项
- 设置 Boot Next 和 Timeout
- 查看启动项详细信息

## 许可证

MIT