"""Privacy-minimal SQLite usage statistics for the Web application."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_BUSY_TIMEOUT_MS = 5_000
DEFAULT_TASK_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_STATS_PATH = Path(__file__).resolve().parents[1] / "var" / "stats.sqlite3"


@dataclass(frozen=True, slots=True)
class UsageMetrics:
    """Anonymous aggregate counters shown by the Web interface."""

    total_generations: int = 0
    weekly_generations: int = 0
    estimated_visitors: int = 0
    page_views: int = 0


class UsageStats:
    """Store anonymous visitor and idempotent generation counters in SQLite."""

    def __init__(
        self,
        database_path: Path,
        *,
        timezone_name: str = DEFAULT_TIMEZONE,
        hmac_key: bytes | None = None,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        task_ttl_seconds: int = DEFAULT_TASK_TTL_SECONDS,
    ) -> None:
        if busy_timeout_ms <= 0 or task_ttl_seconds <= 0:
            raise ValueError("统计超时与任务保留时间必须大于 0。")
        try:
            self.timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"统计时区无效：{timezone_name}") from exc
        self.database_path = database_path.expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = busy_timeout_ms
        self.task_ttl_seconds = task_ttl_seconds
        self._hmac_key = hmac_key or self._load_or_create_key()
        if len(self._hmac_key) < 16:
            raise ValueError("统计 HMAC 密钥至少需要 16 字节。")
        self._initialize()

    @classmethod
    def from_environment(cls) -> UsageStats:
        """Build the shared store from server-side environment settings."""
        configured_path = os.environ.get("STATS_DB_PATH", "").strip()
        database_path = Path(configured_path) if configured_path else DEFAULT_STATS_PATH
        if not configured_path:
            cls._migrate_legacy_database(database_path)
        configured_key = os.environ.get("STATS_HMAC_KEY", "").encode("utf-8") or None
        return cls(
            database_path,
            timezone_name=os.environ.get("REPORT_TIMEZONE", DEFAULT_TIMEZONE).strip()
            or DEFAULT_TIMEZONE,
            hmac_key=configured_key,
        )

    @staticmethod
    def _migrate_legacy_database(destination: Path) -> None:
        """Copy legacy counters once, retaining the old database as a backup."""
        legacy = Path.home() / ".local" / "state" / "weekly-report-agent" / "stats.sqlite3"
        if destination.exists() or not legacy.is_file():
            return
        import fcntl

        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (destination.parent / ".stats-migration.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if destination.exists():
                return
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
                temporary_path = Path(temporary.name)
            try:
                with (
                    closing(
                        sqlite3.connect(legacy.resolve().as_uri() + "?mode=ro", uri=True)
                    ) as source,
                    closing(sqlite3.connect(temporary_path)) as target,
                ):
                    source.backup(target)
                legacy_key = legacy.with_suffix(".sqlite3.key")
                if legacy_key.is_file():
                    key_path = destination.with_suffix(".sqlite3.key")
                    with key_path.open("xb") as output:
                        key_path.chmod(0o600)
                        output.write(legacy_key.read_bytes())
                temporary_path.replace(destination)
            finally:
                temporary_path.unlink(missing_ok=True)

    def _load_or_create_key(self) -> bytes:
        key_path = self.database_path.with_suffix(self.database_path.suffix + ".key")
        temporary = key_path.with_name(f".{key_path.name}.{os.getpid()}.{secrets.token_hex(8)}")
        key = secrets.token_bytes(32)
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, key_path)
            except FileExistsError:
                key = key_path.read_bytes()
        finally:
            temporary.unlink(missing_ok=True)
        if len(key) < 16:
            raise ValueError("统计密钥文件无效，请删除后重新启动服务。")
        return key

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout_ms / 1_000,
        )
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS page_views "
                "(local_date TEXT PRIMARY KEY, view_count INTEGER NOT NULL)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS generation_totals (
                    local_date TEXT PRIMARY KEY,
                    successful_count INTEGER NOT NULL CHECK(successful_count >= 0)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS generation_tasks (
                    task_hash TEXT PRIMARY KEY,
                    expires_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS generation_task_expiry_idx "
                "ON generation_tasks(expires_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS anonymous_visitors (
                    visitor_hash TEXT PRIMARY KEY,
                    first_seen REAL NOT NULL
                )
                """
            )
        with suppress(OSError):
            self.database_path.chmod(0o600)

    def _digest(self, namespace: str, value: str) -> str:
        payload = f"{namespace}:{value}".encode()
        return hmac.new(self._hmac_key, payload, hashlib.sha256).hexdigest()

    def record_visitor(self, visitor_id: str, *, seen_at: float | None = None) -> bool:
        """Record one anonymous browser estimate, returning whether it was new."""
        visitor_hash = self._digest("visitor", visitor_id)
        timestamp = time.time() if seen_at is None else seen_at
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO anonymous_visitors(visitor_hash, first_seen) VALUES (?, ?)",
                (visitor_hash, timestamp),
            )
        return cursor.rowcount == 1

    def record_page_view(self, *, viewed_at: float | None = None) -> None:
        """Persist one successful page view without storing request details."""
        timestamp = time.time() if viewed_at is None else viewed_at
        local_date = datetime.fromtimestamp(timestamp, self.timezone).date().isoformat()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO page_views(local_date, view_count) VALUES (?, 1) "
                "ON CONFLICT(local_date) DO UPDATE SET view_count = view_count + 1",
                (local_date,),
            )

    def record_generation(self, task_id: str, *, generated_at: float | None = None) -> bool:
        """Count a successful task once while retaining only a short-lived task digest."""
        task_hash = self._digest("generation", task_id)
        timestamp = time.time() if generated_at is None else generated_at
        local_date = datetime.fromtimestamp(timestamp, self.timezone).date().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM generation_tasks WHERE expires_at <= ?", (timestamp,))
            cursor = connection.execute(
                "INSERT OR IGNORE INTO generation_tasks(task_hash, expires_at) VALUES (?, ?)",
                (task_hash, timestamp + self.task_ttl_seconds),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    """
                    INSERT INTO generation_totals(local_date, successful_count) VALUES (?, 1)
                    ON CONFLICT(local_date) DO UPDATE
                    SET successful_count = successful_count + 1
                    """,
                    (local_date,),
                )
        return cursor.rowcount == 1

    def metrics(self, *, now: float | None = None) -> UsageMetrics:
        """Return cumulative, current-week, and estimated visitor counters."""
        timestamp = time.time() if now is None else now
        local_now = datetime.fromtimestamp(timestamp, self.timezone)
        local_date = local_now.date().isoformat()
        week_start = (local_now - timedelta(days=local_now.weekday())).date().isoformat()
        with self._connect() as connection:
            total = connection.execute(
                "SELECT COALESCE(SUM(successful_count), 0) FROM generation_totals"
            ).fetchone()[0]
            weekly = connection.execute(
                "SELECT COALESCE(SUM(successful_count), 0) FROM generation_totals "
                "WHERE local_date BETWEEN ? AND ?",
                (week_start, local_date),
            ).fetchone()[0]
            visitors = connection.execute("SELECT COUNT(*) FROM anonymous_visitors").fetchone()[0]
            views = connection.execute(
                "SELECT COALESCE(SUM(view_count), 0) FROM page_views"
            ).fetchone()[0]
        return UsageMetrics(total, weekly, visitors, views)
