"""Short-lived on-disk snapshots used for exact preview downloads."""

from __future__ import annotations

import json
import re
import secrets
import shutil
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
DEFAULT_SNAPSHOT_TTL_SECONDS = 2 * 60 * 60


@dataclass(frozen=True, slots=True)
class ReportSnapshot:
    """A validated report snapshot ready to download."""

    path: Path
    download_name: str


class SnapshotStore:
    """Persist generated reports under unpredictable, expiring tokens."""

    def __init__(self, root: Path, ttl_seconds: int = DEFAULT_SNAPSHOT_TTL_SECONDS) -> None:
        if ttl_seconds <= 0:
            raise ValueError("预览保留时间必须大于 0。")
        self.root = root.resolve()
        self.ttl_seconds = ttl_seconds
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with suppress(OSError):
            self.root.chmod(0o700)

    def cleanup_expired(self, now: float | None = None) -> None:
        """Remove stale snapshots without following links outside the store."""
        current = time.time() if now is None else now
        try:
            entries = tuple(self.root.iterdir())
        except OSError:
            return
        for entry in entries:
            try:
                stat = entry.lstat()
            except OSError:
                continue
            if current - stat.st_mtime <= self.ttl_seconds:
                continue
            try:
                if entry.is_symlink() or not entry.is_dir():
                    entry.unlink(missing_ok=True)
                else:
                    shutil.rmtree(entry)
            except OSError:
                continue

    def create(self, report_content: bytes, download_name: str) -> str:
        """Create an immutable report snapshot and return its random token."""
        if not report_content:
            raise ValueError("无法保存空的周报预览。")
        safe_name = Path(download_name).name
        if not safe_name.lower().endswith(".docx"):
            raise ValueError("预览下载文件名必须是 DOCX。")
        self.cleanup_expired()
        while True:
            token = secrets.token_urlsafe(32)
            directory = self.root / token
            try:
                directory.mkdir(mode=0o700)
                break
            except FileExistsError:  # pragma: no cover - cryptographically improbable
                continue
        try:
            report_path = directory / "report.docx"
            report_path.write_bytes(report_content)
            report_path.chmod(0o600)
            metadata = {
                "download_name": safe_name,
                "expires_at": time.time() + self.ttl_seconds,
            }
            metadata_path = directory / "metadata.json"
            metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
            metadata_path.chmod(0o600)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return token

    def get(self, token: str) -> ReportSnapshot:
        """Resolve an unexpired snapshot token without accepting path traversal."""
        if not TOKEN_PATTERN.fullmatch(token):
            raise ValueError("周报预览链接无效或已经过期，请重新预览。")
        directory = self.root / token
        metadata_path = directory / "metadata.json"
        report_path = directory / "report.docx"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            expires_at = float(metadata["expires_at"])
            download_name = Path(metadata["download_name"]).name
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("周报预览链接无效或已经过期，请重新预览。") from exc
        if expires_at < time.time() or not report_path.is_file():
            shutil.rmtree(directory, ignore_errors=True)
            raise ValueError("周报预览已经过期，请重新预览。")
        if not download_name.lower().endswith(".docx"):
            raise ValueError("周报预览链接无效或已经过期，请重新预览。")
        return ReportSnapshot(report_path, download_name)
