"""Parse weekly progress records from a multi-row CSV file."""

from __future__ import annotations

import csv
import re
from collections.abc import Sequence
from pathlib import Path

from .models import WeekColumn, WeeklyData, WeeklyItem

WEEK_PATTERN = re.compile(r"^第\d+周$")


class CsvFormatError(ValueError):
    """Raised when the source CSV does not follow the expected layout."""


def read_csv_matrix(path: Path) -> list[list[str]]:
    """Read a UTF-8 CSV while preserving its multi-row header structure."""
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            return [row for row in csv.reader(stream)]
    except UnicodeDecodeError as exc:
        raise CsvFormatError(f"CSV 必须使用 UTF-8 编码：{path}") from exc
    except csv.Error as exc:
        raise CsvFormatError(f"CSV 格式无效：{exc}") from exc


def discover_week_columns(rows: Sequence[Sequence[str]]) -> tuple[WeekColumn, ...]:
    """Discover week columns using the first two CSV header rows."""
    if len(rows) < 2:
        raise CsvFormatError("CSV 至少需要两行表头。")

    month_header, week_header = rows[0], rows[1]
    columns: list[WeekColumn] = []
    for index, raw_week in enumerate(week_header):
        week = raw_week.strip()
        if not WEEK_PATTERN.fullmatch(week):
            continue
        month = ""
        for month_index in range(min(index, len(month_header) - 1), -1, -1):
            candidate = month_header[month_index].strip()
            if re.fullmatch(r"(?:\d{4}年)?(?:[1-9]|1[0-2])月", candidate):
                month = candidate
                break
        columns.append(WeekColumn(index=index, month=month, week=week))

    if not columns:
        raise CsvFormatError("未在第二行表头中找到“第N周”列。")
    return tuple(columns)


def _cell(row: Sequence[str], index: int) -> str:
    return row[index].strip() if index < len(row) else ""


def _has_progress(rows: Sequence[Sequence[str]], column: WeekColumn) -> bool:
    return any(_cell(row, 0) and _cell(row, column.index) for row in rows[2:])


def select_week_column(
    rows: Sequence[Sequence[str]],
    columns: Sequence[WeekColumn],
    requested_week: str | None = None,
) -> WeekColumn:
    """Select a requested week or the latest week containing project progress."""
    if requested_week:
        requested = requested_week.strip()
        matches = [
            column
            for column in columns
            if requested in {column.week, column.label, f"{column.month} {column.week}"}
        ]
        if not matches:
            available = "、".join(column.label or column.week for column in columns)
            raise CsvFormatError(f"找不到周次“{requested}”。可用值：{available}。")
        if len(matches) > 1 and all(column.week == requested for column in matches):
            labels = "、".join(column.label for column in matches)
            raise CsvFormatError(f"周次“{requested}”不唯一，请指定月份：{labels}。")
        return matches[0]

    for column in reversed(columns):
        if _has_progress(rows, column):
            return column
    raise CsvFormatError("所有周次列均无有效项目进展。")


def parse_weekly_data(
    rows: Sequence[Sequence[str]], requested_week: str | None = None
) -> WeeklyData:
    """Parse rows and return non-empty project records for one week."""
    if len(rows) < 3:
        raise CsvFormatError("CSV 至少需要两行表头和一行数据。")
    columns = discover_week_columns(rows)
    selected = select_week_column(rows, columns, requested_week)

    items: list[WeeklyItem] = []
    for row in rows[2:]:
        project = _cell(row, 0)
        progress = _cell(row, selected.index)
        if not project or not progress:
            continue
        items.append(
            WeeklyItem(
                project=project,
                detail=_cell(row, 1) or "周度进展",
                status=_cell(row, 2),
                progress=progress,
            )
        )
    return WeeklyData(month=selected.month, week=selected.week, items=tuple(items))


def load_weekly_data(path: Path, requested_week: str | None = None) -> WeeklyData:
    """Read and parse weekly data from a CSV path."""
    if not path.is_file():
        raise FileNotFoundError(f"输入文件不存在：{path}")
    return parse_weekly_data(read_csv_matrix(path), requested_week)
