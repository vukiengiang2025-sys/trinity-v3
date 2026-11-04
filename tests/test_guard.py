from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tests.support import make_config

from core.guard import Guard


class GuardPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = make_config(self.root)
        self.workspace = self.config.workspace_root
        self.guard = Guard(self.config)

    def test_safe_read_inside_workspace(self):
        decision = self.guard.check_path("docs/readme.md")
        self.assertTrue(decision.allowed)

    def test_write_inside_workspace(self):
        decision = self.guard.check_path("src/new_module.py", write=True)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.risk, "medium")

    def test_parent_traversal_denied(self):
        decision = self.guard.check_path("../../etc/passwd")
        self.assertFalse(decision.allowed)
        self.assertIn("ngoài workspace", decision.reason)

    def test_absolute_outside_workspace_denied(self):
        decision = self.guard.check_path("/etc/passwd")
        self.assertFalse(decision.allowed)

    def test_sensitive_files_denied(self):
        for name in (".env", ".env.local", "id_rsa", "credentials.json", "server.pem", "secrets.yaml"):
            with self.subTest(name=name):
                self.assertFalse(self.guard.check_path(name).allowed)

    def test_git_internal_path_denied(self):
        self.assertFalse(self.guard.check_path(".git/config").allowed)

    def test_symlink_escape_denied(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.workspace / "link"
        try:
            os.symlink(outside, link)
        except OSError as exc:
            self.skipTest(f"không tạo được symlink: {exc}")
        self.assertFalse(self.guard.check_path("link/secret.txt").allowed)

    def test_write_disabled_by_profile(self):
        config = make_config(self.root / "second", overrides={"guard": {"allow_write": False}})
        guard = Guard(config)
        self.assertFalse(guard.check_path("file.txt", write=True).allowed)


class GuardCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = make_config(Path(self.tmp.name))
        self.guard = Guard(self.config)

    def assertDenied(self, command):
        decision = self.guard.check_command(command)
        self.assertFalse(decision.allowed, f"expected denied: {command} -> {decision.reason}")
        return decision

    def assertAllowed(self, command, approval=None):
        decision = self.guard.check_command(command)
        self.assertTrue(decision.allowed, f"expected allowed: {command} -> {decision.reason}")
        if approval is not None:
            self.assertEqual(decision.requires_approval, approval)
        return decision

    def test_safe_git_status_allowed(self):
        decision = self.assertAllowed(["git", "status"], approval=False)
        self.assertEqual(decision.risk, "low")

    def test_dangerous_commands_denied(self):
        for command in (
            ["rm", "-rf", "/"],
            ["sudo", "ls"],
            ["mkfs.ext4", "/dev/sda"],
            ["shutdown", "-h", "now"],
            ["echo", "hello"],
            ["/usr/bin/git", "status"],
        ):
            with self.subTest(command=command):
                self.assertDenied(command)

    def test_shell_metacharacters_denied(self):
        self.assertDenied("git status; rm -rf /")
        self.assertDenied("git status && rm -rf /")
        self.assertDenied("cat /etc/passwd | grep root")
        self.assertDenied("echo $(whoami)")

    def test_git_force_and_history_rewrite_denied(self):
        commands = (
            ["git", "push", "--force", "origin", "main"],
            ["git", "push", "-f", "origin", "main"],
            ["git", "push", "--force-with-lease", "origin", "main"],
            ["git", "push", "origin", "+main"],
            ["git", "reset", "--hard", "HEAD~1"],
            ["git", "clean", "-fd"],
            ["git", "branch", "-D", "main"],
            ["git", "branch", "-d", "main"],
            ["git", "commit", "--amend", "-m", "x"],
            ["git", "filter-repo", "--force"],
            ["git", "remote", "add", "origin", "https://example.com/x.git"],
            ["git", "remote", "set-url", "origin", "https://example.com/x.git"],
            ["git", "config", "user.name", "hacker"],
            ["git", "daemon", "--export-all"],
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertDenied(command)

    def test_git_push_requires_approval(self):
        self.assertAllowed(["git", "push", "origin", "main"], approval=True)
        self.assertAllowed(["git", "push", "origin", "main:main"], approval=True)
        self.assertAllowed(["git", "checkout", "main"], approval=True)
        self.assertAllowed(["git", "switch", "main"], approval=True)
        self.assertAllowed(["git", "rm", "old.py"], approval=True)

    def test_git_add_all_requires_approval(self):
        self.assertAllowed(["git", "add", "."], approval=True)
        self.assertAllowed(["git", "add", "-A"], approval=True)
        self.assertAllowed(["git", "add", "README.md"], approval=False)

    def test_git_config_read_only_allowed(self):
        self.assertAllowed(["git", "config", "--get", "user.name"], approval=False)
        self.assertAllowed(["git", "remote", "-v"], approval=False)

    def test_git_dash_c_blocked(self):
        self.assertDenied(["git", "-c", "core.pager=cat", "status"])
        self.assertDenied(["git", "-C", "/etc", "status"])

    def test_path_arguments_are_validated(self):
        self.assertDenied(["cat", "/etc/passwd"])
        self.assertDenied(["cat", "../../etc/passwd"])
        self.assertDenied(["cat", ".env"])
        self.assertDenied(["grep", "-f/etc/passwd", "root"])
        self.assertDenied(["ls", "/etc"])
        self.assertAllowed(["cat", "README.md"])

    def test_find_dangerous_flags_denied(self):
        self.assertDenied(["find", ".", "-delete"])
        self.assertDenied(["find", ".", "-exec", "rm", "{}", ";"])
        self.assertAllowed(["find", ".", "-name", "*.py"], approval=False)

    def test_python_interpreter_guards(self):
        self.assertDenied(["python3", "-c", "print(1)"])
        self.assertDenied(["python3", "-cprint(1)"])
        self.assertDenied(["python3", "-m", "http.server"])
        self.assertDenied(["python3", "-"])
        self.assertAllowed(["python3", "-m", "pytest"], approval=True)

    def test_absolute_executable_denied(self):
        self.assertDenied(["/bin/ls", "-la"])


if __name__ == "__main__":
    unittest.main()
