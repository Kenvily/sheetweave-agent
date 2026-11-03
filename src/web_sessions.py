"""Short-lived on-disk report sessions and exact DOCX snapshots."""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .models import ContentBlock, ParsedInput, PreparedReport, SourceFile, SourceImage, SourceRecord

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
DEFAULT_SNAPSHOT_TTL_SECONDS = 2 * 60 * 60
SESSION_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ReportSnapshot:
    """A validated report snapshot ready to download."""

    path: Path
    download_name: str


@dataclass(frozen=True, slots=True)
class PdfSnapshot:
    """A PDF preview bound to one current report revision."""

    path: Path
    revision: int


@dataclass(frozen=True, slots=True)
class ReportSession:
    """One editable report work state loaded from the shared session directory."""

    token: str
    prepared: PreparedReport
    preset_id: str
    report_date: date
    section_number: int
    content_mode: str
    custom_instruction: str | None
    model: str | None
    download_name: str
    revision: int
    expires_at: float
    template_path: Path
    report_path: Path
    pdf_path: Path | None


class SessionConflictError(ValueError):
    """Raised when a stale browser revision attempts to overwrite newer state."""


class SnapshotStore:
    """Persist immutable snapshots and editable sessions under random tokens."""

    def __init__(self, root: Path, ttl_seconds: int = DEFAULT_SNAPSHOT_TTL_SECONDS) -> None:
        if ttl_seconds <= 0:
            raise ValueError("预览保留时间必须大于 0。")
        self.root = root.resolve()
        self.ttl_seconds = ttl_seconds
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with suppress(OSError):
            self.root.chmod(0o700)

    @classmethod
    def from_environment(cls, root: Path | None = None) -> SnapshotStore:
        """Use identical session configuration in Web and independent workers."""
        value = os.environ.get("REPORT_SESSION_TTL")
        if value is None:
            # Legacy installations may still provide an integer number of seconds.
            legacy = os.environ.get("REPORT_SESSION_TTL_SECONDS", "").strip()
            try:
                ttl = int(legacy) if legacy else DEFAULT_SNAPSHOT_TTL_SECONDS
            except ValueError as exc:
                raise ValueError("REPORT_SESSION_TTL_SECONDS 必须是正整数（秒）。") from exc
            if ttl <= 0:
                raise ValueError("REPORT_SESSION_TTL_SECONDS 必须是正整数（秒）。")
        else:
            value = value.strip() or "2h"
            match = re.fullmatch(r"([0-9]+)[ \t]*(h|d|w|小时|天|周)", value, re.IGNORECASE)
            if match is None or int(match[1]) <= 0:
                raise ValueError("REPORT_SESSION_TTL 请使用正整数加小时/天/周，例如 2h、3d、1w。")
            units = {"h": 3600, "小时": 3600, "d": 86400, "天": 86400, "w": 604800, "周": 604800}
            ttl = int(match[1]) * units[match[2].lower()]
        directory = root or Path(
            os.environ.get("WEEKLY_REPORT_SNAPSHOT_DIR", "").strip()
            or str(Path(tempfile.gettempdir()) / "weekly-report-agent-snapshots")
        )
        return cls(directory, ttl_seconds=ttl)

    @property
    def retention_label(self) -> str:
        """Present the configured lifetime without rounding its duration."""
        for unit, divisor in (("周", 604800), ("天", 86400), ("小时", 3600), ("分钟", 60)):
            if self.ttl_seconds % divisor == 0:
                return f"{self.ttl_seconds // divisor} {unit}"
        return f"{self.ttl_seconds} 秒"

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
            expires_at = stat.st_mtime + self.ttl_seconds
            if entry.is_dir() and not entry.is_symlink():
                # A config change applies to new sessions, never retroactively to saved ones.
                metadata = entry / "state.json"
                if not metadata.is_file():
                    metadata = entry / "metadata.json"
                try:
                    stored = float(json.loads(metadata.read_text(encoding="utf-8"))["expires_at"])
                    if math.isfinite(stored):
                        expires_at = stored
                except (OSError, ValueError, TypeError, KeyError):
                    pass  # Incomplete artifacts use the age-based fallback.
            if current <= expires_at:
                continue
            try:
                if entry.is_symlink() or not entry.is_dir():
                    entry.unlink(missing_ok=True)
                else:
                    shutil.rmtree(entry)
            except OSError:
                continue

    def _new_directory(self) -> tuple[str, Path]:
        self.cleanup_expired()
        while True:
            token = secrets.token_urlsafe(32)
            directory = self.root / token
            try:
                directory.mkdir(mode=0o700)
                return token, directory
            except FileExistsError:  # pragma: no cover - cryptographically improbable
                continue

    @staticmethod
    def _safe_download_name(download_name: str) -> str:
        safe_name = Path(download_name).name
        if not safe_name.lower().endswith(".docx"):
            raise ValueError("预览下载文件名必须是 DOCX。")
        return safe_name

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)

    @staticmethod
    def _write_private(path: Path, content: bytes) -> None:
        if not content:
            raise ValueError("无法保存空文件。")
        path.write_bytes(content)
        path.chmod(0o600)

    def create(self, report_content: bytes, download_name: str) -> str:
        """Create a backward-compatible immutable report snapshot."""
        safe_name = self._safe_download_name(download_name)
        token, directory = self._new_directory()
        try:
            report_path = directory / "report.docx"
            self._write_private(report_path, report_content)
            self._atomic_json(
                directory / "metadata.json",
                {
                    "download_name": safe_name,
                    "expires_at": time.time() + self.ttl_seconds,
                },
            )
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return token

    def create_session(
        self,
        prepared: PreparedReport,
        *,
        preset_id: str,
        report_date: date,
        section_number: int,
        content_mode: str,
        custom_instruction: str | None,
        model: str | None,
        template_content: bytes,
        report_content: bytes,
        download_name: str,
        rule_payload: dict | None = None,
    ) -> str:
        """Persist an editable IR, exact template, and first rendered document."""
        safe_name = self._safe_download_name(download_name)
        token, directory = self._new_directory()
        try:
            if rule_payload is not None:
                from .rules import validate_rule_preset

                frozen = validate_rule_preset(rule_payload)
                self._atomic_json(directory / f"{frozen.id}.json", rule_payload)
            self._write_private(directory / "template.docx", template_content)
            self._write_private(directory / "report-1.docx", report_content)
            self._atomic_json(
                directory / "state.json",
                {
                    "schema_version": SESSION_SCHEMA_VERSION,
                    "expires_at": time.time() + self.ttl_seconds,
                    "revision": 1,
                    "report_name": "report-1.docx",
                    "download_name": safe_name,
                    "preset_id": preset_id,
                    "report_date": report_date.isoformat(),
                    "section_number": section_number,
                    "content_mode": content_mode,
                    "custom_instruction": custom_instruction,
                    "model": model,
                    "prepared": _prepared_to_dict(prepared),
                },
            )
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return token

    def _directory(self, token: str) -> Path:
        if not TOKEN_PATTERN.fullmatch(token):
            raise ValueError("文档预览链接无效或已经过期，请重新预览。")
        return self.root / token

    @contextmanager
    def _locked(self, directory: Path) -> Iterator[None]:
        lock_path = directory / ".lock"
        lock_path.touch(mode=0o600, exist_ok=True)
        with lock_path.open("r+") as stream:
            try:
                import fcntl
            except ImportError as exc:  # pragma: no cover - deployed on Linux/WSL2
                raise RuntimeError("当前平台不支持报告会话文件锁。") from exc
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _valid_pdf_path(directory: Path, revision: int) -> Path | None:
        path = directory / f"preview-{revision}.pdf"
        try:
            if path.is_file() and path.read_bytes()[:5] == b"%PDF-":
                return path
        except OSError:
            pass
        return None

    def load_session(self, token: str) -> ReportSession:
        """Load an unexpired editable report session."""
        directory = self._directory(token)
        state = self._read_state(directory)
        try:
            revision = int(state["revision"])
            if int(state["schema_version"]) != SESSION_SCHEMA_VERSION or revision < 1:
                raise ValueError
            report_name = str(state["report_name"])
            if report_name != f"report-{revision}.docx":
                raise ValueError
            report_date = date.fromisoformat(state["report_date"])
            report_path = directory / report_name
            template_path = directory / "template.docx"
            session = ReportSession(
                token=token,
                prepared=_prepared_from_dict(state["prepared"]),
                preset_id=str(state["preset_id"]),
                report_date=report_date,
                section_number=int(state["section_number"]),
                content_mode=str(state["content_mode"]),
                custom_instruction=state.get("custom_instruction"),
                model=state.get("model"),
                download_name=self._safe_download_name(state["download_name"]),
                revision=revision,
                expires_at=float(state["expires_at"]),
                template_path=template_path,
                report_path=report_path,
                pdf_path=self._valid_pdf_path(directory, revision),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("文档编辑会话无效或已经过期，请重新预览。") from exc
        if not report_path.is_file() or not template_path.is_file():
            raise ValueError("文档编辑会话无效或已经过期，请重新预览。")
        return session

    def _read_state(self, directory: Path) -> dict[str, Any]:
        try:
            state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
            expires_at = float(state["expires_at"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("文档编辑会话无效或已经过期，请重新预览。") from exc
        if not math.isfinite(expires_at) or expires_at < time.time():
            shutil.rmtree(directory, ignore_errors=True)
            raise ValueError("文档编辑会话已经过期，请重新预览。")
        return state

    def update_session(
        self,
        token: str,
        expected_revision: int,
        prepared: PreparedReport,
        report_content: bytes,
        *,
        content_mode: str | None = None,
        custom_instruction: str | None = None,
        model: str | None = None,
    ) -> ReportSession:
        """Atomically publish a new report revision if the caller is current."""
        directory = self._directory(token)
        with self._locked(directory):
            state = self._read_state(directory)
            current_revision = int(state["revision"])
            if current_revision != expected_revision:
                raise SessionConflictError(
                    f"报告已在其他页面更新（当前版本 {current_revision}），"
                    "请刷新后重新操作，当前修改未覆盖新版本。"
                )
            revision = current_revision + 1
            report_name = f"report-{revision}.docx"
            self._write_private(directory / report_name, report_content)
            state.update(
                revision=revision,
                report_name=report_name,
                prepared=_prepared_to_dict(prepared),
            )
            if content_mode is not None:
                state["content_mode"] = content_mode
                state["custom_instruction"] = custom_instruction
                state["model"] = model
            self._atomic_json(directory / "state.json", state)
            for path in directory.glob("report-*.docx"):
                if path.name != report_name:
                    path.unlink(missing_ok=True)
            for path in directory.glob("preview-*.pdf"):
                path.unlink(missing_ok=True)
        return self.load_session(token)

    def publish_pdf(self, token: str, expected_revision: int, content: bytes) -> PdfSnapshot:
        """Publish a PDF only when it still matches the current DOCX revision."""
        if len(content) < 5 or not content.startswith(b"%PDF-"):
            raise ValueError("PDF 版式预览文件无效。")
        directory = self._directory(token)
        with self._locked(directory):
            state = self._read_state(directory)
            current_revision = int(state["revision"])
            if current_revision != expected_revision:
                raise SessionConflictError(
                    f"报告已在转换期间更新（当前版本 {current_revision}），"
                    "旧 PDF 未发布，请重新生成版式预览。"
                )
            path = directory / f"preview-{current_revision}.pdf"
            temporary = directory / f"preview-{current_revision}.tmp"
            self._write_private(temporary, content)
            os.replace(temporary, path)
            for old_path in directory.glob("preview-*.pdf"):
                if old_path != path:
                    old_path.unlink(missing_ok=True)
        return PdfSnapshot(path, current_revision)

    def get_pdf(self, token: str, expected_revision: int | None = None) -> PdfSnapshot:
        """Return the PDF for the current revision and reject stale preview URLs."""
        session = self.load_session(token)
        if expected_revision is not None and expected_revision != session.revision:
            raise SessionConflictError(
                f"PDF 预览版本已过期（当前版本 {session.revision}），请重新生成。"
            )
        if session.pdf_path is None:
            raise ValueError("当前版本尚未生成 PDF 版式预览。")
        return PdfSnapshot(session.pdf_path, session.revision)

    def get(self, token: str) -> ReportSnapshot:
        """Resolve the current editable report or a legacy immutable snapshot."""
        directory = self._directory(token)
        if (directory / "state.json").is_file():
            session = self.load_session(token)
            return ReportSnapshot(session.report_path, session.download_name)
        metadata_path = directory / "metadata.json"
        report_path = directory / "report.docx"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            expires_at = float(metadata["expires_at"])
            download_name = self._safe_download_name(metadata["download_name"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("文档预览链接无效或已经过期，请重新预览。") from exc
        if not math.isfinite(expires_at) or expires_at < time.time() or not report_path.is_file():
            shutil.rmtree(directory, ignore_errors=True)
            raise ValueError("文档预览已经过期，请重新预览。")
        return ReportSnapshot(report_path, download_name)


def _prepared_to_dict(prepared: PreparedReport) -> dict[str, Any]:
    return asdict(prepared)


def _prepared_from_dict(payload: dict[str, Any]) -> PreparedReport:
    source_data = payload["source"]
    source = ParsedInput(
        source_files=tuple(SourceFile(**item) for item in source_data["source_files"]),
        sources=tuple(
            SourceRecord(
                **{
                    **item,
                    "raw_values": tuple(item.get("raw_values", ())),
                    "images": tuple(SourceImage(**image) for image in item.get("images", ())),
                }
            )
            for item in source_data["sources"]
        ),
        field_mapping=dict(source_data["field_mapping"]),
        headers=tuple(source_data["headers"]),
        excluded_sources=tuple(
            SourceRecord(
                **{
                    **item,
                    "raw_values": tuple(item.get("raw_values", ())),
                    "images": tuple(SourceImage(**image) for image in item.get("images", ())),
                }
            )
            for item in source_data.get("excluded_sources", ())
        ),
        warnings=tuple(source_data.get("warnings", ())),
        available_sheets=tuple(source_data.get("available_sheets", ())),
        month=source_data.get("month", ""),
        week=source_data.get("week", ""),
    )
    blocks = tuple(
        ContentBlock(
            **{
                **item,
                "source_ids": tuple(item["source_ids"]),
                "source_fields": tuple(item.get("source_fields", ())),
                "validation_warnings": tuple(item.get("validation_warnings", ())),
                "edited_validation_warnings": tuple(item.get("edited_validation_warnings", ())),
            }
        )
        for item in payload["blocks"]
    )
    return PreparedReport(str(payload["preset_id"]), source, blocks)


def session_preset(session: ReportSession):
    """Load the frozen rule contract, falling back only for legacy sessions."""
    from .rules import load_rule_preset

    root = session.template_path.parent
    if (root / f"{session.preset_id}.json").is_file():
        return load_rule_preset(session.preset_id, root)
    return load_rule_preset(session.preset_id)
