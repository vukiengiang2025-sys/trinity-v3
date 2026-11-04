"""SQLite task history and append-only audit log.

Only redacted text is persisted.  There are deliberately no columns for API
keys, tokens or environment dumps.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from core.utils import StorageError, truncate, utc_now

PENDING = "PENDING"
RUNNING = "RUNNING"
WAITING_APPROVAL = "WAITING_APPROVAL"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

STATUSES = (PENDING, RUNNING, WAITING_APPROVAL, COMPLETED, FAILED, CANCELLED)
TERMINAL_STATUSES = {COMPLETED, FAILED, CANCELLED}
SCHEMA_VERSION = 3
ALLOWED_TRANSITIONS = {
    PENDING: {RUNNING, WAITING_APPROVAL, FAILED, CANCELLED},
    WAITING_APPROVAL: {RUNNING, FAILED, CANCELLED},
    RUNNING: {COMPLETED, FAILED, CANCELLED},
    COMPLETED: set(),
    FAILED: set(),
    CANCELLED: set(),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL,
    adapter TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    profile TEXT NOT NULL,
    prompt TEXT NOT NULL,
    result TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    dry_run INTEGER NOT NULL DEFAULT 0,
    plan_json TEXT NOT NULL DEFAULT '',
    workspace TEXT NOT NULL DEFAULT '',
    approved_by TEXT NOT NULL DEFAULT '',
    approved_at TEXT NOT NULL DEFAULT '',
    approval_scope TEXT NOT NULL DEFAULT '',
    resume_count INTEGER NOT NULL DEFAULT 0,
    runner_id TEXT NOT NULL DEFAULT '',
    runner_pid INTEGER NOT NULL DEFAULT 0,
    runner_host TEXT NOT NULL DEFAULT '',
    heartbeat_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id, id);
"""

