"""Safety Guard: workspace containment, path and command policy.

The guard is deliberately conservative.  It is not a sandbox: it cannot make
an arbitrary interpreter safe.  It prevents the common destructive mistakes
and forces explicit approval for broader operations.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Union

from .config import AppConfig

RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

BLOCKED_DIRS = {".git", ".ssh", ".gnupg", ".aws", ".kube", ".docker"}
SENSITIVE_NAMES = {
    ".env",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "secrets",
    "secrets.json",
}
SENSITIVE_PREFIXES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials", "secrets", ".env")
SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".asc", ".ppk")

PATH_VALUE_OPTIONS = {
    "-C",
    "--git-dir",
    "--work-tree",
    "-o",
    "--output",
    "-f",
    "--file",
    "--path",
    "--directory",
    "--prefix",
    "--out",
}
DANGEROUS_FIND_OPTIONS = {
    "-exec",
    "-execdir",
    "-delete",
    "-ok",
    "-okdir",
    "-fprint",
    "-fprint0",
    "-fprintf",
    "-fls",
}
READ_ONLY_COMMANDS = {"ls", "pwd", "cat", "head", "tail", "wc", "grep", "find"}

GIT_DENIED_SUBCOMMANDS = {
    "reset",
    "clean",
    "filter-repo",
    "filter-branch",
    "gc",
    "prune",
    "update-ref",
    "symbolic-ref",
}
GIT_READ_SUBCOMMANDS = {
    "status",
    "diff",
    "log",
    "show",
    "rev-parse",
    "ls-files",
    "ls-tree",
    "blame",
    "grep",
    "describe",
    "shortlog",
    "whatchanged",
    "version",
    "fsck",
}
GIT_WRITE_SUBCOMMANDS = {
    "add",
    "commit",
    "checkout",
    "switch",
    "merge",
    "rebase",
    "cherry-pick",
    "revert",
    "stash",
    "tag",
    "clone",
    "pull",
    "fetch",
    "init",
    "mv",
    "rm",
    "restore",
}
GIT_MEDIUM_RISK = {"add", "commit", "checkout", "switch", "merge", "stash", "tag", "clone", "init", "mv"}
GIT_HIGH_RISK = {"rebase", "cherry-pick", "revert", "pull", "restore", "push", "checkout", "switch", "rm"}


@dataclass
class GuardDecision:
    allowed: bool
    reason: str
    risk: str = "low"
    requires_approval: bool = False
    kind: str = "generic"

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "risk": self.risk,
            "requires_approval": self.requires_approval,
            "kind": self.kind,
        }


class Guard:
    """Stateless policy evaluator; it never executes anything."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.workspace_root = config.workspace_root
        self.allowed_roots = config.allowed_roots
        self.allowed_commands = {str(item) for item in config.get("guard.allowed_commands", [])}
        self.allow_write = bool(config.get("guard.allow_write", True))
        self.approval_risks = config.approval_risks
        self.max_command_seconds = int(config.get("guard.max_command_seconds", 120))

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------
    def check_path(self, path: Union[str, Path], write: bool = False) -> GuardDecision:
        raw = str(path)
        kind = "write" if write else "read"
        if not raw.strip():
            return GuardDecision(False, "đường dẫn rỗng", "medium", True, kind)
        if "\x00" in raw:
            return GuardDecision(False, "đường dẫn chứa ký tự NUL", "high", True, kind)
        if write and not self.allow_write:
            return GuardDecision(False, "profile này không cho phép ghi file", "high", True, kind)

        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            return GuardDecision(False, "không thể phân giải đường dẫn", "medium", False, kind)

        inside = False
        for root in self.allowed_roots:
            try:
                if resolved == root or os.path.commonpath([str(resolved), str(root)]) == str(root):
                    inside = True
                    break
            except ValueError:
                continue
        if not inside:
            return GuardDecision(
                False,
                f"đường dẫn nằm ngoài workspace cho phép: {resolved}",
                "high",
                False,
                kind,
            )

        lowered_parts = [part.lower() for part in resolved.parts]
        for part in lowered_parts:
            if part in BLOCKED_DIRS:
                return GuardDecision(False, f"không cho phép truy cập thư mục nhạy cảm: {part}", "high", False, kind)
        if self._is_sensitive_name(resolved.name):
            return GuardDecision(
                False,
                f"từ chối file nhạy cảm: {resolved.name}",
                "high",
                False,
                kind,
            )

        if self._contains_symlink_escape(candidate):
            return GuardDecision(False, "đường dẫn đi qua symlink ra ngoài workspace", "high", False, kind)

        risk = "low" if not write else "medium"
        return GuardDecision(True, f"{kind} trong workspace: {resolved}", risk, False, kind)

    def _contains_symlink_escape(self, candidate: Path) -> bool:
        """Reject a path whose existing symlink points outside allowed roots."""

        try:
            current = candidate.absolute()
            while True:
                if current.exists() and current.is_symlink():
                    target = current.resolve(strict=False)
                    return not any(
                        target == root or os.path.commonpath([str(target), str(root)]) == str(root)
                        for root in self.allowed_roots
                    )
                if current.parent == current:
                    return False
                current = current.parent
        except (OSError, ValueError):
            return True

    @staticmethod
    def _is_sensitive_name(name: str) -> bool:
        lowered = name.strip().lower()
        if not lowered:
            return False
        if lowered in SENSITIVE_NAMES:
            return True
        if lowered.startswith(SENSITIVE_PREFIXES):
            return True
        if lowered.endswith(SENSITIVE_SUFFIXES):
            return True
        if "credential" in lowered or "secret" in lowered or "private_key" in lowered:
            return True
        return False

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    def check_command(self, command: Union[str, Sequence[str]]) -> GuardDecision:
        if isinstance(command, str):
            raw = command.strip()
            if not raw:
                return GuardDecision(False, "lệnh rỗng", "medium", True, "command")
            if any(char in raw for char in (";", "|", "&", "`", "$", ">", "<", "\n", "\r", "\x00")):
                return GuardDecision(
                    False,
                    "từ chối shell metacharacter; hãy truyền argv dạng danh sách, không dùng shell",
                    "high",
                    True,
                    "command",
                )
            try:
                argv = shlex.split(raw, posix=True)
            except ValueError as exc:
                return GuardDecision(False, f"không phân tích được lệnh: {exc}", "medium", True, "command")
        else:
            argv = [str(item) for item in command]
            if any("\x00" in item for item in argv):
                return GuardDecision(False, "đối số chứa ký tự NUL", "high", True, "command")

        if not argv:
            return GuardDecision(False, "lệnh rỗng", "medium", True, "command")
        executable = argv[0]
        if not executable or "/" in executable or "\\" in executable:
            return GuardDecision(
                False,
                "executable phải là tên ngắn nằm trong allowlist, không dùng đường dẫn",
                "high",
                True,
                "command",
            )
        if executable not in self.allowed_commands:
            return GuardDecision(
                False,
                f"lệnh {executable!r} không nằm trong allowlist",
                "high",
                True,
                "command",
            )

        risk = self._command_risk(executable, argv[1:])
        write = self._is_write_command(executable, argv[1:])
        git_requires = False
        git_reason = ""

        if executable == "git":
            git_decision = self._check_git(argv[1:], write)
            if not git_decision.allowed:
                return git_decision
            risk = max((risk, git_decision.risk), key=lambda item: RISK_ORDER[item])
            git_requires = git_decision.requires_approval
            git_reason = git_decision.reason

        argument_decision = self._check_arguments(executable, argv[1:], write)
        if not argument_decision.allowed:
            return argument_decision

        requires = risk in self.approval_risks or argument_decision.requires_approval or git_requires
        reason = git_reason or argument_decision.reason or f"lệnh được allowlist: {executable}"
        return GuardDecision(True, reason, risk, requires, "command")

    def _command_risk(self, executable: str, args: Sequence[str]) -> str:
        if executable in ("python3", "pytest"):
            return "high"
        if executable == "git":
            return self._git_risk(args)
        if executable in READ_ONLY_COMMANDS:
            return "low"
        return "medium"

    def _is_write_command(self, executable: str, args: Sequence[str]) -> bool:
        if executable in ("python3", "pytest"):
            return True
        if executable == "git":
            sub = self._git_subcommand(args)
            return sub in GIT_WRITE_SUBCOMMANDS or sub in GIT_HIGH_RISK or sub in {"push", "rm", "mv", "restore"}
        return False

    def _check_arguments(self, executable: str, args: Sequence[str], write: bool) -> GuardDecision:
        if executable == "find":
            for arg in args:
                if arg in DANGEROUS_FIND_OPTIONS:
                    return GuardDecision(False, f"find không được dùng {arg}", "high", True, "command")

        if executable == "python3":
            if any(item == "-c" or (item.startswith("-c") and item != "-c") for item in args):
                return GuardDecision(False, "python3 -c bị chặn (thực thi mã tùy ý)", "high", True, "command")
            if "-" in args:
                return GuardDecision(False, "python3 đọc mã từ stdin bị chặn", "high", True, "command")
            if "-m" in args:
                index = args.index("-m")
                module = args[index + 1] if index + 1 < len(args) else ""
                if module not in {"pytest", "unittest", "compileall", "json.tool"}:
                    return GuardDecision(
                        False,
                        f"python3 -m {module or '(thiếu module)'} không nằm trong allowlist module",
                        "high",
                        True,
                        "command",
                    )

        index = 0
        while index < len(args):
            arg = args[index]
            if arg in PATH_VALUE_OPTIONS and index + 1 < len(args):
                decision = self.check_path(args[index + 1], write=write)
                if not decision.allowed:
                    return decision
                index += 2
                continue
            if "=" in arg and arg.startswith("-"):
                value = arg.split("=", 1)[1]
                if self._looks_like_path(value):
                    decision = self.check_path(value, write=write)
                    if not decision.allowed:
                        return decision
            for short_option in ("-o", "-f"):
                if arg.startswith(short_option) and len(arg) > 2 and not arg.startswith("--"):
                    attached = arg[2:]
                    if self._looks_like_path(attached):
                        decision = self.check_path(attached, write=write)
                        if not decision.allowed:
                            return decision
            if self._looks_like_path(arg) or self._is_sensitive_name(Path(arg).name):
                decision = self.check_path(arg, write=write)
                if not decision.allowed:
                    return decision
            index += 1

        return GuardDecision(True, "", "low", False, "arguments")

    @staticmethod
    def _looks_like_path(value: str) -> bool:
        if not value:
            return False
        if value in (".", ".."):
            return True
        return (
            value.startswith(("/", "~", "./", "../"))
            or "/../" in value
            or value.endswith("/..")
            or ("/" in value and not value.startswith("-"))
        )

    # ------------------------------------------------------------------
    # Git policy
    # ------------------------------------------------------------------
    def _check_git(self, args: Sequence[str], write: bool) -> GuardDecision:
        global_args: list = []
        index = 0
        while index < len(args) and args[index].startswith("-"):
            arg = args[index]
            if arg in ("-C", "--git-dir", "--work-tree"):
                if index + 1 >= len(args):
                    return GuardDecision(False, f"git {arg} thiếu giá trị", "high", True, "git")
                decision = self.check_path(args[index + 1], write=False)
                if not decision.allowed:
                    return decision
                global_args.extend([arg, args[index + 1]])
                index += 2
                continue
            if arg.startswith(("-C", "--git-dir=", "--work-tree=")):
                value = arg.split("=", 1)[1] if "=" in arg else arg[2:]
                if value:
                    decision = self.check_path(value, write=False)
                    if not decision.allowed:
                        return decision
            if arg in ("-c", "--exec-path") or arg.startswith(("-c=", "--exec-path=")):
                return GuardDecision(False, f"git {arg} bị chặn vì có thể thay đổi hành vi", "high", True, "git")
            global_args.append(arg)
            index += 1

        sub = args[index] if index < len(args) else ""
        rest = list(args[index + 1 :])

        if not sub:
            return GuardDecision(True, "git không có subcommand", "low", False, "git")
        if sub in GIT_DENIED_SUBCOMMANDS:
            return GuardDecision(False, f"git {sub} bị chặn vĩnh viễn", "critical", True, "git")

        if sub == "branch":
            if any(arg in ("-d", "-D", "--delete") or arg.startswith("--delete=") for arg in rest):
                return GuardDecision(False, "git branch xóa nhánh bị chặn", "critical", True, "git")
            return GuardDecision(True, "git branch chỉ đọc/liệt kê", "low", False, "git")

        if sub == "remote":
            if any(arg in ("add", "remove", "rm", "set-url", "rename", "prune") for arg in rest):
                return GuardDecision(False, "không cho phép thay đổi remote Git", "critical", True, "git")
            return GuardDecision(True, "git remote chỉ đọc", "low", False, "git")

        if sub == "config":
            read_flags = {"--get", "--get-all", "--get-regexp", "--list", "-l", "--show-origin"}
            if any(arg in read_flags for arg in rest):
                return GuardDecision(True, "git config chỉ đọc", "low", False, "git")
            return GuardDecision(False, "ghi cấu hình Git bị chặn", "critical", True, "git")

        if sub == "push":
            for arg in rest:
                lowered = arg.lower()
                if (
                    lowered in ("-f", "--force", "--force-with-lease", "--force-if-includes", "--mirror", "--delete", "-d")
                    or lowered.startswith("--force-with-lease=")
                    or lowered.startswith("--force=")
                    or arg.startswith(("+", ":"))
                ):
                    return GuardDecision(False, f"force/delete push bị chặn: {arg}", "critical", True, "git")
            return GuardDecision(True, "git push cần phê duyệt rõ ràng", "high", True, "git")

        if sub == "commit":
            if "--amend" in rest:
                return GuardDecision(False, "git commit --amend bị chặn", "critical", True, "git")
            return GuardDecision(True, "git commit", "medium", False, "git")

        if sub == "add":
            if any(arg in (".", "-A", "--all") for arg in rest):
                return GuardDecision(
                    True,
                    "git add toàn bộ cần phê duyệt để tránh stage nhầm file nhạy cảm",
                    "high",
                    True,
                    "git",
                )
            return GuardDecision(True, "git add đường dẫn cụ thể", "medium", False, "git")

        if sub == "tag":
            if any(arg in ("-d", "--delete") for arg in rest):
                return GuardDecision(False, "xóa tag bị chặn", "critical", True, "git")
            return GuardDecision(True, "git tag", "medium", False, "git")

        if sub == "stash":
            if any(arg in ("drop", "clear") for arg in rest):
                return GuardDecision(False, "xóa stash bị chặn", "critical", True, "git")
            return GuardDecision(True, "git stash", "medium", False, "git")

        if sub in GIT_DENIED_SUBCOMMANDS:
            return GuardDecision(False, f"git {sub} bị chặn", "critical", True, "git")
        if sub in GIT_READ_SUBCOMMANDS or sub == "fetch":
            risk = "medium" if sub == "fetch" else "low"
            return GuardDecision(True, f"git {sub}", risk, False, "git")
        if sub in ("push", "rebase", "cherry-pick", "revert", "pull", "restore", "checkout", "switch", "rm"):
            return GuardDecision(True, f"git {sub} cần phê duyệt", "high", True, "git")
        if sub in GIT_WRITE_SUBCOMMANDS or sub in GIT_MEDIUM_RISK:
            return GuardDecision(True, f"git {sub}", "medium", False, "git")
        return GuardDecision(False, f"git {sub} chưa nằm trong allowlist", "high", True, "git")

    def _git_risk(self, args: Sequence[str]) -> str:
        sub = self._git_subcommand(args)
        if sub in GIT_DENIED_SUBCOMMANDS:
            return "critical"
        if sub == "push" or sub in {"rebase", "cherry-pick", "revert", "pull", "restore", "checkout", "switch", "rm"}:
            return "high"
        if sub in GIT_MEDIUM_RISK or sub in GIT_WRITE_SUBCOMMANDS:
            return "medium"
        if sub == "fetch":
            return "medium"
        return "low"

    @staticmethod
    def _git_subcommand(args: Sequence[str]) -> str:
        index = 0
        while index < len(args):
            arg = args[index]
            if arg in ("-C", "--git-dir", "--work-tree"):
                index += 2
                continue
            if arg.startswith("-"):
                index += 1
                continue
            return arg
        return ""
