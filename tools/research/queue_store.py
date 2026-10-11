"""Persistent SQLite task queue for the local research worker."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


TERMINAL_STATES = ("completed", "failed", "cancelled")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def normalize_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("scheduled_at must be an ISO 8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("scheduled_at must include a timezone.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


class QueueStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        parent_existed = self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not parent_existed:
            self.path.parent.chmod(0o700)
        db_existed = self.path.exists()
        self.db = sqlite3.connect(self.path, timeout=5.0)
        if not db_existed:
            self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY,
              name TEXT,
              model_family TEXT NOT NULL,
              model_identifier TEXT NOT NULL,
              endpoint TEXT NOT NULL,
              prompt TEXT NOT NULL,
              max_tokens INTEGER NOT NULL,
              timeout_seconds REAL NOT NULL DEFAULT 600,
              state TEXT NOT NULL CHECK(state IN
                ('pending','running','completed','failed','cancelled')),
              created_at TEXT NOT NULL,
              scheduled_at TEXT NOT NULL,
              started_at TEXT,
              completed_at TEXT,
              result TEXT,
              error TEXT,
              raw_response_json TEXT
            )
            """
        )
        existing={row[1] for row in self.db.execute("PRAGMA table_info(tasks)")}
        for column in ("registry_json", "lifecycle_error", "lifecycle_session_id"):
            if column not in existing:
                self.db.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS tasks_due ON tasks(state, scheduled_at, created_at)"
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "QueueStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def enqueue(
        self,
        *,
        model_family: str,
        model_identifier: str,
        endpoint: str,
        prompt: str,
        scheduled_at: str,
        max_tokens: int = 1024,
        timeout_seconds: float = 600.0,
        name: str | None = None,
        task_id: str | None = None,
        registry_json: str | None = None,
    ) -> str:
        if not prompt.strip() or len(prompt) > 32_000:
            raise ValueError("Prompt must contain 1 to 32,000 characters.")
        if name is not None and len(name) > 80:
            raise ValueError("Task name must be at most 80 characters.")
        if not 1 <= max_tokens <= 8192:
            raise ValueError("max_tokens must be between 1 and 8192.")
        if not 0 < timeout_seconds <= 3600:
            raise ValueError("timeout_seconds must be greater than 0 and at most 3600.")
        scheduled_at = normalize_time(scheduled_at)
        task_id = task_id or str(uuid.uuid4())
        self.db.execute(
            """INSERT INTO tasks
               (id,name,model_family,model_identifier,endpoint,prompt,max_tokens,timeout_seconds,
                state,created_at,scheduled_at,registry_json)
               VALUES (?,?,?,?,?,?,?,?,'pending',?,?,?)""",
            (
                task_id,
                name or None,
                model_family,
                model_identifier,
                endpoint,
                prompt,
                max_tokens,
                timeout_seconds,
                utc_now(),
                scheduled_at,
                registry_json,
            ),
        )
        self.db.commit()
        return task_id

    def claim_due(self, now: str | None = None) -> dict | None:
        due_before = normalize_time(now) if now else utc_now()
        started_at = utc_now()
        with self.transaction():
            row = self.db.execute(
                """SELECT * FROM tasks
                   WHERE state='pending' AND scheduled_at <= ?
                   ORDER BY scheduled_at, created_at LIMIT 1""",
                (due_before,),
            ).fetchone()
            if row is None:
                return None
            changed = self.db.execute(
                "UPDATE tasks SET state='running', started_at=? WHERE id=? AND state='pending'",
                (started_at, row["id"]),
            ).rowcount
            if changed != 1:
                return None
            claimed = self.db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
            return dict(claimed)

    def complete(self, task_id: str, result: str, raw_json: str, session_id: str | None = None) -> bool:
        changed = self.db.execute(
            """UPDATE tasks SET state='completed',completed_at=?,result=?,
               raw_response_json=?,error=NULL,lifecycle_session_id=? WHERE id=? AND state='running'""",
            (utc_now(), result, raw_json, session_id, task_id),
        ).rowcount
        self.db.commit()
        return changed == 1

    def fail(self, task_id: str, error: str, raw_json: str | None = None) -> bool:
        changed = self.db.execute(
            """UPDATE tasks SET state='failed',completed_at=?,error=?,raw_response_json=?
               WHERE id=? AND state='running'""",
            (utc_now(), error, raw_json, task_id),
        ).rowcount
        self.db.commit()
        return changed == 1

    def record_lifecycle(self, task_id: str, session_id: str | None, error: str | None) -> None:
        self.db.execute("UPDATE tasks SET lifecycle_session_id=?,lifecycle_error=? WHERE id=?",
                        (session_id,error,task_id))
        self.db.commit()

    def cancel(self, task_id: str) -> bool:
        with self.transaction():
            changed = self.db.execute(
                """UPDATE tasks SET state='cancelled',completed_at=?
                   WHERE id=? AND state='pending'""",
                (utc_now(), task_id),
            ).rowcount
        return changed == 1

    def recover_running(self) -> int:
        message = (
            "Worker ended while this task was running; inference outcome is unknown. "
            "The task was not retried to preserve at-most-once dispatch."
        )
        changed = self.db.execute(
            """UPDATE tasks SET state='failed',completed_at=?,error=?
               WHERE state='running'""",
            (utc_now(), message),
        ).rowcount
        self.db.commit()
        return changed

    def get(self, task_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_pending(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE state IN ('pending','running') ORDER BY scheduled_at,created_at"
        ).fetchall()
        return [dict(row) for row in rows]

    def list_finished(self) -> list[dict]:
        rows = self.db.execute(
            """SELECT * FROM tasks WHERE state IN ('completed','failed','cancelled')
               ORDER BY completed_at DESC,created_at DESC"""
        ).fetchall()
        return [dict(row) for row in rows]

    def summary(self) -> dict:
        counts = {row["state"]: row["n"] for row in self.db.execute(
            "SELECT state,COUNT(*) AS n FROM tasks GROUP BY state"
        )}
        next_due = self.db.execute(
            "SELECT scheduled_at FROM tasks WHERE state='pending' ORDER BY scheduled_at LIMIT 1"
        ).fetchone()
        running = self.db.execute(
            "SELECT id,name,model_identifier,started_at FROM tasks WHERE state='running' LIMIT 1"
        ).fetchone()
        return {
            "counts": counts,
            "next_due": next_due[0] if next_due else None,
            "running": dict(running) if running else None,
        }
