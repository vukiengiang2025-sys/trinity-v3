"""Common adapter contract for BRAIN, LOGIC and MUSCLE."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse
import urllib.error
import urllib.request

from core.config import AppConfig
from core.utils import ProcessResult, run_process, utc_now, which


class AdapterStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    INSTALLED_BUT_UNVERIFIED = "INSTALLED_BUT_UNVERIFIED"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


class ResultStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"
    DENIED = "DENIED"
    UNVERIFIED = "UNVERIFIED"
    DRY_RUN = "DRY_RUN"


@dataclass
class AdapterHealth:
    name: str
    role: str
    status: AdapterStatus
    version: str = ""
    executable: str = ""
    detail: str = ""
    capabilities: List[str] = field(default_factory=list)
    memory_path: Optional[str] = None
    http_url: Optional[str] = None
    checked_at: str = field(default_factory=utc_now)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "status": self.status.value,
            "version": self.version,
            "executable": self.executable,
            "detail": self.detail,
            "capabilities": list(self.capabilities),
            "memory_path": self.memory_path,
            "http_url": self.http_url,
            "checked_at": self.checked_at,
        }


@dataclass
class AdapterRequest:
    task_id: str
    prompt: str
    action: str
    workspace: Path
    timeout: Optional[int] = None
    dry_run: bool = False
    approved: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AdapterResult:
    adapter: str
    status: ResultStatus
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    duration_seconds: float = 0.0
    started_at: str = field(default_factory=utc_now)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == ResultStatus.SUCCESS

    def as_dict(self) -> dict:
        return {
            "adapter": self.adapter,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
            "duration_seconds": round(self.duration_seconds, 3),
            "started_at": self.started_at,
            "metadata": dict(self.metadata),
        }


class BaseAdapter(ABC):
    name = "base"
    role = "SUPPORT"

    def __init__(self, config: AppConfig, guard: Optional[Any] = None):
        self.config = config
        self.guard = guard
        self._health: Optional[AdapterHealth] = None

    # ------------------------------------------------------------------
    @abstractmethod
    def discover(self, force: bool = False) -> AdapterHealth:
        """Inspect the real installation without starting or changing it."""

    @abstractmethod
    def execute(self, request: AdapterRequest) -> AdapterResult:
        """Execute one task, always with a timeout and argv-only subprocess."""

    # ------------------------------------------------------------------
    def health(self, force: bool = False) -> AdapterHealth:
        return self.discover(force=force)

    def is_available(self) -> bool:
        return self.discover().status == AdapterStatus.AVAILABLE

    def settings(self) -> Dict[str, Any]:
        return self.config.adapter(self.name)

    def binary(self) -> Optional[str]:
        configured = str(self.settings().get("binary", self.name)).strip()
        return which(configured) if configured else None

    def timeout(self, request: Optional[AdapterRequest] = None) -> int:
        configured = int(self.settings().get("timeout_seconds", self.config.timeout_seconds))
        requested = request.timeout if request and request.timeout else configured
        return max(1, min(int(requested), self.config.max_timeout_seconds))

    @staticmethod
    def _probe(argv: List[str], timeout: float = 10.0, cwd: Optional[Path] = None) -> ProcessResult:
        return run_process(argv, timeout=timeout, cwd=cwd, max_output=50_000)

    @staticmethod
    def _version(text: str) -> str:
        match = re.search(r"(\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9.\-]+)?)", text or "")
        return match.group(1) if match else ""

    @staticmethod
    def _is_loopback(url: str) -> bool:
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            return parsed.scheme in ("http", "https") and host in ("127.0.0.1", "localhost", "::1")
        except ValueError:
            return False

    def _http_health(self, base_url: str, path: str, timeout: float = 2.0) -> Dict[str, Any]:
        """Unauthenticated health probe of a local server; never authenticates."""

        if not self._is_loopback(base_url):
            return {"reachable": False, "status_code": None, "detail": "URL không phải loopback"}
        url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                body = response.read(512).decode("utf-8", errors="replace")
                return {"reachable": True, "status_code": response.status, "detail": body[:200]}
        except urllib.error.HTTPError as exc:
            return {"reachable": False, "status_code": exc.code, "detail": f"HTTP {exc.code}"}
        except Exception as exc:  # network errors are expected when no server runs
            return {"reachable": False, "status_code": None, "detail": str(exc)[:200]}

    def _disabled_health(self) -> Optional[AdapterHealth]:
        if not self.settings().get("enabled", True):
            return AdapterHealth(
                name=self.name,
                role=self.role,
                status=AdapterStatus.UNAVAILABLE,
                detail=f"adapter bị tắt trong profile {self.config.profile}",
            )
        return None

    @staticmethod
    def _result_from_process(
        adapter: str,
        process: ProcessResult,
        started_at: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AdapterResult:
        if process.timed_out:
            status = ResultStatus.TIMEOUT
            error = f"quá thời gian cho phép ({process.duration_seconds:.1f}s)"
        elif process.error:
            status = ResultStatus.UNAVAILABLE
            error = process.error
        elif process.exit_code == 0:
            status = ResultStatus.SUCCESS
            error = ""
        else:
            status = ResultStatus.FAILED
            error = (process.stderr or process.stdout or "lệnh thất bại").strip()[:2000]
        return AdapterResult(
            adapter=adapter,
            status=status,
            exit_code=process.exit_code,
            stdout=process.stdout,
            stderr=process.stderr,
            error=error,
            duration_seconds=process.duration_seconds,
            started_at=started_at or utc_now(),
            metadata=metadata or {},
        )


