"""Core profile inference, routing, immutable versions and safe administration."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from openpyxl import Workbook

from sheetweave_agent.adaptation import (
    inspect_table,
    inspect_template,
    propose_rule,
    suggest_profile,
    validate_adaptation,
    validate_requested_outline,
)
from sheetweave_agent.cli import main
from sheetweave_agent.cli import main as cli_main
from sheetweave_agent.document_style import default_styles, validate_styles
from sheetweave_agent.docx_renderer import DOCUMENT_PART, WORD_NS
from sheetweave_agent.input_parser import parse_input
from sheetweave_agent.llm import LLMConfig, LLMService
from sheetweave_agent.models import ParagraphKind
from sheetweave_agent.numbering import validate_numbering
from sheetweave_agent.profiles import ProfileStore
from sheetweave_agent.rules import (
    validate_rule_preset,
)
from sheetweave_agent.service import prepare_report, render_prepared_report
from sheetweave_agent.task_worker import TaskRunner
from sheetweave_agent.validator import ReportValidationError, validate_report
from sheetweave_agent.web import create_app
from sheetweave_agent.web_sessions import session_preset

TEMPLATE = Path("tests/fixtures/templates/reference.docx")


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


SYNTHETIC = (
    "任务名称,本周进展,任务状态,负责人,备注\n"
    "合成任务,周一验证lgb AUC 0.81,进行中,测试人员,仅在本地保存\n"
    "合成任务,完成72.5%验证,暂停,测试人员,追溯备注\n"
    "合成任务,保留未知记录,新状态,测试人员,原始值\n"
)


Q = f"{{{WORD_NS}}}"


def source(tmp_path):
    path = tmp_path / "synthetic.csv"
    path.write_text(SYNTHETIC, encoding="utf-8")
    return path


def proposal(tmp_path):
    path = source(tmp_path)
    table = inspect_table(path)
    document = inspect_template(TEMPLATE)
    rule = propose_rule(table, document)
    rule["document"]["styles"] = default_styles()
    rule["document"]["styles"]["heading_1"].update(font="SimHei", size_pt=18, line_spacing=2)
    rule["document"]["styles"]["body_1"].update(size_pt=12, first_line_chars=1)
    rule["input"]["status_policy"] = {
        "known": ["进行中", "暂停"],
        "include": ["进行中"],
        "unknown": "keep",
    }
    return path, table, rule


def modify_package(path, output, callback):
    with zipfile.ZipFile(path) as package, zipfile.ZipFile(output, "w") as target:
        for info in package.infolist():
            payload = package.read(info)
            if info.filename == DOCUMENT_PART:
                root = ET.fromstring(payload)
                callback(root)
                payload = ET.tostring(root)
            target.writestr(info, payload)


def test_input_policy_keeps_unknown_and_preserves_excluded_originals(tmp_path):
    path, table, rule = proposal(tmp_path)
    preset = validate_adaptation(rule, table)
    parsed = parse_input(path, preset)
    assert [r.row_number for r in parsed.sources] == [2, 4]
    assert parsed.sources[0].raw_values[-1] == "仅在本地保存"
    assert parsed.excluded_sources[0].row_number == 3
    assert parsed.excluded_sources[0].fields["content"] == "完成72.5%验证"
    assert any("状态未知" in item for item in parsed.warnings)
    assert any("第 3 行" in item and "排除" in item for item in parsed.warnings)


def test_unfamiliar_headers_need_confirmation_and_changed_signature_stops(tmp_path):
    path, table, rule = proposal(tmp_path)
    preset = validate_adaptation(rule, table)
    path.write_text(SYNTHETIC.replace("备注", "新备注"), encoding="utf-8")
    with pytest.raises(ValueError, match="表头结构"):
        parse_input(path, preset)
    path.write_text("陌生甲,陌生乙\na,b\n", encoding="utf-8")
    table = inspect_table(path)
    assert len(table["pending"]) >= 2
    with pytest.raises(ValueError, match="唯一存在"):
        validate_adaptation(propose_rule(table, inspect_template(TEMPLATE)), table)


def test_custom_format_contract_and_second_run_tampering(tmp_path):
    path, table, rule = proposal(tmp_path)
    preset = validate_adaptation(rule, table)
    prepared, _ = prepare_report(path, preset=preset)
    output = tmp_path / "custom.docx"
    plan = render_prepared_report(prepared, preset, TEMPLATE, output, date(2026, 10, 16))
    assert any("2026年10月12日" in p.text for p in plan.paragraphs)
    assert any("LightGBM" in p.text and "0.81" in p.text for p in plan.paragraphs)
    with zipfile.ZipFile(output) as package:
        root = ET.fromstring(package.read(DOCUMENT_PART))
    headings = root.findall(f"{Q}body/{Q}p")
    heading = headings[2]
    assert heading.find(f"{Q}r/{Q}rPr/{Q}sz").get(Q + "val") == "36"
    assert heading.find(f"{Q}r/{Q}rPr/{Q}rFonts").get(Q + "ascii") == "SimHei"
    assert heading.find(f"{Q}pPr/{Q}spacing").get(Q + "line") == "480"

    def corrupt(root):
        paragraph = root.findall(f"{Q}body/{Q}p")[-1]
        duplicate = ET.fromstring(ET.tostring(paragraph.find(Q + "r")))
        properties = ET.SubElement(duplicate, Q + "rPr")
        ET.SubElement(properties, Q + "sz", {Q + "val": "12"})
        paragraph.append(duplicate)

    broken = tmp_path / "broken.docx"
    modify_package(output, broken, corrupt)
    with pytest.raises(ReportValidationError, match="字号"):
        validate_report(
            broken,
            1,
            report_title=preset.document.report_title,
            heading_titles=tuple(section.title for section in preset.sections),
            styles=preset.document.styles,
        )


@pytest.mark.parametrize(
    "style",
    [
        {"heading_4": {}},
        {"body_1": {"font": ""}},
        {"body_1": {"size_pt": 10.3}},
        {"body_1": {"line_spacing": 0.5}},
        {"body_1": {"bold": "false"}},
        {"body_1": {"unknown": 1}},
        {"body_1": {"after_pt": float("nan")}},
    ],
)
def test_invalid_format_contracts_rejected(style):
    with pytest.raises(ValueError):
        validate_styles(style)


def test_template_inheritance_and_unsupported_tables(tmp_path):
    template = tmp_path / "headed.docx"

    def change(root):
        body = root.find(Q + "body")
        for child in list(body):
            if child.tag != Q + "sectPr":
                body.remove(child)
        p = ET.Element(Q + "p")
        props = ET.SubElement(p, Q + "pPr")
        ET.SubElement(props, Q + "outlineLvl", {Q + "val": "0"})
        run = ET.SubElement(p, Q + "r")
        rpr = ET.SubElement(run, Q + "rPr")
        ET.SubElement(rpr, Q + "sz", {Q + "val": "36"})
        ET.SubElement(run, Q + "t").text = "合成标题"
        body.insert(0, p)

    modify_package(TEMPLATE, template, change)
    result = inspect_template(template)
    assert result["styles"]["heading_1"]["size_pt"] == 18
    assert result["observations"][0]["paragraph"] == 1
    table_template = tmp_path / "table.docx"
    modify_package(
        template, table_template, lambda root: ET.SubElement(root.find(Q + "body"), Q + "tbl")
    )
    result = inspect_template(table_template)
    assert not result["supported"] and "表格" in result["unsupported"]


def test_profile_versions_persist_restart_and_metadata_only_updates_on_use(tmp_path):
    _, _, rule = proposal(tmp_path)
    library = ProfileStore.from_environment()
    profile_id, version = library.save(rule, TEMPLATE)
    assert version == 1 and library.list()[0]["last_used"] is None
    library.load(profile_id)
    assert library.list()[0]["usage_count"] == 0
    rule["document"]["styles"]["body_1"]["size_pt"] = 13
    assert library.save(rule, TEMPLATE, profile_id=profile_id, expected_version=1)[1] == 2
    restarted = ProfileStore(library.root)
    assert restarted.load(profile_id, 1)[0].document.styles["body_1"]["size_pt"] == 12
    assert restarted.load(profile_id)[0].document.styles["body_1"]["size_pt"] == 13
    with pytest.raises(ValueError, match="已更新"):
        library.save(rule, TEMPLATE, profile_id=profile_id, expected_version=1)
    library.used(profile_id)
    library.used(profile_id, successful=True)
    assert restarted.list()[0]["usage_count"] == 1
    assert restarted.list()[0]["last_successful"]
    for path in library.root.glob("*/*/*"):
        assert path.stat().st_mode & 0o777 == 0o600


def draft_workflow(app):
    client = app.test_client()
    response = client.post(
        "/profiles/analyze", data={"source": (io.BytesIO(SYNTHETIC.encode()), "synthetic.csv")}
    )
    assert response.status_code == 302
    draft_url = response.location
    token = draft_url.rsplit("/", 1)[-1]
    directory = app.config["ADAPTATION_ROOT"] / token
    analysis = json.loads((directory / "analysis.json").read_text())
    rule = analysis["rule"]
    rule["document"]["styles"] = default_styles()
    rule["document"]["styles"]["body_1"]["size_pt"] = 12
    return client, draft_url, rule


def test_web_confirmation_trial_and_long_lived_reuse(tmp_path):
    app = create_app()
    client, draft_url, rule = draft_workflow(app)
    assert client.get(draft_url).status_code == 200
    assert client.get("/profiles").status_code == 200
    assert client.post(draft_url + "/assist").status_code == 400
    assert (
        client.post(draft_url + "/save", json={"rule": rule, "confirmed": False}).status_code == 400
    )
    assert (
        client.post(draft_url + "/save", json={"rule": rule, "confirmed": True}).status_code == 400
    )
    trial = client.post(draft_url + "/trial", json={"rule": rule, "report_date": "2026-10-16"})
    assert trial.status_code == 200, trial.json
    assert trial.json["retained"] == 3
    assert client.get(trial.json["download_url"]).status_code == 200
    edited = json.loads(json.dumps(rule))
    edited["name"] = "未试生成的修改"
    assert (
        client.post(draft_url + "/save", json={"rule": edited, "confirmed": True}).status_code
        == 400
    )
    saved = client.post(draft_url + "/save", json={"rule": rule, "confirmed": True})
    assert saved.status_code == 200, saved.json
    profile_id = saved.json["id"]
    homepage = client.get(saved.json["url"]).get_data(as_text=True)
    assert f'value="{profile_id}" selected' in homepage
    # The next upload reuses saved template/config; user does not upload DOCX again.
    submitted = client.post(
        "/tasks",
        data={
            "preset": profile_id,
            "csv_file": (io.BytesIO(SYNTHETIC.encode()), "next.csv"),
            "report_date": "2026-10-16",
            "content_mode": "original",
        },
    )
    assert submitted.status_code == 202, submitted.json
    queue = app.config["TASK_QUEUE"]
    job = queue.claim("test")
    assert job["payload"]["preset_payload"]["id"] == profile_id
    runner = TaskRunner(queue, app.config["SNAPSHOT_STORE"])
    runner.run(job, "test")
    state = queue.status(submitted.json["id"])
    assert state["state"] == "succeeded", state
    session = app.config["SNAPSHOT_STORE"].load_session(state["result"]["token"])
    assert session_preset(session).document.styles["body_1"]["size_pt"] == 12
    assert client.get(f"/workspace/{session.token}").status_code == 200
    metadata = app.config["PROFILE_STORE"].list()[0]
    assert metadata["usage_count"] == 1 and metadata["last_successful"]


def test_queued_and_editable_reports_freeze_config_across_update_and_deletion(tmp_path):
    app = create_app()
    path, _, rule = proposal(tmp_path)
    library = app.config["PROFILE_STORE"]
    profile_id, _ = library.save(rule, TEMPLATE)
    client = app.test_client()
    response = client.post(
        "/tasks",
        data={
            "preset": profile_id,
            "csv_file": (io.BytesIO(path.read_bytes()), "s.csv"),
            "report_date": "2026-10-16",
            "content_mode": "original",
        },
    )
    assert response.status_code == 202
    queue, snapshots = app.config["TASK_QUEUE"], app.config["SNAPSHOT_STORE"]
    with pytest.raises(ValueError, match="引用"):
        library.delete(profile_id, queue)
    rule["document"]["styles"]["body_1"]["size_pt"] = 14
    library.save(rule, TEMPLATE, profile_id=profile_id, expected_version=1)
    job = queue.claim("owner")
    TaskRunner(queue, snapshots).run(job, "owner")
    result = queue.status(response.json["id"])
    assert result["state"] == "succeeded", result
    session = snapshots.load_session(result["result"]["token"])
    assert session_preset(session).document.styles["body_1"]["size_pt"] == 12
    library.delete(profile_id, queue)
    assert library.list() == []
    assert client.get(f"/workspace/{session.token}").status_code == 200
    assert session_preset(session).document.styles["body_1"]["size_pt"] == 12
    # Save after deletion still uses the frozen rule instead of the deleted library entry.
    from sheetweave_agent.service import edit_report_block

    prepared = edit_report_block(
        session.prepared, session.prepared.blocks[0].block_id, "人工确认0.81"
    )
    render_prepared_report(
        prepared,
        session_preset(session),
        session.template_path,
        tmp_path / "edited.docx",
        session.report_date,
        session.section_number,
    )
    assert prepared.source.excluded_sources[0].fields["content"] == "完成72.5%验证"


def test_profile_cli_reuses_saved_template(tmp_path):
    path, _, rule = proposal(tmp_path)
    profile_id, _ = ProfileStore.from_environment().save(rule, TEMPLATE)
    output = tmp_path / "cli.docx"
    assert (
        cli_main(
            [
                "--profile",
                profile_id,
                "--input",
                str(path),
                "--output",
                str(output),
                "--report-date",
                "2026-10-16",
            ]
        )
        == 0
    )
    assert output.is_file()


def test_adaptation_cannot_change_layout_or_silently_ignore_unknown_rules(tmp_path):
    _, table, rule = proposal(tmp_path)
    rule["document"]["numbering"] = "arbitrary"
    with pytest.raises(ValueError, match="不支持"):
        validate_adaptation(rule, table)
    del rule["document"]["numbering"]
    rule["input"]["status_policy"]["unknown"] = "drop"
    with pytest.raises(ValueError, match="未知状态"):
        validate_adaptation(rule, table)


def test_duplicate_header_does_not_silently_select_first_column(tmp_path):
    path = tmp_path / "duplicate.csv"
    path.write_text("项目,项目,本周工作\n甲,乙,合成记录\n", encoding="utf-8")
    table = inspect_table(path)
    assert "project" not in table["mapping"]
    assert any("多个候选" in item for item in table["pending"])


def test_profile_does_not_expire_when_drafts_and_sessions_expire(tmp_path):
    app = create_app()
    _, draft_url, rule = draft_workflow(app)
    library = app.config["PROFILE_STORE"]
    profile_id, _ = library.save(rule, TEMPLATE)
    app.config["SNAPSHOT_STORE"].cleanup_expired(now=10**12)
    assert library.load(profile_id)[0].id == profile_id
    assert app.test_client().get(draft_url).status_code == 200


def admin_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("manage_profiles", "scripts/manage_profiles.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_admin_menu_keeps_active_profile_and_exits_cleanly_on_eof(tmp_path, monkeypatch, capsys):
    from sheetweave_agent.task_queue import TaskQueue

    module = admin_module()
    _, _, rule = proposal(tmp_path)
    library = ProfileStore.from_environment()
    profile_id, _ = library.save(rule, TEMPLATE)
    queue = TaskQueue.from_environment()
    task, _ = queue.new_directory()
    queue.enqueue(task, "generate", {"preset": profile_id})
    answers = iter(["5", "1", profile_id])

    def answer(_):
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", answer)
    assert module.main(["--no-env"]) == 0
    assert "仍有任务引用" in capsys.readouterr().out
    assert library.load(profile_id)


def test_profile_rename_preserves_immutable_history_rules_template_and_usage(tmp_path):
    _, _, rule = proposal(tmp_path)
    library = ProfileStore.from_environment()
    profile_id, _ = library.save(
        rule, TEMPLATE, description="合成说明", evidence={"synthetic": True}
    )
    library.used(profile_id)
    library.used(profile_id, successful=True)
    old_preset, old_template, old_metadata = library.load(profile_id)
    before = library.list()[0]
    old_json = (old_template.parent / "profile.json").read_bytes()
    assert library.rename(profile_id, "新的方案名称", expected_version=1) == 2
    preset, template, metadata = library.load(profile_id)
    assert preset.name == library.list()[0]["name"] == "新的方案名称"
    assert template.read_bytes() == old_template.read_bytes()
    assert (old_template.parent / "profile.json").read_bytes() == old_json
    assert library.load(profile_id, 1)[0].name == old_preset.name
    assert metadata["description"] == old_metadata["description"]
    assert metadata["evidence"] == old_metadata["evidence"]
    for key in ["usage_count", "created", "last_used", "last_successful"]:
        assert library.list()[0][key] == before[key]
    assert {**metadata["rule"], "name": old_preset.name} == old_metadata["rule"]
    assert library.rename(profile_id, "新的方案名称") == 2


def test_empty_library_directs_user_to_adaptation_and_disables_generation():
    response = create_app().test_client().get("/")
    assert response.status_code == 200
    assert "新建并保存方案" in response.text
    assert 'value="rd3"' not in response.text and 'value="general"' not in response.text
    assert 'name="preset" required disabled' in response.text
    assert 'type="submit" disabled formaction="/preview"' in response.text


def test_cli_and_internal_service_have_no_implicit_rule(capsys, tmp_path):
    with pytest.raises(SystemExit) as error:
        main(["--input", str(tmp_path / "not-read.csv")])
    assert error.value.code == 2 and "--profile" in capsys.readouterr().err
    with pytest.raises(ValueError, match="新建并保存方案"):
        prepare_report(Path("not-read.csv"))
