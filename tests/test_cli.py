from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import PROJECT_ROOT


def run_cli(args, extra_env=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env.pop("TRINITY_PROFILE", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "trinity.py"), *args],
        cwd=str(PROJECT_ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
    )


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "tasks.db"
        self.env = {"TRINITY_LOG_DIR": str(self.root / "logs")}

    def test_version(self):
        result = run_cli(["version"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Trinity v3.0.0", result.stdout)
        self.assertIn("Python", result.stdout)

    def test_help(self):
        result = run_cli(["--help"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("run", result.stdout)
        self.assertIn("doctor", result.stdout)

    def test_status_lists_three_adapters(self):
        result = run_cli(["status"], self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("Hermes", "OpenCode", "OpenClaw", "BRAIN", "LOGIC", "MUSCLE"):
            self.assertIn(name, result.stdout)

    def test_config_linux(self):
        result = run_cli(["config"], self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("profile: linux", result.stdout)
        self.assertIn("min_free_ram_mb: 384", result.stdout)
        self.assertIn("mode: cli", result.stdout)

    def test_config_termux_after_command(self):
        result = run_cli(["config", "--profile", "termux"], self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("profile: termux", result.stdout)
        self.assertIn("min_free_ram_mb: 192", result.stdout)

    def test_config_termux_before_command(self):
        result = run_cli(["--profile", "termux", "config"], self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("profile: termux", result.stdout)

    def test_dry_run_creates_no_db_or_audit(self):
        result = run_cli(["--db", str(self.db), "run", "--dry-run", "xin chào"], self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DRY_RUN", result.stdout)
        self.assertIn("KẾ HOẠCH", result.stdout)
        self.assertFalse(self.db.exists())
        self.assertFalse((self.root / "logs" / "audit.jsonl").exists())

    def test_dry_run_coding_plan_uses_opencode(self):
        result = run_cli(["--db", str(self.db), "run", "--dry-run", "sửa lỗi pytest trong module python"], self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("opencode", result.stdout)
        self.assertIn("CODING", result.stdout)

    def test_high_risk_requires_approval_and_persists_task(self):
        result = run_cli(
            ["--db", str(self.db), "run", "xóa toàn bộ dữ liệu production"],
            self.env,
        )
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("WAITING_APPROVAL", result.stdout)
        match = re.search(r"TRN-\d{8}-\d{6}-[0-9a-f]{6}", result.stdout)
        self.assertIsNotNone(match)
        self.assertTrue(self.db.exists())

        tasks = run_cli(["tasks", "--db", str(self.db)], self.env)
        self.assertEqual(tasks.returncode, 0, tasks.stderr)
        self.assertIn("WAITING_APPROVAL", tasks.stdout)
        self.assertIn(match.group(0), tasks.stdout)

        detail = run_cli(["--db", str(self.db), "task", match.group(0)], self.env)
        self.assertEqual(detail.returncode, 0, detail.stderr)
        self.assertIn(match.group(0), detail.stdout)
        self.assertIn("prompt", detail.stdout)

    def test_tasks_empty(self):
        result = run_cli(["--db", str(self.db), "tasks"], self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Chưa có nhiệm vụ", result.stdout)
        self.assertFalse(self.db.exists())

    def test_unknown_task_returns_usage_error(self):
        result = run_cli(["--db", str(self.db), "task", "TRN-khong-ton-tai"], self.env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("không tìm thấy task", result.stderr)

    def test_invalid_profile_is_rejected(self):
        result = run_cli(["--profile", "../evil", "status"], self.env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("profile", result.stderr)

    def test_dry_run_does_not_change_existing_db(self):
        first = run_cli(["--db", str(self.db), "run", "xóa dữ liệu production"], self.env)
        self.assertEqual(first.returncode, 3, first.stderr)

        def snapshot():
            data = b""
            for suffix in ("", "-wal", "-shm"):
                path = Path(str(self.db) + suffix)
                if path.exists():
                    data += path.read_bytes()
            return data

        before = snapshot()
        second = run_cli(
            ["--db", str(self.db), "run", "--dry-run", "sửa lỗi python"],
            self.env,
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(before, snapshot())

    def test_dry_run_high_risk_creates_no_db(self):
        result = run_cli(["--db", str(self.db), "run", "--dry-run", "xóa dữ liệu production"], self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Phê duyệt     : CÓ", result.stdout)
        self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
