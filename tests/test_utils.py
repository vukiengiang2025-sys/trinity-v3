from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from core.utils import (
    contains_secret,
    mask_mapping,
    redact,
    run_process,
    truncate,
)


class TextSafetyTests(unittest.TestCase):
    def test_redacts_common_credentials(self):
        fake_key = "sk-" + ("k" * 24)
        fake_token = "ghp_" + ("p" * 36)
        samples = [
            "password=hunter2",
            f"api_key: {fake_key}",
            "Authorization: Bearer abcdefghijklmnop",
            fake_token,
            "https://user:secret@example.com/path",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                cleaned = redact(sample)
                self.assertNotIn("hunter2", cleaned)
                self.assertNotIn(fake_key, cleaned)
                self.assertNotIn("abcdefghijklmnop", cleaned)
                self.assertNotIn(fake_token, cleaned)
                self.assertNotIn(":secret@", cleaned)

    def test_private_key_block_redacted(self):
        begin = "-----BEGIN " + "RSA PRIVATE KEY" + "-----"
        end = "-----END " + "RSA PRIVATE KEY" + "-----"
        text = f"{begin}\nMIIE\n{end}"
        self.assertIn("REDACTED", redact(text))
        self.assertNotIn("MIIE", redact(text))

    def test_truncate_redacts_and_limits(self):
        text = "password=hunter2 " + "x" * 100
        result = truncate(text, 30)
        self.assertLessEqual(len(result), 30)
        self.assertNotIn("hunter2", result)

    def test_contains_secret(self):
        fake_token = "ghp_" + ("b" * 36)
        self.assertTrue(contains_secret(f"token={fake_token}"))
        self.assertFalse(contains_secret("Trinity chạy tuần tự"))

    def test_mask_mapping(self):
        fake_key = "sk-" + ("c" * 24)
        data = {
            "api_key": "secret",
            "model": fake_key,
            "nested": {"password": "hunter2", "safe": "ok"},
        }
        masked = mask_mapping(data)
        self.assertEqual(masked["api_key"], "***REDACTED***")
        self.assertNotIn(fake_key, masked["model"])
        self.assertEqual(masked["nested"]["password"], "***REDACTED***")
        self.assertEqual(masked["nested"]["safe"], "ok")


class ProcessTests(unittest.TestCase):
    def test_successful_process(self):
        result = run_process([sys.executable, "-c", "print('hello')"], timeout=5)
        self.assertTrue(result.ok)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("hello", result.stdout)

    def test_timeout_kills_process(self):
        started = time.monotonic()
        result = run_process([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.5)
        elapsed = time.monotonic() - started
        self.assertTrue(result.timed_out)
        self.assertLess(elapsed, 4)
        self.assertIn("thời gian", result.error)

    def test_cwd_and_pwd_are_consistent_in_child(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_process(
                [sys.executable, "-c", "import os; print(os.getcwd()); print(os.environ.get('PWD'))"],
                timeout=5,
                cwd=Path(directory),
            )
        self.assertTrue(result.ok)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], lines[1])

    def test_missing_executable_is_reported(self):
        result = run_process(["definitely-not-an-executable-trinity"], timeout=2)
        self.assertFalse(result.ok)
        self.assertIn("không tìm thấy", result.error)

    def test_output_is_redacted(self):
        result = run_process(
            [sys.executable, "-c", "print('password=hunter2')"],
            timeout=5,
        )
        self.assertNotIn("hunter2", result.stdout)
        self.assertIn("REDACTED", result.stdout)

    def test_sensitive_env_value_is_redacted(self):
        env = dict(os.environ)
        env["TRINITY_TEST_SECRET"] = "s3cr3t-value-12345"
        result = run_process(
            [sys.executable, "-c", "import os; print(os.environ['TRINITY_TEST_SECRET'])"],
            timeout=5,
            env=env,
        )
        self.assertTrue(result.ok)
        self.assertNotIn("s3cr3t-value-12345", result.stdout)
        self.assertIn("***REDACTED***", result.stdout)


if __name__ == "__main__":
    unittest.main()
