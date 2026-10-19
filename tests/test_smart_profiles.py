"""Automatic scheme inference, semantic directory routing and editable numbering."""

import io
import json
from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook

from weekly_report_agent.adaptation import (
    inspect_table,
    inspect_template,
    propose_rule,
    suggest_profile,
    validate_requested_outline,
)
from weekly_report_agent.llm import LLMConfig, LLMService
from weekly_report_agent.models import ParagraphKind
from weekly_report_agent.numbering import validate_numbering
from weekly_report_agent.rules import validate_rule_preset
from weekly_report_agent.service import prepare_report, render_prepared_report
from weekly_report_agent.task_worker import TaskRunner
from weekly_report_agent.web import create_app

TEMPLATE = Path("data/研发三部周报模板-v2.docx")
CSV = (
    "任务名称,本周进展,成果,任务状态\n合成平台,完成AUC 0.81验证,产出模型v2,进行中\n"
    "合成模型,完成72.5%验证,产出评估报告,暂停\n"
)


def scheme(table):
    rule = propose_rule(table, inspect_template(TEMPLATE))
    rule["input"]["column_aliases"]["content"] = ["本周进展"]
    rule["input"]["column_aliases"]["result"] = ["成果"]
    rule["sections"] = [
        {
            "id": "work",
            "title": "研发推进",
            "source_field": "content",
            "source_fields": ["content", "result"],
            "heading_path": ["研发工作", "平台", "研发推进"],
            "group_by": "row",
            "llm_editable": True,
            "match": {},
        }
    ]
    rule["document"]["heading_start_default"] = 9
    rule["document"]["styles"] = {}
    return rule


class ProfileClient:
    def __init__(self):
        self.calls = []

    def complete(self, messages, model):
        context = json.loads(messages[1]["content"])
        self.calls.append(context)
        # Infer an explicit merged body instead of creating one chapter per column.
        table = {
            "layout": context["table"]["layout"],
            "headers": context["table"]["headers"],
            "header_rows": context["table"]["header_rows"],
            "sheet": context["table"]["sheet"],
            "mapping": context["table"]["mapping"],
        }
        return json.dumps(
            {
                "rule": scheme(table),
                "summary": "按项目整合进展与成果。",
                "questions": ["请核对目录归属。"],
            },
            ensure_ascii=False,
        )


def test_smart_upload_runs_in_worker_and_applies_complete_draft(tmp_path):
    mock = ProfileClient()
    app = create_app(llm_service=LLMService(LLMConfig(api_key="mock-only"), mock))
    client = app.test_client()
    response = client.post(
        "/profiles/analyze",
        data={
            "smart": "yes",
            "allow_analysis": "yes",
            "source": (io.BytesIO(CSV.encode()), "synthetic.csv"),
            "desired_outline": "9. 研发工作\n9.1. 平台\n9.1.1. 研发推进",
        },
    )
    assert response.status_code == 302
    assert mock.calls == []
    queue = app.config["TASK_QUEUE"]
    job = queue.claim("smart")
    assert job["payload"]["mode"] == "profile"
    assert client.post(response.location + "/trial", json={"rule": {}}).status_code == 400
    TaskRunner(queue, app.config["SNAPSHOT_STORE"], llm_service=app.config["LLM_SERVICE"]).run(
        job, "smart"
    )
    assert queue.status(job["id"])["state"] == "succeeded"
    assert client.get(response.location + "/status").json["state"] == "ready"
    token = response.location.rsplit("/", 1)[-1]
    analysis = json.loads((app.config["ADAPTATION_ROOT"] / token / "analysis.json").read_text())
    assert analysis["ai_applied"] and analysis["summary"] == "按项目整合进展与成果。"
    assert analysis["rule"]["sections"][0]["source_fields"] == ["content", "result"]
    assert mock.calls[0]["table"]["sample_rows"][0][1] == "完成AUC 0.81验证"
    assert mock.calls[0]["template"]["paragraphs"]
    assert "9.1. 平台" in mock.calls[0]["desired_outline"]
    trial = client.post(
        response.location + "/trial", json={"rule": analysis["rule"], "report_date": "2026-10-16"}
    )
    assert trial.status_code == 200, trial.json
    text = "\n".join(p["text"] for p in trial.json["paragraphs"])
    assert "9.1.1. 研发推进" in text and "产出模型v2" in text and "72.5%" in text
    saved = client.post(
        response.location + "/save", json={"rule": analysis["rule"], "confirmed": True}
    )
    assert saved.status_code == 200
    queued = client.post(
        "/tasks",
        data={
            "preset": saved.json["id"],
            "csv_file": (io.BytesIO(CSV.encode()), "next.csv"),
            "content_mode": "original",
            "report_date": "2026-10-16",
        },
    )
    assert queued.status_code == 202
    job = queue.claim("report")
    TaskRunner(queue, app.config["SNAPSHOT_STORE"]).run(job, "report")
    state = queue.status(job["id"])
    assert state["state"] == "succeeded", state
    page = client.get("/workspace/" + state["result"]["token"])
    assert page.status_code == 200
    assert "9.1.1. 研发推进" in page.text


