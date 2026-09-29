from pathlib import Path

import pytest

from weekly_report_agent.csv_parser import CsvFormatError, load_weekly_data, parse_weekly_data


def test_selects_latest_nonempty_week_and_skips_empty_rows() -> None:
    rows = [
        ["项目", "具体内容", "状态", "2026年9月", ""],
        ["", "", "", "第1周", "第2周"],
        ["项目甲", "建模", "进行中", "第一周内容", ""],
        ["", "无项目", "", "", "不应读取"],
        ["项目乙", "", "完成", "", "第二周内容"],
    ]

    data = parse_weekly_data(rows)

    assert data.label == "2026年9月第2周"
    assert len(data.items) == 1
    assert data.items[0].project == "项目乙"
    assert data.items[0].detail == "周度进展"


def test_requested_week_is_selected() -> None:
    rows = [
        ["项目", "内容", "状态", "2026年9月", ""],
        ["", "", "", "第1周", "第2周"],
        ["项目甲", "建模", "完成", "内容1", "内容2"],
    ]

    data = parse_weekly_data(rows, "2026年9月第1周")

    assert data.week == "第1周"
    assert data.items[0].progress == "内容1"


def test_rejects_malformed_csv_layout() -> None:
    with pytest.raises(CsvFormatError, match="至少需要两行表头和一行数据"):
        parse_weekly_data([["项目"], [""]])


def test_reads_utf8_bom_file(tmp_path: Path) -> None:
    source = tmp_path / "input.csv"
    source.write_text(
        "项目,内容,状态,2026年9月\n,,,第1周\n项目甲,事项,完成,有效进展\n",
        encoding="utf-8-sig",
    )

    data = load_weekly_data(source)

    assert data.items[0].project == "项目甲"
