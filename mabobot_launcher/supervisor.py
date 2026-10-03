"""Process supervision and health monitoring for Mabobot services."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import psutil

from .constants import LOG_DIR, PROJECT_ROOT, SERVICES, ServiceSpec

_PRESERVED_BROWSER_PROCESSES = {"chrome", "chrome.exe", "msedge", "msedge.exe"}
_STATUS_LABELS = {
    "stopped": "已停止",
    "starting": "启动中",
    "running": "正常",
    "degraded": "需关注",
    "stopping": "停止中",
    "error": "异常",
}
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ERROR_OUTPUT_RE = re.compile(
    r"(?:\[(?:ERROR|CRITICAL|FATAL)\])|(?:^(?:ERROR|CRITICAL|FATAL)(?:\s|:))",
    re.IGNORECASE,
)
_WARNING_OUTPUT_RE = re.compile(
    r"(?:\[(?:WARNING|WARN)\])|(?:^(?:WARNING|WARN)(?:\s|:))",
    re.IGNORECASE,
)


def infer_output_level(message: str) -> str:
    """Infer a child-process log level from explicit logging markers."""
    plain = _ANSI_ESCAPE_RE.sub("", str(message or "")).strip()
    if plain.startswith("Traceback (most recent call last):"):
        return "error"
    if _ERROR_OUTPUT_RE.search(plain):
        return "error"
    if _WARNING_OUTPUT_RE.search(plain):
        return "warning"
    return "info"


class LogBuffer:
    def __init__(self, max_items: int = 1200, log_file: Path | None = None):
        self._items: deque[dict[str, Any]] = deque(maxlen=max_items)
        self._sequence = 0
        self._lock = threading.RLock()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        target = log_file or (LOG_DIR / "launcher.log")
        self._logger = logging.getLogger(f"mabobot.launcher.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        handler = RotatingFileHandler(
            target,
            maxBytes=3 * 1024 * 1024,
            backupCount=2,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        self._logger.addHandler(handler)

    def add(self, source: str, message: str, level: str = "info") -> dict[str, Any]:
        normalized_level = (
            level if level in {"info", "success", "warning", "error"} else "info"
        )
        clean_message = str(message or "").rstrip("\r\n")
        with self._lock:
            self._sequence += 1
            item = {
                "seq": self._sequence,
                "timestamp": time.time(),
                "source": source,
                "level": normalized_level,
                "message": clean_message,
            }
            self._items.append(item)
        logger_level = (
            logging.ERROR
            if normalized_level == "error"
            else (logging.WARNING if normalized_level == "warning" else logging.INFO)
        )
        self._logger.log(logger_level, "[%s] %s", source, clean_message)
        return item

    def snapshot(self, since: int = 0) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            items = list(self._items)
            if since > 0:
                items = [item for item in items if int(item["seq"]) > since]
            return items, self._sequence

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


@dataclass
class ServiceRuntime:
    spec: ServiceSpec
    process: subprocess.Popen[str] | None = None
    status: str = "stopped"
    detail: str = "等待启动"
    started_at: float | None = None
    exit_code: int | None = None
    expected_stop: bool = False
    generation: int = 0
    health_failures: int = 0
    crash_times: deque[float] = field(default_factory=lambda: deque(maxlen=8))
    extra: dict[str, Any] = field(default_factory=dict)


def collect_stoppable_processes(root_pid: int) -> tuple[list[psutil.Process], int]:
    """Collect a managed process tree while preserving reusable browsers."""
    try:
        root = psutil.Process(root_pid)
        descendants = root.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return [], 0

    targets: list[psutil.Process] = []
    preserved_count = 0
    for process in reversed(descendants):
        try:
            process_name = process.name().casefold()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            process_name = ""
        if process_name in _PRESERVED_BROWSER_PROCESSES:
            preserved_count += 1
            continue
        targets.append(process)
    targets.append(root)
    return targets, preserved_count


_SECRET_ARG_HINTS = (
    "key",
    "token",
    "secret",
    "password",
    "passwd",
    "pwd",
    "credential",
    "auth",
)


def redact_cmdline(parts: Iterable[str]) -> str:
    """Render a command line for logs while masking secret-like arguments."""
    rendered: list[str] = []
    mask_next = False
    for raw in list(parts or []):
        arg = str(raw)
        if mask_next:
            rendered.append("***")
            mask_next = False
            continue
        if "=" in arg:
            name, _, _value = arg.partition("=")
            if any(hint in name.casefold() for hint in _SECRET_ARG_HINTS):
                rendered.append(f"{name}=***")
                continue
        if arg.startswith("-") and any(
            hint in arg.casefold() for hint in _SECRET_ARG_HINTS
        ):
            rendered.append(arg)
            mask_next = True
            continue
        rendered.append(arg)
    return " ".join(rendered)


def resolve_service_python() -> str:
    """Return the interpreter that the managed services must run under.

    F11b: when the project venv exists we *require* it and never fall back to
    the system interpreter (``C:\\Program Files\\Python312\\...``).  On
    Windows the stdlib venv ``pythonw.exe`` is a redirector stub that launches
    the base interpreter in a child process, so this returns the venv path and
    :meth:`ServiceSupervisor._service_processes` groups the stub with its child
    as one logical instance.  On non-Windows (dev/CI) the current interpreter
    is used.
    """
    venv_dir = PROJECT_ROOT / ".venv"
    if os.name == "nt":
        for name in ("pythonw.exe", "python.exe"):
            candidate = venv_dir / "Scripts" / name
            if candidate.is_file():
                return str(candidate)
        raise RuntimeError(
            f"虚拟环境解释器缺失：{venv_dir / 'Scripts' / 'pythonw.exe'}；"
            "拒绝回退到系统 Python 另起一套"
        )
    candidate = venv_dir / "Scripts" / "python.exe"
    if candidate.is_file():
        return str(candidate)
    return sys.executable


def _is_path_under(candidate: object, root_resolved: Path) -> bool:
    try:
        path = Path(str(candidate))
    except (TypeError, ValueError):
        return False
    if len(str(path)) < 2:
        return False
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        resolved = path
    if resolved == root_resolved:
        return True
    try:
        return root_resolved in resolved.parents
    except (TypeError, ValueError):
        return False


def match_service_cmdline(
    cmdline: Iterable[str],
    script_name: str,
    project_root: Path,
    cwd: str | os.PathLike[str] | None = None,
) -> bool:
    """Return True only for this project's own service process.

    Matches a python process whose command line references ``script_name`` AND
    is tied to ``project_root`` (path under root in argv or cwd equals root).
    This avoids touching the WeChat client, Chrome, the desktop, or unrelated
    python processes.
    """
    parts = [str(part) for part in (cmdline or [])]
    if not parts:
        return False
    joined = " ".join(parts).casefold()
    if "mabobot_launcher" in joined:
        return False
    target = str(script_name).casefold()
    if not any(Path(part).name.casefold() == target for part in parts):
        return False
    try:
        root_resolved = Path(project_root).resolve()
    except (OSError, RuntimeError):
        root_resolved = Path(project_root)
    if any(_is_path_under(part, root_resolved) for part in parts):
        return True
    if cwd is not None and _is_path_under(str(cwd), root_resolved):
        return True
    return False


def plan_duplicate_cleanup(
    processes: Iterable[psutil.Process], keep_pid: int | None = None
) -> tuple[psutil.Process | None, list[psutil.Process]]:
    """Decide which duplicate process to keep (prefer ``keep_pid``, else newest)."""
    process_list = list(processes)
    if not process_list:
        return None, []

    def _create_time(process: psutil.Process) -> float:
        try:
            return float(process.create_time())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 0.0

    keep: psutil.Process | None = None
    if keep_pid is not None:
        for process in process_list:
            try:
                if int(process.pid) == int(keep_pid):
                    keep = process
                    break
            except (TypeError, ValueError):
                continue
    if keep is None:
        keep = max(process_list, key=_create_time)

    kill: list[psutil.Process] = []
    keep_pid_value: int | None = None
    if keep is not None:
        try:
            keep_pid_value = int(keep.pid)
        except (TypeError, ValueError):
            keep_pid_value = None
    for process in process_list:
        if process is keep:
            continue
        try:
            if keep_pid_value is not None and int(process.pid) == keep_pid_value:
                continue
        except (TypeError, ValueError):
            pass
        kill.append(process)
    return keep, kill


def port_listener(port: int) -> dict[str, Any] | None:
    """Best-effort lookup of the process listening on ``port`` (read-only)."""
    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError, RuntimeError):
        return None
    for connection in connections:
        if connection.status != psutil.CONN_LISTEN or not connection.laddr:
            continue
        try:
            if int(connection.laddr.port) != int(port):
                continue
        except (TypeError, ValueError):
            continue
        pid = connection.pid
        name = ""
        if pid:
            try:
                name = psutil.Process(pid).name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                name = ""
        return {"pid": pid, "name": name}
    return None


class ServiceSupervisor:
    def __init__(
        self, project_root: Path = PROJECT_ROOT, specs: Iterable[ServiceSpec] = SERVICES
    ):
        self.project_root = Path(project_root)
        self.logs = LogBuffer()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._shutting_down = False
        self._runtimes = {spec.key: ServiceRuntime(spec=spec) for spec in specs}
        self._monitor_thread: threading.Thread | None = None

    def start_monitoring(self) -> None:
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._stop_event.clear()
        self._monitor_thread = threading.Thread(
            target=self._health_loop,
            name="mabobot-health-monitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def _port_for(self, spec: ServiceSpec) -> int:
        try:
            return int(str(os.getenv(spec.port_env) or spec.default_port).strip())
        except ValueError:
            return spec.default_port

    def _health_url(self, spec: ServiceSpec) -> str:
        return f"http://127.0.0.1:{self._port_for(spec)}{spec.health_path}"

    # --- F11: single-instance and port ownership guards ---

    def _service_processes(self, spec: ServiceSpec) -> list[psutil.Process]:
        """Return one root ``psutil.Process`` per logical service instance.

        F11b: on Windows a venv ``pythonw.exe`` is a redirector stub that
        launches the base interpreter as a child, so a single logical service
        appears as two matching processes.  We collapse any matched process
        whose parent is also a matched process into its parent, otherwise the
        duplicate-cleanup code would kill the stub and (via its job object)
        the real service it is running.
        """
        found: list[psutil.Process] = []
        for process in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline = process.info.get("cmdline") or []
                try:
                    cwd = process.cwd()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    cwd = None
                if match_service_cmdline(
                    cmdline, spec.script, self.project_root, cwd
                ):
                    found.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        matched_pids = {int(process.pid) for process in found}
        roots: list[psutil.Process] = []
        for process in found:
            try:
                parent_pid = int(process.ppid())
            except (
                psutil.NoSuchProcess,
                psutil.AccessDenied,
                psutil.ZombieProcess,
                TypeError,
                ValueError,
            ):
                parent_pid = 0
            if parent_pid and parent_pid in matched_pids:
                continue
            roots.append(process)
        return roots

    @staticmethod
    def _describe_process(process: psutil.Process) -> str:
        pid = getattr(process, "pid", None)
        try:
            started = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(process.create_time())
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError):
            started = "unknown"
        try:
            command = redact_cmdline(process.cmdline())
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            command = "<unavailable>"
        return f"pid={pid} started={started} cmd={command}"

    def _is_stack_service_pid(self, spec: ServiceSpec, pid: int) -> bool:
        try:
            process = psutil.Process(int(pid))
            try:
                cwd = process.cwd()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                cwd = None
            return match_service_cmdline(
                process.cmdline(), spec.script, self.project_root, cwd
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, TypeError):
            return False

    def _reap_duplicate_service_processes(
        self,
        spec: ServiceSpec,
        *,
        keep_pid: int | None = None,
        reason: str = "startup",
    ) -> list[str]:
        """F11.2: keep at most one instance of ``spec.script``; terminate the rest."""
        try:
            processes = self._service_processes(spec)
        except Exception as exc:  # psutil failures must never block startup
            self.logs.add("系统", f"扫描 {spec.label} 重复实例失败：{exc}", "warning")
            return []
        if len(processes) <= 1:
            return []

        keep, kill = plan_duplicate_cleanup(processes, keep_pid=keep_pid)
        if not kill:
            return []

        keep_label = getattr(keep, "pid", None) if keep is not None else None
        self.logs.add(
            "系统",
            f"发现 {len(processes)} 个 {spec.script} 实例（reason={reason}），"
            f"保留 pid={keep_label}，清理 {len(kill)} 个",
            "warning",
        )
        summaries = [self._describe_process(process) for process in kill]
        for process in kill:
            try:
                process.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        _gone, alive = psutil.wait_procs(kill, timeout=3.0)
        for process in alive:
            try:
                process.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if alive:
            psutil.wait_procs(alive, timeout=2.0)
        for summary in summaries:
            self.logs.add("系统", f"已清理重复实例：{summary}", "warning")
        return summaries

    def _preflight_port(self, spec: ServiceSpec) -> tuple[bool, str]:
        """F11.3: check who owns the service port before binding."""
        port = self._port_for(spec)
        owner = port_listener(port)
        if owner is None:
            return True, ""

        pid = owner.get("pid")
        if pid and self._is_stack_service_pid(spec, int(pid)):
            self.logs.add(
                "系统",
                f"{spec.label} 端口 {port} 被本栈重复实例占用（pid={pid}），"
                "正在清理后重试",
                "warning",
            )
            self._reap_duplicate_service_processes(
                spec, keep_pid=None, reason="port_reclaim"
            )
            owner = port_listener(port)
            if owner is None:
                return True, ""
            pid = owner.get("pid")

        name = owner.get("name") or "unknown"
        detail = f"端口 {port} 被非本栈进程占用（pid={pid} name={name}）"
        self.logs.add(
            "系统", f"{detail}，{spec.label} 暂不启动以避免 crash-loop", "warning"
        )
        return False, detail

    def _probe(self, spec: ServiceSpec) -> tuple[bool, dict[str, Any]]:
        try:
            request = urllib.request.Request(
                self._health_url(spec),
                headers={"User-Agent": "Mabobot-Launcher/3"},
            )
            with urllib.request.urlopen(request, timeout=0.8) as response:
                if not 200 <= int(response.status) < 300:
                    return False, {}
                content_type = str(response.headers.get("Content-Type") or "")
                if "json" not in content_type.casefold():
                    return True, {}
                try:
                    payload = json.loads(
                        response.read().decode("utf-8", errors="replace")
                    )
                except (ValueError, UnicodeDecodeError):
                    payload = {}
                return True, payload if isinstance(payload, dict) else {}
        except (OSError, urllib.error.URLError, TimeoutError):
            return False, {}

    def _creation_flags(self) -> int:
        if os.name != "nt":
            return 0
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) | int(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )

    def start_service(self, key: str, *, automatic: bool = False) -> dict[str, Any]:
        with self._lock:
            runtime = self._require_runtime(key)
            if self._shutting_down:
                return self._snapshot_runtime(runtime)
            if runtime.process and runtime.process.poll() is None:
                return self._snapshot_runtime(runtime)

            script = self.project_root / runtime.spec.script
            if not script.is_file():
                runtime.status = "error"
                runtime.detail = f"缺少 {runtime.spec.script}"
                self.logs.add("系统", runtime.detail, "error")
                return self._snapshot_runtime(runtime)

            # F11.2: never start a new instance while a stale duplicate exists.
            self._reap_duplicate_service_processes(
                runtime.spec, keep_pid=None, reason="start"
            )
            # F11.3: refuse to fight a non-stack process for the port.
            port_ok, port_detail = self._preflight_port(runtime.spec)
            if not port_ok:
                runtime.status = "error"
                runtime.detail = port_detail
                runtime.expected_stop = True
                return self._snapshot_runtime(runtime)

            if not automatic:
                runtime.crash_times.clear()
            runtime.generation += 1
            generation = runtime.generation
            runtime.expected_stop = False
            runtime.exit_code = None
            runtime.health_failures = 0
            runtime.extra = {}
            runtime.status = "starting"
            runtime.detail = "正在等待健康检查"

            environment = os.environ.copy()
            environment["PYTHONIOENCODING"] = "utf-8"
            environment["PYTHONUNBUFFERED"] = "1"
            try:
                service_python = resolve_service_python()
            except RuntimeError as exc:
                runtime.status = "error"
                runtime.detail = str(exc)
                self.logs.add("系统", str(exc), "error")
                return self._snapshot_runtime(runtime)
            try:
                process = subprocess.Popen(
                    [service_python, "-u", str(script)],
                    cwd=str(self.project_root),
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=self._creation_flags(),
                )
            except OSError as exc:
                runtime.status = "error"
                runtime.detail = f"启动失败：{exc}"
                self.logs.add(runtime.spec.label, runtime.detail, "error")
                return self._snapshot_runtime(runtime)

            runtime.process = process
            runtime.started_at = time.time()
            self.logs.add("系统", f"正在启动 {runtime.spec.label} · PID {process.pid}")
            threading.Thread(
                target=self._read_output,
                args=(runtime.spec.key, process),
                name=f"mabobot-{runtime.spec.key}-output",
                daemon=True,
            ).start()
            threading.Thread(
                target=self._watch_process,
                args=(runtime.spec.key, process, generation),
                name=f"mabobot-{runtime.spec.key}-watch",
                daemon=True,
            ).start()
            return self._snapshot_runtime(runtime)

    def _read_output(self, key: str, process: subprocess.Popen[str]) -> None:
        stream = process.stdout
        if stream is None:
            return
        try:
            for line in stream:
                message = line.rstrip("\r\n")
                if message:
                    self.logs.add(
                        self._runtimes[key].spec.label,
                        message,
                        infer_output_level(message),
                    )
        except (OSError, ValueError) as exc:
            if process.poll() is None:
                self.logs.add(
                    self._runtimes[key].spec.label, f"日志读取中断：{exc}", "warning"
                )
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _watch_process(
        self, key: str, process: subprocess.Popen[str], generation: int
    ) -> None:
        exit_code = process.wait()
        schedule_restart = False
        restart_delay = 0
        with self._lock:
            runtime = self._runtimes[key]
            if runtime.process is not process:
                return
            runtime.process = None
            runtime.exit_code = exit_code
            runtime.extra = {}
            if runtime.expected_stop or self._shutting_down:
                runtime.status = "stopped"
                runtime.detail = "已停止"
                self.logs.add("系统", f"{runtime.spec.label} 已停止", "success")
                return

            runtime.status = "error"
            runtime.detail = f"进程意外退出（代码 {exit_code}）"
            now = time.monotonic()
            runtime.crash_times.append(now)
            recent_crashes = [
                stamp for stamp in runtime.crash_times if now - stamp <= 300
            ]
            self.logs.add(runtime.spec.label, runtime.detail, "error")
            if len(recent_crashes) <= 5:
                restart_delay = min(30, 2 ** max(1, len(recent_crashes)))
                runtime.detail += f"，{restart_delay} 秒后自动重试"
                schedule_restart = True
            else:
                runtime.detail += "，频繁失败后已暂停自动重试"
                self.logs.add(
                    "系统",
                    f"{runtime.spec.label} 5 分钟内多次退出，已暂停自动重试",
                    "warning",
                )

        if schedule_restart:
            timer = threading.Timer(
                restart_delay,
                self._restart_after_crash,
                args=(key, generation),
            )
            timer.daemon = True
            timer.start()

    def _restart_after_crash(self, key: str, generation: int) -> None:
        with self._lock:
            runtime = self._runtimes[key]
            if (
                self._shutting_down
                or runtime.generation != generation
                or runtime.process is not None
            ):
                return
        self.logs.add(
            "系统", f"正在自动恢复 {self._runtimes[key].spec.label}", "warning"
        )
        self.start_service(key, automatic=True)

    def stop_service(self, key: str) -> dict[str, Any]:
        with self._lock:
            runtime = self._require_runtime(key)
            process = runtime.process
            runtime.generation += 1
            runtime.expected_stop = True
            if not process or process.poll() is not None:
                runtime.process = None
                runtime.status = "stopped"
                runtime.detail = "已停止"
                return self._snapshot_runtime(runtime)
            runtime.status = "stopping"
            runtime.detail = "正在停止进程树"

        targets, preserved_count = collect_stoppable_processes(process.pid)
        if preserved_count:
            self.logs.add(
                "系统",
                f"停止 {runtime.spec.label} 时保留 {preserved_count} 个浏览器进程",
            )
        if not targets:
            try:
                process.terminate()
            except OSError:
                pass
        for target in targets:
            try:
                target.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        _gone, alive = psutil.wait_procs(targets, timeout=2.5)
        for target in alive:
            try:
                target.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass

        # F11.1: confirm no matching instance survives before a restart.
        self._reap_duplicate_service_processes(
            runtime.spec, keep_pid=None, reason="stop_verify"
        )

        with self._lock:
            if runtime.process is process:
                runtime.process = None
                runtime.status = "stopped"
                runtime.detail = "已停止"
                runtime.extra = {}
            return self._snapshot_runtime(runtime)

    def restart_service(self, key: str) -> dict[str, Any]:
        self.stop_service(key)
        return self.start_service(key)

    def start_all(self) -> list[dict[str, Any]]:
        return [self.start_service(key) for key in tuple(self._runtimes)]

    def stop_all(self) -> list[dict[str, Any]]:
        return [self.stop_service(key) for key in tuple(self._runtimes)]

    def restart_all(self) -> list[dict[str, Any]]:
        self.stop_all()
        return self.start_all()

    def _health_loop(self) -> None:
        while not self._stop_event.wait(1.0):
            with self._lock:
                active = [
                    (key, runtime.spec, runtime.process, runtime.status)
                    for key, runtime in self._runtimes.items()
                    if runtime.process and runtime.process.poll() is None
                ]
            for key, spec, process, previous_status in active:
                healthy, payload = self._probe(spec)
                with self._lock:
                    runtime = self._runtimes[key]
                    if runtime.process is not process:
                        continue
                    if healthy:
                        runtime.health_failures = 0
                        runtime.extra = self._health_extra(key, payload)
                        if runtime.status != "running":
                            runtime.status = "running"
                            runtime.detail = "健康检查通过"
                            self.logs.add(
                                "系统", f"{runtime.spec.label} 已就绪", "success"
                            )
                    else:
                        runtime.health_failures += 1
                        elapsed = time.time() - (runtime.started_at or time.time())
                        if (
                            elapsed >= runtime.spec.startup_grace_seconds
                            and runtime.health_failures >= 4
                        ):
                            runtime.status = "degraded"
                            runtime.detail = "进程仍在运行，但健康检查未通过"
                            if previous_status != "degraded":
                                self.logs.add(
                                    runtime.spec.label, runtime.detail, "warning"
                                )

    @staticmethod
    def _health_extra(key: str, payload: dict[str, Any]) -> dict[str, Any]:
        if key != "bot":
            return {}
        return {
            "wechat_connected": bool(payload.get("wechat_connected")),
            "wechat_online": bool(payload.get("wechat_online")),
        }

    def ingest_external_log(
        self, source: str, message: str, level: str = "info"
    ) -> None:
        self.logs.add(source, message, level)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._snapshot_runtime(runtime) for runtime in self._runtimes.values()
            ]

    def _snapshot_runtime(self, runtime: ServiceRuntime) -> dict[str, Any]:
        process = runtime.process
        is_alive = bool(process and process.poll() is None)
        uptime = (
            max(0, int(time.time() - runtime.started_at))
            if is_alive and runtime.started_at
            else 0
        )
        return {
            "key": runtime.spec.key,
            "label": runtime.spec.label,
            "script": runtime.spec.script,
            "status": runtime.status,
            "status_label": _STATUS_LABELS.get(runtime.status, runtime.status),
            "detail": runtime.detail,
            "pid": process.pid if is_alive and process else None,
            "uptime_seconds": uptime,
            "address": f"127.0.0.1:{self._port_for(runtime.spec)}",
            "url": self._health_url(runtime.spec),
            "extra": dict(runtime.extra),
        }

    def _require_runtime(self, key: str) -> ServiceRuntime:
        try:
            return self._runtimes[str(key).strip().casefold()]
        except KeyError as exc:
            raise ValueError(f"Unknown Mabobot service: {key}") from exc

    def shutdown(self) -> None:
        with self._lock:
            self._shutting_down = True
        self._stop_event.set()
        self.stop_all()
