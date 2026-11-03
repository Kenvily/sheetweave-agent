"""Parse CSV/XLSX files into source records with stable physical locations."""

from __future__ import annotations

import csv
import re
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .csv_parser import CsvFormatError, discover_week_columns, select_week_column
from .excel_images import read_excel_images
from .models import ParsedInput, SourceFile, SourceImage, SourceRecord, WeekColumn
from .rules import HistoricalCompletionRule, RulePreset

SUPPORTED_SUFFIXES = {".csv", ".xlsx"}
INVALID_EXCEL_SHEETS = re.compile(r"[\\/*?:\[\]]")


@dataclass(frozen=True, slots=True)
class InputOptions:
    """Selectable metadata discovered from an input file before generation."""

    sheets: tuple[str, ...] = ()
    selected_sheet: str | None = None
    weeks: tuple[str, ...] = ()
    default_week: str | None = None
    notice: str | None = None


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


def _read_xlsx(
    path: Path, sheet: str | None, *, preferred_sheet: str | None = None, allow_empty: bool = False
) -> tuple[list[list[str]], str, tuple[str, ...]]:
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
        selected = sheet or (preferred_sheet if preferred_sheet in available else available[0])
        if selected not in available:
            raise InputFormatError(
                f"找不到工作表“{selected}”。可用工作表：{'、'.join(available)}。"
            )
        worksheet = workbook[selected]
        rows = [[_cell_text(cell) for cell in row] for row in worksheet.iter_rows(values_only=True)]
    finally:
        workbook.close()
    if not allow_empty and (not rows or not any(any(cell for cell in row) for row in rows)):
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


def _normalized_status(text: str) -> str:
    """Normalize harmless differences before exact status comparison."""
    compact = re.sub(r"\s+", "", text).strip("，,。；;：:！! ")
    return compact.replace("已经", "已").replace("现已", "已")


def _should_mark_historical_completion(
    row: Sequence[str],
    status_index: int | None,
    week_columns: Sequence[WeekColumn],
    selected_index: int,
    rule: HistoricalCompletionRule | None,
) -> bool:
    """Return whether a completed row already had content before the selected week."""
    if rule is None or not rule.enabled or status_index is None:
        return False
    completed_statuses = {_normalized_status(status) for status in rule.completed_statuses}
    if _normalized_status(_value(row, status_index)) not in completed_statuses:
        return False
    return any(
        _value(row, column.index) for column in week_columns if column.index < selected_index
    )


def _parse_weekly_matrix(
    rows: Sequence[Sequence[str]],
    preset: RulePreset,
    filename: str,
    file_type: str,
    sheet: str | None,
    available_sheets: tuple[str, ...],
    requested_week: str | None,
    overrides: dict[str, str] | None,
    images: dict[int, tuple[SourceImage, ...]] | None = None,
) -> ParsedInput:
    images = images or {}
    if len(rows) <= preset.input.header_rows:
        raise InputFormatError("输入至少需要两行表头和一行数据。")
    headers = _unique_headers(rows[0])
    try:
        week_columns = discover_week_columns(rows)
        selection_rows = [list(row) for row in rows]
        periods = {column.index for column in week_columns}
        for row_number, pictures in images.items():
            if preset.input.header_rows < row_number <= len(selection_rows):
                for picture in pictures:
                    if picture.column in periods:
                        selection_rows[row_number - 1].extend(
                            [""] * max(0, picture.column + 1 - len(selection_rows[row_number - 1]))
                        )
                        if not selection_rows[row_number - 1][picture.column]:
                            selection_rows[row_number - 1][picture.column] = "附图。"
        week_column = select_week_column(selection_rows, week_columns, requested_week)
    except CsvFormatError as exc:
        raise InputFormatError(str(exc)) from exc
    resolved_overrides = dict(overrides or {})
    aliases = preset.input.column_aliases.get("content", ())
    if "content" not in resolved_overrides and not any(alias in headers for alias in aliases):
        period_indexes = {column.index for column in week_columns}
        if any(
            i in period_indexes and header in aliases
            for i, header in enumerate(preset.input.expected_headers)
        ):
            # Old saved schemes can require content even when its period heading changes.
            resolved_overrides["content"] = headers[week_column.index]
    mapping = resolve_fields(headers, preset, resolved_overrides)

    sources: list[SourceRecord] = []
    historical_completed_count = 0
    status_index = mapping.get("status")
    completion_rule = preset.input.historical_completion
    data_rows = rows[preset.input.header_rows :]
    for row_number, row in enumerate(data_rows, start=preset.input.header_rows + 1):
        project = _value(row, mapping["project"])
        body_index = mapping.get("content", week_column.index)
        if any(column.index == body_index for column in week_columns):
            body_index = week_column.index
        pictures = tuple(
            image
            for image in images.get(row_number, ())
            if image.column not in periods or image.column == body_index
        )
        progress = _value(row, body_index) or ("附图。" if pictures else "")
        if not project:
            continue
        historical_completed = _should_mark_historical_completion(
            row,
            status_index,
            week_columns,
            week_column.index,
            completion_rule,
        )
        if not progress and not historical_completed:
            continue
        fields = {field: _value(row, index) for field, index in mapping.items()}
        fields["detail"] = fields.get("detail") or "周度进展"
        fields["status"] = fields.get("status", "")
        if historical_completed:
            progress = completion_rule.output_text
            historical_completed_count += 1
        fields["progress"] = progress
        if "content" in mapping:
            fields["content"] = progress
        sources.append(
            SourceRecord(
                source_id=_source_id("f1", sheet, row_number),
                file_id="f1",
                sheet=sheet,
                row_number=row_number,
                fields=fields,
                original_text=progress,
                raw_values=tuple(row),
                historical_completed=historical_completed,
                images=pictures,
            )
        )
    if not sources:
        raise InputFormatError(f"所选周次“{week_column.label}”没有有效项目进展。")
    warnings = list(_warnings_for_sources(sources))
    if historical_completed_count:
        warnings.append(
            f"根据状态列，{historical_completed_count} 项已在前序周次有记录的事项"
            f"本次仅标记为“{completion_rule.output_text}”，不会调用 LLM 整理旧内容。"
        )
    return ParsedInput(
        source_files=(SourceFile("f1", filename, file_type, sheet),),
        sources=tuple(sources),
        field_mapping={field: headers[index] for field, index in mapping.items()},
        headers=headers,
        warnings=tuple(warnings),
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
    images: dict[int, tuple[SourceImage, ...]] | None = None,
) -> ParsedInput:
    images = images or {}
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
        if not fields.get("content") and images.get(row_number):
            fields["content"] = "附图。"
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
                images=images.get(row_number, ()),
            )
        )
    if not sources:
        raise InputFormatError("没有可生成文档的有效数据行。")
    return ParsedInput(
        source_files=(SourceFile("f1", filename, file_type, sheet),),
        sources=tuple(sources),
        field_mapping={field: headers[index] for field, index in mapping.items()},
        headers=headers,
        warnings=_warnings_for_sources(sources),
        available_sheets=available_sheets,
    )


