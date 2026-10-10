from pathlib import Path

import pytest
from openpyxl import Workbook

from weekly_report_agent.input_parser import InputFormatError, parse_input
from weekly_report_agent.rules import load_rule_preset


def test_general_csv_preserves_headers_raw_rows_and_physical_line_numbers(tmp_path: Path) -> None:
    source = tmp_path / "general.csv"
    source.write_text(
        "项目名称,本周工作,问题,后续计划,备注\n"
        "项目甲,完成模型验证,样本偏少,补充样本,保留原始列\n"
        "项目乙,完成接口联调,,,第二条备注\n",
        encoding="utf-8-sig",
    )

    parsed = parse_input(source, load_rule_preset("general"))

    assert parsed.headers == ("项目名称", "本周工作", "问题", "后续计划", "备注")
    assert parsed.field_mapping == {
        "project": "项目名称",
        "content": "本周工作",
        "issues": "问题",
        "next_plan": "后续计划",
    }
    assert [record.row_number for record in parsed.sources] == [2, 3]
    assert parsed.sources[0].source_id == "f1:CSV:2"
    assert parsed.sources[0].raw_values[-1] == "保留原始列"


def test_manual_field_mapping_and_readable_input_errors(tmp_path: Path) -> None:
    source = tmp_path / "mapped.csv"
    source.write_text("任务,进展描述\n任务甲,完成验证\n", encoding="utf-8")
    preset = load_rule_preset("general")

    parsed = parse_input(
        source,
        preset,
        field_mapping={"project": "任务", "content": "进展描述"},
    )
    assert parsed.sources[0].fields["content"] == "完成验证"

    with pytest.raises(InputFormatError, match="指定的列不存在"):
        parse_input(source, preset, field_mapping={"project": "不存在"})

    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(InputFormatError, match="输入文件为空"):
        parse_input(empty, preset)

    invalid_encoding = tmp_path / "gbk.csv"
    invalid_encoding.write_bytes("项目,本周工作\n项目甲,完成\n".encode("gbk"))
    with pytest.raises(InputFormatError, match="UTF-8"):
        parse_input(invalid_encoding, preset)


def test_xlsx_sheet_selection_and_physical_rows(tmp_path: Path) -> None:
    source = tmp_path / "weekly.xlsx"
    workbook = Workbook()
    ignored = workbook.active
    ignored.title = "说明"
    ignored.append(["此页不是数据"])
    data = workbook.create_sheet("周报数据")
    data.append(["项目", "本周工作", "问题与风险", "下周计划"])
    data.append(["项目甲", "完成 AUC 0.81 验证", "", "补充样本"])
    data.append([None, None, None, None])
    data.append(["项目甲", "完成 PSI 分析", "数据待确认", ""])
    workbook.save(source)
    workbook.close()

    parsed = parse_input(source, load_rule_preset("general"), sheet="周报数据")

    assert parsed.available_sheets == ("说明", "周报数据")
    assert parsed.source_files[0].sheet == "周报数据"
    assert [record.row_number for record in parsed.sources] == [2, 4]
    assert parsed.sources[1].source_id == "f1:周报数据:4"

    with pytest.raises(InputFormatError, match="找不到工作表"):
        parse_input(source, load_rule_preset("general"), sheet="不存在")
