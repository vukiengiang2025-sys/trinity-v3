"""Shared test helpers (no pytest dependency)."""

from __future__ import annotations

import stat
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import AppConfig, _deep_merge, load_config  # noqa: E402
from core.resource import ResourceDecision, ResourceSnapshot  # noqa: E402
from core.utils import ResourceError  # noqa: E402
from adapters.base import (  # noqa: E402
    AdapterHealth,
    AdapterRequest,
    AdapterResult,
    AdapterStatus,
    BaseAdapter,
    ResultStatus,
)


def make_config(
    tmp_path: Path,
    profile: str = "linux",
    workspace: Optional[Path] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> AppConfig:
    config = load_config(profile, config_dir=PROJECT_ROOT / "config", project_root=PROJECT_ROOT)
    ws = Path(workspace or (tmp_path / "workspace"))
    ws.mkdir(parents=True, exist_ok=True)
    config.data = _deep_merge(
        config.data,
        {
            "workspace": {"root": str(ws), "allowed_roots": ["."]},
            "storage": {
                "db_path": str(tmp_path / "trinity.db"),
                "log_dir": str(tmp_path / "logs"),
                "max_result_chars": 20000,
            },
            "resources": {"lock_path": str(tmp_path / "logs" / ".trinity.lock")},
        },
    )
    if overrides:
        config.data = _deep_merge(config.data, overrides)
    return config


def make_fake_executable(tmp_path: Path, name: str = "fake-tool", mode: str = "ok") -> Path:
    path = Path(tmp_path) / name
    body = f'''#!/usr/bin/env python3
import sys, time
args = sys.argv[1:]
mode = {mode!r}
if "--version" in args:
    print("9.9.9")
    sys.exit(0)
if "--help" in args:
    print("usage: fake-tool --help\\ncommands: memory skill exec command run")
    sys.exit(0)
if args[:2] == ["memory", "search"]:
    print("memory result")
    sys.exit(0)
if args[:2] == ["memory", "store"]:
    print("memory stored")
    sys.exit(0)
if mode == "timeout":
    time.sleep(5)
    sys.exit(0)
if mode == "permission":
    print("permission required: approval needed before tool use", file=sys.stderr)
    sys.exit(2)
if mode == "fail":
    print("intentional failure", file=sys.stderr)
    sys.exit(2)
if args and args[0] == "exec":
    print("executed: " + " ".join(args[1:]))
    sys.exit(0)
if "run" in args:
    if mode == "json":
        print('{{"ok": true, "message": "agent output"}}')
    elif mode == "invalid-json":
        print("this is not json")
    elif mode == "agent-missing":
        print('{{"type": "error", "error": {{"type": "unknown", "message": "Agent not found: trinity-readonly"}}}}')
    elif mode == "empty":
        pass
    else:
        print('{{"type": "text", "text": "agent output"}}')
    sys.exit(0)
print("fake-tool " + " ".join(args))
sys.exit(0)
'''
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class FakeAdapter(BaseAdapter):
    name = "fake"

    def __init__(
        self,
        name: str = "fake",
        status: AdapterStatus = AdapterStatus.AVAILABLE,
        result_status: ResultStatus = ResultStatus.SUCCESS,
        stdout: str = "ok",
        error: str = "",
        raise_error: bool = False,
    ):
        self.config = None
        self.guard = None
        self._health = None
        self.name = name
        self.role = "TEST"
        self._status = status
        self._result_status = result_status
        self._stdout = stdout
        self._error = error
        self._raise_error = raise_error
        self.calls: List[AdapterRequest] = []

    def with_config(self, config: AppConfig) -> "FakeAdapter":
        self.config = config
        return self

    def discover(self, force: bool = False) -> AdapterHealth:
        return AdapterHealth(name=self.name, role=self.role, status=self._status, detail="fake adapter")

    def execute(self, request: AdapterRequest) -> AdapterResult:
        self.calls.append(request)
        if self._raise_error:
            raise RuntimeError("fake adapter exploded")
        return AdapterResult(
            adapter=self.name,
            status=self._result_status,
            exit_code=0 if self._result_status == ResultStatus.SUCCESS else 1,
            stdout=self._stdout,
            error=self._error,
        )


class FakeResource:
    def __init__(self, allowed: bool = True, reason: str = "ok", lock_error: bool = False):
        self.allowed = allowed
        self.reason = reason
        self.lock_error = lock_error
        self.preflight_calls = 0

    def preflight(self, dry_run: bool = False) -> ResourceDecision:
        self.preflight_calls += 1
        return ResourceDecision(
            allowed=self.allowed if not dry_run else True,
            reason=self.reason,
            warnings=[] if self.allowed else [self.reason],
            snapshot=ResourceSnapshot(1024, 512, True, "test"),
            disk_free_mb=1000,
            dry_run=dry_run,
        )

    def acquire_lock(self, timeout: float = 0.0):
        if self.lock_error:
            raise ResourceError("lock busy")
        return NullLock()

    def clamp_timeout(self, requested=None) -> int:
        return int(requested) if requested else 60


class NullLock:
    def release(self) -> None:
        pass