# Columns added in v3.1.  They are also applied to existing v3.0 databases.
MIGRATION_COLUMNS = {
    "plan_json": "TEXT NOT NULL DEFAULT ''",
    "workspace": "TEXT NOT NULL DEFAULT ''",
    "approved_by": "TEXT NOT NULL DEFAULT ''",
    "approved_at": "TEXT NOT NULL DEFAULT ''",
    "approval_scope": "TEXT NOT NULL DEFAULT ''",
    "resume_count": "INTEGER NOT NULL DEFAULT 0",
    "runner_id": "TEXT NOT NULL DEFAULT ''",
    "runner_pid": "INTEGER NOT NULL DEFAULT 0",
    "runner_host": "TEXT NOT NULL DEFAULT ''",
    "heartbeat_at": "REAL NOT NULL DEFAULT 0",
}


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    for name, ddl in MIGRATION_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def _secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _secure_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class TaskManager:
    """Persistent task state.  A read on a missing DB never creates it."""

    def __init__(self, db_path: Union[str, Path], max_result_chars: int = 20000):
        self.db_path = str(db_path)
        self.max_result_chars = max(1000, int(max_result_chars))
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------
    # Connection / schema
    # ------------------------------------------------------------------
    def _connect(self, create: bool) -> Optional[sqlite3.Connection]:
        if self._conn is not None:
            if create:
                try:
                    _ensure_schema(self._conn)
                except sqlite3.Error as exc:
                    raise StorageError(f"không tạo được schema SQLite: {exc}") from exc
            return self._conn
        is_memory = self.db_path == ":memory:"
        path = Path(self.db_path)
        if not is_memory:
            if not path.exists() and not create:
                return None
            _secure_dir(path.parent)
        try:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
        except sqlite3.Error as exc:
            raise StorageError(f"không mở được SQLite {self.db_path}: {exc}") from exc
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        if not is_memory:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
            _secure_file(path)
        self._conn = conn
        if create:
            try:
                _ensure_schema(conn)
            except sqlite3.Error as exc:
                raise StorageError(f"không tạo được schema SQLite: {exc}") from exc
        else:
            try:
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
                ).fetchone()
            except sqlite3.Error:
                return None
            if row is None:
                return None
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def __enter__(self) -> "TaskManager":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def create_task(
        self,
        task_id: str,
        task_type: str,
        prompt: str,
        adapter: str,
        risk_level: str,
        profile: str,
        status: str = PENDING,
        dry_run: bool = False,
        error: str = "",
        plan_json: str = "",
        workspace: str = "",
    ) -> Dict[str, Any]:
        if not task_id or not str(task_id).strip():
            raise StorageError("Task ID không được rỗng")
        if status not in STATUSES:
            raise StorageError(f"trạng thái không hợp lệ: {status}")
        now = utc_now()
        conn = self._connect(create=True)
        assert conn is not None
        try:
            conn.execute(
                """
                INSERT INTO tasks
                    (id, created_at, updated_at, task_type, status, adapter,
                     risk_level, profile, prompt, result, error, dry_run,
                     plan_json, workspace)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?)
                """,
                (
                    str(task_id),
                    now,
                    now,
                    str(task_type),
                    status,
                    str(adapter),
                    str(risk_level),
                    str(profile),
                    truncate(prompt, self.max_result_chars),
                    truncate(error, self.max_result_chars),
                    1 if dry_run else 0,
                    truncate(plan_json, 10_000),
                    truncate(workspace, 2000),
                ),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, created_at, kind, message) VALUES (?, ?, ?, ?)",
                (str(task_id), now, "created", truncate(f"{task_type} via {adapter} ({risk_level})", 2000)),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"Task ID đã tồn tại: {task_id}") from exc
        except sqlite3.Error as exc:
            raise StorageError(f"không tạo được task: {exc}") from exc
        task = self.get_task(str(task_id))
        if task is None:
            raise StorageError("không đọc lại được task vừa tạo")
        return task

    def update_status(
        self,
        task_id: str,
        status: str,
        result: Optional[str] = None,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        if status not in STATUSES:
            raise StorageError(f"trạng thái không hợp lệ: {status}")
        conn = self._connect(create=False)
        if conn is None:
            raise StorageError(f"không tìm thấy task: {task_id}")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (str(task_id),)).fetchone()
        if row is None:
            raise StorageError(f"không tìm thấy task: {task_id}")
        current = row["status"]
        if status != current and status not in ALLOWED_TRANSITIONS.get(current, set()):
            raise StorageError(f"không thể chuyển {current} -> {status} cho task {task_id}")

        now = utc_now()
        assignments = ["status = ?", "updated_at = ?"]
        values: List[Any] = [status, now]
        if result is not None:
            assignments.append("result = ?")
            values.append(truncate(result, self.max_result_chars))
        if error is not None:
            assignments.append("error = ?")
            values.append(truncate(error, self.max_result_chars))
        values.append(str(task_id))
        try:
            conn.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?", values)
            if status != current:
                conn.execute(
                    "INSERT INTO task_events (task_id, created_at, kind, message) VALUES (?, ?, ?, ?)",
                    (str(task_id), now, "status", truncate(f"{current} -> {status}", 2000)),
                )
            conn.commit()
        except sqlite3.Error as exc:
            raise StorageError(f"không cập nhật được task: {exc}") from exc
        task = self.get_task(str(task_id))
        if task is None:
            raise StorageError("không đọc lại được task sau cập nhật")
        return task

    def mark_running(self, task_id: str, runner_id: str, pid: int, host: str) -> Dict[str, Any]:
        """Atomically move PENDING -> RUNNING and establish a runner lease."""

        conn = self._connect(create=True)
        if conn is None:
            raise StorageError(f"không tìm thấy task: {task_id}")
        now = utc_now()
        try:
            cursor = conn.execute(
                """
                UPDATE tasks
                   SET status = ?, updated_at = ?, runner_id = ?, runner_pid = ?,
                       runner_host = ?, heartbeat_at = ?
                 WHERE id = ? AND status = ?
                """,
                (RUNNING, now, truncate(runner_id, 200), int(pid), truncate(host, 200),
                 time.time(), str(task_id), PENDING),
            )
            if cursor.rowcount != 1:
                row = conn.execute("SELECT status FROM tasks WHERE id = ?", (str(task_id),)).fetchone()
                current = row["status"] if row else "missing"
                raise StorageError(f"không thể bắt đầu task {task_id}: trạng thái {current}")
            conn.execute(
                "INSERT INTO task_events (task_id, created_at, kind, message) VALUES (?, ?, ?, ?)",
                (str(task_id), now, "status", f"{PENDING} -> {RUNNING}"),
            )
            conn.commit()
        except StorageError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise StorageError(f"không bắt đầu được task: {exc}") from exc
        task = self.get_task(str(task_id))
        if task is None:
            raise StorageError("không đọc lại được task sau khi bắt đầu")
        return task

    def heartbeat(self, task_id: str, runner_id: str) -> bool:
        """Refresh the lease only if this runner still owns a RUNNING task.

        Heartbeat runs in a separate thread.  A dedicated SQLite connection
        avoids sharing the main thread's connection across threads.
        """

        is_memory = self.db_path == ":memory:"
        if not is_memory and not Path(self.db_path).exists():
            return False
        try:
            conn = self._conn if is_memory else sqlite3.connect(self.db_path, timeout=5.0)
        except sqlite3.Error:
            return False
        if conn is None:
            return False
        try:
            cursor = conn.execute(
                "UPDATE tasks SET heartbeat_at = ? WHERE id = ? AND status = ? AND runner_id = ?",
                (time.time(), str(task_id), RUNNING, str(runner_id)),
            )
            conn.commit()
            return cursor.rowcount == 1
        except sqlite3.Error:
            return False
        finally:
            if not is_memory:
                conn.close()

    def add_event(self, task_id: str, kind: str, message: str) -> None:
        conn = self._connect(create=False)
        if conn is None:
            raise StorageError(f"không tìm thấy task: {task_id}")
        try:
            conn.execute(
                "INSERT INTO task_events (task_id, created_at, kind, message) VALUES (?, ?, ?, ?)",
                (str(task_id), utc_now(), str(kind)[:100], truncate(message, 4000)),
            )
            conn.commit()
        except sqlite3.Error as exc:
            raise StorageError(f"không ghi được event: {exc}") from exc

    def cancel_task(self, task_id: str) -> Dict[str, Any]:
        return self.update_status(task_id, CANCELLED, error="người dùng hủy tác vụ")

    def recover_running_task(self, task_id: str, reason: str, stale_before: float) -> bool:
        """Atomically fail a RUNNING task only if its lease is still stale."""

        conn = self._connect(create=False)
        if conn is None:
            return False
        now = utc_now()
        safe_error = truncate(reason, self.max_result_chars)
        try:
            cursor = conn.execute(
                """
                UPDATE tasks
                   SET status = ?, updated_at = ?, error = ?
                 WHERE id = ? AND status = ?
                   AND (heartbeat_at = 0 OR heartbeat_at <= ?)
                """,
                (FAILED, now, safe_error, str(task_id), RUNNING, float(stale_before)),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO task_events (task_id, created_at, kind, message) VALUES (?, ?, ?, ?)",
                (str(task_id), now, "status", f"{RUNNING} -> {FAILED} (recovery)"),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, created_at, kind, message) VALUES (?, ?, ?, ?)",
                (str(task_id), now, "recovered", safe_error),
            )
            conn.commit()
            return True
        except sqlite3.Error:
            conn.rollback()
            return False

    def fail_running_tasks(self, reason: str, stale_before: Optional[float] = None) -> int:
        """Mark orphaned RUNNING tasks as FAILED after a restart.

        With ``stale_before``, only tasks whose heartbeat is old (or missing)
        are touched.  A fresh heartbeat means another runner may still be
        alive even if the lock file was lost.  The caller must hold the
        single-task lock; this method intentionally does not acquire it.
        """

        conn = self._connect(create=False)
        if conn is None:
            return 0
        sql = "SELECT id FROM tasks WHERE status = ?"
        params: List[Any] = [RUNNING]
        if stale_before is not None:
            sql += " AND (heartbeat_at = 0 OR heartbeat_at <= ?)"
            params.append(float(stale_before))
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return 0
        cutoff = time.time() if stale_before is None else float(stale_before)
        return sum(1 for row in rows if self.recover_running_task(row["id"], reason, cutoff))

    def claim_for_resume(
        self,
        task_id: str,
        approver: str,
        scope: str,
        runner_id: str = "",
        pid: int = 0,
        host: str = "",
    ) -> Dict[str, Any]:
        """Atomically claim a WAITING_APPROVAL task for exactly one runner.

        The conditional UPDATE is the duplicate-execution barrier: only one
        process can move WAITING_APPROVAL -> RUNNING for a given task.
        """

        if self.db_path != ":memory:" and not Path(self.db_path).exists():
            raise StorageError(f"không tìm thấy task: {task_id}")
        conn = self._connect(create=True)
        if conn is None:
            raise StorageError(f"không tìm thấy task: {task_id}")
        now = utc_now()
        safe_actor = truncate(approver or "unknown", 200)
        safe_scope = truncate(scope, 500)
        try:
            cursor = conn.execute(
                """
                UPDATE tasks
                   SET status = ?, updated_at = ?, approved_by = ?, approved_at = ?,
                       approval_scope = ?, resume_count = resume_count + 1,
                       runner_id = ?, runner_pid = ?, runner_host = ?, heartbeat_at = ?
                 WHERE id = ? AND status = ?
                """,
                (RUNNING, now, safe_actor, now, safe_scope,
                 truncate(runner_id, 200), int(pid), truncate(host, 200), time.time(),
                 str(task_id), WAITING_APPROVAL),
            )
            if cursor.rowcount != 1:
                row = conn.execute("SELECT status FROM tasks WHERE id = ?", (str(task_id),)).fetchone()
                current = row["status"] if row else "missing"
                raise StorageError(
                    f"không thể resume task {task_id}: trạng thái hiện tại là {current}"
                )
            conn.execute(
                "INSERT INTO task_events (task_id, created_at, kind, message) VALUES (?, ?, ?, ?)",
                (
                    str(task_id),
                    now,
                    "approval",
                    truncate(f"{safe_actor} phê duyệt resume; scope={safe_scope}", 2000),
                ),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, created_at, kind, message) VALUES (?, ?, ?, ?)",
                (str(task_id), now, "status", f"{WAITING_APPROVAL} -> {RUNNING} (resume)"),
            )
            conn.commit()
        except StorageError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise StorageError(f"không claim được task để resume: {exc}") from exc
        task = self.get_task(str(task_id))
        if task is None:
            raise StorageError("không đọc lại được task sau khi claim")
        return task

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        conn = self._connect(create=False)
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (str(task_id),)).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None:
            return None
        task = dict(row)
        task["dry_run"] = bool(task.get("dry_run"))
        for key, default in (
            ("plan_json", ""),
            ("workspace", ""),
            ("approved_by", ""),
            ("approved_at", ""),
            ("approval_scope", ""),
            ("resume_count", 0),
            ("runner_id", ""),
            ("runner_pid", 0),
            ("runner_host", ""),
            ("heartbeat_at", 0.0),
        ):
            task.setdefault(key, default)
        try:
            events = conn.execute(
                "SELECT created_at, kind, message FROM task_events WHERE task_id = ? ORDER BY id",
                (str(task_id),),
            ).fetchall()
        except sqlite3.OperationalError:
            events = []
        task["events"] = [dict(event) for event in events]
        return task

    def list_tasks(self, limit: int = 20, status: Optional[str] = None) -> List[Dict[str, Any]]:
        conn = self._connect(create=False)
        if conn is None:
            return []
        limit = max(1, min(int(limit), 500))
        sql = "SELECT * FROM tasks"
        params: List[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []
        result = []
        for row in rows:
            item = dict(row)
            item["dry_run"] = bool(item.get("dry_run"))
            result.append(item)
        return result

    def summary(self) -> Dict[str, int]:
        conn = self._connect(create=False)
        if conn is None:
            return {}
        try:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
        except sqlite3.OperationalError:
            return {}
        return {row["status"]: int(row["n"]) for row in rows}


class AuditLog:
    """Append-only, redacted JSONL audit trail."""

    def __init__(self, log_dir: Union[str, Path], filename: str = "audit.jsonl"):
        self.log_dir = Path(log_dir)
        self.path = self.log_dir / filename

    def write(self, event: str, task_id: Optional[str] = None, detail: Any = None, level: str = "info") -> bool:
        record = {
            "ts": utc_now(),
            "level": str(level),
            "event": str(event),
            "task_id": task_id or "",
            "detail": self._sanitize(detail),
        }
        try:
            _secure_dir(self.log_dir)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            _secure_file(self.path)
            return True
        except OSError:
            return False

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                lowered = str(key).lower()
                if any(token in lowered for token in ("password", "passwd", "secret", "token", "api_key", "apikey", "private_key")):
                    result[str(key)] = "***REDACTED***"
                else:
                    result[str(key)] = self._sanitize(item)
            return result
        if isinstance(value, (list, tuple)):
            return [self._sanitize(item) for item in value]
        if value is None:
            return None
        return truncate(value, 4000)
