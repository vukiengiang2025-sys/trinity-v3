from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from tests.support import PROJECT_ROOT


class StaticSecurityTests(unittest.TestCase):
    def source_files(self):
        for path in PROJECT_ROOT.rglob("*.py"):
            if any(part in {".git", "__pycache__", ".venv", "venv"} for part in path.parts):
                continue
            if path.name == "test_security_static.py":
                continue
            yield path

    def test_no_shell_true_or_os_system(self):
        for path in self.source_files():
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name == "system" and isinstance(func, ast.Attribute):
                    owner = getattr(func.value, "id", "")
                    self.assertNotEqual(owner, "os", f"os.system in {path}")
                if name in {"Popen", "run", "call", "check_call", "check_output"}:
                    for keyword in node.keywords:
                        if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant):
                            self.assertIsNot(keyword.value.value, True, f"shell=True in {path}")

    def test_no_obvious_credential_literals(self):
        patterns = re.compile(
            r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
            r"sk-[A-Za-z0-9]{12,}|AKIA[0-9A-Z]{16}|"
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
        )
        for path in self.source_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            self.assertIsNone(patterns.search(text), f"credential-like literal in {path}")


if __name__ == "__main__":
    unittest.main()
