"""LOGIC adapter: OpenCode.

The CLI is used as a subprocess with an argv list.  The HTTP API is only
probed (unauthenticated health check) and is disabled by default because the
authenticated execution endpoints have not been verified on this install.
No OpenCode configuration file is read, written or modified.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.utils import truncate, utc_now
from .base import (
    AdapterHealth,
    AdapterRequest,
    AdapterResult,
    AdapterStatus,
    BaseAdapter,
    ResultStatus,
    run_process,
)


class OpenCodeAdapter(BaseAdapter):
    name = "opencode"
    role = "LOGIC"

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
        http_enabled = bool(settings.get("http_enabled", False))
        http_url = str(settings.get("http_url", "")) if http_enabled else ""
        http_result = None
        if http_enabled and http_url:
            http_result = self._http_health(
                http_url, str(settings.get("http_health_path", "/global/health"))
            )

        if not binary:
            capabilities = ["http_health"] if http_result and http_result.get("reachable") else []
            status = AdapterStatus.INSTALLED_BUT_UNVERIFIED if capabilities else AdapterStatus.UNAVAILABLE
            detail = "không tìm thấy OpenCode CLI trong PATH"
            if http_result and http_result.get("reachable"):
                detail += "; server HTTP sống nhưng endpoint thực thi chưa được xác minh"
            self._health = AdapterHealth(
                name=self.name,
                role=self.role,
                status=status,
                detail=detail,
                capabilities=capabilities,
                http_url=http_url or None,
            )
            return self._health

        version_proc = self._probe([binary, "--version"], timeout=10)
        version = self._version(version_proc.stdout or version_proc.stderr) if version_proc.ok else ""
        help_proc = self._probe([binary, "run", "--help"], timeout=10)
        cli_verified = version_proc.ok and help_proc.ok
        capabilities = []
        if cli_verified:
            capabilities = ["code_task", "general_task", "cli_run", "json_output"]
        mode = str(settings.get("mode", "cli")).lower()
        if http_result and http_result.get("reachable"):
            capabilities.append("http_health")

        require_explicit = bool(settings.get("require_explicit_agent", True))
        configured_agent = settings.get("agent")
        write_agent = settings.get("write_agent")
        if cli_verified and mode in ("cli", "auto"):
            status = AdapterStatus.AVAILABLE
            policy = (
                "execute yêu cầu agent cụ thể đã kiểm tra permission"
                if require_explicit and not configured_agent
                else f"agent={configured_agent or 'OpenCode default'}"
            )
            if write_agent:
                policy += f"; write_agent={write_agent}"
            else:
                policy += "; write_agent chưa cấu hình"
            detail = f"CLI đã xác minh (mode={mode}); {policy}"
        elif cli_verified and mode == "http":
            # Health checks are not execution endpoints; never claim HTTP is ready.
            status = AdapterStatus.INSTALLED_BUT_UNVERIFIED
            detail = "CLI có mặt nhưng profile yêu cầu HTTP; endpoint thực thi chưa xác minh"
        else:
            status = AdapterStatus.INSTALLED_BUT_UNVERIFIED
            detail = "tìm thấy executable nhưng chưa xác minh được --version/run --help"

        self._health = AdapterHealth(
            name=self.name,
            role=self.role,
            status=status,
            version=version,
            executable=binary,
            detail=detail,
            capabilities=capabilities,
            http_url=http_url or None,
        )
        return self._health

    def execution_ready(self, action: str = ""):
        health = self.discover(force=True)
        if health.status != AdapterStatus.AVAILABLE:
            return False, health.detail or "OpenCode không khả dụng"
        settings = self.settings()
        if action == "code_write":
            if not settings.get("write_agent"):
                return (
                    False,
                    "chưa cấu hình adapters.opencode.write_agent; "
                    "controlled write bị từ chối",
                )
            return True, ""
        if bool(settings.get("require_explicit_agent", True)) and not settings.get("agent"):
            return (
                False,
                "chưa cấu hình adapters.opencode.agent; "
                "không tự dùng agent mặc định của OpenCode",
            )
        return True, ""

    def execute(self, request: AdapterRequest) -> AdapterResult:
        started = utc_now()
        settings = self.settings()
        mode = str(settings.get("mode", "cli")).lower()
        if mode == "http":
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.UNVERIFIED,
                error="HTTP execution chưa được xác minh trên phiên bản OpenCode này; đặt mode: cli",
                started_at=started,
            )

        health = self.discover()
        if health.status != AdapterStatus.AVAILABLE:
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.UNAVAILABLE,
                error=health.detail or "OpenCode không khả dụng",
                started_at=started,
            )

        if request.action not in ("code_task", "general_task", "code_write"):
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.UNVERIFIED,
                error=f"action không được hỗ trợ: {request.action}",
                started_at=started,
            )

        prompt = request.prompt or ""
        if not prompt.strip():
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.FAILED,
                error="prompt rỗng",
                started_at=started,
            )
        if len(prompt) > 100_000:
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.FAILED,
                error="prompt quá dài (giới hạn 100.000 ký tự)",
                started_at=started,
            )
        if "\x00" in prompt or prompt.startswith("-"):
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.DENIED,
                error="prompt chứa NUL hoặc bắt đầu bằng '-' (nguy cơ argument injection)",
                started_at=started,
            )

        if request.action == "code_write":
            agent = settings.get("write_agent")
            if not agent and not request.dry_run:
                return AdapterResult(
                    adapter=self.name,
                    status=ResultStatus.DENIED,
                    error=(
                        "chưa cấu hình adapters.opencode.write_agent; "
                        "controlled write bị từ chối"
                    ),
                    started_at=started,
                )
        else:
            agent = settings.get("agent")
        require_explicit = bool(settings.get("require_explicit_agent", True))
        if require_explicit and not agent and not request.dry_run:
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.DENIED,
                error=(
                    "chưa cấu hình adapters.opencode.agent; "
                    "Trinity không tự dùng agent mặc định của OpenCode. "
                    "Chỉ đặt agent sau khi kiểm tra permission của nó."
                ),
                started_at=started,
            )

        binary = self.binary()
        if not binary:
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.UNAVAILABLE,
                error="không tìm thấy OpenCode CLI",
                started_at=started,
            )

        output_format = str(settings.get("format", "json"))
        if output_format not in ("default", "json"):
            output_format = "json"
        standalone = bool(settings.get("standalone", True))
        argv = [binary, "run"]
        if standalone:
            # Avoid the user's long-running OpenCode service and its project
            # config cache; the private server exits when this run completes.
            argv.append("--standalone")
        argv.extend(
            [
                "--format",
                output_format,
                "--title",
                f"trinity-{request.task_id}",
            ]
        )
        model = settings.get("model")
        if model:
            argv.extend(["--model", str(model)])
        if agent:
            argv.extend(["--agent", str(agent)])
        argv.append(prompt)

        if request.dry_run:
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.DRY_RUN,
                stdout="",
                started_at=started,
                metadata={
                    "mode": "cli",
                    "argv_preview": [part if part != prompt else "<prompt>" for part in argv],
                },
            )

        workspace = Path(request.workspace)
        if not workspace.is_dir():
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.FAILED,
                error=f"workspace không tồn tại hoặc không phải thư mục: {workspace}",
                started_at=started,
            )

        env = dict(os.environ)
        env["TRINITY_TASK_ID"] = request.task_id
        env["TRINITY_ADAPTER"] = self.name

        timeout = self.timeout(request)
        process = run_process(argv, timeout=timeout, cwd=workspace, env=env, max_output=150_000)
        metadata = {
            "mode": "cli",
            "format": output_format,
            "timeout_seconds": timeout,
            "version": health.version,
        }
        result = self._result_from_process(self.name, process, started_at=started, metadata=metadata)
        if output_format == "json":
            events, parse_error = self._parse_json_events(result.stdout)
            result.metadata["json_valid"] = parse_error == ""
            if result.status == ResultStatus.SUCCESS and parse_error:
                result.status = ResultStatus.FAILED
                result.error = truncate(f"OpenCode JSON output không hợp lệ: {parse_error}", 2000)
            elif events:
                error_event = next(
                    (event for event in events if isinstance(event, dict) and event.get("type") == "error"),
                    None,
                )
                if error_event is not None:
                    message = self._event_error_message(error_event)
                    result.metadata["opencode_error"] = truncate(message, 500)
                    if result.status == ResultStatus.SUCCESS:
                        result.status = ResultStatus.FAILED
                        result.error = truncate(f"OpenCode error: {message}", 2000)
                    lowered = message.lower()
                    if any(token in lowered for token in ("permission", "approval", "approve", "not allowed", "external_directory")):
                        result.metadata["permission_required"] = True
        if result.status != ResultStatus.SUCCESS:
            combined = f"{result.stdout}\n{result.stderr}".lower()
            if any(token in combined for token in ("permission", "approval", "approve", "not allowed", "external_directory")):
                result.metadata["permission_required"] = True
                if "phê duyệt" not in (result.error or "").lower():
                    result.error = truncate(
                        "OpenCode yêu cầu phê duyệt; Trinity không dùng --auto. "
                        + (result.error or "kiểm tra permission của agent OpenCode"),
                        2000,
                    )
        return result

    @staticmethod
    def _parse_json_events(text: str):
        payload = (text or "").strip()
        if not payload:
            return [], "rỗng"
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, list):
                return parsed, ""
            return [parsed], ""
        except (TypeError, json.JSONDecodeError):
            pass
        events = []
        for line_no, line in enumerate(payload.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                return [], f"dòng {line_no}: {exc.msg}"
        if not events:
            return [], "không có JSON event"
        return events, ""

    @staticmethod
    def _event_error_message(event: dict) -> str:
        error = event.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("type") or "OpenCode error")
        return str(event.get("message") or error or "OpenCode error")
