"""SQLite-backed task queue with atomic claims, leases and private artifacts."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .web_sessions import TOKEN_PATTERN

TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
ROOT = Path(__file__).resolve().parents[1]


class TaskCancelled(RuntimeError):
    """Raised when a task is cancelled or its worker loses ownership."""


class QueueFullError(ValueError):
    """Raised when the bounded queue cannot accept another task."""


class TaskQueue:
    """Coordinate Web processes and independent workers using short transactions."""

    def __init__(
        self,
        database: Path,
        root: Path,
        *,
        concurrency: int = 2,
        capacity: int = 100,
        lease_seconds: float = 60,
        retention_seconds: float = 7200,
    ) -> None:
        if not 1 <= concurrency <= 16 or capacity < 1 or lease_seconds < 1 or retention_seconds < 1:
            raise ValueError("任务队列配置无效。")
        self.database, self.root = database.resolve(), root.resolve()
        self.concurrency, self.capacity = concurrency, capacity
        self.lease_seconds, self.retention_seconds = lease_seconds, retention_seconds
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(self.database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        except FileExistsError:
            pass
        self.database.chmod(0o600)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL, created REAL NOT NULL, started REAL,
                    finished REAL, owner TEXT, lease_until REAL,
                    result TEXT, error TEXT, event_bytes INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS tasks_state_created ON tasks(state, created);
                CREATE TABLE IF NOT EXISTS task_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_task_sequence ON task_events(task_id, sequence);
            """)
        self.database.chmod(0o600)

    @classmethod
    def from_environment(cls) -> TaskQueue:
        """Use project-local paths and explicit bounded concurrency by default."""
        return cls(
            Path(os.environ.get("TASK_DB_PATH", "").strip() or ROOT / "var/tasks.sqlite3"),
            Path(os.environ.get("TASK_FILES_DIR", "").strip() or ROOT / "var/tasks"),
            concurrency=int(os.environ.get("TASK_MAX_CONCURRENCY", "2")),
            capacity=int(os.environ.get("TASK_QUEUE_CAPACITY", "100")),
        )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def new_directory(self) -> tuple[str, Path]:
        """Allocate an unpredictable task ID and a private upload directory."""
        token = secrets.token_urlsafe(32)
        directory = self.root / token
        directory.mkdir(mode=0o700)
        return token, directory

    def directory(self, token: str) -> Path:
        if not TOKEN_PATTERN.fullmatch(token):
            raise ValueError("任务不存在或已过期。")
        return self.root / token

    def enqueue(self, token: str, kind: str, payload: dict) -> None:
        """Persist a ready task only after its uploaded files are complete."""
        self.directory(token)
        if kind not in {"generate", "regenerate", "pdf"}:
            raise ValueError("不支持的任务类型。")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute(
                "SELECT count(*) FROM tasks WHERE state IN ('queued','running','finalizing')"
            ).fetchone()[0]
            if count >= self.capacity:
                raise QueueFullError("生成任务较多，请稍后再提交。")
            db.execute(
                "INSERT INTO tasks(id,kind,payload,state,created) VALUES (?,?,?,'queued',?)",
                (token, kind, json.dumps(payload, ensure_ascii=False), time.time()),
            )

    @staticmethod
    def _recover(db: sqlite3.Connection, now: float) -> None:
        # Fail rather than replay potentially billed LLM requests after a worker crash.
        db.execute(
            "UPDATE tasks SET state='failed',finished=?,error=?,owner=NULL,lease_until=NULL "
            "WHERE state IN ('running','finalizing') AND lease_until < ?",
            (now, "后台 Worker 中断，本次任务未完成，请重新提交。", now),
        )

    def claim(self, owner: str) -> dict | None:
        """Atomically claim the oldest task within the shared global concurrency cap."""
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._recover(db, now)
            db.execute(
                "UPDATE tasks SET state='failed',finished=?,error=? "
                "WHERE state='queued' AND created<?",
                (now, "任务等待超过保留时间，请重新提交。", now - self.retention_seconds),
            )
            running = db.execute(
                "SELECT count(*) FROM tasks WHERE state IN ('running','finalizing') "
                "OR (state='cancelled' AND lease_until>=?)",
                (now,),
            ).fetchone()[0]
            if running >= self.concurrency:
                return None
            row = db.execute(
                "SELECT * FROM tasks WHERE state='queued' ORDER BY created,id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE tasks SET state='running',owner=?,lease_until=?,started=? WHERE id=?",
                (owner, now + self.lease_seconds, now, row["id"]),
            )
            return {"id": row["id"], "kind": row["kind"], "payload": json.loads(row["payload"])}

    def heartbeat(self, token: str, owner: str) -> bool:
        """Renew ownership; retain capacity until a cancelled in-flight call finishes."""
        with self.connect() as db:
            return (
                db.execute(
                    "UPDATE tasks SET lease_until=? WHERE id=? AND owner=? "
                    "AND state IN ('running','finalizing','cancelled') AND lease_until>=?",
                    (time.time() + self.lease_seconds, token, owner, time.time()),
                ).rowcount
                == 1
            )

    def emit(self, token: str, owner: str, kind: str, payload: dict) -> None:
        """Store bounded content events without holding a lock across LLM work."""
        value = json.dumps(payload, ensure_ascii=False)
        size = len(value.encode())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=?", (token,)).fetchone()
            if (
                not row
                or row["owner"] != owner
                or row["state"] != "running"
                or row["lease_until"] < time.time()
            ):
                raise TaskCancelled()
            if kind == "delta" and row["event_bytes"] + size > 16 * 1024 * 1024:
                return
            db.execute(
                "INSERT INTO task_events(task_id,kind,payload) VALUES (?,?,?)", (token, kind, value)
            )
            db.execute("UPDATE tasks SET event_bytes=event_bytes+? WHERE id=?", (size, token))

    def begin_publish(self, token: str, owner: str) -> None:
        """Fence cancellation before committing externally visible snapshots."""
        with self.connect() as db:
            changed = db.execute(
                "UPDATE tasks SET state='finalizing' WHERE id=? AND owner=? "
                "AND state='running' AND lease_until>=?",
                (token, owner, time.time()),
            ).rowcount
            if not changed:
                raise TaskCancelled()

    def finish(
        self, token: str, owner: str, *, result: dict | None = None, error: str | None = None
    ) -> bool:
        """Publish a terminal result only for the owning worker with a live lease."""
        with self.connect() as db:
            changed = db.execute(
                "UPDATE tasks SET state=CASE WHEN state='cancelled' THEN 'cancelled' ELSE ? END,"
                "result=?,error=?,finished=?,owner=NULL,lease_until=NULL WHERE id=? AND owner=? "
                "AND lease_until>=?",
                (
                    "succeeded" if result is not None else "failed",
                    json.dumps(result) if result else None,
                    error,
                    time.time(),
                    token,
                    owner,
                    time.time(),
                ),
            ).rowcount
            return changed == 1

    def cancel(self, token: str) -> bool:
        """Cancel waiting/running work; an already publishing result cannot be revoked."""
        self.directory(token)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM tasks WHERE id=?", (token,)).fetchone()
            if not row:
                raise ValueError("任务不存在或已过期。")
            if row["state"] == "finalizing":
                return False
            if row["state"] in TERMINAL_STATES:
                return row["state"] == "cancelled"
            db.execute(
                "UPDATE tasks SET state='cancelled',finished=? "
                "WHERE id=? AND state IN ('queued','running')",
                (time.time(), token),
            )
            return True

    def status(self, token: str, after: int = 0) -> dict:
        """Expose status and incremental events, never submission payloads or secrets."""
        self.directory(token)
        if after < 0:
            raise ValueError("进度游标无效。")
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (token,)).fetchone()
            if (
                row
                and row["state"] in {"running", "finalizing"}
                and row["lease_until"] < time.time()
            ):
                db.execute(
                    "UPDATE tasks SET state='failed',finished=?,error=?,"
                    "owner=NULL,lease_until=NULL WHERE id=? "
                    "AND state IN ('running','finalizing') AND lease_until<?",
                    (
                        time.time(),
                        "后台 Worker 中断，本次任务未完成，请重新提交。",
                        token,
                        time.time(),
                    ),
                )
                row = db.execute("SELECT * FROM tasks WHERE id=?", (token,)).fetchone()
            if (
                not row
                or (row["finished"] or row["created"]) + self.retention_seconds < time.time()
            ):
                raise ValueError("任务不存在或已过期。")
            events = db.execute(
                "SELECT * FROM task_events WHERE task_id=? AND sequence>? "
                "ORDER BY sequence LIMIT 101",
                (token, after),
            ).fetchall()
            position = (
                db.execute(
                    "SELECT count(*) FROM tasks WHERE state='queued' "
                    "AND (created<? OR (created=? AND id<?))",
                    (row["created"], row["created"], token),
                ).fetchone()[0]
                + 1
                if row["state"] == "queued"
                else None
            )
        page = events[:100]
        return {
            "id": token,
            "state": row["state"],
            "queue_position": position,
            "queue_seconds": round(
                (row["started"] or row["finished"] or time.time()) - row["created"], 3
            ),
            "run_seconds": round((row["finished"] or time.time()) - row["started"], 3)
            if row["started"]
            else None,
            "created_at": row["created"],
            "started_at": row["started"],
            "finished_at": row["finished"],
            "result": json.loads(row["result"]) if row["result"] else None,
            "error": row["error"],
            "events": [
                {"sequence": e["sequence"], "kind": e["kind"], "payload": json.loads(e["payload"])}
                for e in page
            ],
            "cursor": page[-1]["sequence"] if page else after,
            "has_more": len(events) > 100,
        }

    def cleanup(self) -> None:
        """Expire only queue-owned terminal files, never other project directories."""
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._recover(db, now)
            db.execute(
                "UPDATE tasks SET state='failed',finished=?,error=? "
                "WHERE state='queued' AND created<?",
                (now, "任务等待超过保留时间，请重新提交。", now - self.retention_seconds),
            )
            rows = db.execute(
                "SELECT id FROM tasks WHERE state IN ('succeeded','failed','cancelled') "
                "AND finished<? AND (lease_until IS NULL OR lease_until<?)",
                (now - self.retention_seconds, now),
            ).fetchall()
            for row in rows:
                db.execute("DELETE FROM tasks WHERE id=?", (row["id"],))
        for row in rows:
            shutil.rmtree(self.directory(row["id"]), ignore_errors=True)