def test_smart_analysis_requires_explicit_sample_content_consent():
    app = create_app(llm_service=LLMService(LLMConfig(api_key="mock-only"), ProfileClient()))
    response = app.test_client().post(
        "/profiles/analyze", data={"smart": "yes", "source": (io.BytesIO(CSV.encode()), "s.csv")}
    )
    assert response.status_code == 400 and "脱敏" in response.text
    assert app.config["TASK_QUEUE"].claim("no-call") is None


def test_sheets_are_discovered_without_model_calls(tmp_path):
    file = tmp_path / "book.xlsx"
    book = Workbook()
    book.active.title = "汇总"
    book.create_sheet("工作明细")
    book.save(file)
    app = create_app()
    client = app.test_client()
    result = client.post(
        "/profiles/input-options", data={"source": (io.BytesIO(file.read_bytes()), "book.xlsx")}
    )
    assert result.json["sheets"] == ["汇总", "工作明细"] and result.json["selected_sheet"] == "汇总"
    csv = client.post(
        "/profiles/input-options", data={"source": (io.BytesIO(CSV.encode()), "s.csv")}
    )
    assert csv.json["sheets"] == []


def test_multi_field_directory_preserves_sources_and_numbering(tmp_path):
    path = tmp_path / "s.csv"
    path.write_text(CSV)
    table = inspect_table(path)
    rule = scheme(table)
    preset = validate_rule_preset(rule)
    prepared, _ = prepare_report(path, preset=preset)
    assert len(prepared.blocks) == 2
    assert prepared.blocks[0].source_fields == ("content", "result")
    assert prepared.blocks[0].source_ids == ("f1:CSV:2",)
    plan = render_prepared_report(
        prepared, preset, TEMPLATE, tmp_path / "out.docx", date(2026, 10, 16)
    )
    headings = [p.text for p in plan.paragraphs if p.kind.value.startswith("heading")]
    assert headings == ["9. 研发工作", "9.1. 平台", "9.1.1. 研发推进"]
    assert any(p.text.startswith("2、") for p in plan.paragraphs)
    assert any("产出模型v2" in p.text for p in plan.paragraphs)
    rule["sections"][0]["match"] = {"project": ["合成平台"]}
    with pytest.raises(ValueError, match="目录未覆盖来源行：3"):
        prepare_report(path, preset=validate_rule_preset(rule))


def test_model_bad_fields_are_repaired_at_most_twice(tmp_path):
    path = tmp_path / "s.csv"
    path.write_text(CSV)
    table = inspect_table(path)
    analysis = {
        "table": table,
        "document": inspect_template(TEMPLATE),
        "rule": scheme(table),
        "requirements": "",
    }

    class Bad:
        calls = 0

        def complete(self, messages, model):
            self.calls += 1
            return '{"rule":{"id":"escape"}}'

    client = Bad()
    with pytest.raises(ValueError, match="两轮"):
        suggest_profile(path, TEMPLATE, analysis, client=client, model="mock")
    assert client.calls == 2


