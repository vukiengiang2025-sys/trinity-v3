from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from core.utils import StorageError
from storage.memory import AuditLog, TaskManager


class TaskManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "tasks.db"
        self.manager = TaskManager(self.db)
        self.addCleanup(self.manager.close)

    def _create(self, task_id="TRN-20260101-120000-abcdef", prompt="làm việc"):
        return self.manager.create_task(
            task_id=task_id,
            task_type="CODING",
            prompt=prompt,
            adapter="opencode",
            risk_level="medium",
            profile="linux",
        )

    def test_create_and_get_task(self):
        task = self._create()
        self.assertEqual(task["id"], "TRN-20260101-120000-abcdef")
        self.assertEqual(task["status"], "PENDING")
        self.assertEqual(task["task_type"], "CODING")
        self.assertEqual(task["adapter"], "opencode")
        self.assertTrue(task["created_at"])
        self.assertTrue(task["events"])

    def test_update_status_flow(self):
        self._create()
        task = self.manager.update_status("TRN-20260101-120000-abcdef", "RUNNING")
        self.assertEqual(task["status"], "RUNNING")
        task = self.manager.update_status("TRN-20260101-120000-abcdef", "COMPLETED", result="xong")
        self.assertEqual(task["status"], "COMPLETED")
        self.assertEqual(task["result"], "xong")

    def test_invalid_transition_rejected(self):
        self._create()
        with self.assertRaises(StorageError):
            self.manager.update_status("TRN-20260101-120000-abcdef", "COMPLETED")
        self.manager.update_status("TRN-20260101-120000-abcdef", "RUNNING")
        self.manager.update_status("TRN-20260101-120000-abcdef", "COMPLETED")
        with self.assertRaises(StorageError):
            self.manager.update_status("TRN-20260101-120000-abcdef", "RUNNING")

    def test_cancel_task(self):
        self._create()
        task = self.manager.cancel_task("TRN-20260101-120000-abcdef")
        self.assertEqual(task["status"], "CANCELLED")
        self.assertIn("hủy", task["error"])

    def test_duplicate_task_id_rejected(self):
        self._create()
        with self.assertRaises(StorageError):
            self._create()

    def test_events_are_appended(self):
        self._create()
        self.manager.add_event("TRN-20260101-120000-abcdef", "note", "thông tin thêm")
        task = self.manager.get_task("TRN-20260101-120000-abcdef")
        kinds = [event["kind"] for event in task["events"]]
        self.assertIn("created", kinds)
        self.assertIn("note", kinds)

    def test_secrets_are_redacted_in_prompt_result_and_events(self):
        fake_token = "ghp_" + ("t" * 36)
        prompt = f"password=hunter2 token={fake_token}"
        self._create(prompt=prompt)
        self.manager.add_event("TRN-20260101-120000-abcdef", "debug", prompt)
        self.manager.update_status("TRN-20260101-120000-abcdef", "RUNNING")
        self.manager.update_status(
            "TRN-20260101-120000-abcdef",
            "COMPLETED",
            result=f"done password=hunter2 token={fake_token}",
        )
        task = self.manager.get_task("TRN-20260101-120000-abcdef")
        serialized = json.dumps(task, ensure_ascii=False)
        self.assertNotIn("hunter2", serialized)
        self.assertNotIn(fake_token, serialized)
        self.assertIn("REDACTED", serialized)

        raw = b""
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.db) + suffix)
            if path.exists():
                raw += path.read_bytes()
        self.assertNotIn(b"hunter2", raw)
        self.assertNotIn(fake_token.encode(), raw)

    def test_read_missing_db_does_not_create_it(self):
        missing = Path(self.tmp.name) / "missing.db"
        manager = TaskManager(missing)
        self.assertIsNone(manager.get_task("TRN-x"))
        self.assertEqual(manager.list_tasks(), [])
        self.assertEqual(manager.summary(), {})
        self.assertFalse(missing.exists())
        manager.close()

    def test_list_and_summary(self):
        self._create("TRN-20260101-120000-aaaaaa")
        self._create("TRN-20260101-120001-bbbbbb")
        self.manager.update_status("TRN-20260101-120001-bbbbbb", "RUNNING")
        self.manager.update_status("TRN-20260101-120001-bbbbbb", "COMPLETED", result="ok")
        self.assertEqual(len(self.manager.list_tasks()), 2)
        self.assertEqual(len(self.manager.list_tasks(limit=1)), 1)
        self.assertEqual(len(self.manager.list_tasks(status="COMPLETED")), 1)
        summary = self.manager.summary()
        self.assertEqual(summary.get("PENDING"), 1)
        self.assertEqual(summary.get("COMPLETED"), 1)

    def test_db_permissions_are_private(self):
        self._create()
        mode = os.stat(self.db).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_memory_database_supported(self):
        manager = TaskManager(":memory:")
        try:
            task = manager.create_task(
                task_id="TRN-memory",
                task_type="GENERAL",
                prompt="hello",
                adapter="opencode",
                risk_level="low",
                profile="linux",
            )
            self.assertEqual(task["status"], "PENDING")
        finally:
            manager.close()


class AuditLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_dir = Path(self.tmp.name) / "logs"
        self.audit = AuditLog(self.log_dir)

    def test_audit_redacts_and_is_private(self):
        fake_token = "ghp_" + ("a" * 36)
        ok = self.audit.write(
            "test_event",
            task_id="TRN-1",
            detail={
                "password": "hunter2",
                "note": f"token={fake_token}",
            },
        )
        self.assertTrue(ok)
        text = self.audit.path.read_text(encoding="utf-8")
        self.assertNotIn("hunter2", text)
        self.assertNotIn(fake_token, text)
        self.assertIn("REDACTED", text)
        record = json.loads(text.strip())
        self.assertEqual(record["event"], "test_event")
        self.assertEqual(os.stat(self.audit.path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
