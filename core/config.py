"""Profile loading and effective configuration for Trinity.

Two profiles are shipped: ``linux`` (Linux Mint/Debian/Ubuntu) and ``termux``
(Android Termux).  A profile is data, not code; adapters and guards read the
same keys regardless of platform.
"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .utils import ConfigError, mask_mapping, which

VERSION = "3.0.0"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

DEFAULTS: Dict[str, Any] = {
    "version": 1,
    "platform": {
        "name": "linux",
        "display_name": "Linux",
    },
    "workspace": {
        "root": ".",
        "allowed_roots": ["."],
    },
    "storage": {
        "db_path": "~/.trinity/trinity.db",
        "log_dir": "logs",
        "max_result_chars": 20000,
    },
    "resources": {
        "min_free_ram_mb": 384,
        "min_free_disk_mb": 200,
        "max_concurrent_tasks": 1,
        "default_timeout_seconds": 900,
        "max_timeout_seconds": 3600,
        "lock_path": "logs/.trinity.lock",
        "on_low_memory": "deny",
    },
    "guard": {
        "allowed_commands": [
            "git",
            "ls",
            "pwd",
            "cat",
            "head",
            "tail",
            "wc",
            "grep",
            "find",
            "python3",
            "pytest",
        ],
        "require_approval_for_risk": ["high"],
        "max_command_seconds": 120,
        "allow_write": True,
    },
    "adapters": {
        "opencode": {
            "enabled": True,
            "binary": "opencode",
            "mode": "cli",
            "http_enabled": False,
            "http_url": "http://127.0.0.1:4096",
            "http_health_path": "/global/health",
            "timeout_seconds": 900,
            "format": "json",
            "model": None,
            "agent": None,
        },
        "hermes": {
            "enabled": True,
            "binary": "hermes",
            "timeout_seconds": 300,
            "memory_path": None,
            "memory_search_command": [],
            "memory_store_command": [],
        },
        "openclaw": {
            "enabled": True,
            "binary": "openclaw",
            "timeout_seconds": 300,
            "gateway_enabled": False,
            "gateway_url": "http://127.0.0.1:18789",
            "gateway_health_path": "/health",
            "invoke_prefix": [],
            "command_allowlist": [
                "git",
                "ls",
                "pwd",
                "cat",
                "grep",
                "find",
                "python3",
            ],
        },
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def is_termux() -> bool:
    """Detect Termux without assuming any particular Android layout."""

    prefix = os.environ.get("PREFIX", "")
    if "com.termux" in prefix:
        return True
    return Path("/data/data/com.termux/files/usr").exists()


def detect_profile() -> str:
    env_profile = os.environ.get("TRINITY_PROFILE", "").strip()
    if env_profile:
        return env_profile
    return "termux" if is_termux() else "linux"


def _resolve_path(value: str, project_root: Path) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


@dataclass
class AppConfig:
    profile: str
    data: Dict[str, Any]
    source_path: Path
    project_root: Path
    warnings: List[str] = field(default_factory=list)

    # -- generic access -------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        current: Any = self.data
        for part in dotted.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current

    def section(self, name: str) -> Dict[str, Any]:
        value = self.data.get(name, {})
        return value if isinstance(value, dict) else {}

    def adapter(self, name: str) -> Dict[str, Any]:
        adapters = self.section("adapters")
        value = adapters.get(name, {})
        return value if isinstance(value, dict) else {}

    # -- resolved paths -------------------------------------------------
    @property
    def workspace_root(self) -> Path:
        return _resolve_path(str(self.get("workspace.root", ".")), self.project_root)

    @property
    def allowed_roots(self) -> List[Path]:
        raw = self.get("workspace.allowed_roots", ["."])
        if not isinstance(raw, list) or not raw:
            raise ConfigError("workspace.allowed_roots phải là danh sách không rỗng")
        roots = [_resolve_path(str(item), self.workspace_root) for item in raw]
        # ``allowed_roots`` entries are relative to the workspace root, not the
        # project root; this keeps ``[".", ".."]`` predictable in profiles.
        return roots

    @property
    def db_path(self) -> Path:
        override = os.environ.get("TRINITY_DB_PATH", "").strip()
        if override:
            return _resolve_path(override, self.project_root)
        return _resolve_path(str(self.get("storage.db_path", "~/.trinity/trinity.db")), self.project_root)

    @property
    def log_dir(self) -> Path:
        override = os.environ.get("TRINITY_LOG_DIR", "").strip()
        if override:
            return _resolve_path(override, self.project_root)
        return _resolve_path(str(self.get("storage.log_dir", "logs")), self.project_root)

    @property
    def lock_path(self) -> Path:
        return _resolve_path(str(self.get("resources.lock_path", "logs/.trinity.lock")), self.project_root)

    @property
    def timeout_seconds(self) -> int:
        return max(1, int(self.get("resources.default_timeout_seconds", 900)))

    @property
    def max_timeout_seconds(self) -> int:
        return max(self.timeout_seconds, int(self.get("resources.max_timeout_seconds", 3600)))

    @property
    def approval_risks(self) -> set:
        raw = self.get("guard.require_approval_for_risk", ["high"])
        return {str(item).lower() for item in raw}

    @property
    def min_free_ram_mb(self) -> int:
        return int(self.get("resources.min_free_ram_mb", 384))

    @property
    def min_free_disk_mb(self) -> int:
        return int(self.get("resources.min_free_disk_mb", 200))

    def public_dict(self) -> Dict[str, Any]:
        """Effective config safe to print: masked and with resolved paths."""

        data = mask_mapping(copy.deepcopy(self.data))
        data["_meta"] = {
            "profile": self.profile,
            "source": str(self.source_path),
            "project_root": str(self.project_root),
            "workspace_root": str(self.workspace_root),
            "allowed_roots": [str(item) for item in self.allowed_roots],
            "db_path": str(self.db_path),
            "log_dir": str(self.log_dir),
            "warnings": list(self.warnings),
        }
        return data


def load_config(
    profile: Optional[str] = None,
    config_dir: Optional[Path] = None,
    project_root: Optional[Path] = None,
) -> AppConfig:
    """Load ``config/<profile>.yaml`` and merge it over safe defaults."""

    root = Path(project_root or PROJECT_ROOT).resolve()
    cfg_dir = Path(config_dir or (root / "config")).resolve()
    selected = (profile or detect_profile()).strip().lower()
    if not _PROFILE_RE.match(selected):
        raise ConfigError(f"tên profile không hợp lệ: {selected!r}")

    path = cfg_dir / f"{selected}.yaml"
    if not path.is_file():
        available = sorted(item.stem for item in cfg_dir.glob("*.yaml"))
        raise ConfigError(
            f"không tìm thấy profile {selected!r} tại {path}. "
            f"Profile có sẵn: {', '.join(available) or 'không có'}"
        )

    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML không hợp lệ trong {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"không đọc được {path}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} phải chứa một YAML mapping")

    data = _deep_merge(DEFAULTS, loaded)
    data["profile"] = selected
    warnings: List[str] = []

    if loaded.get("profile") not in (None, selected):
        warnings.append(
            f"file ghi profile={loaded.get('profile')!r} nhưng đang nạp {selected!r}; dùng tên file làm chuẩn"
        )

    allowed = data.get("guard", {}).get("allowed_commands")
    if not isinstance(allowed, list) or not allowed:
        raise ConfigError("guard.allowed_commands phải là danh sách không rỗng")
    if not all(isinstance(item, str) and item.strip() for item in allowed):
        raise ConfigError("guard.allowed_commands chỉ được chứa chuỗi không rỗng")

    if not isinstance(data.get("workspace", {}).get("allowed_roots"), list):
        raise ConfigError("workspace.allowed_roots phải là danh sách")

    config = AppConfig(profile=selected, data=data, source_path=path, project_root=root, warnings=warnings)

    # Validate resolved paths early so commands fail with a clear message.
    _ = config.workspace_root
    _ = config.allowed_roots
    _ = config.db_path
    _ = config.log_dir

    if not which(str(data.get("adapters", {}).get("opencode", {}).get("binary", "opencode"))):
        warnings.append("OpenCode chưa có trong PATH; adapter LOGIC sẽ báo UNAVAILABLE")
    return config
