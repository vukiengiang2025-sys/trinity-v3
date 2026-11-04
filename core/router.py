"""Rule-based task router.

Routing is intentionally deterministic and cheap: no extra model call is made
just to classify a request.  A route is a *plan*, never an execution
permission; the executor re-checks risk, resources and guard policy.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from .config import AppConfig
from .utils import TrinityError

ROLE = {
    "hermes": "BRAIN",
    "opencode": "LOGIC",
    "openclaw": "MUSCLE",
}
ADAPTER_FOR_CATEGORY = {
    "memory": "hermes",
    "coding": "opencode",
    "automation": "openclaw",
    "general": "opencode",
}
ACTION_FOR_CATEGORY = {
    "memory": "memory_query",
    "coding": "code_task",
    "automation": "automation_task",
    "general": "general_task",
}


class RouterError(TrinityError):
    pass


@dataclass
class Route:
    task_id: str
    task_type: str
    selected_adapter: str
    execution_plan: List[Dict[str, str]]
    risk_level: str
    matched_rules: List[str] = field(default_factory=list)
    approval_required: bool = False
    raw_prompt: str = ""

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "selected_adapter": self.selected_adapter,
            "execution_plan": list(self.execution_plan),
            "risk_level": self.risk_level,
            "matched_rules": list(self.matched_rules),
            "approval_required": self.approval_required,
        }


MEMORY_KEYWORDS = (
    "memory",
    "ghi nho",
    "luu nho",
    "luu memory",
    "truy xuat",
    "ky uc",
    "nho lai",
    "da tung",
    "skill",
    "hermes",
    "brain",
    "context",
    "tom tat lich su",
    "ho so ca nhan",
)
CODING_KEYWORDS = (
    "code",
    "codebase",
    "coding",
    "lap trinh",
    "sua loi",
    "bug",
    "refactor",
    "viet ham",
    "viet test",
    "unit test",
    "pytest",
    "unittest",
    "python",
    "javascript",
    "typescript",
    "java",
    "kotlin",
    "android",
    "compile",
    "build",
    "debug",
    "review code",
    "api",
    "implement",
    "tinh nang",
    "module",
    "function",
    "ham",
    "test",
)
AUTOMATION_KEYWORDS = (
    "chay lenh",
    "shell",
    "terminal",
    "bash",
    "cron",
    "systemd",
    "tu dong hoa",
    "automation",
    "cai dat",
    "go cai",
    "backup",
    "sao luu",
    "git",
    "push",
    "pull",
    "deploy",
    "quan ly tien trinh",
    "kill process",
    "openclaw",
    "gateway",
    "cli",
    "lenh he thong",
    "file he thong",
    "mount",
    "usb",
    "adb",
    "system",
)
HIGH_RISK_KEYWORDS = (
    "xoa",
    "xoa bo",
    "delete",
    "rm -rf",
    "reset --hard",
    "force push",
    "push --force",
    "format",
    "drop database",
    "drop table",
    "sudo",
    "root",
    "production",
    "prod",
    "deploy",
    "shutdown",
    "reboot",
    "ghi de",
    "overwrite",
    "rewrite history",
    "cai dat he thong",
    "go cai he thong",
    "thay doi ssh",
    "api key",
    "token",
    "mat khau",
    "password",
    "secret",
    "credentials",
)
MEDIUM_RISK_KEYWORDS = (
    "sua",
    "chinh sua",
    "edit",
    "write",
    "tao file",
    "tao thu muc",
    "commit",
    "push",
    "cai dat",
    "install",
    "chmod",
    "chown",
    "cron",
    "systemd",
    "git",
    "backup",
    "sao luu",
    "di chuyen",
    "copy",
    "rename",
    "build",
    "cap nhat",
    "update",
    "refactor",
    "code",
    "test",
)
STORE_KEYWORDS = ("luu", "ghi nho", "store", "save", "them vao memory")
NEGATION_RE = re.compile(r"\b(?:khong(?:\s+(?:duoc|cho\s+phep))?|no|not|do\s+not|don't|without)\b")


def normalize(text: str) -> str:
    lowered = (text or "").lower().replace("đ", "d")
    decomposed = unicodedata.normalize("NFD", lowered)
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn")


def _hits(text: str, keywords: Iterable[str]) -> List[str]:
    found: List[str] = []
    for keyword in keywords:
        pattern = re.escape(keyword)
        regex = re.compile(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])")
        for match in regex.finditer(text):
            if not _is_negated(text, match.start()):
                found.append(keyword)
                break
    return found


def _is_negated(text: str, start: int) -> bool:
    """Skip a keyword when the same clause explicitly negates it.

    This prevents "không chạy shell" from adding an AUTOMATION step while
    keeping a positive mention elsewhere in the prompt effective.
    """

    prefix = text[max(0, start - 60) : start]
    clause = re.split(r"[,.;!?]", prefix)[-1]
    return bool(NEGATION_RE.search(clause))


class Router:
    def __init__(self, config: AppConfig):
        self.config = config

    def route(self, prompt: str, task_id: Optional[str] = None) -> Route:
        if not isinstance(prompt, str) or not prompt.strip():
            raise RouterError("nội dung nhiệm vụ không được rỗng")
        normalized = normalize(prompt)
        hits: Dict[str, List[str]] = {
            "memory": _hits(normalized, MEMORY_KEYWORDS),
            "coding": _hits(normalized, CODING_KEYWORDS),
            "automation": _hits(normalized, AUTOMATION_KEYWORDS),
        }
        categories = [name for name in ("memory", "coding", "automation") if hits[name]]

        if not categories:
            task_type = "GENERAL"
            categories = ["general"]
        elif len(categories) > 1:
            task_type = "MIXED"
        else:
            task_type = categories[0].upper()

        selected_adapter = "multi" if task_type == "MIXED" else ADAPTER_FOR_CATEGORY[categories[0]]
        plan = self._build_plan(categories, normalized)
        risk = self._risk(normalized, task_type)

        return Route(
            task_id=task_id or self.new_task_id(),
            task_type=task_type,
            selected_adapter=selected_adapter,
            execution_plan=plan,
            risk_level=risk,
            matched_rules=[f"{name}:{','.join(values)}" for name, values in hits.items() if values],
            approval_required=risk in self.config.approval_risks,
            raw_prompt=prompt,
        )

    @staticmethod
    def new_task_id() -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return f"TRN-{stamp}-{secrets.token_hex(3)}"

    def _build_plan(self, categories: List[str], normalized: str) -> List[Dict[str, str]]:
        # Canonical order: recall context (BRAIN), change code (LOGIC), then
        # touch the system (MUSCLE).  Execution is strictly sequential.
        if categories == ["general"]:
            ordered = ["general"]
        else:
            ordered = [name for name in ("memory", "coding", "automation") if name in categories]
        plan: List[Dict[str, str]] = []
        for index, category in enumerate(ordered, start=1):
            adapter = ADAPTER_FOR_CATEGORY[category]
            action = ACTION_FOR_CATEGORY[category]
            if category == "memory" and _hits(normalized, STORE_KEYWORDS):
                action = "memory_store"
            plan.append(
                {
                    "step": str(index),
                    "category": category,
                    "adapter": adapter,
                    "role": ROLE.get(adapter, "SUPPORT"),
                    "action": action,
                    "reason": {
                        "memory": "truy xuất/lưu ngữ cảnh và kỹ năng",
                        "coding": "đọc, phân tích, sửa mã và kiểm thử",
                        "automation": "thao tác hệ thống trong allowlist",
                        "general": "xử lý yêu cầu thông thường",
                    }[category],
                }
            )
        return plan

    def _risk(self, normalized: str, task_type: str) -> str:
        if _hits(normalized, HIGH_RISK_KEYWORDS):
            return "high"
        if _hits(normalized, MEDIUM_RISK_KEYWORDS):
            return "medium"
        if task_type in ("CODING", "AUTOMATION", "MIXED"):
            return "medium"
        return "low"
