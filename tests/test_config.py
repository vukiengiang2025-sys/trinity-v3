from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import make_config

from core.config import ConfigError, detect_profile, load_config
from core.utils import contains_secret, redact


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_linux_profile_loads(self):
        config = make_config(self.root, "linux")
        self.assertEqual(config.profile, "linux")
        self.assertEqual(config.min_free_ram_mb, 384)
        self.assertEqual(config.min_free_disk_mb, 200)
        self.assertEqual(config.timeout_seconds, 900)
        self.assertEqual(config.get("adapters.opencode.mode"), "cli")
        self.assertTrue(config.source_path.name.endswith("linux.yaml"))

    def test_termux_profile_loads_with_different_thresholds(self):
        config = make_config(self.root, "termux")
        self.assertEqual(config.profile, "termux")
        self.assertEqual(config.min_free_ram_mb, 192)
        self.assertEqual(config.timeout_seconds, 600)
        self.assertEqual(config.max_timeout_seconds, 1800)
        self.assertLess(config.min_free_ram_mb, 384)

    def test_detect_profile_termux_env(self):
        with mock.patch.dict(os.environ, {"PREFIX": "/data/data/com.termux/files/usr"}, clear=False):
            self.assertEqual(detect_profile(), "termux")

    def test_detect_profile_linux(self):
        with mock.patch.dict(os.environ, {"PREFIX": ""}, clear=False):
            with mock.patch("core.config.is_termux", return_value=False):
                self.assertEqual(detect_profile(), "linux")

    def test_invalid_profile_name_rejected(self):
        with self.assertRaises(ConfigError):
            load_config("../../etc/passwd")

    def test_missing_profile_reports_available(self):
        with self.assertRaises(ConfigError) as context:
            load_config("khong_ton_tai")
        self.assertIn("linux", str(context.exception))
        self.assertIn("termux", str(context.exception))

    def test_allowed_roots_resolved_inside_workspace(self):
        config = make_config(self.root)
        roots = config.allowed_roots
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0], config.workspace_root)

    def test_public_dict_masks_credentials(self):
        config = make_config(self.root)
        fake_key = "sk-" + ("x" * 24)
        fake_token = "ghp_" + ("y" * 36)
        config.data["adapters"]["opencode"]["api_key"] = fake_key
        config.data["adapters"]["hermes"]["token"] = fake_token
        public = config.public_dict()
        self.assertEqual(public["adapters"]["opencode"]["api_key"], "***REDACTED***")
        self.assertEqual(public["adapters"]["hermes"]["token"], "***REDACTED***")
        text = str(public)
        self.assertNotIn(fake_key, text)
        self.assertNotIn(fake_token, text)

    def test_redaction_helpers(self):
        fake_token = "ghp_" + ("z" * 36)
        text = f"password=hunter2 token={fake_token}"
        cleaned = redact(text)
        self.assertNotIn("hunter2", cleaned)
        self.assertNotIn(fake_token, cleaned)
        self.assertTrue(contains_secret(text))
        self.assertFalse(contains_secret("xin chào bình thường"))


if __name__ == "__main__":
    unittest.main()
