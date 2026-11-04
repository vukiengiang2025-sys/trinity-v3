"""BRAIN adapter: Hermes.

Hermes is not assumed to exist, and its memory directory is never assumed to
be ``~/.hermes/memories``.  The adapter only executes command templates that
the operator has explicitly configured after verifying them; otherwise it
reports INSTALLED_BUT_UNVERIFIED instead of guessing a CLI.
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


class HermesAdapter(BaseAdapter):
    name = "hermes"
    role = "BRAIN"

    def __init__(self, config: AppConfig, guard: Any = None):
        super().__init__(config, guard)

    def _memory_path(self) -> Optional[str]:
        configured = self.settings().get("memory_path")
        raw = str(configured).strip() if configured else os.environ.get("HERMES_MEMORY_PATH", "").strip()
        if not raw:
            return None
        return str(Path(raw).expanduser())

    def discover(self, force: bool = False) -> AdapterHealth:
        if self._health is not None and not force:
            return self._health
        disabled = self._disabled_health()
        if disabled:
            self._health = disabled
            return disabled

        memory_path = self._memory_path()
        binary = self.binary()
        if not binary:
            self._health = AdapterHealth(
                name=self.name,
                role=self.role,
                status=AdapterStatus.UNAVAILABLE,
                detail="không tìm thấy Hermes CLI; không tạo hoặc đoán memory directory",
                memory_path=memory_path,
            )
            return self._health

        version_proc = self._probe([binary, "--version"], timeout=10)
        version = self._version(version_proc.stdout or version_proc.stderr) if version_proc.ok else ""
        if not version_proc.ok:
            self._health = AdapterHealth(
                name=self.name,
                role=self.role,
                status=AdapterStatus.INSTALLED_BUT_UNVERIFIED,
                executable=binary,
                detail="tìm thấy executable nhưng --version thất bại",
                memory_path=memory_path,
            )
            return self._health

        help_proc = self._probe([binary, "--help"], timeout=10)
        help_text = (help_proc.stdout + help_proc.stderr).lower()
        mentions_memory = "memory" in help_text or "skill" in help_text
        search_template = self._template("memory_search_command")
        store_template = self._template("memory_store_command")
        capabilities: List[str] = []
        if search_template:
            capabilities.append("memory_query")
        if store_template:
            capabilities.append("memory_store")

        if mentions_memory and capabilities:
            status = AdapterStatus.AVAILABLE
            detail = "CLI và command template memory đã được cấu hình"
        elif mentions_memory:
            status = AdapterStatus.INSTALLED_BUT_UNVERIFIED
            detail = "CLI có tính năng memory nhưng chưa cấu hình command template đã xác minh"
        else:
            status = AdapterStatus.INSTALLED_BUT_UNVERIFIED
            detail = "CLI phản hồi nhưng chưa xác minh được khả năng memory/skill"

        self._health = AdapterHealth(
            name=self.name,
            role=self.role,
            status=status,
            version=version,
            executable=binary,
            detail=detail,
            capabilities=capabilities,
            memory_path=memory_path,
        )
        return self._health

    def _template(self, key: str) -> Optional[List[str]]:
        value = self.settings().get(key)
        if not isinstance(value, list) or not value:
            return None
        if not all(isinstance(item, str) and item for item in value):
            return None
        return list(value)

    def execute(self, request: AdapterRequest) -> AdapterResult:
        started = utc_now()
        health = self.discover()
        if health.status == AdapterStatus.UNAVAILABLE:
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.UNAVAILABLE,
                error=health.detail or "Hermes không khả dụng",
                started_at=started,
            )
        if health.status != AdapterStatus.AVAILABLE:
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.UNVERIFIED,
                error=health.detail or "Hermes chưa được xác minh",
                started_at=started,
            )
        if request.action == "memory_query":
            template = self._template("memory_search_command")
        elif request.action == "memory_store":
            template = self._template("memory_store_command")
        else:
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.UNVERIFIED,
                error=f"action không được hỗ trợ: {request.action}",
                started_at=started,
            )
        if not template:
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.UNVERIFIED,
                error="chưa cấu hình command template cho hành động này",
                started_at=started,
            )

        memory_path = self._memory_path()
        replacements = {
            "{query}": request.prompt,
            "{prompt}": request.prompt,
            "{task_id}": request.task_id,
            "{memory_path}": memory_path or "",
        }
        argv: List[str] = []
        for part in template:
            expanded = part
            for placeholder, value in replacements.items():
                expanded = expanded.replace(placeholder, value)
            argv.append(expanded)

        if "{memory_path}" in " ".join(template) and not memory_path:
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.UNVERIFIED,
                error="command template dùng {memory_path} nhưng chưa xác định memory path",
                started_at=started,
            )

        configured_binary = str(self.settings().get("binary", "hermes"))
        if argv[0] == configured_binary:
            resolved = health.executable
            if not resolved:
                return AdapterResult(
                    adapter=self.name,
                    status=ResultStatus.UNAVAILABLE,
                    error="không tìm thấy Hermes CLI",
                    started_at=started,
                )
            argv[0] = resolved
        elif not which(argv[0]):
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.UNAVAILABLE,
                error=f"không tìm thấy executable: {argv[0]}",
                started_at=started,
            )

        if any("\x00" in part for part in argv):
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.DENIED,
                error="command template chứa ký tự NUL",
                started_at=started,
            )

        if request.dry_run:
            return AdapterResult(
                adapter=self.name,
                status=ResultStatus.DRY_RUN,
                started_at=started,
                metadata={
                    "argv_preview": [part if part not in (request.prompt,) else "<prompt>" for part in argv],
                    "memory_path": memory_path,
                },
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
        process = run_process(argv, timeout=timeout, cwd=workspace, env=env, max_output=100_000)
        return self._result_from_process(
            self.name,
            process,
            started_at=started,
            metadata={"memory_path": memory_path, "timeout_seconds": timeout},
        )
