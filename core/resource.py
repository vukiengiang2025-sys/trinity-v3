"""Resource Guard: RAM/disk checks and the sequential execution lock."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import List, Optional

from .config import AppConfig
from .utils import FileLock, ResourceError, utc_now


@dataclass
class ResourceSnapshot:
    total_mb: Optional[float]
    available_mb: Optional[float]
    readable: bool
    source: str
    checked_at: str = field(default_factory=utc_now)

    def as_dict(self) -> dict:
        return {
            "total_mb": round(self.total_mb, 1) if self.total_mb is not None else None,
            "available_mb": round(self.available_mb, 1) if self.available_mb is not None else None,
            "readable": self.readable,
            "source": self.source,
            "checked_at": self.checked_at,
        }


@dataclass
class ResourceDecision:
    allowed: bool
    reason: str
    warnings: List[str]
    snapshot: ResourceSnapshot
    disk_free_mb: Optional[float]
    dry_run: bool = False

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "warnings": list(self.warnings),
            "memory": self.snapshot.as_dict(),
            "disk_free_mb": round(self.disk_free_mb, 1) if self.disk_free_mb is not None else None,
            "dry_run": self.dry_run,
        }


def read_memory() -> ResourceSnapshot:
    """Read available RAM without ever treating an error as zero free RAM."""

    total_kb: Optional[float] = None
    available_kb: Optional[float] = None
    free_kb: Optional[float] = None
    buffers_kb: Optional[float] = None
    cached_kb: Optional[float] = None
    source = "unknown"

    try:
        with open("/proc/meminfo", "r", encoding="ascii", errors="replace") as handle:
            for line in handle:
                key, _, value = line.partition(":")
                number = value.strip().split(" ")[0] if value.strip() else ""
                if not number.isdigit():
                    continue
                amount = float(number)
                if key == "MemTotal":
                    total_kb = amount
                elif key == "MemAvailable":
                    available_kb = amount
                elif key == "MemFree":
                    free_kb = amount
                elif key == "Buffers":
                    buffers_kb = amount
                elif key == "Cached":
                    cached_kb = amount
        if total_kb is not None:
            source = "/proc/meminfo"
    except OSError:
        pass

    if available_kb is None and free_kb is not None:
        available_kb = free_kb + (buffers_kb or 0) + (cached_kb or 0)
        source = "/proc/meminfo (MemFree+Buffers+Cached)"

    if total_kb is None or available_kb is None:
        try:
            page = os.sysconf("SC_PAGE_SIZE")
            total_pages = os.sysconf("SC_PHYS_PAGES")
            available_pages = os.sysconf("SC_AVPHYS_PAGES")
            total_kb = float(total_pages * page) / 1024.0
            available_kb = float(available_pages * page) / 1024.0
            source = "sysconf"
        except (ValueError, OSError, AttributeError):
            return ResourceSnapshot(None, None, False, "unreadable")

    return ResourceSnapshot(
        total_mb=(total_kb / 1024.0) if total_kb is not None else None,
        available_mb=(available_kb / 1024.0) if available_kb is not None else None,
        readable=True,
        source=source,
    )


class ResourceGuard:
    def __init__(self, config: AppConfig):
        self.config = config
        self.min_ram_mb = config.min_free_ram_mb
        self.min_disk_mb = config.min_free_disk_mb
        self.on_low_memory = str(config.get("resources.on_low_memory", "deny")).lower()

    def disk_free_mb(self) -> Optional[float]:
        target = self.config.workspace_root
        if not target.exists():
            target = self.config.project_root
        try:
            usage = shutil.disk_usage(str(target))
            return float(usage.free) / (1024.0 * 1024.0)
        except OSError:
            return None

    def preflight(self, dry_run: bool = False) -> ResourceDecision:
        snapshot = read_memory()
        disk_free = self.disk_free_mb()
        warnings: List[str] = []

        if not snapshot.readable or snapshot.available_mb is None:
            # Unknown is not zero.  Continue with a warning so the operator can
            # decide, but never pretend the machine has free memory.
            warnings.append("không đọc được RAM khả dụng; tiếp tục ở chế độ thận trọng")
        elif snapshot.available_mb < self.min_ram_mb:
            message = (
                f"RAM khả dụng {snapshot.available_mb:.0f} MB thấp hơn ngưỡng "
                f"{self.min_ram_mb} MB của profile {self.config.profile}"
            )
            if dry_run or self.on_low_memory == "warn":
                warnings.append(message)
            else:
                return ResourceDecision(False, message, warnings, snapshot, disk_free, dry_run)

        if disk_free is None:
            warnings.append("không đọc được dung lượng ổ đĩa; tiếp tục ở chế độ thận trọng")
        elif disk_free < self.min_disk_mb:
            message = (
                f"ổ đĩa chỉ còn {disk_free:.0f} MB, dưới ngưỡng {self.min_disk_mb} MB"
            )
            if dry_run:
                warnings.append(message)
            else:
                return ResourceDecision(False, message, warnings, snapshot, disk_free, dry_run)

        reason = "tài nguyên đủ cho một tác vụ tuần tự"
        return ResourceDecision(True, reason, warnings, snapshot, disk_free, dry_run)

    def acquire_lock(self, timeout: float = 0.0) -> FileLock:
        lock = FileLock(self.config.lock_path, timeout=timeout)
        if not lock.acquire():
            raise ResourceError(
                "đang có tác vụ khác chạy; Trinity chỉ chạy tuần tự. "
                f"Lock: {self.config.lock_path}"
            )
        return lock

    def clamp_timeout(self, requested: Optional[float] = None) -> int:
        default = self.config.timeout_seconds
        maximum = self.config.max_timeout_seconds
        if requested is None:
            return default
        try:
            value = int(requested)
        except (TypeError, ValueError):
            return default
        return max(1, min(value, maximum))
