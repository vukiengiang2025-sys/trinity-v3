#!/usr/bin/env python3
"""One-command Termux/Linux self-test for Trinity.

Safe by design:
- does not execute an agent prompt;
- does not write outside the project/temp directory;
- does not read secrets or browser data;
- never starts Hermes/OpenClaw/OpenCode servers.
"""

from __future__ import annotations

import argparse
import os
import platform
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import VERSION, is_termux, load_config  # noqa: E402
from core.resource import ResourceGuard, read_memory  # noqa: E402
from core.utils import FileLock, run_process, utc_now  # noqa: E402
from storage.memory import FAILED, RUNNING, TaskManager  # noqa: E402


class Report:
    def __init__(self):
        self.rows = []

    def add(self, name, status, detail):
        self.rows.append((name, status, detail))

    def has_failure(self):
        return any(status == "FAIL" for _, status, _ in self.rows)

    def render(self, profile, quick):
        lines = [
            "TRINITY TERMUX SELF TEST",
            "=" * 32,
            f"Thời gian : {utc_now()}",
            f"Trinity   : v{VERSION}",
            f"Profile   : {profile}",
            f"Python    : {platform.python_version()}",
            f"Nền tảng  : {platform.platform()}",
            f"Termux    : {'CÓ' if is_termux() else 'KHÔNG'} (PREFIX={os.environ.get('PREFIX', '') or '<empty>'})",
            f"Chế độ    : {'quick (bỏ unittest)' if quick else 'full (gồm unittest)'}",
            "",
        ]
        for name, status, detail in self.rows:
            lines.append(f"[{status:<4}] {name:<32} {detail}")
        failures = sum(1 for _, status, _ in self.rows if status == "FAIL")
        warnings = sum(1 for _, status, _ in self.rows if status == "WARN")
        lines.extend(
            [
                "",
                f"Tổng: {len(self.rows)} mục, {failures} FAIL, {warnings} WARN",
                "Kết luận: " + ("CẦN XỬ LÝ FAIL" if failures else "ĐỦ ĐIỀU KIỆN CHẠY THỬ"),
                "Lưu ý: self-test này không chứng minh agent production an toàn.",
            ]
        )
        return "\n".join(lines) + "\n"


def add_check(report, name, func, critical=True):
    try:
        detail = func()
        if str(detail).startswith("WARN"):
            report.add(name, "WARN", str(detail)[:300])
        else:
            report.add(name, "PASS", detail or "OK")
    except Exception as exc:
        report.add(name, "FAIL" if critical else "WARN", str(exc)[:300])


def check_config():
    config = load_config("termux")
    if config.profile != "termux":
        raise RuntimeError(f"profile không phải termux: {config.profile}")
    return f"RAM ngưỡng {config.min_free_ram_mb} MB, timeout {config.timeout_seconds}s"


def check_memory():
    snapshot = read_memory()
    if not snapshot.readable or snapshot.available_mb is None:
        return "WARN: không đọc được RAM; Trinity sẽ chạy thận trọng"
    return f"{snapshot.available_mb:.0f} MB khả dụng / {snapshot.total_mb:.0f} MB"


def check_resources():
    config = load_config("termux")
    decision = ResourceGuard(config).preflight(dry_run=True)
    detail = f"RAM/disk: {'đủ' if decision.allowed else 'không đủ'}; {decision.reason}"
    if decision.warnings:
        detail += " | " + "; ".join(decision.warnings)
    return detail


def check_sqlite(workdir: Path):
    db = workdir / "self-test.db"
    manager = TaskManager(db)
    try:
        manager.create_task(
            task_id="TRN-termux-selftest",
            task_type="GENERAL",
            prompt="self-test only",
            adapter="opencode",
            risk_level="low",
            profile="termux",
        )
        manager.mark_running("TRN-termux-selftest", "self-test-runner", os.getpid(), platform.node())
        if not manager.heartbeat("TRN-termux-selftest", "self-test-runner"):
            raise RuntimeError("heartbeat không cập nhật được")
        manager.fail_running_tasks("self-test stale lease", stale_before=time.time() + 1)
        task = manager.get_task("TRN-termux-selftest")
        if task is None or task["status"] != FAILED:
            raise RuntimeError("recovery lease không đánh dấu FAILED")
        conn = sqlite3.connect(db)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity: {integrity}")
        return f"{db}; integrity=ok; lease/recovery=ok"
    finally:
        manager.close()


