#!/usr/bin/env python3
"""TRINITY v3.1 - Lightweight AI Agent Orchestrator.

Usage:
    python3 trinity.py status
    python3 trinity.py doctor
    python3 trinity.py run "Nội dung nhiệm vụ"
    python3 trinity.py tasks
    python3 trinity.py task <task_id>
    python3 trinity.py config
    python3 trinity.py version
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from core.config import VERSION, AppConfig, detect_profile, load_config
from core.executor import ExecutionOutcome, Executor
from core.guard import Guard
from core.resource import ResourceGuard, read_memory
from core.task import TaskManager
from core.utils import ConfigError, TrinityError, contains_secret, redact, run_process, truncate
from storage.memory import AuditLog
from adapters.hermes import HermesAdapter
from adapters.openclaw import OpenClawAdapter
from adapters.opencode import OpenCodeAdapter

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_APPROVAL = 3


class VNParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        print(f"Lỗi: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def _add_global_options(parser: argparse.ArgumentParser, suppress: bool = False) -> None:
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument("--profile", default=default, help="Profile cấu hình: linux hoặc termux (mặc định: tự phát hiện)")
    parser.add_argument("--db", default=default, help="Ghi đè đường dẫn SQLite (dùng cho test/kiểm tra)")
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="In thông tin chi tiết",
    )


def build_parser() -> VNParser:
    parser = VNParser(
        prog="trinity",
        description="TRINITY v3.1 - Lightweight AI Agent Orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Ví dụ:\n"
            "  python3 trinity.py status\n"
            "  python3 trinity.py run --dry-run \"sửa lỗi pytest trong core/router.py\"\n"
            "  python3 trinity.py run --write --approve \"sửa lỗi pytest trong workspace\"\n"
            "  python3 trinity.py run --approve \"chạy lệnh git status\"\n"
            "  python3 trinity.py resume TRN-20260101-120000-abcdef --approve\n"
            "  python3 trinity.py task TRN-20260101-120000-abcdef"
        ),
    )
    _add_global_options(parser)
    parser.add_argument("--version", action="version", version=f"Trinity v{VERSION}")
    sub = parser.add_subparsers(dest="command", required=False, parser_class=VNParser)

    common = VNParser(add_help=False)
    _add_global_options(common, suppress=True)

    sub.add_parser("status", parents=[common], help="Trạng thái hệ thống và adapter")
    sub.add_parser("doctor", parents=[common], help="Chẩn đoán môi trường và cấu hình")
    sub.add_parser("config", parents=[common], help="In cấu hình hiệu lực (đã che secrets)")
    sub.add_parser("version", parents=[common], help="In phiên bản Trinity")

    run = sub.add_parser("run", parents=[common], help="Lập kế hoạch và chạy một nhiệm vụ")
    run.add_argument("prompt", nargs="+", help="Nội dung nhiệm vụ")
    run.add_argument("--dry-run", action="store_true", help="Chỉ lập kế hoạch, không ghi dữ liệu")
    run.add_argument("--approve", action="store_true", help="Phê duyệt rõ ràng cho tác vụ rủi ro cao")
    run.add_argument(
        "--write",
        action="store_true",
        help="Bật controlled-write agent; bắt buộc đi kèm --approve",
    )
    run.add_argument("--timeout", type=_positive_int, default=None, help="Timeout mỗi adapter (giây)")

    resume = sub.add_parser("resume", parents=[common], help="Tiếp tục task WAITING_APPROVAL bằng Task ID cũ")
    resume.add_argument("task_id", help="Task ID đang WAITING_APPROVAL")
    resume.add_argument("--approve", action="store_true", help="Phê duyệt rõ ràng để resume")
    resume.add_argument("--dry-run", action="store_true", help="Kiểm tra điều kiện resume, không ghi dữ liệu")
    resume.add_argument("--timeout", type=_positive_int, default=None, help="Timeout mỗi adapter (giây)")

    tasks = sub.add_parser("tasks", parents=[common], help="Liệt kê lịch sử nhiệm vụ")
    tasks.add_argument("--limit", type=_positive_int, default=20, help="Số nhiệm vụ tối đa (mặc định 20)")
    tasks.add_argument(
        "--status",
        choices=["PENDING", "RUNNING", "WAITING_APPROVAL", "COMPLETED", "FAILED", "CANCELLED"],
        default=None,
        help="Lọc theo trạng thái",
    )

    task = sub.add_parser("task", parents=[common], help="Xem chi tiết một nhiệm vụ")
    task.add_argument("task_id", help="Task ID (ví dụ TRN-...)")

    return parser


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("phải là số nguyên dương") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("phải là số nguyên dương")
    return number


def _db_path(config: AppConfig, args: argparse.Namespace) -> Path:
    override = getattr(args, "db", None)
    if override:
        return Path(str(override)).expanduser().resolve()
    return config.db_path


def _task_manager(config: AppConfig, args: argparse.Namespace) -> TaskManager:
    return TaskManager(_db_path(config, args), max_result_chars=int(config.get("storage.max_result_chars", 20000)))


def _executor(config: AppConfig, args: argparse.Namespace) -> Executor:
    return Executor(
        config,
        task_manager=_task_manager(config, args),
        audit=AuditLog(config.log_dir),
    )


def _adapters(config: AppConfig) -> Dict[str, Any]:
    guard = Guard(config)
    return {
        "hermes": HermesAdapter(config, guard),
        "opencode": OpenCodeAdapter(config, guard),
        "openclaw": OpenClawAdapter(config, guard),
    }


def _os_pretty() -> str:
    try:
        with open("/etc/os-release", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.system()


def _platform_line(config: Optional[AppConfig] = None) -> str:
    name = _os_pretty()
    machine = platform.machine() or "?"
    if config is not None and config.profile == "termux":
        return f"Termux/Android ({machine})"
    return f"{name} / {machine}"


def _print_header(title: str) -> None:
    print(title)
    print("=" * len(title))


def cmd_version(args: argparse.Namespace) -> int:
    print(f"Trinity v{VERSION}")
    print(f"Python {platform.python_version()} ({platform.system().lower()})")
    print(f"Profile tự phát hiện: {detect_profile()}")
    return EXIT_OK


def cmd_status(args: argparse.Namespace, config: AppConfig) -> int:
    resource = ResourceGuard(config).preflight(dry_run=True)
    _print_header(f"TRINITY v{VERSION}")
    print(f"Profile      : {config.profile}  ({config.source_path})")
    print(f"Nền tảng     : {_platform_line(config)}")
    print(f"Workspace    : {config.workspace_root}")
    print(f"Allowed roots: {', '.join(str(item) for item in config.allowed_roots)}")
    db_path = _db_path(config, args)
    print(f"SQLite       : {db_path} ({'đã có' if db_path.exists() else 'chưa tạo'})")
    print(f"Audit log    : {config.log_dir / 'audit.jsonl'}")
    print(f"Lock         : {config.lock_path}")
    memory = resource.snapshot
    if memory.readable and memory.available_mb is not None:
        total = f"{memory.total_mb:.0f} MB" if memory.total_mb is not None else "?"
        print(f"RAM          : {memory.available_mb:.0f} MB khả dụng / {total} ({memory.source})")
    else:
        print("RAM          : không đọc được (không coi là 0 MB)")
    if resource.disk_free_mb is not None:
        print(f"Ổ đĩa        : {resource.disk_free_mb:.0f} MB trống")
    for warning in resource.warnings:
        print(f"Cảnh báo     : {warning}")

    print("\nAdapters:")
    adapters = _adapters(config)
    for key, name, role in (
        ("hermes", "Hermes", "BRAIN "),
        ("opencode", "OpenCode", "LOGIC "),
        ("openclaw", "OpenClaw", "MUSCLE"),
    ):
        health = adapters[key].health(force=True)
        version = f" v{health.version}" if health.version else ""
        print(f"  {role}  {name:<10} {health.status.value:<24}{version}")
        print(f"          {health.detail}")
        if health.executable:
            print(f"          executable: {health.executable}")
        if health.memory_path:
            print(f"          memory path: {health.memory_path}")
        if args.verbose and health.capabilities:
            print(f"          capabilities: {', '.join(health.capabilities)}")
    for warning in config.warnings:
        print(f"Cảnh báo     : {warning}")
    return EXIT_OK


def _doctor_check(checks: List[Tuple[str, str, str]], name: str, status: str, detail: str) -> None:
    checks.append((name, status, detail))


def cmd_doctor(args: argparse.Namespace, config: AppConfig) -> int:
    checks: List[Tuple[str, str, str]] = []
    _doctor_check(
        checks,
        "Python >= 3.9",
        "PASS" if sys.version_info >= (3, 9) else "FAIL",
        platform.python_version(),
    )
    _doctor_check(
        checks,
        "Hệ điều hành Linux",
        "PASS" if platform.system() == "Linux" else "WARN",
        _platform_line(config),
    )
    if config.profile == "termux" and not os.environ.get("PREFIX", "").startswith("/data/data/com.termux"):
        _doctor_check(checks, "Profile Termux", "WARN", "đang chọn termux nhưng không thấy môi trường Termux")
    else:
        _doctor_check(checks, "Profile", "PASS", f"{config.profile} ({config.source_path})")

    workspace = config.workspace_root
    if workspace.is_dir():
        _doctor_check(checks, "Workspace", "PASS" if os.access(workspace, os.R_OK | os.W_OK) else "WARN", str(workspace))
    else:
        _doctor_check(checks, "Workspace", "FAIL", f"không tồn tại: {workspace}")

    for label, path in (("SQLite", _db_path(config, args).parent), ("Audit log", config.log_dir)):
        probe = path
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        writable = probe.exists() and os.access(probe, os.W_OK)
        _doctor_check(checks, f"{label} directory", "PASS" if writable else "FAIL", f"{path} (kiểm tra tại {probe})")

    memory = read_memory()
    if memory.readable and memory.available_mb is not None:
        status = "PASS" if memory.available_mb >= config.min_free_ram_mb else "WARN"
        _doctor_check(
            checks,
            "RAM khả dụng",
            status,
            f"{memory.available_mb:.0f} MB (ngưỡng {config.min_free_ram_mb} MB, {memory.source})",
        )
    else:
        _doctor_check(checks, "RAM khả dụng", "WARN", "không đọc được; không coi là 0 MB")

    disk = ResourceGuard(config).disk_free_mb()
    if disk is None:
        _doctor_check(checks, "Dung lượng ổ đĩa", "WARN", "không đọc được")
    else:
        _doctor_check(
            checks,
            "Dung lượng ổ đĩa",
            "PASS" if disk >= config.min_free_disk_mb else "WARN",
            f"{disk:.0f} MB (ngưỡng {config.min_free_disk_mb} MB)",
        )

    git = shutil.which("git")
    if git:
        proc = run_process([git, "--version"], timeout=5)
        _doctor_check(checks, "Git", "PASS" if proc.ok else "WARN", (proc.stdout or proc.stderr).strip() or "có executable")
    else:
        _doctor_check(checks, "Git", "WARN", "không tìm thấy git")

    db_path = _db_path(config, args)
    if db_path.exists():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
            try:
                conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            finally:
                conn.close()
            _doctor_check(checks, "SQLite integrity", "PASS", str(db_path))
        except sqlite3.Error as exc:
            _doctor_check(checks, "SQLite integrity", "FAIL", str(exc))
    else:
        _doctor_check(checks, "SQLite integrity", "PASS", "chưa có DB; sẽ tạo khi chạy nhiệm vụ")

    adapters = _adapters(config)
    for key, label in (("hermes", "BRAIN/Hermes"), ("opencode", "LOGIC/OpenCode"), ("openclaw", "MUSCLE/OpenClaw")):
        health = adapters[key].health(force=True)
        status = "PASS" if health.status.value == "AVAILABLE" else ("FAIL" if health.status.value == "ERROR" else "WARN")
        detail = f"{health.status.value}: {health.detail}"
        if key == "opencode" and health.status.value == "AVAILABLE":
            adapter_config = config.adapter("opencode")
            if adapter_config.get("require_explicit_agent", True) and not adapter_config.get("agent"):
                status = "WARN"
                detail += " — chưa cấu hình agent; execution sẽ fail closed"
        _doctor_check(checks, label, status, detail)

    guard = Guard(config)
    safe_ok = guard.check_command(["git", "status"]).allowed
    dangerous_ok = not guard.check_command(["rm", "-rf", "/"]).allowed
    _doctor_check(
        checks,
        "Safety Guard self-test",
        "PASS" if safe_ok and dangerous_ok else "FAIL",
        "allowlist git status, chặn rm -rf /" if safe_ok and dangerous_ok else "policy bất thường",
    )

    secret_paths = _secret_config_values(config.data)
    _doctor_check(
        checks,
        "Secrets trong config",
        "PASS" if not secret_paths else "FAIL",
        "không phát hiện" if not secret_paths else f"phát hiện ở: {', '.join(secret_paths)}",
    )

    _print_header(f"TRINITY DOCTOR - {config.profile}")
    for name, status, detail in checks:
        print(f"[{status:<4}] {name:<26} {detail}")
    failures = sum(1 for _, status, _ in checks if status == "FAIL")
    warnings = sum(1 for _, status, _ in checks if status == "WARN")
    print(f"\nTổng: {len(checks)} mục, {failures} FAIL, {warnings} WARN")
    if failures:
        print("Kết luận: cần sửa các mục FAIL trước khi chạy tác vụ.")
        return EXIT_FAILED
    print("Kết luận: môi trường và cấu hình đủ điều kiện để chạy (adapter thiếu sẽ báo UNAVAILABLE).")
    return EXIT_OK


def _secret_config_values(data: Dict[str, Any], prefix: str = "") -> List[str]:
    found: List[str] = []
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            found.extend(_secret_config_values(value, path))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    found.extend(_secret_config_values(item, f"{path}[{index}]"))
                elif isinstance(item, str) and contains_secret(item):
                    found.append(f"{path}[{index}]")
        elif isinstance(value, str) and contains_secret(value):
            found.append(path)
    return found


def cmd_run(args: argparse.Namespace, config: AppConfig) -> int:
    prompt = " ".join(args.prompt).strip()
    if not prompt:
        print("Lỗi: nội dung nhiệm vụ không được rỗng", file=sys.stderr)
        return EXIT_USAGE
    outcome = _executor(config, args).execute(
        prompt,
        dry_run=bool(args.dry_run),
        approve=bool(args.approve),
        timeout=args.timeout,
        write=bool(args.write),
    )
    _print_route(outcome)
    if outcome.resource and outcome.resource.warnings:
        for warning in outcome.resource.warnings:
            print(f"Cảnh báo: {warning}")
    print()
    if outcome.dry_run:
        print(f"Kết quả: DRY_RUN - {outcome.message}")
        return EXIT_OK
    if outcome.status == "WAITING_APPROVAL":
        print(f"Kết quả: WAITING_APPROVAL - {outcome.message}")
        print(f"Task ID: {outcome.task_id}")
        return EXIT_APPROVAL
    if outcome.status == "COMPLETED":
        print(f"Kết quả: COMPLETED - {outcome.message}")
        if outcome.task and outcome.task.get("result"):
            print("\n" + str(outcome.task["result"]))
        print(f"\nTask ID: {outcome.task_id}")
        return EXIT_OK
    print(f"Kết quả: {outcome.status} - {outcome.message}")
    if outcome.task:
        print(f"Task ID: {outcome.task_id}")
    return EXIT_FAILED


def cmd_resume(args: argparse.Namespace, config: AppConfig) -> int:
    outcome = _executor(config, args).resume(
        args.task_id,
        approve=bool(args.approve),
        timeout=args.timeout,
        dry_run=bool(args.dry_run),
    )
    _print_route(outcome)
    if outcome.resource and outcome.resource.warnings:
        for warning in outcome.resource.warnings:
            print(f"Cảnh báo: {warning}")
    print()
    if outcome.dry_run:
        print(f"Kết quả: DRY_RUN - {outcome.message}")
        return EXIT_OK
    if outcome.status == "WAITING_APPROVAL":
        print(f"Kết quả: WAITING_APPROVAL - {outcome.message}")
        print(f"Task ID: {outcome.task_id}")
        return EXIT_APPROVAL
    if outcome.status == "COMPLETED":
        print(f"Kết quả: COMPLETED - {outcome.message}")
        if outcome.task and outcome.task.get("result"):
            print("\n" + str(outcome.task["result"]))
        print(f"\nTask ID: {outcome.task_id}")
        return EXIT_OK
    print(f"Kết quả: {outcome.status} - {outcome.message}")
    if outcome.task:
        print(f"Task ID: {outcome.task_id}")
    return EXIT_FAILED


def _print_route(outcome: ExecutionOutcome) -> None:
    route = outcome.route
    _print_header("KẾ HOẠCH")
    print(f"Task ID       : {route.task_id}")
    print(f"Loại          : {route.task_type}")
    print(f"Adapter chính : {route.selected_adapter}")
    print(f"Mức rủi ro    : {route.risk_level}")
    print(f"Phê duyệt     : {'CÓ' if route.approval_required else 'không'}")
    print(f"Controlled write: {'CÓ' if route.write_mode else 'không'}")
    if route.matched_rules:
        print(f"Rule khớp     : {'; '.join(route.matched_rules)}")
    print("Các bước (tuần tự):")
    for step in route.execution_plan:
        print(
            f"  {step['step']}. {step['role']:<6} {step['adapter']:<8} "
            f"{step['action']:<16} {step['reason']}"
        )
    if outcome.resource and outcome.resource.snapshot.readable and outcome.resource.snapshot.available_mb is not None:
        snapshot = outcome.resource.snapshot
        disk = outcome.resource.disk_free_mb
        disk_text = f"{disk:.0f} MB trống" if disk is not None else "không đọc được"
        print(f"Tài nguyên    : RAM {snapshot.available_mb:.0f} MB khả dụng, ổ đĩa {disk_text}")


def cmd_tasks(args: argparse.Namespace, config: AppConfig) -> int:
    manager = _task_manager(config, args)
    tasks = manager.list_tasks(limit=args.limit, status=args.status)
    if not tasks:
        print("Chưa có nhiệm vụ nào.")
        return EXIT_OK
    print(f"{'TASK ID':<30} {'CREATED':<20} {'TYPE':<10} {'STATUS':<18} {'ADAPTER':<8} RISK")
    for task in tasks:
        print(
            f"{task['id']:<30} {task['created_at']:<20} {task['task_type']:<10} "
            f"{task['status']:<18} {task['adapter']:<8} {task['risk_level']}"
        )
    return EXIT_OK


def cmd_task(args: argparse.Namespace, config: AppConfig) -> int:
    manager = _task_manager(config, args)
    task = manager.get_task(args.task_id)
    if task is None:
        print(f"Lỗi: không tìm thấy task {args.task_id}", file=sys.stderr)
        return EXIT_USAGE
    _print_header(f"TASK {task['id']}")
    for key in ("created_at", "updated_at", "task_type", "status", "adapter", "risk_level", "profile"):
        print(f"{key:<12}: {task.get(key, '')}")
    print(f"resume_count: {task.get('resume_count', 0)}")
    print(f"prompt      : {redact(task.get('prompt', ''))}")
    if task.get("approved_by"):
        print(f"approved_by : {redact(task['approved_by'])}")
        print(f"approved_at : {task.get('approved_at', '')}")
        print(f"scope       : {redact(task.get('approval_scope', ''))}")
    if task.get("plan_json"):
        try:
            plan = json.loads(task["plan_json"])
            print("Kế hoạch    :")
            for step in plan:
                print(
                    f"  {step.get('step', '?')}. {step.get('role', ''):<6} "
                    f"{step.get('adapter', ''):<8} {step.get('action', '')}"
                )
        except (json.JSONDecodeError, TypeError):
            print("Kế hoạch    : (không đọc được plan_json)")
    if task.get("error"):
        print(f"error       : {redact(task['error'])}")
    if task.get("result"):
        print("\nKết quả:")
        print(truncate(task["result"], 20000))
    if task.get("events"):
        print("\nLịch sử:")
        for event in task["events"]:
            print(f"  {event['created_at']} [{event['kind']}] {redact(event['message'])}")
    return EXIT_OK


def cmd_config(args: argparse.Namespace, config: AppConfig) -> int:
    print(
        yaml.safe_dump(
            config.public_dict(),
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=120,
        )
    )
    for warning in config.warnings:
        print(f"# Cảnh báo: {warning}")
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_USAGE
    if args.command == "version":
        return cmd_version(args)

    try:
        config = load_config(profile=getattr(args, "profile", None))
        if args.verbose and config.warnings:
            for warning in config.warnings:
                print(f"Cảnh báo: {warning}", file=sys.stderr)
        if args.command == "status":
            return cmd_status(args, config)
        if args.command == "doctor":
            return cmd_doctor(args, config)
        if args.command == "run":
            return cmd_run(args, config)
        if args.command == "resume":
            return cmd_resume(args, config)
        if args.command == "tasks":
            return cmd_tasks(args, config)
        if args.command == "task":
            return cmd_task(args, config)
        if args.command == "config":
            return cmd_config(args, config)
        parser.print_help()
        return EXIT_USAGE
    except ConfigError as exc:
        print(f"Lỗi cấu hình: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except TrinityError as exc:
        print(f"Lỗi: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        print("\nĐã hủy bởi người dùng.", file=sys.stderr)
        return 130
    except Exception as exc:  # pragma: no cover - defensive top-level boundary
        print(f"Lỗi không mong muốn: {redact(exc)}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
