"""Serialize WSL control-plane calls and stop probe storms after transport failures."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from typing import Any, Callable, Sequence

from app.utils.subprocess_utils import apply_hidden_process_defaults

_logger = logging.getLogger(__name__)


class WslCircuitOpenError(subprocess.SubprocessError):
    """Raised while the WSL control-plane circuit is cooling down."""


class WslTransportError(subprocess.SubprocessError):
    """Raised for an explicit WSL control-plane transport failure."""


_RUN_LOCK = threading.Lock()
_STATE_LOCK = threading.RLock()
_consecutive_failures = 0
_open_until = 0.0
_last_error = ""

# 自愈状态（2026-09-30 新增）：熔断后自动做一次最小 WSL 探活
_RECOVERY_LOCK = threading.Lock()
_last_recovery_at = 0.0
_recovery_runner: Callable[..., Any] = subprocess.run


def _positive_number(name: str, default: float) -> float:
    try:
        value = float(str(os.getenv(name) or default).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _failure_threshold() -> int:
    return max(1, int(_positive_number("MABOBOT_WSL_CIRCUIT_FAILURES", 1)))


def _cooldown_seconds() -> float:
    return _positive_number("MABOBOT_WSL_CIRCUIT_SECONDS", 90.0)


def _wsl_distro() -> str:
    """自愈探活用的发行版；默认 Ubuntu-24.04，可用 MABOBOT_WSL_DISTRO 覆盖。"""
    return (os.getenv("MABOBOT_WSL_DISTRO") or "Ubuntu-24.04").strip()


def _recovery_cooldown_seconds() -> float:
    return _positive_number("MABOBOT_WSL_RECOVERY_SECONDS", 300.0)


def _recovery_timeout_seconds() -> float:
    # 2026-09-30: 冷启动（重启后 WSL 虚拟机尚未起来）实测需要几十秒，
    # 30 秒会在冷启动上超时，导致熔断无法解除（探测被熔断拦截，WSL 永远热不起来）。
    return _positive_number("MABOBOT_WSL_RECOVERY_TIMEOUT_SECONDS", 120.0)


def _attempt_wsl_recovery(detail: str) -> bool:
    """熔断后做一次最小 WSL 探活（等价于 `wsl.exe -d <distro> -- true`）。

    2026-09-30 事故：WSL 控制面返回 `Wsl/Service/E_UNEXPECTED`，之后每次探测都失败，
    熔断持续 20 分钟（累计 551 次），Codex 运行时全程不可用，只能人工探活解锁。
    这里把那次人工动作自动化：成功则立即解除熔断，失败则保持熔断并等冷却。
    最佳努力：任何异常都必须被吞掉，绝不影响调用方。
    """
    global _last_recovery_at
    now = time.monotonic()
    if now - _last_recovery_at < _recovery_cooldown_seconds():
        return False
    with _RECOVERY_LOCK:
        now = time.monotonic()
        if now - _last_recovery_at < _recovery_cooldown_seconds():
            return False
        _last_recovery_at = now
    command = ["wsl.exe"]
    distro = _wsl_distro()
    if distro:
        command += ["-d", distro]
    command += ["--", "true"]
    _logger.warning("WSL 自愈：熔断后尝试最小探活 %s（触发原因：%s）", " ".join(command), detail[:300])
    try:
        run_kwargs = apply_hidden_process_defaults(
            {
                "timeout": _recovery_timeout_seconds(),
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
            }
        )
        result = _recovery_runner(command, **run_kwargs)
    except BaseException as exc:  # noqa: BLE001 - 自愈绝不允许抛出
        _logger.warning("WSL 自愈：探活异常 %s: %s", type(exc).__name__, exc)
        return False
    rc = int(getattr(result, "returncode", 1) or 0)
    if rc == 0 and not _is_transport_failure(result):
        _record_success()
        _logger.warning("WSL 自愈：成功，已解除熔断，下次探测直接放行")
        return True
    stdout = getattr(result, "stdout", b"") or b""
    stderr = getattr(result, "stderr", b"") or b""
    tail = f"{stdout!r} {stderr!r}".strip()[-300:]
    _logger.warning("WSL 自愈：仍然失败 rc=%s %s", rc, tail)
    return False


def _circuit_error(now: float) -> WslCircuitOpenError | None:
    with _STATE_LOCK:
        if now >= _open_until:
            return None
        remaining = max(1, int(_open_until - now + 0.999))
        detail = _last_error or "WSL transport unavailable"
    return WslCircuitOpenError(f"WSL 探测已熔断，请在约 {remaining} 秒后重试：{detail}")


def _record_success() -> None:
    global _consecutive_failures, _open_until, _last_error
    with _STATE_LOCK:
        _consecutive_failures = 0
        _open_until = 0.0
        _last_error = ""


def _record_failure(error: BaseException | str) -> None:
    global _consecutive_failures, _open_until, _last_error
    detail = str(error or "WSL transport unavailable").strip()[-1000:]
    opened = False
    with _STATE_LOCK:
        _consecutive_failures += 1
        _last_error = detail
        if _consecutive_failures >= _failure_threshold():
            _open_until = time.monotonic() + _cooldown_seconds()
            opened = True
    # 熔断打开后立刻尝试一次自愈（在 _STATE_LOCK 之外执行，避免长时间持锁）
    if opened:
        try:
            _attempt_wsl_recovery(detail)
        except BaseException:  # noqa: BLE001 - 自愈失败绝不能影响熔断语义
            _logger.warning("WSL 自愈：调度异常，已忽略", exc_info=True)


def _is_transport_failure(result: Any) -> bool:
    if int(getattr(result, "returncode", 0) or 0) == 0:
        return False
    detail = f"{getattr(result, 'stdout', '')}\n{getattr(result, 'stderr', '')}".lower()
    markers = (
        "utilbindvsock",
        "initcreateprocessutilityvm",
        "wsl/service",
        "wsl_e_",
        "the virtual machine or container exited unexpectedly",
        "the operation timed out",
        "socket failed",
    )
    return any(marker in detail for marker in markers)


def run_guarded_wsl_command(
    command: Sequence[str],
    *,
    runner: Callable[..., Any] | None = None,
    **kwargs: Any,
) -> Any:
    """Run one WSL command at a time and fail fast during circuit cooldown."""
    execute = runner or subprocess.run
    run_kwargs = apply_hidden_process_defaults(kwargs)
    error = _circuit_error(time.monotonic())
    if error is not None:
        raise error
    with _RUN_LOCK:
        error = _circuit_error(time.monotonic())
        if error is not None:
            raise error
        try:
            result = execute(list(command), **run_kwargs)
        except (OSError, subprocess.TimeoutExpired) as exc:
            _record_failure(exc)
            raise
        if _is_transport_failure(result):
            detail = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "WSL transport failure")
            _record_failure(str(detail).strip())
            raise WslTransportError(str(detail).strip())
        else:
            _record_success()
        return result


def wsl_circuit_status() -> dict[str, Any]:
    """Expose non-sensitive diagnostics for health endpoints and tests."""
    now = time.monotonic()
    with _STATE_LOCK:
        return {
            "open": now < _open_until,
            "retry_after_seconds": max(0, int(_open_until - now + 0.999)),
            "consecutive_failures": _consecutive_failures,
            "last_error": _last_error,
            "last_recovery_seconds_ago": (
                round(now - _last_recovery_at, 1) if _last_recovery_at else None
            ),
        }


def reset_wsl_probe_guard() -> None:
    """Reset process-local state; intended for recovery hooks and isolated tests."""
    global _last_recovery_at
    with _RECOVERY_LOCK:
        _last_recovery_at = 0.0
    _record_success()