def test_configured_numbering_changes_only_prefixes(tmp_path):
    path = tmp_path / "s.csv"
    path.write_text(CSV)
    rule = scheme(inspect_table(path))
    rule["document"]["numbering"] = {
        "heading_1": "第{h1}章 ",
        "heading_2": "（{h2}）",
        "body_1": "{n}. ",
    }
    preset = validate_rule_preset(rule)
    prepared, _ = prepare_report(path, preset=preset)
    plan = render_prepared_report(
        prepared, preset, TEMPLATE, tmp_path / "numbered.docx", date(2026, 10, 16)
    )
    assert [p.text for p in plan.paragraphs if p.kind is ParagraphKind.HEADING_1] == [
        "第9章 研发工作"
    ]
    assert [p.text for p in plan.paragraphs if p.kind is ParagraphKind.HEADING_2] == ["（1）平台"]
    assert any("0.81" in p.text for p in plan.paragraphs)
    with pytest.raises(ValueError, match="不支持"):
        validate_numbering({"heading_1": "{__class__}"})


def test_two_level_directory_resets_body_counters(tmp_path):
    path = tmp_path / "s.csv"
    path.write_text(CSV)
    rule = scheme(inspect_table(path))
    rule["sections"] = [
        {
            **rule["sections"][0],
            "id": f"item_{i}",
            "heading_path": ["研发工作", name],
            "match": {"project": [name]},
        }
        for i, name in enumerate(["合成模型", "合成平台"])
    ]
    preset = validate_rule_preset(rule)
    prepared, _ = prepare_report(path, preset=preset)
    plan = render_prepared_report(
        prepared, preset, TEMPLATE, tmp_path / "two.docx", date(2026, 10, 16)
    )
    assert [p.text for p in plan.paragraphs if p.kind is ParagraphKind.HEADING_2] == [
        "9.1. 合成模型",
        "9.2. 合成平台",
    ]


def test_local_1016_file_follows_requested_directory(tmp_path):
    path = Path("data/周报1016.xlsx")
    if not path.exists():
        pytest.skip("用户本地样例不纳入可分发测试夹具")
    table = inspect_table(path)
    rule = propose_rule(table, inspect_template(TEMPLATE))
    rule["document"]["heading_start_default"] = 9
    entries = [
        ("百擎平台", "百擎产品及研发推进", ["百擎平台"]),
        ("LDM", "模型研发", ["大模型预训练", "大模型培训材料"]),
        ("LDM", "模型评估", ["BaaS数据评估"]),
        ("纯三方产品", "评分产品研发", ["纯三方产品"]),
        ("泛行业数据测试", "三方数据源在泛行业的效果评估", ["泛行业数据测试"]),
    ]
    rule["sections"] = [
        {
            "id": f"part_{i}",
            "title": detail,
            "heading_path": ["大模型产品研发", project, detail],
            "source_field": "progress",
            "source_fields": ["progress"],
            "match": {"project": values},
            "group_by": "row",
            "llm_editable": True,
        }
        for i, (project, detail, values) in enumerate(entries)
    ]
    prepared, preset = prepare_report(path, preset=validate_rule_preset(rule))
    plan = render_prepared_report(
        prepared, preset, TEMPLATE, tmp_path / "local.docx", date(2026, 10, 16)
    )
    headings = [p.text for p in plan.paragraphs if p.kind.value.startswith("heading")]
    assert headings == [
        "9. 大模型产品研发",
        "9.1. 百擎平台",
        "9.1.1. 百擎产品及研发推进",
        "9.2. LDM",
        "9.2.1. 模型研发",
        "9.2.2. 模型评估",
        "9.3. 纯三方产品",
        "9.3.1. 评分产品研发",
        "9.4. 泛行业数据测试",
        "9.4.1. 三方数据源在泛行业的效果评估",
    ]
    assert len(prepared.source.sources) == 6
    assert prepared.source.month == ""


