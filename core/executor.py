"""Sequential task executor.

The executor is the only component allowed to turn a route into real work.
It re-checks risk approval, resources, the single-task lock and every adapter
result.  Steps are executed one at a time, never in parallel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.config import AppConfig
from core.guard import Guard
from core.resource import ResourceDecision, ResourceGuard
from core.router import Route, Router
from core.task import PENDING, RUNNING, WAITING_APPROVAL, FAILED, COMPLETED
from core.utils import TrinityError, truncate, utc_now
from storage.memory import AuditLog, TaskManager
from adapters.base import AdapterRequest, AdapterResult, ResultStatus
from adapters.hermes import HermesAdapter
from adapters.openclaw import OpenClawAdapter
from adapters.opencode import OpenCodeAdapter


@dataclass
class StepReport:
    step: Dict[str, str]
    status: str
    detail: str = ""
    duration_seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "step": self.step,
            "status": self.status,
            "detail": self.detail,
            "duration_seconds": round(self.duration_seconds, 3),
        }


@dataclass
class ExecutionOutcome:
    task_id: str
    status: str
    message: str
    route: Route
    dry_run: bool = False
    approval_required: bool = False
    task: Optional[Dict[str, Any]] = None
    resource: Optional[ResourceDecision] = None
    steps: List[StepReport] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "message": self.message,
            "dry_run": self.dry_run,
            "approval_required": self.approval_required,
            "route": self.route.as_dict(),
            "resource": self.resource.as_dict() if self.resource else None,
            "steps": [step.as_dict() for step in self.steps],
            "task": self.task,
        }


class Executor:
    def __init__(
        self,
        config: AppConfig,
        router: Optional[Router] = None,
        guard: Optional[Guard] = None,
        resource: Optional[ResourceGuard] = None,
        task_manager: Optional[TaskManager] = None,
        audit: Optional[AuditLog] = None,
        adapters: Optional[Dict[str, Any]] = None,
    ):
        self.config = config
        self.router = router or Router(config)
        self.guard = guard or Guard(config)
        self.resource = resource or ResourceGuard(config)
        self.task_manager = task_manager or TaskManager(
            config.db_path, max_result_chars=int(config.get("storage.max_result_chars", 20000))
        )
        self.audit = audit or AuditLog(config.log_dir)
        self.adapters = adapters or {
            "hermes": HermesAdapter(config, self.guard),
            "opencode": OpenCodeAdapter(config, self.guard),
            "openclaw": OpenClawAdapter(config, self.guard),
        }

    # ------------------------------------------------------------------
    def execute(
        self,
        prompt: str,
        dry_run: bool = False,
        approve: bool = False,
        timeout: Optional[int] = None,
    ) -> ExecutionOutcome:
        route = self.router.route(prompt)

        if dry_run:
            resource = self.resource.preflight(dry_run=True)
            return ExecutionOutcome(
                task_id=route.task_id,
                status="DRY_RUN",
                message="DRY-RUN: chỉ lập kế hoạch, không chạy adapter, không ghi SQLite/audit",
                route=route,
                dry_run=True,
                approval_required=route.approval_required,
                resource=resource,
            )

        if route.risk_level in self.config.approval_risks and not approve:
            task = self._create_task(route, prompt, status=PENDING)
            task = self.task_manager.update_status(
                route.task_id,
                WAITING_APPROVAL,
                error=f"cần phê duyệt cho mức rủi ro {route.risk_level}; chạy lại với --approve",
            )
            self._audit("approval_required", route.task_id, {"risk": route.risk_level, "task_type": route.task_type})
            return ExecutionOutcome(
                task_id=route.task_id,
                status=WAITING_APPROVAL,
                message=(
                    f"Tác vụ rủi ro {route.risk_level} cần phê duyệt. "
                    f"Chạy lại với --approve nếu bạn đã kiểm tra kế hoạch."
                ),
                route=route,
                approval_required=True,
                task=task,
            )

        resource = self.resource.preflight(dry_run=False)
        task = self._create_task(route, prompt, status=PENDING)
        if not resource.allowed:
            task = self._mark_failed(route.task_id, f"Resource Guard từ chối: {resource.reason}")
            self._audit("resource_denied", route.task_id, {"reason": resource.reason})
            return ExecutionOutcome(
                task_id=route.task_id,
                status=FAILED,
                message=f"Resource Guard từ chối: {resource.reason}",
                route=route,
                task=task,
                resource=resource,
            )

        if approve and route.risk_level in self.config.approval_risks:
            self.task_manager.add_event(route.task_id, "approval", f"người dùng phê duyệt risk={route.risk_level}")
            self._audit("approval_granted", route.task_id, {"risk": route.risk_level})

        lock = None
        try:
            lock = self.resource.acquire_lock(timeout=0)
        except TrinityError as exc:
            task = self._mark_failed(route.task_id, str(exc))
            return ExecutionOutcome(
                task_id=route.task_id,
                status=FAILED,
                message=str(exc),
                route=route,
                task=task,
                resource=resource,
            )

        steps: List[StepReport] = []
        try:
            task = self.task_manager.update_status(route.task_id, RUNNING)
            self._audit("task_started", route.task_id, {"task_type": route.task_type, "plan": route.execution_plan})
            task = self._run_plan(route, prompt, steps, timeout, approve)
            if task["status"] == COMPLETED:
                self._audit("task_completed", route.task_id, {"steps": len(steps)})
                message = f"Hoàn thành {len(steps)} bước tuần tự."
            else:
                self._audit("task_failed", route.task_id, {"error": task.get("error", "")})
                message = task.get("error", "tác vụ thất bại")
            return ExecutionOutcome(
                task_id=route.task_id,
                status=task["status"],
                message=message,
                route=route,
                task=task,
                resource=resource,
                steps=steps,
            )
        except Exception as exc:  # storage/guard failures must not leave a silent task
            error = truncate(f"lỗi điều phối: {exc}", 2000)
            task = self._mark_failed(route.task_id, error)
            self._audit("task_failed", route.task_id, {"error": error})
            return ExecutionOutcome(
                task_id=route.task_id,
                status=FAILED,
                message=error,
                route=route,
                task=task,
                resource=resource,
                steps=steps,
            )
        finally:
            if lock is not None:
                lock.release()

    # ------------------------------------------------------------------
    def _run_plan(
        self,
        route: Route,
        prompt: str,
        steps: List[StepReport],
        timeout: Optional[int],
        approve: bool,
    ) -> Dict[str, Any]:
        outputs: List[str] = []
        for index, step in enumerate(route.execution_plan, start=1):
            adapter_name = step.get("adapter", "")
            adapter = self.adapters.get(adapter_name)
            if adapter is None:
                error = f"không có adapter {adapter_name!r} cho bước {index}"
                self.task_manager.add_event(route.task_id, "error", error)
                return self._mark_failed(route.task_id, error)

            self.task_manager.add_event(
                route.task_id,
                "adapter_start",
                f"bước {index}/{len(route.execution_plan)}: {adapter_name} ({step.get('role', '')})",
            )
            self._audit(
                "adapter_started",
                route.task_id,
                {"step": index, "adapter": adapter_name, "action": step.get("action", "")},
            )
            started = utc_now()
            try:
                request = AdapterRequest(
                    task_id=route.task_id,
                    prompt=prompt,
                    action=str(step.get("action", "")),
                    workspace=self.config.workspace_root,
                    timeout=self.resource.clamp_timeout(timeout),
                    dry_run=False,
                    approved=approve,
                )
                result = adapter.execute(request)
            except Exception as exc:
                result = AdapterResult(
                    adapter=adapter_name,
                    status=ResultStatus.FAILED,
                    error=truncate(f"adapter lỗi: {exc}", 2000),
                )

            detail = self._result_detail(result)
            steps.append(
                StepReport(
                    step=step,
                    status=result.status.value,
                    detail=detail,
                    duration_seconds=result.duration_seconds,
                )
            )
            self.task_manager.add_event(
                route.task_id,
                "adapter_result",
                f"bước {index}/{len(route.execution_plan)}: {adapter_name} -> {result.status.value} "
                f"({result.duration_seconds:.1f}s) {detail}",
            )
            self._audit(
                "adapter_finished",
                route.task_id,
                {
                    "step": index,
                    "adapter": adapter_name,
                    "status": result.status.value,
                    "duration_seconds": result.duration_seconds,
                },
            )

            if result.status != ResultStatus.SUCCESS:
                error = (
                    f"bước {index} ({adapter_name}) thất bại: "
                    f"{result.status.value}: {result.error or 'không rõ lỗi'}"
                )
                return self._mark_failed(route.task_id, error)

            if result.stdout:
                outputs.append(f"[BƯỚC {index} - {adapter_name}]\n{result.stdout.strip()}")
            elif result.stderr:
                outputs.append(f"[BƯỚC {index} - {adapter_name}]\n{result.stderr.strip()}")

        summary = f"Hoàn thành {len(steps)} bước tuần tự."
        if outputs:
            summary += "\n\n" + "\n\n".join(outputs)
        return self.task_manager.update_status(
            route.task_id,
            COMPLETED,
            result=truncate(summary, int(self.config.get("storage.max_result_chars", 20000))),
        )

    @staticmethod
    def _result_detail(result: AdapterResult) -> str:
        if result.error:
            return truncate(result.error, 1000)
        text = (result.stdout or result.stderr or "").strip()
        return truncate(text, 1000)

    def _create_task(self, route: Route, prompt: str, status: str) -> Dict[str, Any]:
        return self.task_manager.create_task(
            task_id=route.task_id,
            task_type=route.task_type,
            prompt=prompt,
            adapter=route.selected_adapter,
            risk_level=route.risk_level,
            profile=self.config.profile,
            status=status,
        )

    def _mark_failed(self, task_id: str, error: str) -> Dict[str, Any]:
        safe_error = truncate(error, 2000)
        try:
            return self.task_manager.update_status(task_id, FAILED, error=safe_error)
        except Exception:
            try:
                task = self.task_manager.get_task(task_id)
                if task is not None:
                    return task
            except Exception:
                pass
            # Keep the executor's return contract even if SQLite itself is down.
            return {"id": task_id, "status": FAILED, "error": safe_error, "result": "", "events": []}

    def _audit(self, event: str, task_id: str, detail: Any = None) -> None:
        self.audit.write(event, task_id=task_id, detail=detail)
