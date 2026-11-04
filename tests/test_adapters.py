from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import make_config, make_fake_executable

from adapters.base import AdapterRequest, AdapterStatus, ResultStatus
from adapters.hermes import HermesAdapter
from adapters.openclaw import OpenClawAdapter
from adapters.opencode import OpenCodeAdapter
from core.utils import ProcessResult


class AdapterTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = make_config(self.root)
        self.workspace = self.config.workspace_root

    def request(self, action="code_task", prompt="làm việc", dry_run=False, metadata=None, timeout=None):
        return AdapterRequest(
            task_id="TRN-test",
            prompt=prompt,
            action=action,
            workspace=self.workspace,
            dry_run=dry_run,
            metadata=metadata or {},
            timeout=timeout,
        )


class OpenCodeAdapterTests(AdapterTestBase):
    def setUp(self):
        super().setUp()
        self.config.data["adapters"]["opencode"]["agent"] = "test-agent"

    def test_missing_binary_is_unavailable(self):
        self.config.data["adapters"]["opencode"]["binary"] = "definitely-not-installed-trinity"
        adapter = OpenCodeAdapter(self.config)
        health = adapter.discover(force=True)
        self.assertEqual(health.status, AdapterStatus.UNAVAILABLE)
        result = adapter.execute(self.request())
        self.assertEqual(result.status, ResultStatus.UNAVAILABLE)

    def test_fake_cli_discovery_and_execution(self):
        fake = make_fake_executable(self.root, "opencode-fake")
        self.config.data["adapters"]["opencode"]["binary"] = str(fake)
        adapter = OpenCodeAdapter(self.config)
        health = adapter.discover(force=True)
        self.assertEqual(health.status, AdapterStatus.AVAILABLE)
        self.assertEqual(health.version, "9.9.9")
        result = adapter.execute(self.request(prompt="sửa lỗi python"))
        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertIn("agent output", result.stdout)

    def test_dry_run_does_not_execute(self):
        fake = make_fake_executable(self.root, "opencode-fake")
        self.config.data["adapters"]["opencode"]["binary"] = str(fake)
        adapter = OpenCodeAdapter(self.config)
        adapter.discover(force=True)
        result = adapter.execute(self.request(dry_run=True))
        self.assertEqual(result.status, ResultStatus.DRY_RUN)
        self.assertIn("argv_preview", result.metadata)
        self.assertNotIn("agent output", result.stdout)

    def test_timeout_is_enforced(self):
        fake = make_fake_executable(self.root, "opencode-timeout", mode="timeout")
        self.config.data["adapters"]["opencode"]["binary"] = str(fake)
        adapter = OpenCodeAdapter(self.config)
        adapter.discover(force=True)
        result = adapter.execute(self.request(timeout=1))
        self.assertEqual(result.status, ResultStatus.TIMEOUT)
        self.assertLess(result.duration_seconds, 4.5)

    def test_argument_injection_blocked(self):
        fake = make_fake_executable(self.root, "opencode-fake")
        self.config.data["adapters"]["opencode"]["binary"] = str(fake)
        adapter = OpenCodeAdapter(self.config)
        adapter.discover(force=True)
        result = adapter.execute(self.request(prompt="--dangerous-flag"))
        self.assertEqual(result.status, ResultStatus.DENIED)

    def test_http_mode_is_not_claimed_verified(self):
        fake = make_fake_executable(self.root, "opencode-fake")
        self.config.data["adapters"]["opencode"]["binary"] = str(fake)
        self.config.data["adapters"]["opencode"]["mode"] = "http"
        adapter = OpenCodeAdapter(self.config)
        health = adapter.discover(force=True)
        self.assertEqual(health.status, AdapterStatus.INSTALLED_BUT_UNVERIFIED)
        result = adapter.execute(self.request())
        self.assertEqual(result.status, ResultStatus.UNVERIFIED)

    def test_nonzero_exit_is_failed(self):
        fake = make_fake_executable(self.root, "opencode-fail", mode="fail")
        self.config.data["adapters"]["opencode"]["binary"] = str(fake)
        adapter = OpenCodeAdapter(self.config)
        adapter.discover(force=True)
        result = adapter.execute(self.request())
        self.assertEqual(result.status, ResultStatus.FAILED)

    def test_json_output_is_detected_without_parsing_away_raw_text(self):
        fake = make_fake_executable(self.root, "opencode-json", mode="json")
        self.config.data["adapters"]["opencode"]["binary"] = str(fake)
        adapter = OpenCodeAdapter(self.config)
        adapter.discover(force=True)
        result = adapter.execute(self.request())
        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertTrue(result.metadata.get("json_valid"))
        self.assertIn('"ok": true', result.stdout)

    def test_invalid_json_output_fails_closed(self):
        fake = make_fake_executable(self.root, "opencode-invalid", mode="invalid-json")
        self.config.data["adapters"]["opencode"]["binary"] = str(fake)
        adapter = OpenCodeAdapter(self.config)
        adapter.discover(force=True)
        result = adapter.execute(self.request())
        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertFalse(result.metadata.get("json_valid"))
        self.assertIn("JSON", result.error)

    def test_empty_json_output_fails_closed(self):
        fake = make_fake_executable(self.root, "opencode-empty", mode="empty")
        self.config.data["adapters"]["opencode"]["binary"] = str(fake)
        adapter = OpenCodeAdapter(self.config)
        adapter.discover(force=True)
        result = adapter.execute(self.request())
        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertFalse(result.metadata.get("json_valid"))

    def test_error_event_with_exit_zero_fails_closed(self):
        fake = make_fake_executable(self.root, "opencode-agent-missing", mode="agent-missing")
        self.config.data["adapters"]["opencode"]["binary"] = str(fake)
        adapter = OpenCodeAdapter(self.config)
        adapter.discover(force=True)
        result = adapter.execute(self.request())
        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertIn("Agent not found", result.error)
        self.assertIn("Agent not found", result.metadata.get("opencode_error", ""))

    def test_permission_failure_is_surfaced(self):
        fake = make_fake_executable(self.root, "opencode-permission", mode="permission")
        self.config.data["adapters"]["opencode"]["binary"] = str(fake)
        adapter = OpenCodeAdapter(self.config)
        adapter.discover(force=True)
        result = adapter.execute(self.request())
        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertTrue(result.metadata.get("permission_required"))
        self.assertIn("phê duyệt", result.error)

    def test_default_agent_is_not_used_implicitly(self):
        fake = make_fake_executable(self.root, "opencode-fake")
        self.config.data["adapters"]["opencode"]["binary"] = str(fake)
        self.config.data["adapters"]["opencode"]["agent"] = None
        adapter = OpenCodeAdapter(self.config)
        adapter.discover(force=True)
        ready, reason = adapter.execution_ready()
        self.assertFalse(ready)
        self.assertIn("chưa cấu hình", reason)
        result = adapter.execute(self.request())
        self.assertEqual(result.status, ResultStatus.DENIED)
        self.assertIn("chưa cấu hình", result.error)

    def test_explicit_agent_is_execution_ready(self):
        fake = make_fake_executable(self.root, "opencode-fake")
        self.config.data["adapters"]["opencode"]["binary"] = str(fake)
        self.config.data["adapters"]["opencode"]["agent"] = "reviewed-agent"
        adapter = OpenCodeAdapter(self.config)
        adapter.discover(force=True)
        ready, reason = adapter.execution_ready()
        self.assertTrue(ready, reason)

    def test_standalone_is_used_and_auto_is_never_passed(self):
        fake = make_fake_executable(self.root, "opencode-fake")
        self.config.data["adapters"]["opencode"]["binary"] = str(fake)
        self.config.data["adapters"]["opencode"]["agent"] = "reviewed-agent"
        adapter = OpenCodeAdapter(self.config)
        adapter.discover(force=True)
        process = ProcessResult([], 0, "{}", "", False, 0.1)
        with mock.patch("adapters.opencode.run_process", return_value=process) as runner:
            result = adapter.execute(self.request())
        self.assertEqual(result.status, ResultStatus.SUCCESS)
        argv = runner.call_args.args[0]
        self.assertIn("--standalone", argv)
        self.assertIn("--agent", argv)
        self.assertNotIn("--auto", argv)


