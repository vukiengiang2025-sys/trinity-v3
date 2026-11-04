from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import make_config

from core.resource import ResourceGuard, ResourceSnapshot, read_memory
from core.utils import FileLock, ResourceError


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = make_config(Path(self.tmp.name))
        self.guard = ResourceGuard(self.config)

    def test_read_memory_on_linux(self):
        snapshot = read_memory()
        if not snapshot.readable:
            self.skipTest("môi trường không đọc được RAM")
        self.assertIsNotNone(snapshot.available_mb)
        self.assertGreaterEqual(snapshot.available_mb, 0)

    def test_unreadable_ram_is_unknown_not_zero(self):
        with mock.patch("builtins.open", side_effect=OSError("blocked")):
            with mock.patch("os.sysconf", side_effect=OSError("blocked")):
                snapshot = read_memory()
        self.assertFalse(snapshot.readable)
        self.assertIsNone(snapshot.available_mb)
        self.assertIsNone(snapshot.total_mb)

    def test_sysconf_fallback_when_proc_meminfo_unavailable(self):
        values = {
            "SC_PAGE_SIZE": 4096,
            "SC_PHYS_PAGES": 1_000_000,
            "SC_AVPHYS_PAGES": 500_000,
        }
        with mock.patch("builtins.open", side_effect=OSError("no /proc")):
            with mock.patch("os.sysconf", side_effect=lambda name: values[name]):
                snapshot = read_memory()
        self.assertTrue(snapshot.readable)
        self.assertEqual(snapshot.source, "sysconf")
        self.assertIsNotNone(snapshot.available_mb)
        self.assertGreater(snapshot.available_mb, 0)

    def test_unreadable_ram_continues_with_warning(self):
        unknown = ResourceSnapshot(None, None, False, "unreadable")
        with mock.patch("core.resource.read_memory", return_value=unknown):
            decision = self.guard.preflight(dry_run=False)
        self.assertTrue(decision.allowed)
        self.assertTrue(any("không đọc được RAM" in warning for warning in decision.warnings))

    def test_low_memory_denied_for_real_run(self):
        low = ResourceSnapshot(2048, 100, True, "test")
        with mock.patch("core.resource.read_memory", return_value=low):
            decision = self.guard.preflight(dry_run=False)
        self.assertFalse(decision.allowed)
        self.assertIn("RAM", decision.reason)

    def test_low_memory_allowed_for_dry_run_with_warning(self):
        low = ResourceSnapshot(2048, 100, True, "test")
        with mock.patch("core.resource.read_memory", return_value=low):
            decision = self.guard.preflight(dry_run=True)
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.warnings)

    def test_low_memory_warn_policy(self):
        config = make_config(Path(self.tmp.name) / "warn", overrides={"resources": {"on_low_memory": "warn"}})
        guard = ResourceGuard(config)
        low = ResourceSnapshot(2048, 10, True, "test")
        with mock.patch("core.resource.read_memory", return_value=low):
            decision = guard.preflight(dry_run=False)
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.warnings)

    def test_low_disk_denied(self):
        enough = ResourceSnapshot(2048, 1024, True, "test")
        with mock.patch("core.resource.read_memory", return_value=enough):
            with mock.patch.object(self.guard, "disk_free_mb", return_value=10.0):
                decision = self.guard.preflight(dry_run=False)
        self.assertFalse(decision.allowed)
        self.assertIn("ổ đĩa", decision.reason)

    def test_sequential_lock_prevents_concurrent_run(self):
        first = self.guard.acquire_lock()
        try:
            with self.assertRaises(ResourceError):
                self.guard.acquire_lock()
        finally:
            first.release()
        second = self.guard.acquire_lock()
        second.release()

    def test_file_lock_releases(self):
        path = Path(self.tmp.name) / "test.lock"
        first = FileLock(path)
        self.assertTrue(first.acquire())
        second = FileLock(path)
        self.assertFalse(second.acquire())
        first.release()
        self.assertTrue(second.acquire())
        second.release()

    def test_timeout_clamping(self):
        self.assertEqual(self.guard.clamp_timeout(None), 900)
        self.assertEqual(self.guard.clamp_timeout(5000), 3600)
        self.assertEqual(self.guard.clamp_timeout("bad"), 900)
        self.assertEqual(self.guard.clamp_timeout(1), 1)


if __name__ == "__main__":
    unittest.main()
