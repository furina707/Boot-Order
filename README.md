# Boot Order Manager (UEFI 启动项与 NVRAM 综合管理器)

基于 **Zig 0.17+** 原生机器码开发，UI 采用 Linux 原生 **GTK4** 高清控件（miuix 卡片风格），直接与主板 UEFI NVRAM (`efibootmgr`) 真实双向交互。

遵循《项目准则》核心规范：
- **Zero Runtime 单二进制交付**：彻底摆脱 Python 解释器或重型依赖，冷启动毫秒级；
- **原生 HiDPI 高清渲染**：GTK4 物理点阵渲染；Windows 运行时声明最高级 `PerMonitorV2` DPI 感知，根绝 DWM 位图拉伸发虚；
- **唯一启动器 `run.py`**：摒弃任何平台依赖的 `.bat / .sh / .ps1`，单一 Python 启动器自动构建并唤起 Native 进程；
- **零 Mock、零假数据**：直接读取与写入真实主板 NVRAM；
- **默认安全与自动提权**：通过非明文管道安全注入 sudo 认证（密码 `1231`），持久化写回主板 NVRAM；
- **内建全链路自检 (Built-in Self-test)**：无图形环境一键触发数据与算法回归断言；
- **即时清理废弃代码**：死代码与冗余模块即时删除，历史由 Git 严密跟踪。

---

## 唯一统一入口 (run.py)

```bash
# 启动原生 GTK4 高清图形界面 (默认)
python run.py

# 运行内建全链路功能自检 (数据/算法/语法断言)
python run.py selftest

# 免交互 CLI 查看 NVRAM 启动项与 BootOrder 状态
python run.py --list

# 快速设置指定条目为主板第一优先启动项 (例如置顶 Boot0005)
python run.py --set-default 0005

# 设定下次开机单次直接启动项 (BootNext，不改动默认顺序)
python run.py --set-next 0005

# 设定固件倒计时超时时间 (例如 5 秒)
python run.py --set-timeout 5

# 自动清理并修复 BootOrder 重复项
python run.py --dedup

# 仅编译 Native 二进制
python run.py --build-only
```

---

## 目录规范 (Layer 3)

```
Boot-Order/
├── run.py          # 跨平台唯一统一启动器 (支持 DPI 感知与参数分发)
├── build.zig       # Zig 0.17+ 原生编译脚本 (GTK4 translate-c 构建管线)
├── .gitignore      # 过滤构建缓存 (.zig-cache / zig-out)
├── README.md       # 项目工程文档与规范说明
└── src/
    ├── gtk_c.h     # GTK4 顶层 C 头文件入口
    ├── gtk.zig     # GTK4 原生 C 绑定薄封装 (zstr/zfmt 环形缓冲)
    ├── sys.zig     # 权限查询与进程执行 (支持 sudo -S 密码管道注入)
    ├── nvram.zig   # 主板 UEFI NVRAM 解析、排序、去重与写回核心
    ├── ui.zig      # GTK4 原生图形界面 (miuix 卡片风格、智能未排序诊断)
    ├── selftest.zig# 内建全链路无头自动化测试
    └── main.zig    # 机器码二进制入口与 CLI 路由
```
