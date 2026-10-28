"""CSV/XLSX mapping, weekly selection and completed-status behavior."""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from openpyxl import Workbook

from sheetweave_agent.csv_parser import CsvFormatError, load_weekly_data, parse_weekly_data
from sheetweave_agent.input_parser import InputFormatError, parse_input
from sheetweave_agent.llm import ContentMode, LLMConfig, LLMService
from sheetweave_agent.rules import load_rule_preset
from sheetweave_agent.service import prepare_report, process_report


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


ROOT = Path(__file__).resolve().parents[1]


TEMPLATE = ROOT / "data/研发三部周报模板-v2.docx"


def _matrix_csv(*rows: str) -> str:
    return "项目,具体内容,状态,2026年9月,,\n,,,第1周,第2周,第3周\n" + "\n".join(rows) + "\n"


def _write_matrix(tmp_path: Path, *rows: str) -> Path:
    source = tmp_path / "weekly.csv"
    source.write_text(_matrix_csv(*rows), encoding="utf-8")
    return source


class EchoClient:
    def __init__(self) -> None:
        self.calls: list[tuple[Sequence[dict[str, str]], str]] = []

    def complete(self, messages: Sequence[dict[str, str]], model: str) -> str:
        self.calls.append((messages, model))
        request = json.loads(messages[-1]["content"])
        return json.dumps(
            {
                "blocks": [
                    {"block_id": block["block_id"], "text": block["editable_text"]}
                    for block in request["blocks"]
                ]
            },
            ensure_ascii=False,
        )


def _llm_service(client: EchoClient) -> LLMService:
    return LLMService(
        LLMConfig(
            api_key="test-only-key",
            model="deepseek-chat",
            allowed_models=("deepseek-chat",),
        ),
        client,
    )


@pytest.mark.parametrize("status", ("完成", "已完成", "已经完成", "已结项"))
def test_completed_status_with_prior_week_is_marked_completed(tmp_path: Path, status: str) -> None:
    source = _write_matrix(
        tmp_path,
        f"项目甲,事项A,{status},历史工作,补充内容,本周不再整理的旧内容",
        "项目甲,事项B,进行中,历史工作,继续处理,本周有效内容",
    )

    prepared, _preset = prepare_report(
        source,
        preset=load_rule_preset("rd3", ROOT / "tests/fixtures/rules"),
        requested_week="第3周",
    )

    completed, active = prepared.source.sources
    assert completed.historical_completed is True
    assert completed.fields["progress"] == "已完成"
    assert completed.original_text == "已完成"
    assert active.historical_completed is False
    assert active.original_text == "本周有效内容"
    assert [block.original_text for block in prepared.blocks] == ["已完成", "本周有效内容"]
    assert [block.llm_editable for block in prepared.blocks] == [False, True]


def test_status_is_only_completion_signal(tmp_path: Path) -> None:
    source = _write_matrix(
        tmp_path,
        "项目甲,事项A,进行中,项目已完成,任务已结项,本周继续验证",
    )

    parsed = parse_input(
        source, load_rule_preset("rd3", ROOT / "tests/fixtures/rules"), requested_week="第3周"
    )

    assert parsed.sources[0].historical_completed is False
    assert parsed.sources[0].original_text == "本周继续验证"


def test_historical_completed_block_is_not_sent_to_llm(tmp_path: Path) -> None:
    source = _write_matrix(
        tmp_path,
        "项目甲,旧事项,完成,历史成果,历史补录,不应发送的旧内容",
        "项目乙,当前事项,进行中,前期调研,持续开发,允许发送文本",
    )
    prepared, _preset = prepare_report(
        source,
        preset=load_rule_preset("rd3", ROOT / "tests/fixtures/rules"),
        requested_week="第3周",
    )
    client = EchoClient()

    result = process_report(prepared, ContentMode.POLISH, llm_service=_llm_service(client))

    assert len(client.calls) == 1
    request_text = client.calls[0][0][-1]["content"]
    assert "允许发送文本" in request_text
    assert "不应发送的旧内容" not in request_text
    completed = next(block for block in result.blocks if block.original_text == "已完成")
    assert completed.generated_text is None
    assert completed.llm_editable is False


def _flexible_matrix_preset():
    from sheetweave_agent.rules import preset_payload, validate_rule_preset

    rule = preset_payload(load_rule_preset("general"))
    rule["input"].update(
        layout="weekly_matrix",
        header_rows=2,
        sheet_default="旧工作表",
        required_fields=["project", "content"],
        column_aliases={
            "project": ["项目"],
            "detail": ["具体内容"],
            "status": ["状态"],
            "content": ["2026年9月"],
        },
        expected_headers=["项目", "具体内容", "状态", "2026年9月", "未命名列5"],
    )
    rule["sections"] = [
        {
            "id": "body",
            "title": "工作",
            "source_field": "content",
            "llm_editable": True,
            "group_by": "row",
        }
    ]
    return validate_rule_preset(rule)


