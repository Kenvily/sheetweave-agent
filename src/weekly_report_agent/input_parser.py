"""Parse CSV/XLSX files into source records with stable physical locations."""

from __future__ import annotations

import csv
import re
import zipfile
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .csv_parser import CsvFormatError, discover_week_columns, select_week_column
from .models import ParsedInput, SourceFile, SourceRecord
from .rules import RulePreset

SUPPORTED_SUFFIXES = {".csv", ".xlsx"}
INVALID_EXCEL_SHEETS = re.compile(r"[\\/*?:\[\]]")


class InputFormatError(ValueError):
    """Raised when an uploaded source cannot be parsed safely."""


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value).strip()


def _read_csv(path: Path) -> list[list[str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = [[cell.strip() for cell in row] for row in csv.reader(stream)]
    except UnicodeDecodeError as exc:
        raise InputFormatError("CSV 必须使用 UTF-8 或 UTF-8-SIG 编码。") from exc
    except csv.Error as exc:
        raise InputFormatError(f"CSV 格式无效：{exc}。") from exc
    if not rows or not any(any(cell for cell in row) for row in rows):
        raise InputFormatError("输入文件为空。")
    return rows


def list_xlsx_sheets(path: Path) -> tuple[str, ...]:
    """List XLSX worksheet names without evaluating formulas or links."""
    try:
        from openpyxl import load_workbook
        from openpyxl.utils.exceptions import InvalidFileException
    except ImportError as exc:  # pragma: no cover - dependency is pinned by the project
        raise InputFormatError("读取 XLSX 需要安装 openpyxl。") from exc
    try:
        workbook = load_workbook(path, read_only=True, data_only=True, keep_links=False)
    except (InvalidFileException, OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        raise InputFormatError(f"XLSX 文件无效：{exc}。") from exc
    try:
        return tuple(workbook.sheetnames)
    finally:
        workbook.close()


def _read_xlsx(path: Path, sheet: str | None) -> tuple[list[list[str]], str, tuple[str, ...]]:
    try:
        from openpyxl import load_workbook
        from openpyxl.utils.exceptions import InvalidFileException
    except ImportError as exc:  # pragma: no cover
        raise InputFormatError("读取 XLSX 需要安装 openpyxl。") from exc
    try:
        workbook = load_workbook(path, read_only=True, data_only=True, keep_links=False)
    except (InvalidFileException, OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        raise InputFormatError(f"XLSX 文件无效：{exc}。") from exc
    try:
        available = tuple(workbook.sheetnames)
        if not available:
            raise InputFormatError("XLSX 不包含工作表。")
        selected = sheet or available[0]
        if selected not in available:
            raise InputFormatError(
                f"找不到工作表“{selected}”。可用工作表：{'、'.join(available)}。"
            )
        worksheet = workbook[selected]
        rows = [[_cell_text(cell) for cell in row] for row in worksheet.iter_rows(values_only=True)]
    finally:
        workbook.close()
    if not rows or not any(any(cell for cell in row) for row in rows):
        raise InputFormatError(f"工作表“{selected}”为空。")
    return rows, selected, available


def _unique_headers(row: Sequence[str]) -> tuple[str, ...]:
    values: list[str] = []
    seen: dict[str, int] = {}
    for index, raw in enumerate(row, start=1):
        value = raw.strip() or f"未命名列{index}"
        seen[value] = seen.get(value, 0) + 1
        values.append(value if seen[value] == 1 else f"{value}#{seen[value]}")
    return tuple(values)


def resolve_fields(
    headers: Sequence[str],
    preset: RulePreset,
    overrides: dict[str, str] | None = None,
) -> dict[str, int]:
    """Resolve canonical fields to physical column indexes."""
    override_values = {
        key: value.strip() for key, value in (overrides or {}).items() if value.strip()
    }
    normalized = {header.strip(): index for index, header in enumerate(headers) if header.strip()}
    mapping: dict[str, int] = {}
    for field, aliases in preset.input.column_aliases.items():
        candidates = (override_values[field],) if field in override_values else aliases
        for alias in candidates:
            if alias in normalized:
                mapping[field] = normalized[alias]
                break
        if field in override_values and field not in mapping:
            raise InputFormatError(f"字段“{field}”指定的列不存在：{override_values[field]}。")
    missing = [field for field in preset.input.required_fields if field not in mapping]
    if missing:
        readable = "、".join(missing)
        raise InputFormatError(f"缺少必填字段：{readable}。请检查表头或手动字段映射。")
    return mapping


def _value(row: Sequence[str], index: int) -> str:
    return row[index].strip() if index < len(row) else ""


def _source_id(file_id: str, sheet: str | None, row_number: int) -> str:
    location = sheet or "CSV"
    return f"{file_id}:{location}:{row_number}"


def _warnings_for_sources(sources: Sequence[SourceRecord]) -> tuple[str, ...]:
    warnings: list[str] = []
    seen: dict[tuple[tuple[str, str], ...], int] = {}
    for source in sources:
        signature = tuple(sorted(source.fields.items()))
        if signature in seen:
            warnings.append(f"第 {source.row_number} 行与第 {seen[signature]} 行内容重复，已保留。")
        else:
            seen[signature] = source.row_number
    return tuple(warnings)


def _parse_weekly_matrix(
    rows: Sequence[Sequence[str]],
    preset: RulePreset,
    filename: str,
    file_type: str,
    sheet: str | None,
    available_sheets: tuple[str, ...],
    requested_week: str | None,
    overrides: dict[str, str] | None,
) -> ParsedInput:
    if len(rows) <= preset.input.header_rows:
        raise InputFormatError("输入至少需要两行表头和一行数据。")
    headers = _unique_headers(rows[0])
    mapping = resolve_fields(headers, preset, overrides)
    try:
        week_column = select_week_column(rows, discover_week_columns(rows), requested_week)
    except CsvFormatError as exc:
        raise InputFormatError(str(exc)) from exc

    sources: list[SourceRecord] = []
    data_rows = rows[preset.input.header_rows :]
    for row_number, row in enumerate(data_rows, start=preset.input.header_rows + 1):
        project = _value(row, mapping["project"])
        progress = _value(row, week_column.index)
        if not project or not progress:
            continue
        fields = {field: _value(row, index) for field, index in mapping.items()}
        fields["detail"] = fields.get("detail") or "周度进展"
        fields["status"] = fields.get("status", "")
        fields["progress"] = progress
        sources.append(
            SourceRecord(
                source_id=_source_id("f1", sheet, row_number),
                file_id="f1",
                sheet=sheet,
                row_number=row_number,
                fields=fields,
                original_text=progress,
                raw_values=tuple(row),
            )
        )
    if not sources:
        raise InputFormatError(f"所选周次“{week_column.label}”没有有效项目进展。")
    return ParsedInput(
        source_files=(SourceFile("f1", filename, file_type, sheet),),
        sources=tuple(sources),
        field_mapping={field: headers[index] for field, index in mapping.items()},
        headers=headers,
        warnings=_warnings_for_sources(sources),
        available_sheets=available_sheets,
        month=week_column.month,
        week=week_column.week,
    )


def _parse_records(
    rows: Sequence[Sequence[str]],
    preset: RulePreset,
    filename: str,
    file_type: str,
    sheet: str | None,
    available_sheets: tuple[str, ...],
    overrides: dict[str, str] | None,
) -> ParsedInput:
    header_index = preset.input.header_rows - 1
    if len(rows) <= header_index + 1:
        raise InputFormatError("输入文件只有表头，没有数据行。")
    headers = _unique_headers(rows[header_index])
    mapping = resolve_fields(headers, preset, overrides)
    sources: list[SourceRecord] = []
    for row_number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        fields = {field: _value(row, index) for field, index in mapping.items()}
        if not any(fields.values()):
            continue
        if not fields.get("project"):
            raise InputFormatError(f"第 {row_number} 行缺少项目名称。")
        if not fields.get("content"):
            raise InputFormatError(f"第 {row_number} 行缺少本周工作内容。")
        original = "\n".join(value for value in fields.values() if value)
        sources.append(
            SourceRecord(
                source_id=_source_id("f1", sheet, row_number),
                file_id="f1",
                sheet=sheet,
                row_number=row_number,
                fields=fields,
                original_text=original,
                raw_values=tuple(row),
            )
        )
    if not sources:
        raise InputFormatError("没有可生成周报的有效数据行。")
    return ParsedInput(
        source_files=(SourceFile("f1", filename, file_type, sheet),),
        sources=tuple(sources),
        field_mapping={field: headers[index] for field, index in mapping.items()},
        headers=headers,
        warnings=_warnings_for_sources(sources),
        available_sheets=available_sheets,
    )


def parse_input(
    path: Path,
    preset: RulePreset,
    *,
    requested_week: str | None = None,
    sheet: str | None = None,
    field_mapping: dict[str, str] | None = None,
    display_filename: str | None = None,
) -> ParsedInput:
    """Parse CSV/XLSX into normalized records according to a rule preset."""
    if not path.is_file():
        raise FileNotFoundError(f"输入文件不存在：{path}")
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise InputFormatError("输入文件必须是 .csv 或 .xlsx。")
    if suffix == ".csv":
        rows = _read_csv(path)
        selected_sheet = None
        available_sheets: tuple[str, ...] = ()
        file_type = "csv"
    else:
        rows, selected_sheet, available_sheets = _read_xlsx(path, sheet)
        file_type = "xlsx"
    filename = display_filename or path.name
    if preset.input.layout == "weekly_matrix":
        return _parse_weekly_matrix(
            rows,
            preset,
            filename,
            file_type,
            selected_sheet,
            available_sheets,
            requested_week,
            field_mapping,
        )
    return _parse_records(
        rows,
        preset,
        filename,
        file_type,
        selected_sheet,
        available_sheets,
        field_mapping,
    )