def _period_header(header: str) -> bool:
    header = re.sub(r"#\d+$", "", header)
    return bool(re.fullmatch(r"(?:(?:\d{4}年)?(?:[1-9]|1[0-2])月|第\d+周|未命名列\d+)", header))


def _headers_match(
    rows: Sequence[Sequence[str]], preset: RulePreset, headers: tuple[str, ...]
) -> bool:
    expected = preset.input.expected_headers
    if not expected or expected == headers:
        return True
    if preset.input.layout != "weekly_matrix":
        return False
    periods = {column.index for column in discover_week_columns(rows)}
    first = min(periods)
    # Only recognized period headings at/after the weekly region are flexible.
    expected_fixed = tuple(h for i, h in enumerate(expected) if i < first or not _period_header(h))
    actual_fixed = tuple(h for i, h in enumerate(headers) if i not in periods)
    return expected_fixed == actual_fixed


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
        rows, selected_sheet, available_sheets = _read_xlsx(
            path, sheet, preferred_sheet=preset.input.sheet_default
        )
        file_type = "xlsx"
    images, image_warnings = {}, ()
    if file_type == "xlsx":
        try:
            images, image_warnings = read_excel_images(path, selected_sheet)
        except ValueError as exc:
            raise InputFormatError(str(exc)) from exc
    filename = display_filename or path.name
    if preset.input.layout == "weekly_matrix":
        parsed = _parse_weekly_matrix(
            rows,
            preset,
            filename,
            file_type,
            selected_sheet,
            available_sheets,
            requested_week,
            field_mapping,
            images,
        )
    else:
        parsed = _parse_records(
            rows,
            preset,
            filename,
            file_type,
            selected_sheet,
            available_sheets,
            field_mapping,
            images,
        )

    if not _headers_match(rows, preset, parsed.headers):
        raise InputFormatError(
            "固定字段表头结构与已确认方案不同，请进入方案适配重新分析、确认并保存新版本。"
        )
    parsed = replace(parsed, warnings=parsed.warnings + image_warnings)
    policy = preset.input.status_policy
    if not policy:
        return parsed
    include = {_normalized_status(value) for value in policy.get("include", [])}
    known = {_normalized_status(value) for value in policy.get("known", [])}
    kept, excluded, warnings = [], [], list(parsed.warnings)
    for record in parsed.sources:
        status = _normalized_status(record.fields.get("status", ""))
        if status in known and status not in include:
            excluded.append(record)
            warnings.append(
                f"第 {record.row_number} 行按已确认状态策略排除：{status}。原始记录保留供追溯。"
            )
        else:
            kept.append(record)
            if status not in known:
                warnings.append(f"第 {record.row_number} 行状态未知或为空，已保留，需核对。")
    if not kept:
        raise InputFormatError("状态策略排除了所有记录，请调整方案后重试。")
    return replace(
        parsed, sources=tuple(kept), excluded_sources=tuple(excluded), warnings=tuple(warnings)
    )


def inspect_input_options(
    path: Path,
    preset: RulePreset,
    *,
    sheet: str | None = None,
) -> InputOptions:
    """Discover worksheet and weekly-column choices without building report content."""
    if not path.is_file():
        raise FileNotFoundError(f"输入文件不存在：{path}")
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise InputFormatError("输入文件必须是 .csv 或 .xlsx。")
    if suffix == ".csv":
        rows = _read_csv(path)
        selected_sheet = None
        sheets: tuple[str, ...] = ()
    else:
        rows, selected_sheet, sheets = _read_xlsx(
            path, sheet, preferred_sheet=preset.input.sheet_default, allow_empty=True
        )

    weeks: tuple[str, ...] = ()
    default_week: str | None = None
    notice = None
    if preset.input.layout == "weekly_matrix":
        try:
            columns = discover_week_columns(rows)
            weeks = tuple(column.label for column in columns)
            try:
                default_week = select_week_column(rows, columns).label
            except CsvFormatError:
                notice = "当前工作表各周暂无有效进展，请核对所选周次或切换工作表。"
        except CsvFormatError:
            notice = "当前工作表未识别到周次，请选择包含周期数据的工作表。"
    elif not rows or not any(any(cell for cell in row) for row in rows):
        notice = "当前工作表为空，请选择其他工作表。"
    return InputOptions(
        sheets=sheets,
        selected_sheet=selected_sheet,
        weeks=weeks,
        default_week=default_week,
        notice=notice,
    )
