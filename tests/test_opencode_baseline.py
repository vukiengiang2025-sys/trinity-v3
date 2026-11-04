from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "opencode_readonly"


class OpenCodeReadonlyBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config_bytes = (FIXTURE / "opencode.json").read_bytes()
        cls.config = json.loads(cls.config_bytes)
        cls.baseline = json.loads((FIXTURE / "baseline.json").read_text(encoding="utf-8"))
        cls.agent = cls.config["agents"]["trinity-readonly"]
        cls.rules = cls.agent["permissions"]

    def test_config_hash_matches_frozen_baseline(self):
        digest = hashlib.sha256(self.config_bytes).hexdigest()
        self.assertEqual(digest, self.baseline["config_sha256"])

    def test_required_deny_rules_are_present_and_last(self):
        required = (
            "edit",
            "bash",
            "webfetch",
            "websearch",
            "external_directory",
            "task",
            "skill",
            "todowrite",
            "question",
        )
        for action in required:
            matching = [rule for rule in self.rules if rule.get("action") == action]
            self.assertTrue(matching, f"missing rule for {action}")
            self.assertEqual(matching[-1].get("effect"), "deny", f"{action} must end in deny")

    def test_read_is_allowed_but_sensitive_paths_are_denied(self):
        read_rules = [rule for rule in self.rules if rule.get("action") == "read"]
        self.assertTrue(any(rule.get("resource") == "*" and rule.get("effect") == "allow" for rule in read_rules))
        for resource in ("*.env", "*.env.*", "/etc/**", "/home/**", "/root/**", "/proc/**", "/sys/**"):
            matching = [rule for rule in read_rules if rule.get("resource") == resource]
            self.assertTrue(matching, f"missing read deny for {resource}")
            self.assertEqual(matching[-1].get("effect"), "deny")

    def test_baseline_metadata(self):
        self.assertEqual(self.baseline["opencode_version"], "2.2.5")
        self.assertEqual(self.baseline["agent"], "trinity-readonly")
        self.assertEqual(self.agent["mode"], "primary")
        self.assertTrue(any(task_id.startswith("TRN-") for task_id in self.baseline["task_ids"]))
        self.assertFalse(self.baseline["permission_test"]["workspace_changed"])
        self.assertFalse(self.baseline["permission_test"]["git_changed"])

    def test_durability_runs_are_frozen(self):
        runs = self.baseline["durability_runs"]
        self.assertGreaterEqual(len(runs), 3)
        for run in runs:
            self.assertEqual(run["status"], "COMPLETED")
            self.assertTrue(run["task_id"].startswith("TRN-"))
            self.assertGreater(run["result_chars"], 0)

    def test_failure_modes_are_fail_closed(self):
        failures = {item["case"]: item for item in self.baseline["failure_modes"]}
        self.assertIn("missing_agent", failures)
        self.assertIn("timeout", failures)
        self.assertTrue(all(item["status"] == "FAILED" for item in failures.values()))

    def test_approval_metadata_is_complete(self):
        approval = self.baseline.get("approval", {})
        self.assertTrue(approval.get("required"))
        for key in ("approved_by", "approved_at", "reason", "policy"):
            self.assertTrue(approval.get(key), f"missing approval field: {key}")

    def test_deny_to_allow_mutation_is_detected(self):
        def valid(config):
            rules = config["agents"]["trinity-readonly"]["permissions"]
            for action in (
                "edit",
                "bash",
                "webfetch",
                "websearch",
                "external_directory",
                "task",
                "skill",
                "todowrite",
                "question",
            ):
                matching = [rule for rule in rules if rule.get("action") == action]
                if not matching or matching[-1].get("effect") != "deny":
                    return False
            return True

        self.assertTrue(valid(self.config))
        mutated = json.loads(json.dumps(self.config))
        for rule in mutated["agents"]["trinity-readonly"]["permissions"]:
            if rule.get("action") == "edit":
                rule["effect"] = "allow"
                break
        self.assertFalse(valid(mutated))

    def test_fixture_contains_no_provider_secrets(self):
        text = self.config_bytes.decode("utf-8")
        for marker in ("apiKey", "api_key", "password", "Bearer ", "sk-", "ghp_"):
            self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()
