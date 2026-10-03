"""Single-instance support for the desktop launcher."""

from __future__ import annotations

import ctypes
import os
import uuid
from pathlib import Path
from typing import IO, Self

from .constants import DATA_DIR, SHOW_SIGNAL_FILE


def _pid_alive(pid: int) -> bool:
    """True if a process with this pid currently exists."""
    if pid <= 0:
        return False
    try:
        import psutil

        return psutil.pid_exists(pid)
    except Exception:
        pass
    if os.name == "nt":
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return True  # be conservative: assume alive
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


class SingleInstance:
    # F11.1: Global\ prefix so the mutex is visible across all Windows sessions.
    MUTEX_NAME = r"Global\MabobotDesktopLauncherV3"
    ERROR_ALREADY_EXISTS = 183

    def __init__(self, lock_file: Path | None = None):
        self.lock_file = lock_file or (DATA_DIR / "launcher.lock")
        self._mutex = None
        self._file: IO[str] | None = None
        self._owns_mutex = False
        self._owns_file = False

    # ----------------------------------------------------------------- F11.2
    def _acquire_windows(self) -> bool:
        """Acquire the global mutex AND an owner-pid lock file.

        F11.2 (2026-09-26): the previous implementation used
        ``ctypes.windll.kernel32`` plus a bare ``kernel32.GetLastError()``
        call. Without ``use_last_error=True`` ctypes performs its own Win32
        calls (attribute lookup, lazy LoadLibrary, argument conversion) that
        clobber the thread's last-error value, so ``ERROR_ALREADY_EXISTS``
        was never observed and a second launcher started happily — observed
        live on 2026-09-26 20:54:46 as TWO mabobot_launcher processes plus
        TWO wx_bot.py processes started in the very same second, which then
        fought over the shared WeChat UI lock (bridge_timeout / 136 s send
        stalls / faux "wechat_offline").
        """
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = (
            ctypes.c_void_p,  # LPSECURITY_ATTRIBUTES
            ctypes.c_int,     # BOOL bInitialOwner
            ctypes.c_wchar_p, # LPCWSTR lpName
        )
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)

        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, self.MUTEX_NAME)
        last_error = ctypes.get_last_error()
        if not handle:
            raise ctypes.WinError(last_error)
        self._mutex = handle
        if last_error == self.ERROR_ALREADY_EXISTS:
            # Somebody else owns the name. Do NOT fall through to a second
            # guard: report the duplicate immediately.
            return False
        self._owns_mutex = True

        # Belt & braces: an exclusively-created owner file catches the case
        # where the mutex handle is inherited by a crash-restarted or
        # re-parented process (a stale mutex with no live launcher).
        return self._acquire_owner_file()

    def _read_owner_pid(self) -> int:
        """Return the pid recorded in the lock file, or 0 if absent/unreadable.

        F11b: a lock file that exists but is empty (or unparsable) must NOT be
        treated as ``0`` (which used to mean "stale, reclaim it"). Callers that
        need to tell "no file" apart from "empty file" use
        :meth:`_owner_file_state`.
        """
        return self._owner_file_state()[1]

    def _owner_file_state(self) -> tuple[bool, int, bool]:
        """Return ``(exists, owner_pid, empty)`` for the owner lock file."""
        try:
            raw = self.lock_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return (False, 0, False)
        except OSError:
            return (True, 0, True)
        text = raw.strip()
        if not text:
            return (True, 0, True)
        try:
            return (True, int(text), False)
        except ValueError:
            return (True, 0, True)

    def _write_pid_atomically(self, pid_bytes: bytes) -> None:
        """Create ``self.lock_file`` holding ``pid_bytes`` without an empty window.

        F11b TOCTOU fix for suspicious point B: the old code did
        ``os.open(O_CREAT|O_EXCL)`` and *then* wrote the pid, so a concurrent
        launcher could read a zero-byte file, call it stale and steal the lock.
        We now write a private temp file first and hard-link it into place; the
        link is atomic and the published file is never empty.  If hard links are
        unavailable we fall back to the exclusive-create-then-write path (the
        empty window is still neutralised by :meth:`_acquire_owner_file`).
        """
        temp = self.lock_file.with_name(
            f"{self.lock_file.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with open(temp, "wb") as handle:
                handle.write(pid_bytes)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
        except OSError:
            try:
                temp.unlink()
            except OSError:
                pass
            raise
        try:
            os.link(temp, self.lock_file)
        except FileExistsError:
            raise
        except OSError:
            fd = os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, pid_bytes)
            finally:
                os.close(fd)
        finally:
            try:
                temp.unlink()
            except OSError:
                pass

    def _release_owner_file(self) -> None:
        if self._owns_file:
            try:
                self.lock_file.unlink()
            except OSError:
                pass
            self._owns_file = False

    def _acquire_owner_file(self) -> bool:
        try:
            self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Never refuse to start because of an IO problem.
            return True

        pid_bytes = str(os.getpid()).encode("ascii")
        try:
            self._write_pid_atomically(pid_bytes)
        except FileExistsError:
            pass
        except OSError:
            # Never refuse to start because of an IO problem.
            return True
        else:
            self._owns_file = True
            return True

        # The lock file already exists. Decide without ever unlinking an
        # occupied-but-empty file (F11b suspicious point B).
        exists, owner, empty = self._owner_file_state()
        if not exists:
            # The holder released it between our link attempt and this read.
            try:
                self._write_pid_atomically(pid_bytes)
            except (FileExistsError, OSError):
                return False
            self._owns_file = True
            return True
        if empty:
            # Present but not yet populated: a live creator is mid-write.
            # Treat as occupied (fresh) instead of stealing it.
            return False
        if _pid_alive(owner):
            return False
        # Genuinely stale: the recorded owner pid no longer exists.
        try:
            self.lock_file.unlink()
        except OSError:
            return False
        try:
            self._write_pid_atomically(pid_bytes)
        except (FileExistsError, OSError):
            return False
        self._owns_file = True
        return True

    # ------------------------------------------------------------------ base
    def acquire(self) -> bool:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            return self._acquire_windows()
        return self._acquire_posix()

    def _acquire_posix(self) -> bool:
        # The owner-pid file is the source of truth on every platform so that
        # the empty-lockfile guarantee holds here too; flock adds a second,
        # kernel-backed guard against pid reuse.
        if not self._acquire_owner_file():
            return False
        try:
            self._file = self.lock_file.open("a+", encoding="utf-8")
            import fcntl

            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._release_owner_file()
            if self._file:
                self._file.close()
                self._file = None
            return False
        self._write_owner()
        return True

    def _write_owner(self) -> None:
        if not self._file:
            return
        try:
            self._file.seek(0)
            self._file.truncate()
            self._file.write(str(os.getpid()))
            self._file.flush()
        except OSError:
            pass

    def describe_owner(self) -> str:
        """Best-effort description of the current lock holder (read-only)."""
        if os.name == "nt":
            owner = self._read_owner_pid()
            return f"mutex={self.MUTEX_NAME} owner_pid={owner or 'unknown'}"
        try:
            text = self.lock_file.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        return f"pid={text or 'unknown'} lock_file={self.lock_file}"

    def request_show(self) -> None:
        SHOW_SIGNAL_FILE.write_text(str(os.getpid()), encoding="ascii")

    def release(self) -> None:
        if os.name == "nt":
            if self._mutex:
                ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(self._mutex))
                self._mutex = None
            self._owns_mutex = False
            self._release_owner_file()
            return
        if self._file:
            import fcntl

            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self._file.close()
            self._file = None
        self._release_owner_file()

    def __enter__(self) -> Self:
        if not self.acquire():
            self.request_show()
            raise RuntimeError("Mabobot launcher is already running")
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()
