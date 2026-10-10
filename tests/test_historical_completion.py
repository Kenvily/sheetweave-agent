import io
import json
import zipfile
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest

from weekly_report_agent.input_parser import parse_input
from weekly_report_agent.llm import ContentMode, LLMConfig, LLMService
from weekly_report_agent.rules import RulePresetError, load_rule_preset, validate_rule_preset
from weekly_report_agent.service import generate_report, prepare_report, process_report
from weekly_report_agent.web import create_app

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "data/研发三部周报模板.docx"


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


def _custom_matrix_payload() -> dict:
    return {
        "id": "matrix",
        "name": "其他部门周报",
        "schema_version": 1,
        "input": {
            "layout": "weekly_matrix",
            "header_rows": 2,
            "required_fields": ["project", "detail"],
            "column_aliases": {
                "project": ["项目"],
                "detail": ["具体内容"],
                "status": ["状态"],
            },
            "historical_completion": {
                "enabled": True,
                "completed_statuses": ["完成", "已完成"],
                "output_text": "已完成",
            },
        },
        "sections": [
            {
                "id": "weekly_work",
                "title": "本周工作",
                "source_field": "progress",
                "group_by": "project",
                "llm_editable": True,
            }
        ],
        "document": {
            "report_title": "其他部门周报",
            "template": "template.docx",
            "heading_start_default": 1,
        },
    }


@pytest.mark.parametrize("status", ("完成", "已完成", "已经完成", "已结项"))
def test_completed_status_with_prior_week_is_marked_completed(tmp_path: Path, status: str) -> None:
    source = _write_matrix(
        tmp_path,
        f"项目甲,事项A,{status},历史工作,补充内容,本周不再整理的旧内容",
        "项目甲,事项B,进行中,历史工作,继续处理,本周有效内容",
    )

    prepared, _preset = prepare_report(source, requested_week="第3周")

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

    parsed = parse_input(source, load_rule_preset("rd3"), requested_week="第3周")

    assert parsed.sources[0].historical_completed is False
    assert parsed.sources[0].original_text == "本周继续验证"


def test_first_week_and_rows_without_prior_content_keep_current_text(tmp_path: Path) -> None:
    first_week = _write_matrix(tmp_path, "项目甲,事项A,完成,首周成果,,")
    parsed = parse_input(first_week, load_rule_preset("rd3"), requested_week="第1周")
    assert parsed.sources[0].historical_completed is False
    assert parsed.sources[0].original_text == "首周成果"

    no_prior = _write_matrix(tmp_path, "项目甲,事项A,完成,,本周首次完成,")
    parsed = parse_input(no_prior, load_rule_preset("rd3"), requested_week="第2周")
    assert parsed.sources[0].historical_completed is False
    assert parsed.sources[0].original_text == "本周首次完成"


def test_completed_row_with_empty_current_week_is_still_included(tmp_path: Path) -> None:
    source = _write_matrix(tmp_path, "项目甲,事项A,完成,历史成果,,")

    parsed = parse_input(source, load_rule_preset("rd3"), requested_week="第3周")

    assert len(parsed.sources) == 1
    assert parsed.sources[0].historical_completed is True
    assert parsed.sources[0].original_text == "已完成"


def test_rule_is_reusable_by_other_weekly_matrix_presets(tmp_path: Path) -> None:
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "matrix.json").write_text(
        json.dumps(_custom_matrix_payload(), ensure_ascii=False), encoding="utf-8"
    )
    source = _write_matrix(
        tmp_path,
        "项目甲,已完成事项,完成,历史成果,,旧内容",
        "项目甲,进行中事项,进行中,历史调研,,当前工作",
    )

    prepared, preset = prepare_report(
        source,
        preset_id="matrix",
        requested_week="第3周",
        rules_dir=rules_dir,
    )

    assert preset.id == "matrix"
    assert [block.original_text for block in prepared.blocks] == ["已完成", "当前工作"]
    assert [block.llm_editable for block in prepared.blocks] == [False, True]
    assert all(block.group_key == "项目甲" for block in prepared.blocks)


def test_historical_completed_block_is_not_sent_to_llm(tmp_path: Path) -> None:
    source = _write_matrix(
        tmp_path,
        "项目甲,旧事项,完成,历史成果,历史补录,不应发送的旧内容",
        "项目乙,当前事项,进行中,前期调研,持续开发,允许发送文本",
    )
    prepared, _preset = prepare_report(source, requested_week="第3周")
    client = EchoClient()

    result = process_report(prepared, ContentMode.POLISH, llm_service=_llm_service(client))

    assert len(client.calls) == 1
    request_text = client.calls[0][0][-1]["content"]
    assert "允许发送文本" in request_text
    assert "不应发送的旧内容" not in request_text
    completed = next(block for block in result.blocks if block.original_text == "已完成")
    assert completed.generated_text is None
    assert completed.llm_editable is False


def test_docx_keeps_item_but_replaces_old_content_with_completed(tmp_path: Path) -> None:
    source = _write_matrix(
        tmp_path,
        "项目甲,旧事项,完成,历史成果,历史补录,不应写入文档",
        "项目乙,当前事项,进行中,前期调研,持续开发,应写入文档",
    )
    output = tmp_path / "report.docx"

    generate_report(source, TEMPLATE, output, date(2026, 10, 10), requested_week="第3周")

    with zipfile.ZipFile(output) as package:
        document = package.read("word/document.xml").decode("utf-8")
    assert "旧事项" in document
    assert "已完成" in document
    assert "不应写入文档" not in document
    assert "应写入文档" in document


def test_web_preview_marks_status_rule_result() -> None:
    app = create_app(TEMPLATE)
    app.config["TESTING"] = True
    response = app.test_client().post(
        "/preview",
        data={
            "csv_file": (
                io.BytesIO(
                    _matrix_csv(
                        "项目甲,旧事项,完成,历史成果,历史补录,旧内容",
                        "项目乙,当前事项,进行中,前期调研,持续开发,保留内容",
                    ).encode()
                ),
                "weekly.csv",
            ),
            "preset": "rd3",
            "week": "第3周",
            "report_date": "2026-10-10",
            "content_mode": "original",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert "2 行参与生成" in response.text
    assert "根据状态列" in response.text
    assert "已完成 · 跳过 LLM" in response.text
    assert "旧事项" in response.text
    assert "已完成" in response.text


def test_historical_completion_rule_requires_week_matrix_and_status_mapping() -> None:
    payload = _custom_matrix_payload()
    payload["input"]["layout"] = "records"
    with pytest.raises(RulePresetError, match="weekly_matrix"):
        validate_rule_preset(payload)

    payload = _custom_matrix_payload()
    del payload["input"]["column_aliases"]["status"]
    with pytest.raises(RulePresetError, match="status 列别名"):
        validate_rule_preset(payload)