def test_explicit_directory_order_is_a_constraint(tmp_path):
    path = tmp_path / "s.csv"
    path.write_text(CSV)
    preset = validate_rule_preset(scheme(inspect_table(path)))
    assert (
        validate_requested_outline("9. 研发工作\n9.1. 平台\n9.1.1. 研发推进", preset.sections) == 9
    )
    with pytest.raises(ValueError, match="目录层级或顺序"):
        validate_requested_outline("9. 研发工作\n9.1. 模型", preset.sections)


@pytest.mark.parametrize("group_by,expected_blocks", [("row", 3), ("project", 2), ("section", 2)])
def test_dynamic_titles_follow_new_data_and_merge_only_same_heading(
    tmp_path, group_by, expected_blocks
):
    source = tmp_path / "dynamic.csv"
    source.write_text(
        "任务名称,本周进展,成果,任务状态\n"
        "新平台,完成AUC 0.81验证,模型v2,进行中\n"
        "新平台,完成72.5%验证,评估报告,进行中\n"
        "新项目,完成部署,版本v3,进行中\n"
    )
    rule = scheme(inspect_table(source))
    rule["sections"][0].update(
        heading_path=["研发工作", "项目"], heading_fields=["", "project"], group_by=group_by
    )
    preset = validate_rule_preset(rule)
    prepared, _ = prepare_report(source, preset=preset)
    assert len(prepared.blocks) == expected_blocks
    if group_by != "row":
        assert len(prepared.blocks[0].source_ids) == 2
        assert "0.81" in prepared.blocks[0].original_text
        assert "72.5%" in prepared.blocks[0].original_text
    plan = render_prepared_report(
        prepared, preset, TEMPLATE, tmp_path / "dynamic.docx", date(2026, 10, 16)
    )
    assert [p.text for p in plan.paragraphs if p.kind.value.startswith("heading")] == [
        "9. 研发工作",
        "9.1. 新平台",
        "9.2. 新项目",
    ]


def test_fixed_directory_can_merge_different_projects_into_one_editable_block(tmp_path):
    source = tmp_path / "fixed.csv"
    source.write_text(CSV)
    rule = scheme(inspect_table(source))
    rule["sections"][0]["group_by"] = "section"
    prepared, _ = prepare_report(source, preset=validate_rule_preset(rule))
    assert len(prepared.blocks) == 1
    assert len(prepared.blocks[0].source_ids) == 2
    assert prepared.blocks[0].llm_editable


@pytest.mark.parametrize("fields", [["project"], ["", "unknown", ""], ["", 2, ""]])
def test_invalid_dynamic_heading_references_are_rejected(tmp_path, fields):
    source = tmp_path / "invalid.csv"
    source.write_text(CSV)
    rule = scheme(inspect_table(source))
    rule["sections"][0]["heading_fields"] = fields
    with pytest.raises(ValueError, match="标题来源"):
        validate_rule_preset(rule)


def test_merged_source_lists_restart_as_siblings_not_nested_items(tmp_path):
    source = tmp_path / "lists.csv"
    source.write_text(
        "任务名称,本周进展,成果,任务状态\n"
        "平台,1、完成AUC 0.81验证,模型v2,进行中\n"
        "平台,1、完成72.5%验证,评估报告,进行中\n"
    )
    rule = scheme(inspect_table(source))
    rule["sections"][0]["group_by"] = "section"
    preset = validate_rule_preset(rule)
    prepared, _ = prepare_report(source, preset=preset)
    plan = render_prepared_report(
        prepared, preset, TEMPLATE, tmp_path / "lists.docx", date(2026, 10, 16)
    )
    top = [p.text for p in plan.paragraphs if p.kind is ParagraphKind.BODY_1]
    assert top == ["1、完成AUC 0.81验证。", "2、模型v2。", "3、完成72.5%验证。", "4、评估报告。"]