class HermesAdapterTests(AdapterTestBase):
    def test_missing_binary_is_unavailable_and_creates_no_directory(self):
        memory_path = self.root / "hermes-memory"
        self.config.data["adapters"]["hermes"]["binary"] = "definitely-not-installed-hermes"
        self.config.data["adapters"]["hermes"]["memory_path"] = str(memory_path)
        with mock.patch.dict(os.environ, {"HERMES_MEMORY_PATH": ""}, clear=False):
            adapter = HermesAdapter(self.config)
            health = adapter.discover(force=True)
        self.assertEqual(health.status, AdapterStatus.UNAVAILABLE)
        self.assertEqual(health.memory_path, str(memory_path))
        self.assertFalse(memory_path.exists())
        result = adapter.execute(self.request(action="memory_query"))
        self.assertEqual(result.status, ResultStatus.UNAVAILABLE)

    def test_cli_without_verified_templates_is_unverified(self):
        fake = make_fake_executable(self.root, "hermes-fake")
        self.config.data["adapters"]["hermes"]["binary"] = str(fake)
        with mock.patch.dict(os.environ, {"HERMES_MEMORY_PATH": ""}, clear=False):
            adapter = HermesAdapter(self.config)
            health = adapter.discover(force=True)
            self.assertEqual(health.status, AdapterStatus.INSTALLED_BUT_UNVERIFIED)
            self.assertIsNone(health.memory_path)
            result = adapter.execute(self.request(action="memory_query"))
            self.assertEqual(result.status, ResultStatus.UNVERIFIED)

    def test_verified_template_executes(self):
        fake = make_fake_executable(self.root, "hermes-fake")
        self.config.data["adapters"]["hermes"]["binary"] = str(fake)
        self.config.data["adapters"]["hermes"]["memory_search_command"] = [
            str(fake),
            "memory",
            "search",
            "{query}",
        ]
        with mock.patch.dict(os.environ, {"HERMES_MEMORY_PATH": ""}, clear=False):
            adapter = HermesAdapter(self.config)
            health = adapter.discover(force=True)
            self.assertEqual(health.status, AdapterStatus.AVAILABLE)
            result = adapter.execute(self.request(action="memory_query", prompt="dự án cũ"))
        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertIn("memory result", result.stdout)

    def test_unsupported_action_returns_unverified(self):
        fake = make_fake_executable(self.root, "hermes-fake")
        self.config.data["adapters"]["hermes"]["binary"] = str(fake)
        self.config.data["adapters"]["hermes"]["memory_search_command"] = [str(fake), "memory", "search", "{query}"]
        with mock.patch.dict(os.environ, {"HERMES_MEMORY_PATH": ""}, clear=False):
            adapter = HermesAdapter(self.config)
            adapter.discover(force=True)
            result = adapter.execute(self.request(action="code_task"))
        self.assertEqual(result.status, ResultStatus.UNVERIFIED)


