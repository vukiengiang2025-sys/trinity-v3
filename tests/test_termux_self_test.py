from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import PROJECT_ROOT


class TermuxSelfTestScriptTests(unittest.TestCase):
    def test_quick_self_test_runs_on_linux_without_agent_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "termux-report.txt"
            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "termux_self_test.py"),
                    "--quick",
                    "--output",
                    str(output),
                ],
                cwd=str(PROJECT_ROOT),
                text=True,
                capture_output=True,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.exists())
            text = output.read_text(encoding="utf-8")
            self.assertIn("TRINITY TERMUX SELF TEST", text)
            self.assertIn("SQLite + lease/recovery", text)
            self.assertIn("fcntl lock", text)
            self.assertIn("Subprocess timeout", text)
            self.assertNotIn("[FAIL]", text)


if __name__ == "__main__":
    unittest.main()
