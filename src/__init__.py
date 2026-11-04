"""
================================================================================
  Boot-Order 核心业务逻辑包 (Core Package)
================================================================================
规范守则遵循：
1. 严禁使用 .bat / .sh / .ps1 文件，run.py 为唯一统一启动器。
2. 核心业务逻辑均位于 src 目录内。
3. 零假代码、零模拟数据，所有底层操作直接与系统 API 真实交互。
"""

from src.manager import (
    BootConfig,
    BootEntry,
    EfibootmgrBackend,
    BcdeditBackend,
    BACKEND,
    parse_boot_config,
)

from src.esp import (
    EspPartitionInfo,
    find_all_esp_partitions,
    inspect_esp_partitions,
)

from src.bootloader import (
    GrubConfigManager,
    SystemdBootManager,
    interactive_bootloader_menu,
)

from src.repair import (
    diagnose_boot_system,
    print_diagnostic_report,
    interactive_repair_menu,
)

from src.win11 import (
    Win11BootDiagnostic,
    Win11Analyzer,
    Win11ActionHandler,
    format_win11_report_text,
)

from src.kernel import (
    KernelInfo,
    KernelManager,
    interactive_kernel_menu,
)

__all__ = [
    "BootConfig",
    "BootEntry",
    "EfibootmgrBackend",
    "BcdeditBackend",
    "BACKEND",
    "parse_boot_config",
    "EspPartitionInfo",
    "find_all_esp_partitions",
    "inspect_esp_partitions",
    "GrubConfigManager",
    "SystemdBootManager",
    "interactive_bootloader_menu",
    "diagnose_boot_system",
    "print_diagnostic_report",
    "interactive_repair_menu",
    "Win11BootDiagnostic",
    "Win11Analyzer",
    "Win11ActionHandler",
    "format_win11_report_text",
    "KernelInfo",
    "KernelManager",
    "interactive_kernel_menu",
]