class OpenClawAdapterTests(AdapterTestBase):
    def _configured_adapter(self, fake):
        self.config.data["adapters"]["openclaw"]["binary"] = str(fake)
        self.config.data["adapters"]["openclaw"]["invoke_prefix"] = [str(fake), "exec"]
        self.config.data["adapters"]["openclaw"]["command_allowlist"] = ["git", "ls", "pwd"]
        return OpenClawAdapter(self.config, guard=GuardForTests(self.config))

    def test_missing_binary_is_unavailable(self):
        self.config.data["adapters"]["openclaw"]["binary"] = "definitely-not-installed-openclaw"
        adapter = OpenClawAdapter(self.config)
        health = adapter.discover(force=True)
        self.assertEqual(health.status, AdapterStatus.UNAVAILABLE)
        result = adapter.execute(self.request(action="automation_task", metadata={"argv": ["git", "status"]}))
        self.assertEqual(result.status, ResultStatus.UNAVAILABLE)

    def test_without_invoke_prefix_is_unverified(self):
        fake = make_fake_executable(self.root, "openclaw-fake")
        self.config.data["adapters"]["openclaw"]["binary"] = str(fake)
        adapter = OpenClawAdapter(self.config)
        health = adapter.discover(force=True)
        self.assertEqual(health.status, AdapterStatus.INSTALLED_BUT_UNVERIFIED)

    def test_allowlisted_command_executes(self):
        fake = make_fake_executable(self.root, "openclaw-fake")
        adapter = self._configured_adapter(fake)
        self.assertEqual(adapter.discover(force=True).status, AdapterStatus.AVAILABLE)
        result = adapter.execute(self.request(action="automation_task", metadata={"argv": ["git", "status"]}))
        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertIn("executed", result.stdout)

    def test_dangerous_command_denied_by_guard(self):
        fake = make_fake_executable(self.root, "openclaw-fake")
        adapter = self._configured_adapter(fake)
        adapter.discover(force=True)
        result = adapter.execute(self.request(action="automation_task", metadata={"argv": ["rm", "-rf", "/"]}))
        self.assertEqual(result.status, ResultStatus.DENIED)

    def test_high_risk_command_needs_approval(self):
        fake = make_fake_executable(self.root, "openclaw-fake")
        adapter = self._configured_adapter(fake)
        adapter.discover(force=True)
        denied = adapter.execute(
            self.request(action="automation_task", metadata={"argv": ["git", "push", "origin", "main"]})
        )
        self.assertEqual(denied.status, ResultStatus.DENIED)
        approved_request = self.request(
            action="automation_task",
            metadata={"argv": ["git", "push", "origin", "main"]},
        )
        approved_request.approved = True
        approved = adapter.execute(approved_request)
        self.assertEqual(approved.status, ResultStatus.SUCCESS)

    def test_missing_argv_is_unverified(self):
        fake = make_fake_executable(self.root, "openclaw-fake")
        adapter = self._configured_adapter(fake)
        adapter.discover(force=True)
        result = adapter.execute(self.request(action="automation_task"))
        self.assertEqual(result.status, ResultStatus.UNVERIFIED)

    def test_no_guard_fails_closed(self):
        fake = make_fake_executable(self.root, "openclaw-fake")
        self.config.data["adapters"]["openclaw"]["binary"] = str(fake)
        self.config.data["adapters"]["openclaw"]["invoke_prefix"] = [str(fake), "exec"]
        adapter = OpenClawAdapter(self.config, guard=None)
        adapter.discover(force=True)
        result = adapter.execute(self.request(action="automation_task", metadata={"argv": ["git", "status"]}))
        self.assertEqual(result.status, ResultStatus.DENIED)


class GuardForTests:
    def __init__(self, config):
        from core.guard import Guard

        self._guard = Guard(config)

    def check_command(self, argv):
        return self._guard.check_command(argv)


if __name__ == "__main__":
    unittest.main()
