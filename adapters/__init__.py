"""Trinity adapters: BRAIN (Hermes), LOGIC (OpenCode), MUSCLE (OpenClaw)."""

from .base import AdapterHealth, AdapterRequest, AdapterResult, AdapterStatus, BaseAdapter, ResultStatus
from .hermes import HermesAdapter
from .openclaw import OpenClawAdapter
from .opencode import OpenCodeAdapter

__all__ = [
    "BaseAdapter",
    "AdapterHealth",
    "AdapterRequest",
    "AdapterResult",
    "AdapterStatus",
    "ResultStatus",
    "HermesAdapter",
    "OpenCodeAdapter",
    "OpenClawAdapter",
]
