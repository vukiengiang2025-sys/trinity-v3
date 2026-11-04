from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import FakeAdapter, FakeResource, make_config

from adapters.base import AdapterStatus
from core.executor import Executor
from core.resource import ResourceGuard
from core.utils import FileLock, ResourceError, StorageError, TrinityError
from storage.memory import AuditLog, TaskManager


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = make_config(self.root)
        self.audit = AuditLog(self.config.log_dir)
        self.task_manager = TaskManager(self.config.db_path)
        self.addCleanup(self.task_manager.close)
        self.adapter = FakeAdapter("opencode", stdout="resumed output")

    def executor(self, adapter=None, resource=None, config=None):
        return Executor(
            config or self.config,
            task_manager=self.task_manager,
            audit=self.audit,
            resource=resource or FakeResource(),
            adapters={"opencode": adapter or self.adapter},
        )

    def make_waiting_task(self, prompt="xóa dữ liệu production"):
        outcome = self.executor().execute(prompt)
        self.assertEqual(outcome.status, "WAITING_APPROVAL")
        self.assertEqual(self.adapter.calls, [])
        return outcome.task_id

    def test_waiting_task_stores_plan_and_workspace(self):
        task_id = self.make_waiting_task()
        task = self.task_manager.get_task(task_id)
        self.assertTrue(task["plan_json"])
        plan = json.loads(task["plan_json"])
        self.assertEqual(plan[0]["adapter"], "opencode")
        self.assertEqual(task["workspace"], str(self.config.workspace_root))
        self.assertEqual(task["resume_count"], 0)

    def test_resume_without_approval_does_not_execute(self):
        task_id = self.make_waiting_task()
        outcome = self.executor().resume(task_id)
        self.assertEqual(outcome.status, "WAITING_APPROVAL")
        self.assertEqual(self.adapter.calls, [])
        task = self.task_manager.get_task(task_id)
        self.assertEqual(task["status"], "WAITING_APPROVAL")
        self.assertEqual(task["approved_by"], "")

    def test_resume_completes_same_task_and_records_approval(self):
        task_id = self.make_waiting_task()
        outcome = self.executor().resume(task_id, approve=True, approver="tester")
        self.assertEqual(outcome.status, "COMPLETED")
        self.assertEqual(outcome.task_id, task_id)
        self.assertEqual(len(self.adapter.calls), 1)
        task = self.task_manager.get_task(task_id)
        self.assertEqual(task["status"], "COMPLETED")
        self.assertEqual(task["approved_by"], "tester")
        self.assertTrue(task["approved_at"])
        self.assertIn("plan_sha256=", task["approval_scope"])
        self.assertEqual(task["resume_count"], 1)
        self.assertEqual(len(self.task_manager.list_tasks()), 1)
        events = [event["kind"] for event in task["events"]]
        self.assertIn("approval", events)

    def test_resume_dry_run_does_not_mutate(self):
        task_id = self.make_waiting_task()
        before = self.task_manager.get_task(task_id)
        outcome = self.executor().resume(task_id, approve=True, dry_run=True)
        self.assertEqual(outcome.status, "DRY_RUN")
        after = self.task_manager.get_task(task_id)
        self.assertEqual(after["status"], "WAITING_APPROVAL")
        self.assertEqual(after["approved_by"], "")
        self.assertEqual(after["resume_count"], 0)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(before["plan_json"], after["plan_json"])

    def test_resume_wrong_status_rejected(self):
        outcome = self.executor().execute("sửa lỗi python")
        self.assertEqual(outcome.status, "COMPLETED")
        with self.assertRaises(TrinityError):
            self.executor().resume(outcome.task_id, approve=True)

    def test_resume_missing_plan_rejected(self):
        self.task_manager.create_task(
            task_id="TRN-old-no-plan",
            task_type="CODING",
            prompt="old",
            adapter="opencode",
            risk_level="high",
            profile="linux",
            status="WAITING_APPROVAL",
            workspace=str(self.config.workspace_root),
        )
        with self.assertRaises(TrinityError):
            self.executor().resume("TRN-old-no-plan", approve=True)
        self.assertEqual(self.adapter.calls, [])

    def test_resume_profile_mismatch_rejected(self):
        task_id = self.make_waiting_task()
        other = make_config(self.root, "termux")
        with self.assertRaises(TrinityError):
            self.executor(config=other).resume(task_id, approve=True)

    def test_resume_workspace_mismatch_rejected(self):
        task_id = self.make_waiting_task()
        other_workspace = self.root / "other-workspace"
        other_workspace.mkdir()
        other = make_config(self.root, "linux", workspace=other_workspace)
        with self.assertRaises(TrinityError):
            self.executor(config=other).resume(task_id, approve=True)

    def test_unavailable_adapter_keeps_task_waiting(self):
        task_id = self.make_waiting_task()
        unavailable = FakeAdapter("opencode", status=AdapterStatus.UNAVAILABLE)
        outcome = self.executor(adapter=unavailable).resume(task_id, approve=True)
        self.assertEqual(outcome.status, "WAITING_APPROVAL")
        self.assertIn("chưa sẵn sàng", outcome.message)
        self.assertEqual(unavailable.calls, [])
        self.assertEqual(self.task_manager.get_task(task_id)["status"], "WAITING_APPROVAL")

    def test_resource_denied_keeps_task_waiting(self):
        task_id = self.make_waiting_task()
        outcome = self.executor(resource=FakeResource(allowed=False, reason="RAM thấp")).resume(
            task_id, approve=True
        )
        self.assertEqual(outcome.status, "WAITING_APPROVAL")
        self.assertIn("Resource Guard", outcome.message)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.task_manager.get_task(task_id)["status"], "WAITING_APPROVAL")

    def test_busy_lock_keeps_task_waiting(self):
        task_id = self.make_waiting_task()
        outcome = self.executor(resource=FakeResource(lock_error=True)).resume(task_id, approve=True)
        self.assertEqual(outcome.status, "WAITING_APPROVAL")
        self.assertIn("lock busy", outcome.message)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.task_manager.get_task(task_id)["status"], "WAITING_APPROVAL")

    def test_claim_is_atomic_and_cannot_run_twice(self):
        task_id = self.make_waiting_task()
        first = self.task_manager.claim_for_resume(task_id, "tester", "scope-1")
        self.assertEqual(first["status"], "RUNNING")
        with self.assertRaises(StorageError):
            self.task_manager.claim_for_resume(task_id, "tester-2", "scope-2")
        task = self.task_manager.get_task(task_id)
        self.assertEqual(task["resume_count"], 1)

    def test_old_schema_is_migrated(self):
        old_db = self.root / "old.db"
        conn = sqlite3.connect(old_db)
        conn.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                task_type TEXT NOT NULL, status TEXT NOT NULL, adapter TEXT NOT NULL,
                risk_level TEXT NOT NULL, profile TEXT NOT NULL, prompt TEXT NOT NULL,
                result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                dry_run INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                created_at TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL
            );
            INSERT INTO tasks VALUES
                ('TRN-old', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z',
                 'GENERAL', 'WAITING_APPROVAL', 'opencode', 'high', 'linux', 'old', '', '', 0);
            """
        )
        conn.commit()
        conn.close()

        manager = TaskManager(old_db)
        self.addCleanup(manager.close)
        old = manager.get_task("TRN-old")
        self.assertEqual(old["plan_json"], "")
        self.assertEqual(old["resume_count"], 0)

        manager.create_task(
            task_id="TRN-new",
            task_type="GENERAL",
            prompt="new",
            adapter="opencode",
            risk_level="low",
            profile="linux",
        )
        conn = sqlite3.connect(old_db)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        finally:
            conn.close()
        for name in ("plan_json", "workspace", "approved_by", "approved_at", "approval_scope", "resume_count", "runner_id", "runner_pid", "runner_host", "heartbeat_at"):
            self.assertIn(name, columns)


class LockCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = make_config(Path(self.tmp.name))

    def test_flock_unsupported_fails_closed(self):
        lock = FileLock(self.config.lock_path)
        with mock.patch("fcntl.flock", side_effect=OSError(95, "Operation not supported")):
            self.assertFalse(lock.acquire())
        self.assertIn("filesystem", lock.error)
        with mock.patch("fcntl.flock", side_effect=OSError(95, "Operation not supported")):
            with self.assertRaises(ResourceError):
                ResourceGuard(self.config).acquire_lock()

    def test_lock_file_cannot_be_opened_fails_closed(self):
        lock = FileLock(self.config.lock_path)
        with mock.patch("builtins.open", side_effect=OSError("permission denied")):
            self.assertFalse(lock.acquire())
        self.assertIn("không mở được lock file", lock.error)

    def test_non_posix_lock_is_not_silently_ignored(self):
        lock = FileLock(self.config.lock_path)
        with mock.patch("os.name", "nt"):
            self.assertFalse(lock.acquire())
        self.assertIn("POSIX", lock.error)


if __name__ == "__main__":
    unittest.main()
