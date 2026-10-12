"""Bounded structural checks for DOCX packages and uploaded templates."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

REQUIRED_DOCX_PARTS = {
    "[Content_Types].xml",
    "_rels/.rels",
    "word/document.xml",
    "word/styles.xml",
    "word/settings.xml",
}
MAX_DOCX_ENTRIES = 2048
MAX_DOCX_ENTRY_BYTES = 16 * 1024 * 1024
MAX_DOCX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_COMPRESSION_RATIO = 250


class DocxSecurityError(ValueError):
    """Raised when a DOCX package is malformed or exceeds safe limits."""


@dataclass(frozen=True, slots=True)
class DocxPackageInfo:
    """Summary of a structurally valid and bounded DOCX package."""

    entry_count: int
    uncompressed_bytes: int


def _safe_part_name(name: str) -> bool:
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        return False
    path = PurePosixPath(name)
    return not any(part in {"", ".", ".."} for part in path.parts)


def inspect_docx_package(path: Path) -> DocxPackageInfo:
    """Validate package paths, size bounds, CRCs, required parts, and core XML."""
    if not path.is_file():
        raise DocxSecurityError(f"DOCX 文件不存在：{path}")
    if path.suffix.lower() != ".docx":
        raise DocxSecurityError("文件必须使用 .docx 扩展名。")

    try:
        with zipfile.ZipFile(path, "r") as package:
            entries = package.infolist()
            if len(entries) > MAX_DOCX_ENTRIES:
                raise DocxSecurityError("DOCX 包含过多文件，已拒绝处理。")

            names: set[str] = set()
            total_size = 0
            for entry in entries:
                name = entry.filename
                if not _safe_part_name(name):
                    raise DocxSecurityError(f"DOCX 包含不安全路径：{name}")
                if name in names:
                    raise DocxSecurityError(f"DOCX 包含重复部件：{name}")
                names.add(name)
                if entry.flag_bits & 0x1:
                    raise DocxSecurityError("DOCX 包含加密部件，无法安全处理。")
                if entry.file_size > MAX_DOCX_ENTRY_BYTES:
                    raise DocxSecurityError(f"DOCX 部件过大：{name}")
                total_size += entry.file_size
                if total_size > MAX_DOCX_UNCOMPRESSED_BYTES:
                    raise DocxSecurityError("DOCX 解压后体积过大，已拒绝处理。")
                if (
                    entry.file_size > 1024 * 1024
                    and entry.compress_size > 0
                    and entry.file_size / entry.compress_size > MAX_COMPRESSION_RATIO
                ):
                    raise DocxSecurityError(f"DOCX 部件压缩比异常：{name}")

            missing = REQUIRED_DOCX_PARTS - names
            if missing:
                raise DocxSecurityError(f"DOCX 缺少必要部件：{'、'.join(sorted(missing))}")
            damaged = package.testzip()
            if damaged is not None:
                raise DocxSecurityError(f"DOCX 包含损坏部件：{damaged}")
            for name in (
                "[Content_Types].xml",
                "_rels/.rels",
                "word/document.xml",
                "word/styles.xml",
                "word/settings.xml",
            ):
                try:
                    ET.fromstring(package.read(name))
                except ET.ParseError as exc:
                    raise DocxSecurityError(f"DOCX XML 无效：{name}") from exc
    except zipfile.BadZipFile as exc:
        raise DocxSecurityError("文件不是有效的 DOCX 压缩包。") from exc

    return DocxPackageInfo(len(entries), total_size)
