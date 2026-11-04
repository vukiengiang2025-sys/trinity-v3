from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.support import FakeAdapter, FakeResource, make_config

from adapters.base import AdapterStatus, ResultStatus
from core.executor import Executor
from storage.memory import AuditLog, TaskManager


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = make_config(self.root)
        self.audit = AuditLog(self.config.log_dir)

    def executor(self, adapters, resource=None):
        manager = TaskManager(self.config.db_path)
        self.addCleanup(manager.close)
        return Executor(
            self.config,
            task_manager=manager,
            audit=self.audit,
            resource=resource or FakeResource(),
            adapters=adapters,
        )

    def test_successful_coding_task_is_persisted(self):
        adapter = FakeAdapter("opencode", stdout="agent output")
        executor = self.executor({"opencode": adapter})
        outcome = executor.execute("sửa lỗi pytest trong module python")
        self.assertEqual(outcome.status, "COMPLETED")
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(adapter.calls[0].action, "code_task")
        task = TaskManager(self.config.db_path).get_task(outcome.task_id)
        self.assertEqual(task["status"], "COMPLETED")
        self.assertIn("agent output", task["result"])
        self.assertTrue(self.audit.path.exists())

    def test_dry_run_does_not_create_db_or_audit(self):
        adapter = FakeAdapter("opencode")
        executor = self.executor({"opencode": adapter})
        outcome = executor.execute("sửa lỗi python", dry_run=True)
        self.assertEqual(outcome.status, "DRY_RUN")
        self.assertEqual(adapter.calls, [])
        self.assertFalse(Path(self.config.db_path).exists())
        self.assertFalse(self.audit.path.exists())

    def test_high_risk_without_approval_waits(self):
        adapter = FakeAdapter("opencode")
        executor = self.executor({"opencode": adapter})
        outcome = executor.execute("xóa toàn bộ dữ liệu production")
        self.assertEqual(outcome.status, "WAITING_APPROVAL")
        self.assertTrue(outcome.approval_required)
        self.assertEqual(adapter.calls, [])
        task = TaskManager(self.config.db_path).get_task(outcome.task_id)
        self.assertEqual(task["status"], "WAITING_APPROVAL")

    def test_high_risk_with_approval_executes(self):
        adapter = FakeAdapter("opencode", stdout="done")
        executor = self.executor({"opencode": adapter})
        outcome = executor.execute("xóa toàn bộ dữ liệu production", approve=True)
        self.assertEqual(outcome.status, "COMPLETED")
        self.assertEqual(len(adapter.calls), 1)
        self.assertTrue(adapter.calls[0].approved)

    def test_unavailable_adapter_fails_task(self):
        adapter = FakeAdapter("opencode", result_status=ResultStatus.UNAVAILABLE, error="not installed")
        executor = self.executor({"opencode": adapter})
        outcome = executor.execute("sửa lỗi python")
        self.assertEqual(outcome.status, "FAILED")
        self.assertIn("UNAVAILABLE", outcome.message)
        task = TaskManager(self.config.db_path).get_task(outcome.task_id)
        self.assertEqual(task["status"], "FAILED")

    def test_adapter_timeout_fails_task(self):
        adapter = FakeAdapter("opencode", result_status=ResultStatus.TIMEOUT, error="too slow")
        executor = self.executor({"opencode": adapter})
        outcome = executor.execute("sửa lỗi python")
        self.assertEqual(outcome.status, "FAILED")
        self.assertIn("TIMEOUT", outcome.message)

    def test_sequential_order_and_mixed_plan(self):
        calls = []

        class RecordingAdapter(FakeAdapter):
            def execute(self, request):
                calls.append((self.name, request.action))
                return super().execute(request)

        adapters = {
            "hermes": RecordingAdapter("hermes"),
            "opencode": RecordingAdapter("opencode"),
            "openclaw": RecordingAdapter("openclaw"),
        }
        executor = self.executor(adapters)
        outcome = executor.execute("đọc memory, refactor code rồi chạy lệnh git status")
        self.assertEqual(outcome.status, "COMPLETED")
        self.assertEqual(
            calls,
            [
                ("hermes", "memory_query"),
                ("opencode", "code_task"),
                ("openclaw", "automation_task"),
            ],
        )
        self.assertEqual([step.status for step in outcome.steps], ["SUCCESS", "SUCCESS", "SUCCESS"])

    def test_failure_stops_sequential_plan(self):
        hermes = FakeAdapter("hermes", result_status=ResultStatus.FAILED, error="brain down")
        opencode = FakeAdapter("opencode")
        executor = self.executor({"hermes": hermes, "opencode": opencode})
        outcome = executor.execute("đọc memory rồi refactor code")
        self.assertEqual(outcome.status, "FAILED")
        self.assertEqual(len(hermes.calls), 1)
        self.assertEqual(opencode.calls, [])

    def test_resource_denied_skips_adapters(self):
        adapter = FakeAdapter("opencode")
        executor = self.executor({"opencode": adapter}, resource=FakeResource(allowed=False, reason="RAM thấp"))
        outcome = executor.execute("sửa lỗi python")
        self.assertEqual(outcome.status, "FAILED")
        self.assertIn("Resource Guard", outcome.message)
        self.assertEqual(adapter.calls, [])

    def test_busy_lock_fails_task(self):
        adapter = FakeAdapter("opencode")
        executor = self.executor({"opencode": adapter}, resource=FakeResource(lock_error=True))
        outcome = executor.execute("sửa lỗi python")
        self.assertEqual(outcome.status, "FAILED")
        self.assertIn("lock busy", outcome.message)
        self.assertEqual(adapter.calls, [])

    def test_adapter_exception_is_contained(self):
        adapter = FakeAdapter("opencode", raise_error=True)
        executor = self.executor({"opencode": adapter})
        outcome = executor.execute("sửa lỗi python")
        self.assertEqual(outcome.status, "FAILED")
        self.assertIn("adapter lỗi", outcome.message)

    def test_result_secrets_are_redacted(self):
        fake_token = "ghp_" + ("e" * 36)
        adapter = FakeAdapter(
            "opencode",
            stdout=f"done token={fake_token}",
            error="password=hunter2",
        )
        executor = self.executor({"opencode": adapter})
        outcome = executor.execute("sửa lỗi python")
        self.assertEqual(outcome.status, "COMPLETED")
        task = TaskManager(self.config.db_path).get_task(outcome.task_id)
        serialized = task["result"] + task["error"]
        self.assertNotIn(fake_token, serialized)
        self.assertNotIn("hunter2", serialized)
        self.assertIn("REDACTED", serialized)


if __name__ == "__main__":
    unittest.main()
