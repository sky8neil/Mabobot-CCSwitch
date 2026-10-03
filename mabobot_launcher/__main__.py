"""Windows entry point for ``pythonw -m mabobot_launcher``."""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .application import DesktopLauncher
from .constants import LOG_DIR, PROJECT_ROOT
from .instance import SingleInstance


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mabobot 桌面启动器")
    parser.add_argument("--startup", action="store_true", help="Windows 登录启动模式")
    return parser.parse_args()


def _expected_venv_interpreter() -> Path | None:
    """Return the only interpreter the launcher may run under on Windows."""
    if os.name != "nt":
        return None
    candidate = PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if candidate.is_file():
        return candidate
    return None


def interpreter_is_venv() -> bool:
    """F11b: enforce the venv interpreter and never fall back to system Python.

    Inside a Windows venv the redirector stub sets ``__PYVENV_LAUNCHER__`` so
    the base interpreter still reports the venv path via ``sys.executable``;
    a genuine system-Python launch reports ``C:\\Program Files\\Python312``
    and is refused here.  When no venv exists at all we refuse on Windows
    rather than silently spinning up a second stack under system Python.
    """
    expected = _expected_venv_interpreter()
    if expected is None:
        return os.name != "nt"
    actual = Path(sys.executable)
    try:
        if actual.resolve() == expected.resolve():
            return True
    except OSError:
        pass
    return os.path.normcase(str(actual)) == os.path.normcase(str(expected))


def record_launcher_event(message: str) -> None:
    """Append a launcher event to stderr and logs/launcher.log."""
    print(message, file=sys.stderr)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        import datetime

        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with (LOG_DIR / "launcher.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} [WARNING] {message}\n")
    except OSError:
        pass


def record_duplicate_launch(instance: SingleInstance) -> None:
    """F11.1: 记录第二个启动器实例被拒绝的原因，不静默 double-spawn。"""
    record_launcher_event(
        f"Mabobot launcher duplicate start refused: already_running ({instance.describe_owner()})"
    )


def show_error(message: str) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, "Mabobot 启动失败", 0x10)
    else:
        print(message, file=sys.stderr)


def main() -> int:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    if not interpreter_is_venv():
        expected = _expected_venv_interpreter()
        message = (
            "Mabobot launcher refused to start: it must run under the project venv "
            f"interpreter ({expected or PROJECT_ROOT / '.venv' / 'Scripts' / 'pythonw.exe'}), "
            f"not {sys.executable}"
        )
        record_launcher_event(message)
        show_error(
            "Mabobot 启动器必须使用项目 .venv 解释器运行，已拒绝使用系统 Python 另起一套。\n\n"
            f"当前解释器：{sys.executable}\n"
            f"期望解释器：{expected or PROJECT_ROOT / '.venv' / 'Scripts' / 'pythonw.exe'}"
        )
        return 3
    instance = SingleInstance()
    try:
        if not instance.acquire():
            record_duplicate_launch(instance)
            instance.request_show()
            return 0
        if os.name != "nt":
            show_error("Mabobot 桌面启动器目前仅支持 Windows 10/11。")
            return 2
        return DesktopLauncher(startup_mode=args.startup).run()
    except Exception as exc:
        show_error(f"桌面启动器无法启动：\n\n{exc}\n\n请查看 logs/launcher.log。")
        return 1
    finally:
        instance.release()


if __name__ == "__main__":
    raise SystemExit(main())
