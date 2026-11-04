"""Sequential task executor.

The executor is the only component allowed to turn a route into real work.
It re-checks risk approval, resources, the single-task lock and every adapter
result.  Steps are executed one at a time, never in parallel.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
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


class TaskHeartbeat:
    """Keep a task lease fresh while an adapter is running."""

    def __init__(self, task_manager: TaskManager, task_id: str, runner_id: str, interval: float = 5.0):
        self.task_manager = task_manager
        self.task_id = task_id
        self.runner_id = runner_id
        self.interval = max(1.0, float(interval))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"trinity-heartbeat-{task_id}", daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.task_manager.heartbeat(self.task_id, self.runner_id)
            except Exception:
                # A missing heartbeat will cause recovery to treat the task as
                # stale, which is safer than claiming it completed.
                return

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self.interval + 1)


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
        self.runner_host = socket.gethostname()
        self.runner_id = f"{self.runner_host}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
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
        write: bool = False,
    ) -> ExecutionOutcome:
        if not dry_run:
            self.recover_interrupted_tasks()
        route = self.router.route(prompt, write=write)

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

        if (route.risk_level in self.config.approval_risks or write) and not approve:
            task = self._create_task(route, prompt, status=PENDING)
            task = self.task_manager.update_status(
                route.task_id,
                WAITING_APPROVAL,
                error=(
                    f"cần phê duyệt cho mức rủi ro {route.risk_level}"
                    + (" và chế độ controlled-write" if write else "")
                    + "; dùng resume --approve"
                ),
            )
            self._audit("approval_required", route.task_id, {"risk": route.risk_level, "task_type": route.task_type})
            return ExecutionOutcome(
                task_id=route.task_id,
                status=WAITING_APPROVAL,
                message=(
                    f"Tác vụ rủi ro {route.risk_level} cần phê duyệt. "
                    f"Dùng: python3 trinity.py resume {route.task_id} --approve"
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
        heartbeat: Optional[TaskHeartbeat] = None
        try:
            task = self.task_manager.mark_running(
                route.task_id, self.runner_id, os.getpid(), self.runner_host
            )
            self._audit(
                "task_started",
                route.task_id,
                {"task_type": route.task_type, "plan": route.execution_plan, "runner_id": self.runner_id},
            )
            heartbeat = TaskHeartbeat(
                self.task_manager,
                route.task_id,
                self.runner_id,
                interval=float(self.config.get("resources.heartbeat_interval_seconds", 5)),
            )
            heartbeat.start()
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
            if heartbeat is not None:
                heartbeat.stop()
            if lock is not None:
                lock.release()

    # ------------------------------------------------------------------
    def recover_interrupted_tasks(self) -> int:
        """Fail tasks left RUNNING by a previous Trinity process.

        Recovery only runs when the single-task lock can be acquired.  If a
        live Trinity holds the lock, the RUNNING task belongs to it and must
        not be touched.
        """

        try:
            lock = self.resource.acquire_lock(timeout=0)
        except TrinityError:
            return 0
        stale_seconds = max(0.0, float(self.config.get("resources.recovery_stale_seconds", 60)))
        try:
            count = self.task_manager.fail_running_tasks(
                "Trinity dừng giữa tác vụ hoặc lease đã hết hạn; "
                "task được đánh dấu FAILED khi khởi động lại",
                stale_before=time.time() - stale_seconds,
            )
        finally:
            lock.release()
        if count:
            self._audit("tasks_recovered", "", {"count": count})
        return count

    # ------------------------------------------------------------------
    def resume(
        self,
        task_id: str,
        approve: bool = False,
        timeout: Optional[int] = None,
        dry_run: bool = False,
        approver: Optional[str] = None,
    ) -> ExecutionOutcome:
        """Resume an existing WAITING_APPROVAL task without creating a new one.

        The original plan and Task ID are reused.  Approval is explicit, the
        current workspace/profile/resources are re-checked, and a conditional
        SQLite claim prevents two runners from executing the same task.
        """

        if not dry_run:
            self.recover_interrupted_tasks()
        task = self.task_manager.get_task(task_id)
        if task is None:
            raise TrinityError(f"không tìm thấy task: {task_id}")
        status = str(task.get("status", ""))
        if status != WAITING_APPROVAL:
            raise TrinityError(
                f"chỉ resume được task WAITING_APPROVAL; task {task_id} đang {status}"
            )
        if str(task.get("profile", "")) != self.config.profile:
            raise TrinityError(
                f"profile của task ({task.get('profile')}) khác profile hiện tại ({self.config.profile})"
            )

        stored_workspace = str(task.get("workspace", "")).strip()
        if not stored_workspace:
            raise TrinityError("task v3.0 không lưu workspace; không thể resume an toàn")
        try:
            workspace = Path(stored_workspace).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            raise TrinityError(f"workspace của task không hợp lệ: {exc}") from exc
        if workspace != self.config.workspace_root:
            raise TrinityError(
                f"workspace của task ({workspace}) khác workspace hiện tại ({self.config.workspace_root})"
            )

        plan = self._load_plan(task)
        prompt = str(task.get("prompt", ""))
        if not prompt.strip():
            raise TrinityError("task không có prompt; không thể resume an toàn")

        route = Route(
            task_id=task_id,
            task_type=str(task.get("task_type", "GENERAL")),
            selected_adapter=str(task.get("adapter", "opencode")),
            execution_plan=plan,
            risk_level=str(task.get("risk_level", "high")),
            matched_rules=["resume"],
            approval_required=True,
            write_mode=any(step.get("action") == "code_write" for step in plan),
            raw_prompt=prompt,
        )

        guard_decision = self.guard.check_path(self.config.workspace_root, write=True)
        if not guard_decision.allowed:
            message = f"Guard từ chối workspace khi resume: {guard_decision.reason}"
            if dry_run:
                return ExecutionOutcome(
                    task_id=task_id,
                    status="DRY_RUN",
                    message=message,
                    route=route,
                    dry_run=True,
                    approval_required=True,
                    task=task,
                )
            return self._resume_blocked(task_id, route, task, message)

        if dry_run:
            resource = self.resource.preflight(dry_run=True)
            return ExecutionOutcome(
                task_id=task_id,
                status="DRY_RUN",
                message="DRY-RUN resume: kế hoạch cũ được kiểm tra, không ghi dữ liệu và không chạy adapter",
                route=route,
                dry_run=True,
                approval_required=True,
                task=task,
                resource=resource,
            )

        if not approve:
            return ExecutionOutcome(
                task_id=task_id,
                status=WAITING_APPROVAL,
                message=f"Resume cần phê duyệt rõ ràng: python3 trinity.py resume {task_id} --approve",
                route=route,
                approval_required=True,
                task=task,
            )

        resource = self.resource.preflight(dry_run=False)
        if not resource.allowed:
            return self._resume_blocked(
                task_id,
                route,
                task,
                f"Resource Guard từ chối resume: {resource.reason}",
                resource=resource,
            )

        for step in plan:
            adapter_name = step.get("adapter", "")
            adapter = self.adapters.get(adapter_name)
            if adapter is None:
                raise TrinityError(f"không có adapter {adapter_name!r} để resume")
            try:
                ready, detail = adapter.execution_ready(str(step.get("action", "")))
            except Exception as exc:
                raise TrinityError(f"không kiểm tra được adapter {adapter_name}: {exc}") from exc
            if not ready:
                return self._resume_blocked(
                    task_id,
                    route,
                    task,
                    f"Adapter {adapter_name} chưa sẵn sàng: {detail or 'không khả dụng'}. "
                    "Task vẫn WAITING_APPROVAL.",
                    resource=resource,
                )

        try:
            lock = self.resource.acquire_lock(timeout=0)
        except TrinityError as exc:
            return self._resume_blocked(
                task_id,
                route,
                task,
                f"Không thể resume: {exc}",
                resource=resource,
            )

        steps: List[StepReport] = []
        claimed = False
        heartbeat: Optional[TaskHeartbeat] = None
        try:
            actor = approver or self._current_actor()
            scope = self._approval_scope(route)
            task = self.task_manager.claim_for_resume(
                task_id,
                actor,
                scope,
                runner_id=self.runner_id,
                pid=os.getpid(),
                host=self.runner_host,
            )
            claimed = True
            self._audit(
                "resume_started",
                task_id,
                {"actor": actor, "scope": scope, "runner_id": self.runner_id},
            )
            heartbeat = TaskHeartbeat(
                self.task_manager,
                task_id,
                self.runner_id,
                interval=float(self.config.get("resources.heartbeat_interval_seconds", 5)),
            )
            heartbeat.start()
            task = self._run_plan(route, prompt, steps, timeout, True)
            if task["status"] == COMPLETED:
                self._audit("task_completed", task_id, {"steps": len(steps), "resumed": True})
                message = f"Hoàn thành resume {len(steps)} bước tuần tự."
            else:
                self._audit("task_failed", task_id, {"error": task.get("error", ""), "resumed": True})
                message = task.get("error", "tác vụ resume thất bại")
            return ExecutionOutcome(
                task_id=task_id,
                status=task["status"],
                message=message,
                route=route,
                approval_required=True,
                task=task,
                resource=resource,
                steps=steps,
            )
        except Exception as exc:
            error = truncate(f"lỗi resume: {exc}", 2000)
            if claimed:
                task = self._mark_failed(task_id, error)
                self._audit("task_failed", task_id, {"error": error, "resumed": True})
                return ExecutionOutcome(
                    task_id=task_id,
                    status=FAILED,
                    message=error,
                    route=route,
                    approval_required=True,
                    task=task,
                    resource=resource,
                    steps=steps,
                )
            current = self.task_manager.get_task(task_id)
            current_status = (current or {}).get("status", WAITING_APPROVAL)
            return ExecutionOutcome(
                task_id=task_id,
                status=current_status,
                message=f"Không claim được task để resume: {error}",
                route=route,
                approval_required=True,
                task=current,
                resource=resource,
                steps=steps,
            )
        finally:
            if heartbeat is not None:
                heartbeat.stop()
            lock.release()

    def _resume_blocked(
        self,
        task_id: str,
        route: Route,
        task: Optional[Dict[str, Any]],
        message: str,
        resource: Optional[ResourceDecision] = None,
    ) -> ExecutionOutcome:
        try:
            self.task_manager.add_event(task_id, "resume_blocked", message)
        except Exception:
            pass
        self._audit("resume_blocked", task_id, {"reason": message})
        return ExecutionOutcome(
            task_id=task_id,
            status=WAITING_APPROVAL,
            message=message,
            route=route,
            approval_required=True,
            task=task,
            resource=resource,
        )

    @staticmethod
    def _load_plan(task: Dict[str, Any]) -> List[Dict[str, str]]:
        raw = str(task.get("plan_json", "") or "")
        if not raw.strip():
            raise TrinityError("task không có execution_plan đã lưu; không thể resume an toàn")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TrinityError(f"execution_plan đã lưu không phải JSON hợp lệ: {exc}") from exc
        if not isinstance(data, list) or not data:
            raise TrinityError("execution_plan đã lưu rỗng hoặc sai định dạng")
        if len(data) > 10:
            raise TrinityError("execution_plan đã lưu vượt giới hạn 10 bước")
        plan: List[Dict[str, str]] = []
        for index, step in enumerate(data, start=1):
            if not isinstance(step, dict):
                raise TrinityError(f"bước {index} trong plan không phải object")
            adapter_name = str(step.get("adapter", "")).strip()
            action = str(step.get("action", "")).strip()
            if not adapter_name or not action:
                raise TrinityError(f"bước {index} thiếu adapter/action")
            plan.append({str(key): str(value) for key, value in step.items()})
        return plan

    def _approval_scope(self, route: Route) -> str:
        payload = json.dumps(
            {
                "task_id": route.task_id,
                "profile": self.config.profile,
                "workspace": str(self.config.workspace_root),
                "risk": route.risk_level,
                "plan": route.execution_plan,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return f"task={route.task_id}; profile={self.config.profile}; risk={route.risk_level}; plan_sha256={digest}"

    @staticmethod
    def _current_actor() -> str:
        try:
            return getpass.getuser()
        except Exception:
            return os.environ.get("USER", "unknown")

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
            plan_json=json.dumps(route.execution_plan, ensure_ascii=False, sort_keys=True),
            workspace=str(self.config.workspace_root),
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
