"""MUSCLE adapter: OpenClaw.

OpenClaw commands are never generated from free text and never run through a
shell.  The caller must supply an explicit argv list; the guard and the
OpenClaw-specific allowlist must both approve it.  The gateway is only probed
when explicitly enabled, and is never started by Trinity.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List, Optional

from core.config import AppConfig
from core.utils import utc_now, which
from .base import (
    AdapterHealth,
    AdapterRequest,
    AdapterResult,
    AdapterStatus,
    BaseAdapter,
    ResultStatus,
    run_process,
)


class OpenClawAdapter(BaseAdapter):
    name = "openclaw"
    role = "MUSCLE"

    def __init__(self, config: AppConfig, guard: Any = None):
        super().__init__(config, guard)

    def discover(self, force: bool = False) -> AdapterHealth:
        if self._health is not None and not force:
            return self._health
        disabled = self._disabled_health()
        if disabled:
            self._health = disabled
            return disabled

        settings = self.settings()
        binary = self.binary()
        gateway_enabled = bool(settings.get("gateway_enabled", False))
        gateway_url = str(settings.get("gateway_url", "")) if gateway_enabled else ""
        gateway_result = None
        if gateway_enabled and gateway_url:
            gateway_result = self._http_health(
                gateway_url, str(settings.get("gateway_health_path", "/health"))
            )
        capabilities: List[str] = []
        if gateway_result and gateway_result.get("reachable"):
            capabilities.append("gateway_health")

        if not binary:
            status = AdapterStatus.INSTALLED_BUT_UNVERIFIED if capabilities else AdapterStatus.UNAVAILABLE
            detail = "không tìm thấy OpenClaw CLI"
            if capabilities:
                detail += "; gateway sống nhưng endpoint thực thi chưa được xác minh"
            self._health = AdapterHealth(
                name=self.name,
                role=self.role,
                status=status,
                detail=detail,
                capabilities=capabilities,
                http_url=gateway_url or None,
            )
            return self._health

        version_proc = self._probe([binary, "--version"], timeout=10)
        version = self._version(version_proc.stdout or version_proc.stderr) if version_proc.ok else ""
        help_proc = self._probe([binary, "--help"], timeout=10)
        help_text = (help_proc.stdout + help_proc.stderr).lower()
        mentions_exec = any(token in help_text for token in ("exec", "command", "run"))
        prefix = self._invoke_prefix()
        if version_proc.ok and mentions_exec and prefix:
            status = AdapterStatus.AVAILABLE
            detail = "CLI và invoke_prefix đã được cấu hình"
            capabilities.extend(["automation_task", "run_command", "cli_exec"])
        elif version_proc.ok:
            status = AdapterStatus.INSTALLED_BUT_UNVERIFIED
            detail = "CLI phản hồi nhưng chưa cấu hình invoke_prefix đã xác minh"
        else:
            status = AdapterStatus.INSTALLED_BUT_UNVERIFIED
            detail = "tìm thấy executable nhưng --version thất bại"

        self._health = AdapterHealth(
            name=self.name,
            role=self.role,
            status=status,
            version=version,
            executable=binary,
            detail=detail,
            capabilities=capabilities,
            http_url=gateway_url or None,
        )
        return self._health

    def _invoke_prefix(self) -> Optional[List[str]]:
        value = self.settings().get("invoke_prefix")
        if not isinstance(value, list) or not value:
            return None
        if not all(isinstance(item, str) and item for item in value):
            return None
        return list(value)

    def execute(self, request: AdapterRequest) -> AdapterResult:
        started = utc_now()
        health = self.discover()
        if health.status != AdapterStatus.AVAILABLE:
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.UNAVAILABLE,
                error=health.detail or "OpenClaw không khả dụng",
                started_at=started,
            )
        if request.action not in ("automation_task", "run_command"):
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.UNVERIFIED,
                error=f"action không được hỗ trợ: {request.action}",
                started_at=started,
            )

        raw_argv = request.metadata.get("argv")
        if not isinstance(raw_argv, list) or not raw_argv:
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.UNVERIFIED,
                error="Trinity không tự sinh shell command từ văn bản; cần truyền argv đã kiểm tra",
                started_at=started,
            )
        if not all(isinstance(item, str) and item for item in raw_argv):
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.DENIED,
                error="argv phải là danh sách chuỗi không rỗng",
                started_at=started,
            )
        if self.guard is None:
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.DENIED,
                error="không có Safety Guard; từ chối thực thi (fail closed)",
                started_at=started,
            )

        decision = self.guard.check_command(raw_argv)
        if not decision.allowed:
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.DENIED,
                error=f"Guard từ chối: {decision.reason}",
                started_at=started,
                metadata={"risk": decision.risk},
            )
        if decision.requires_approval and not request.approved:
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.DENIED,
                error=f"cần phê duyệt cho lệnh rủi ro {decision.risk}",
                started_at=started,
                metadata={"risk": decision.risk},
            )

        allowed_commands = self.settings().get("command_allowlist", [])
        if not isinstance(allowed_commands, list) or raw_argv[0] not in allowed_commands:
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.DENIED,
                error=f"lệnh {raw_argv[0]!r} không nằm trong OpenClaw command_allowlist",
                started_at=started,
            )

        prefix = self._invoke_prefix()
        if not prefix:
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.UNVERIFIED,
                error="chưa cấu hình invoke_prefix cho OpenClaw",
                started_at=started,
            )
        configured_binary = str(self.settings().get("binary", "openclaw"))
        if prefix[0] == configured_binary:
            if not health.executable:
                return AdapterResult(
                    adapter=self.name,
                    status=ResultStatus.UNAVAILABLE,
                    error="không tìm thấy OpenClaw CLI",
                    started_at=started,
                )
            prefix = list(prefix)
            prefix[0] = health.executable
        elif not which(prefix[0]):
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.UNAVAILABLE,
                error=f"không tìm thấy executable: {prefix[0]}",
                started_at=started,
            )

        argv = prefix + list(raw_argv)
        if request.dry_run:
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.DRY_RUN,
                started_at=started,
                metadata={"argv_preview": argv, "risk": decision.risk},
            )

        workspace = Path(request.workspace)
        if not workspace.is_dir():
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.FAILED,
                error=f"workspace không tồn tại: {workspace}",
                started_at=started,
            )

        env = dict(os.environ)
        env["TRINITY_TASK_ID"] = request.task_id
        env["TRINITY_ADAPTER"] = self.name
        timeout = self.timeout(request)
        # Automation commands are additionally capped by the guard policy.
        if self.guard is not None:
            timeout = min(timeout, int(getattr(self.guard, "max_command_seconds", timeout)))
        process = run_process(argv, timeout=timeout, cwd=workspace, env=env, max_output=100_000)
        return self._result_from_process(
            self.name,
            process,
            started_at=started,
            metadata={"timeout_seconds": timeout, "risk": decision.risk},
        )
