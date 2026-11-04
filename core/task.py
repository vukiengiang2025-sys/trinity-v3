"""Task Manager facade.

The SQLite implementation lives in :mod:`storage.memory` because it is a
storage concern.  Core modules and tests import the public API from here so
the implementation can be replaced without touching the executor.
"""

from storage.memory import (  # noqa: F401
    ALLOWED_TRANSITIONS,
    CANCELLED,
    COMPLETED,
    FAILED,
    PENDING,
    RUNNING,
    STATUSES,
    TERMINAL_STATUSES,
    WAITING_APPROVAL,
    AuditLog,
    TaskManager,
)

__all__ = [
    "TaskManager",
    "AuditLog",
    "PENDING",
    "RUNNING",
    "WAITING_APPROVAL",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "STATUSES",
    "TERMINAL_STATUSES",
    "ALLOWED_TRANSITIONS",
]
