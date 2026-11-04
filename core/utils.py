"""Small, dependency-free utilities shared by Trinity components.

Nothing in this module may log, persist or print credentials.  Every
free-text value that leaves the process boundary is passed through
:func:`redact` first.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Sequence

MAX_TEXT_CHARS = 200_000


class TrinityError(Exception):
    """Base class for expected, user-facing Trinity errors."""


class ConfigError(TrinityError):
    """Invalid or unreadable configuration."""


class GuardError(TrinityError):
    """A safety rule rejected an operation."""


class ResourceError(TrinityError):
    """Not enough resources, or the execution lock is busy."""


class StorageError(TrinityError):
    """SQLite or audit-log failure."""


# ---------------------------------------------------------------------------
# Time / text helpers
# ---------------------------------------------------------------------------


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp with a ``Z`` suffix."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def redact(value: object) -> str:
    """Mask common credential shapes in arbitrary text.

    The redaction is best effort: it protects against accidents, it is not a
    substitute for never putting secrets into prompts or configuration.
    """

    if value is None:
        return ""
    text = str(value)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def contains_secret(value: object) -> bool:
    """Return True when *value* still looks like it contains a credential."""

    if value is None:
        return False
    text = str(value)
    return any(pattern.search(text) for pattern, _ in _SECRET_PATTERNS)


def truncate(text: object, limit: int = 20_000, suffix: str = "\n...[đã cắt bớt]") -> str:
    """Redact and truncate text for storage or display."""

    safe = redact(text)
    if limit <= 0:
        return ""
    if len(safe) <= limit:
        return safe
    return safe[: max(0, limit - len(suffix))] + suffix


def mask_mapping(data: Mapping[str, object]) -> dict:
    """Return a deep copy with credential-looking keys masked."""

    result: dict = {}
    for key, value in data.items():
        lowered = str(key).lower()
        if any(
            token in lowered
            for token in (
                "password",
                "passwd",
                "secret",
                "token",
                "api_key",
                "apikey",
                "private_key",
                "access_key",
                "authorization",
                "credential",
            )
        ):
            result[key] = "***REDACTED***"
        elif isinstance(value, Mapping):
            result[key] = mask_mapping(value)
        elif isinstance(value, list):
            result[key] = [mask_mapping(item) if isinstance(item, Mapping) else redact(item) for item in value]
        elif isinstance(value, str):
            result[key] = redact(value)
        else:
            result[key] = value
    return result


def safe_json(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)


def which(binary: str) -> Optional[str]:
    """Resolve an executable without executing it."""

    if not binary:
        return None
    if os.sep in binary or (os.altsep and os.altsep in binary):
        path = Path(binary).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
        return None
    return shutil.which(binary)


# ---------------------------------------------------------------------------
# Subprocess execution (argv only, never shell=True)
# ---------------------------------------------------------------------------


@dataclass
class ProcessResult:
    argv: list
    exit_code: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.error


def _child_limits(timeout_seconds: int) -> None:
    """Best-effort per-child limits; POSIX only, never raises."""

    try:
        import resource

        cpu = max(1, int(timeout_seconds) + 10)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:  # pragma: no cover - platform dependent
        return


def run_process(
    argv: Sequence[object],
    timeout: float,
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    max_output: int = MAX_TEXT_CHARS,
) -> ProcessResult:
    """Run an argv list with a hard wall-clock timeout.

    ``shell=True`` is never used.  A process group is created so a timeout can
    terminate the whole tree, not just the direct child.
    """

    args = [str(part) for part in argv]
    if not args:
        raise ValueError("argv rỗng")
    started = time.monotonic()
    child_env = dict(os.environ if env is None else env)
    try:
        proc = subprocess.Popen(
            args,
            cwd=str(cwd) if cwd else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            env=child_env,
            start_new_session=True,
            preexec_fn=(lambda: _child_limits(int(max(1, timeout)))) if os.name == "posix" else None,
        )
    except FileNotFoundError:
        return ProcessResult(args, None, "", "", False, time.monotonic() - started, "không tìm thấy executable")
    except OSError as exc:
        return ProcessResult(args, None, "", "", False, time.monotonic() - started, f"không thể chạy: {exc}")

    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=max(0.1, float(timeout)))
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except Exception:
            stdout, stderr = "", ""

    duration = time.monotonic() - started
    return ProcessResult(
        argv=args,
        exit_code=proc.returncode,
        stdout=redact(stdout)[:max_output],
        stderr=redact(stderr)[:max_output],
        timed_out=timed_out,
        duration_seconds=duration,
        error="quá thời gian cho phép" if timed_out else "",
    )


# ---------------------------------------------------------------------------
# Sequential execution lock (fcntl, Linux / Termux)
# ---------------------------------------------------------------------------


class FileLock:
    """Advisory exclusive lock that prevents concurrent agent execution."""

    def __init__(self, path: Path, timeout: float = 0.0):
        self.path = Path(path)
        self.timeout = timeout
        self._handle = None
        self.error = ""

    def acquire(self) -> bool:
        self.error = ""
        if os.name != "posix":  # pragma: no cover - target is Linux/Termux
            self.error = "fcntl chỉ hỗ trợ POSIX; từ chối chạy không có khóa"
            return False
        try:
            import fcntl
        except ImportError:  # pragma: no cover
            self.error = "không import được fcntl; từ chối chạy không có khóa"
            return False

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        try:
            handle = open(self.path, "a+", encoding="utf-8")
        except OSError as exc:
            self.error = f"không mở được lock file {self.path}: {exc}"
            return False
        deadline = time.monotonic() + max(0.0, self.timeout)
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                handle.seek(0)
                handle.truncate()
                handle.write(f"pid={os.getpid()} started={utc_now()}\n")
                handle.flush()
                self._handle = handle
                return True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self.error = "đang có tác vụ khác giữ lock"
                    handle.close()
                    return False
                time.sleep(0.05)
            except OSError as exc:
                # Filesystem không hỗ trợ flock (ví dụ shared storage trên Android).
                # Fail closed thay vì chạy không có khóa tuần tự.
                self.error = f"filesystem không hỗ trợ khóa fcntl: {exc}"
                try:
                    handle.close()
                except OSError:
                    pass
                return False

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._handle.close()
        finally:
            self._handle = None

    def __enter__(self) -> "FileLock":
        if not self.acquire():
            raise ResourceError(f"đang có tác vụ khác chạy (lock: {self.path})")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


# ---------------------------------------------------------------------------
# Internal credential patterns
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = (
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|secret|password|passwd)\b"
            r"\s*[:=]\s*['\"]?([^\s,;'\"}{]{4,})"
        ),
        r"\1=***REDACTED***",
    ),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer ***REDACTED***"),
    (
        re.compile(
            r"\b(?:sk-[A-Za-z0-9_\-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|"
            r"github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9\-]{10,})\b"
        ),
        "***REDACTED***",
    ),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        "***REDACTED PRIVATE KEY***",
    ),
    (re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1***:***@"),
)