def test_model_automatically_proposes_titles_and_merges_rows_per_heading(tmp_path):
    source = tmp_path / "mode.csv"
    source.write_text(CSV)
    table = inspect_table(source)
    client = ProfileClient()
    analysis = {
        "table": table,
        "document": inspect_template(TEMPLATE),
        "rule": scheme(table),
        "requirements": "",
    }
    result = suggest_profile(source, TEMPLATE, analysis, client=client, model="mock")
    assert result["rule"]["sections"][0]["group_by"] == "section"
    assert "heading_mode" not in client.calls[0]
    assert len(client.calls) == 1


def test_blank_week_header_is_identified_as_body_source(tmp_path):
    source = tmp_path / "weekly.csv"
    source.write_text(
        "项目,具体内容,状态,参与人员,计划开始日期,计划结束日期,,\n"
        ",,,,,,第1周,第2周\n"
        "合成平台,研发,进行中,角色A,,,本周AUC 0.81,下周72.5%\n"
    )
    table = inspect_table(source)
    assert table["layout"] == "weekly_matrix"
    assert [(column["name"], column.get("weekly_period")) for column in table["columns"][6:]] == [
        ("未命名列7", "第1周"),
        ("未命名列8", "第2周"),
    ]
    rule = propose_rule(table, inspect_template(TEMPLATE))
    rule["sections"] = [
        {
            "id": "work",
            "title": "研发工作",
            "source_field": "progress",
            "heading_path": ["研发工作"],
            "group_by": "row",
            "llm_editable": True,
        }
    ]
    preset = validate_rule_preset(rule)
    for week, text in [("第1周", "本周AUC 0.81"), ("第2周", "下周72.5%")]:
        prepared, _ = prepare_report(source, preset=preset, requested_week=week)
        assert prepared.blocks[0].original_text == text
        plan = render_prepared_report(
            prepared, preset, TEMPLATE, tmp_path / f"{week}.docx", date(2026, 10, 16)
        )
        assert any(text in paragraph.text for paragraph in plan.paragraphs)


@pytest.mark.parametrize("primary", ["未命名列5", "补充说明"])
def test_weekly_primary_body_mapping_can_be_changed_and_follows_selected_week(tmp_path, primary):
    source = tmp_path / "body-choice.csv"
    source.write_text(
        "项目,具体内容,补充说明,,\n,,,,第2周\n平台,研发,完成AUC 0.81验证,,完成72.5%验证\n"
    )
    table = inspect_table(source)
    # The weekly body is physically the fifth column; another mapped field can replace it.
    rule = propose_rule(table, inspect_template(TEMPLATE))
    rule["input"]["column_aliases"]["content"] = [primary]
    rule["sections"] = [
        {
            "id": "body",
            "title": "研发",
            "heading_path": ["研发"],
            "source_field": "content",
            "source_fields": ["content"],
            "group_by": "section",
            "llm_editable": True,
        }
    ]
    prepared, _ = prepare_report(source, preset=validate_rule_preset(rule))
    expected = "完成72.5%验证" if primary == "未命名列5" else "完成AUC 0.81验证"
    assert prepared.blocks[0].original_text == expected


def test_directory_filters_support_arbitrary_mapped_fields(tmp_path):
    source = tmp_path / "filters.csv"
    source.write_text(
        "任务名称,本周进展,成果,任务状态,任务类型\n"
        "平台,完成AUC 0.81验证,模型v2,进行中,产品\n"
        "平台,完成72.5%验证,报告,进行中,基础\n"
    )
    rule = scheme(inspect_table(source))
    rule["input"]["column_aliases"]["category"] = ["任务类型"]
    section = rule["sections"][0]
    rule["sections"] = [
        dict(
            section,
            id=f"work_{index}",
            heading_path=["研发工作", value],
            group_by="section",
            match={"category": [value]},
        )
        for index, value in enumerate(["产品", "基础"])
    ]
    prepared, _ = prepare_report(source, preset=validate_rule_preset(rule))
    assert len(prepared.blocks) == 2
    assert [len(block.source_ids) for block in prepared.blocks] == [1, 1]