def test_missing_preferred_sheet_falls_back_but_explicit_selection_is_strict(tmp_path):
    from dataclasses import replace

    from sheetweave_agent.input_parser import inspect_input_options

    path = tmp_path / "weekly.xlsx"
    book = Workbook()
    book.active.title = "本周数据"
    book.active.append(["项目", "本周工作"])
    book.active.append(["合成项目", "本周进展"])
    other = book.create_sheet("其他数据")
    other.append(["项目", "本周工作"])
    other.append(["合成项目", "其他进展"])
    book.save(path)
    base = load_rule_preset("general")
    preset = replace(base, input=replace(base.input, sheet_default="旧名称"))
    assert inspect_input_options(path, preset).selected_sheet == "本周数据"
    assert parse_input(path, preset).sources[0].fields["content"] == "本周进展"
    assert parse_input(path, preset, sheet="其他数据").sources[0].fields["content"] == "其他进展"
    preferred = replace(base, input=replace(base.input, sheet_default="其他数据"))
    assert parse_input(path, preferred).source_files[0].sheet == "其他数据"
    with pytest.raises(InputFormatError, match="找不到工作表"):
        parse_input(path, preset, sheet="不存在")


@pytest.mark.parametrize("weeks", [1, 2, 3])
def test_matrix_month_and_week_columns_are_runtime_scope(tmp_path, weeks):
    import csv

    from sheetweave_agent.input_parser import inspect_input_options

    path = tmp_path / "matrix.csv"
    with path.open("w", newline="") as output:
        csv.writer(output).writerows(
            [
                ["项目", "具体内容", "状态", "2026年10月", *([""] * (weeks - 1))],
                ["", "", "", *[f"第{i}周" for i in range(1, weeks + 1)]],
                ["合成项目", "事项", "进行中", *[f"进展{i}" for i in range(1, weeks + 1)]],
            ]
        )
    preset = _flexible_matrix_preset()
    options = inspect_input_options(path, preset)
    assert options.default_week == f"2026年10月第{weeks}周"
    assert len(options.weeks) == weeks
    assert parse_input(path, preset).sources[0].fields["content"] == f"进展{weeks}"
    assert parse_input(path, preset, requested_week="第1周").sources[0].fields["content"] == "进展1"
    with pytest.raises(InputFormatError, match="找不到周次"):
        parse_input(path, preset, requested_week="第9周")


def test_fixed_header_changes_still_require_confirmation(tmp_path):
    path = tmp_path / "matrix.csv"
    path.write_text(
        "项目,任务说明,状态,2026年10月,\n,,,第1周,第2周\n合成项目,事项,进行中,进展1,进展2\n"
    )
    with pytest.raises(InputFormatError, match="表头结构"):
        parse_input(path, _flexible_matrix_preset())


def test_options_allow_switching_away_from_empty_first_sheet(tmp_path):
    from sheetweave_agent.input_parser import inspect_input_options

    path = tmp_path / "matrix.xlsx"
    book = Workbook()
    book.active.title = "说明"
    data = book.create_sheet("10月周报")
    for row in [
        ["项目", "具体内容", "状态", "2026年10月", ""],
        ["", "", "", "第1周", "第2周"],
        ["合成项目", "事项", "进行中", "第一周进展", "第二周进展"],
    ]:
        data.append(row)
    book.save(path)
    preset = _flexible_matrix_preset()
    options = inspect_input_options(path, preset)
    assert options.sheets == ("说明", "10月周报")
    assert options.notice and options.weeks == ()
    selected = inspect_input_options(path, preset, sheet="10月周报")
    assert selected.weeks == ("2026年10月第1周", "2026年10月第2周")
    assert parse_input(path, preset, sheet="10月周报", requested_week="第1周").week == "第1周"


def test_saved_profile_web_options_and_queue_honor_sheet_and_week(tmp_path):
    import io

    from sheetweave_agent.profiles import ProfileStore
    from sheetweave_agent.rules import preset_payload
    from sheetweave_agent.task_worker import TaskRunner
    from sheetweave_agent.web import create_app

    path = tmp_path / "matrix.xlsx"
    book = Workbook()
    book.active.title = "说明"
    data = book.create_sheet("新名称")
    for row in [
        ["项目", "具体内容", "状态", "2026年10月", ""],
        ["", "", "", "第1周", "第2周"],
        ["合成项目", "事项", "进行中", "第一周进展", "第二周进展"],
    ]:
        data.append(row)
    book.save(path)
    template = Path(__file__).resolve().parents[1] / "data/研发三部周报模板-v2.docx"
    ProfileStore.from_environment().save(
        preset_payload(_flexible_matrix_preset()), template, profile_id="p_flexible"
    )
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    def form(**values):
        return {
            "preset": "p_flexible",
            "csv_file": (io.BytesIO(path.read_bytes()), "matrix.xlsx"),
            **values,
        }

    initial = client.post("/input-options", data=form())
    assert initial.status_code == 200 and initial.json["sheets"] == ["说明", "新名称"]
    assert initial.json["notice"]
    options = client.post("/input-options", data=form(sheet="新名称"))
    assert options.json["weeks"] == ["2026年10月第1周", "2026年10月第2周"]
    response = client.post(
        "/tasks",
        data=form(
            sheet="新名称",
            week="2026年10月第1周",
            report_date="2026-10-23",
            content_mode="original",
        ),
    )
    assert response.status_code == 202
    queue = app.config["TASK_QUEUE"]
    runner = TaskRunner.from_environment(queue)
    runner.run(queue.claim("scope-test"), "scope-test")
    status = queue.status(response.json["id"])
    assert status["state"] == "succeeded", status.get("error")
    session = app.config["SNAPSHOT_STORE"].load_session(status["result"]["token"])
    assert session.prepared.source.source_files[0].sheet == "新名称"
    assert session.prepared.source.week == "第1周"
    assert session.prepared.blocks[0].original_text == "第一周进展"
