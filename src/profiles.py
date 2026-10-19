"""Permanent server-side profile library; independent of expiring task artifacts."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .docx_security import inspect_docx_package
from .rules import PRESET_ID_PATTERN, preset_payload, validate_rule_preset

ROOT = Path(__file__).resolve().parents[1]


class ProfileStore:
    """Serialize profile version changes, submissions and administrator deletion."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.root.parent / f"{self.root.name}.sqlite3"
        fd = os.open(self.database, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        self.database.chmod(0o600)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS profiles (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                    version INTEGER NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    last_used REAL, last_successful REAL, usage_count INTEGER NOT NULL DEFAULT 0
                );
            """)

    @classmethod
    def from_environment(cls):
        return cls(Path(os.environ.get("REPORT_PROFILES_DIR", "").strip() or ROOT / "var/profiles"))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def locked(self):
        """Hold through enqueue/delete to close the reference-check race."""
        path = self.root / ".lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "r+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def list(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM profiles ORDER BY updated DESC")]

    def load(self, profile_id: str, version: int | None = None):
        if not PRESET_ID_PATTERN.fullmatch(profile_id) or not profile_id.startswith("p_"):
            raise ValueError("自定义方案 ID 无效。")
        with self.connect() as db:
            row = db.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if row is None:
            raise ValueError("方案不存在或已由管理员删除。")
        version = row["version"] if version is None else version
        if type(version) is not int or version < 1 or version > row["version"]:
            raise ValueError("方案版本不存在。")
        directory = self.root / profile_id / str(version)
        payload = json.loads((directory / "profile.json").read_text(encoding="utf-8"))
        return validate_rule_preset(payload["rule"]), directory / "template.docx", payload

    def save(
        self,
        rule: dict,
        template: Path,
        *,
        description: str = "",
        evidence: dict | None = None,
        profile_id: str | None = None,
        expected_version: int | None = None,
    ) -> tuple[str, int]:
        """Publish an immutable version only after the caller has confirmed its trial."""
        inspect_docx_package(template)
        with self.locked():
            profile_id = profile_id or "p_" + secrets.token_hex(10)
            with self.connect() as db:
                previous = db.execute(
                    "SELECT version FROM profiles WHERE id=?", (profile_id,)
                ).fetchone()
                if previous is None and expected_version is not None:
                    raise ValueError("原方案已不存在，请另存为新方案。")
                if previous and previous["version"] != expected_version:
                    raise ValueError("方案已更新，请重新载入再保存。")
                rule = {**rule, "id": profile_id}
                preset = validate_rule_preset(rule)
                version = previous["version"] + 1 if previous else 1
                parent = self.root / profile_id
                parent.mkdir(exist_ok=True, mode=0o700)
                directory = parent / str(version)
                if directory.exists():
                    raise ValueError("方案目录存在未完成版本，请管理员检查后恢复，未覆盖。")
                directory.mkdir(mode=0o700)
                try:
                    payload = {
                        "version": version,
                        "description": description,
                        "evidence": evidence or {},
                        "rule": preset_payload(preset),
                    }
                    (directory / "profile.json").write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    shutil.copyfile(template, directory / "template.docx")
                    for path in directory.iterdir():
                        path.chmod(0o600)
                    now = time.time()
                    db.execute(
                        "INSERT INTO profiles(id,name,description,version,created,updated) "
                        "VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
                        "description=excluded.description,"
                        "version=excluded.version,updated=excluded.updated",
                        (profile_id, preset.name, description, version, now, now),
                    )
                except Exception:
                    shutil.rmtree(directory)
                    raise
        return profile_id, version

    def used(self, profile_id: str, *, successful: bool = False):
        with self.connect() as db:
            if successful:
                db.execute(
                    "UPDATE profiles SET last_successful=? WHERE id=?", (time.time(), profile_id)
                )
            else:
                db.execute(
                    "UPDATE profiles SET last_used=?,usage_count=usage_count+1 WHERE id=?",
                    (time.time(), profile_id),
                )

    def delete(self, profile_id: str, queue) -> None:
        """Reject deletion of built-ins or profiles referenced by active queued work."""
        with self.locked():
            self.load(profile_id)
            with queue.connect() as db:
                rows = db.execute(
                    "SELECT payload FROM tasks WHERE state IN ('queued','running','finalizing') "
                    "OR owner IS NOT NULL"
                ).fetchall()
            if any(json.loads(row["payload"]).get("preset") == profile_id for row in rows):
                raise ValueError("仍有任务引用此方案，暂不能删除。")
            # Rename first: a crash can leave private trash, never a usable half-deleted version.
            directory = self.root / profile_id
            trash = self.root / (".deleted-" + secrets.token_hex(10))
            directory.rename(trash)
            try:
                with self.connect() as db:
                    db.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
            except Exception:
                trash.rename(directory)
                raise
            shutil.rmtree(trash)
