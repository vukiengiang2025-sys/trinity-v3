from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.support import FakeAdapter, FakeResource, make_config

from core.executor import Executor
from storage.memory import AuditLog, TaskManager


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = make_config(self.root)
        self.manager = TaskManager(self.config.db_path)
        self.addCleanup(self.manager.close)
        self.audit = AuditLog(self.config.log_dir)
        self.adapter = FakeAdapter("opencode", stdout="ok")

    def executor(self, resource=None):
        return Executor(
            self.config,
            task_manager=self.manager,
            audit=self.audit,
            resource=resource or FakeResource(),
            adapters={"opencode": self.adapter},
        )

    def make_running(self, task_id="TRN-running"):
        self.manager.create_task(
            task_id=task_id,
            task_type="CODING",
            prompt="interrupted task",
            adapter="opencode",
            risk_level="medium",
            profile="linux",
        )
        self.manager.update_status(task_id, "RUNNING")
        return task_id

    def test_running_task_is_recovered_as_failed(self):
        task_id = self.make_running()
        count = self.executor().recover_interrupted_tasks()
        self.assertEqual(count, 1)
        task = self.manager.get_task(task_id)
        self.assertEqual(task["status"], "FAILED")
        self.assertIn("khởi động lại", task["error"])
        self.assertIn("recovered", [event["kind"] for event in task["events"]])
        self.assertNotEqual(task["status"], "COMPLETED")

    def test_recovery_is_idempotent(self):
        self.make_running()
        executor = self.executor()
        self.assertEqual(executor.recover_interrupted_tasks(), 1)
        self.assertEqual(executor.recover_interrupted_tasks(), 0)

    def test_recovery_skips_when_lock_is_busy(self):
        task_id = self.make_running()
        busy = FakeResource(lock_error=True)
        self.assertEqual(self.executor(resource=busy).recover_interrupted_tasks(), 0)
        self.assertEqual(self.manager.get_task(task_id)["status"], "RUNNING")
        self.assertEqual(self.executor().recover_interrupted_tasks(), 1)
        self.assertEqual(self.manager.get_task(task_id)["status"], "FAILED")

    def test_fresh_heartbeat_is_not_recovered(self):
        self.manager.create_task(
            task_id="TRN-fresh",
            task_type="CODING",
            prompt="live",
            adapter="opencode",
            risk_level="medium",
            profile="linux",
        )
        self.manager.mark_running("TRN-fresh", "runner-a", os.getpid(), "host-a")
        self.assertEqual(self.executor().recover_interrupted_tasks(), 0)
        self.assertEqual(self.manager.get_task("TRN-fresh")["status"], "RUNNING")

        conn = sqlite3.connect(self.config.db_path)
        try:
            conn.execute("UPDATE tasks SET heartbeat_at = 0 WHERE id = ?", ("TRN-fresh",))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.executor().recover_interrupted_tasks(), 1)
        self.assertEqual(self.manager.get_task("TRN-fresh")["status"], "FAILED")

    def test_mark_running_and_heartbeat_lease(self):
        self.manager.create_task(
            task_id="TRN-lease",
            task_type="CODING",
            prompt="lease",
            adapter="opencode",
            risk_level="medium",
            profile="linux",
        )
        task = self.manager.mark_running("TRN-lease", "runner-a", os.getpid(), "host-a")
        self.assertEqual(task["runner_id"], "runner-a")
        self.assertEqual(task["runner_pid"], os.getpid())
        self.assertTrue(self.manager.heartbeat("TRN-lease", "runner-a"))
        self.assertFalse(self.manager.heartbeat("TRN-lease", "runner-b"))
        with self.assertRaises(Exception):
            self.manager.mark_running("TRN-lease", "runner-b", os.getpid(), "host-b")

    def test_recovery_does_not_touch_waiting_approval(self):
        self.manager.create_task(
            task_id="TRN-waiting",
            task_type="GENERAL",
            prompt="wait",
            adapter="opencode",
            risk_level="high",
            profile="linux",
            status="WAITING_APPROVAL",
        )
        self.assertEqual(self.executor().recover_interrupted_tasks(), 0)
        self.assertEqual(self.manager.get_task("TRN-waiting")["status"], "WAITING_APPROVAL")

    def test_dry_run_does_not_recover(self):
        task_id = self.make_running()
        outcome = self.executor().execute("sửa lỗi python", dry_run=True)
        self.assertEqual(outcome.status, "DRY_RUN")
        self.assertEqual(self.manager.get_task(task_id)["status"], "RUNNING")
        self.assertEqual(self.adapter.calls, [])

    def test_execute_recovers_orphan_before_creating_new_task(self):
        old_id = self.make_running("TRN-orphan")
        outcome = self.executor().execute("sửa lỗi python")
        self.assertEqual(outcome.status, "COMPLETED")
        self.assertEqual(self.manager.get_task(old_id)["status"], "FAILED")
        self.assertEqual(len(self.manager.list_tasks()), 2)
        self.assertEqual(len(self.adapter.calls), 1)


if __name__ == "__main__":
    unittest.main()