def check_lock(workdir: Path):
    path = workdir / ".termux.lock"
    first = FileLock(path)
    if not first.acquire():
        raise RuntimeError(f"không acquire được lock: {first.error}")
    second = FileLock(path)
    try:
        if second.acquire():
            second.release()
            raise RuntimeError("lock thứ hai acquire được (mutual exclusion lỗi)")
        detail = f"lock OK; second error={second.error}"
    finally:
        first.release()
    return detail


def check_timeout(workdir: Path):
    started = time.monotonic()
    result = run_process(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        timeout=1,
        cwd=workdir,
    )
    elapsed = time.monotonic() - started
    if not result.timed_out:
        raise RuntimeError("timeout không kích hoạt")
    if elapsed > 4:
        raise RuntimeError(f"timeout quá chậm: {elapsed:.1f}s")
    return f"timeout OK ({elapsed:.1f}s), process group bị kill"


def check_cli():
    result = run_process(
        [sys.executable, str(ROOT / "trinity.py"), "--profile", "termux", "version"],
        timeout=30,
        cwd=ROOT,
    )
    if not result.ok:
        raise RuntimeError((result.stderr or result.stdout or "CLI thất bại").strip()[:300])
    return result.stdout.strip().splitlines()[0] if result.stdout.strip() else "CLI OK"


def check_adapters():
    from adapters.hermes import HermesAdapter
    from adapters.openclaw import OpenClawAdapter
    from adapters.opencode import OpenCodeAdapter

    config = load_config("termux")
    parts = []
    for adapter in (HermesAdapter(config), OpenCodeAdapter(config), OpenClawAdapter(config)):
        health = adapter.discover(force=True)
        parts.append(f"{adapter.name}={health.status.value}")
    return "; ".join(parts)


def check_unittest():
    result = run_process(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"],
        timeout=900,
        cwd=ROOT,
        max_output=200_000,
    )
    if not result.ok:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-8:]
        raise RuntimeError("unittest FAILED: " + " | ".join(tail))
    return "unittest PASS"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Trinity Termux/Linux self-test")
    parser.add_argument("--quick", action="store_true", help="Bỏ qua chạy toàn bộ unittest")
    parser.add_argument("--output", default=None, help="Đường dẫn file báo cáo")
    args = parser.parse_args(argv)

    profile = "termux" if is_termux() else "linux"
    report = Report()
    report.add("Python >= 3.9", "PASS" if sys.version_info >= (3, 9) else "FAIL", platform.python_version())
    add_check(report, "Cấu hình Termux", check_config)
    add_check(report, "RAM", check_memory, critical=False)
    add_check(report, "Resource Guard", check_resources, critical=False)

    with tempfile.TemporaryDirectory(prefix="trinity-termux-selftest-") as tmp:
        work = Path(tmp)
        add_check(report, "SQLite + lease/recovery", lambda: check_sqlite(work))
        add_check(report, "fcntl lock", lambda: check_lock(work))
        add_check(report, "Subprocess timeout", lambda: check_timeout(work))

    add_check(report, "CLI", check_cli)
    add_check(report, "Adapter discovery", check_adapters, critical=False)
    if not args.quick:
        add_check(report, "Unittest suite", check_unittest)

    text = report.render(profile, args.quick)
    output = Path(args.output).expanduser() if args.output else ROOT / "logs" / f"termux-self-test-{time.strftime('%Y%m%d-%H%M%S')}.txt"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    print(text)
    print(f"Báo cáo: {output}")
    return 1 if report.has_failure() else 0


if __name__ == "__main__":
    raise SystemExit(main())
