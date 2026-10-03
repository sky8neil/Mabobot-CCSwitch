"""Shared paths and service definitions for the desktop launcher."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
UI_DIR = Path(__file__).resolve().parent / "ui"
ASSET_DIR = Path(__file__).resolve().parent / "assets"
APP_ICON_FILE = ASSET_DIR / "mabobot.ico"
APP_ICON_IMAGE = ASSET_DIR / "mabobot-icon.png"

SETTINGS_FILE = DATA_DIR / "launcher_settings.json"
STATE_FILE = DATA_DIR / "launcher_state.json"
CONTROL_SIGNAL_FILE = PROJECT_ROOT / ".restart_signal"
SHOW_SIGNAL_FILE = PROJECT_ROOT / ".launcher_show"
BOOTSTRAP_SCRIPT = PROJECT_ROOT / "scripts" / "launcher" / "bootstrap.ps1"
INSTALL_SCRIPT = PROJECT_ROOT / "scripts" / "install.ps1"

LAUNCHER_ID = "mabobot.desktop.launcher"
SIGNAL_PROTOCOL = 4


@dataclass(frozen=True)
class ServiceSpec:
    key: str
    label: str
    script: str
    port_env: str
    default_port: int
    health_path: str = "/health"
    startup_grace_seconds: float = 45.0


SERVICES = (
    ServiceSpec("bot", "微信 Bot", "wx_bot.py", "WX_BOT_PORT", 5555),
    ServiceSpec(
        "web",
        "Web 服务",
        "start.py",
        "WEB_PORT",
        # 2026-09-26: 8888 被 Windows 系统服务 iphlpsvc(svchost) 占用，
        # 导致 Web 面板无法绑定（F11 端口预检只能 WARN，面板就一直起不来）。
        # 2026-09-30: 用户要求改到高位段 21889，彻底避开 Windows 系统服务（iphlpsvc）与
        # WSL2 端口转发对 8889/8888 段的抢占（8888 现被 iphlpsvc 常驻占用）。
        # 仍可用环境变量 WEB_PORT 覆盖。
        21889,
        startup_grace_seconds=150.0,
    ),
)
