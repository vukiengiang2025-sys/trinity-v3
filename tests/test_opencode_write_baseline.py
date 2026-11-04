from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "opencode_write"


class OpenCodeWriteBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = (FIXTURE / "opencode.json").read_bytes()
        cls.config = json.loads(cls.raw)
        cls.baseline = json.loads((FIXTURE / "baseline.json").read_text(encoding="utf-8"))
        cls.agent = cls.config["agents"]["trinity-write"]
        cls.rules = cls.agent["permissions"]

    def test_write_config_hash_is_frozen(self):
        self.assertEqual(hashlib.sha256(self.raw).hexdigest(), self.baseline["config_sha256"])

    def test_edit_is_allowed(self):
        edit = [rule for rule in self.rules if rule.get("action") == "edit"]
        self.assertTrue(edit)
        self.assertEqual(edit[-1].get("effect"), "allow")

    def test_external_and_sensitive_access_denied(self):
        for action, resource in (
            ("external_directory", "*"),
            ("read", "*.env"),
            ("read", "*.env.*"),
            ("read", "/etc/**"),
            ("read", "/home/**"),
        ):
            matches = [r for r in self.rules if r.get("action") == action and r.get("resource") == resource]
            self.assertTrue(matches, f"missing {action}:{resource}")
            self.assertEqual(matches[-1].get("effect"), "deny")

    def test_bash_allowlist_and_blanket_deny(self):
        bash = [r for r in self.rules if r.get("action") == "bash"]
        self.assertEqual(bash[0].get("effect"), "deny")
        allowed = {r.get("resource") for r in bash if r.get("effect") == "allow"}
        for resource in (
            "git status*",
            "git diff*",
            "git log*",
            "python3 -m unittest*",
            "python3 -m py_compile*",
        ):
            self.assertIn(resource, allowed)

    def test_live_write_and_approval_are_recorded(self):
        self.assertEqual(self.baseline["live_write_status"], "COMPLETED")
        self.assertTrue(self.baseline["live_write_task_id"].startswith("TRN-"))
        self.assertTrue(self.baseline["approval_task_id"].startswith("TRN-"))
        self.assertIn("--write --approve", self.baseline["approval"])
        self.assertEqual(self.agent["mode"], "primary")


if __name__ == "__main__":
    unittest.main()
