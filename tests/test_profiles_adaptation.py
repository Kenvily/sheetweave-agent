"""Adaptation boundaries, persistent versions and queued generation regressions."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from openpyxl import Workbook

from weekly_report_agent.adaptation import (
    assist_mapping,
    inspect_requirements,
    inspect_table,
    inspect_template,
    propose_rule,
    validate_adaptation,
)
from weekly_report_agent.cli import main as cli_main
from weekly_report_agent.document_style import default_styles, validate_styles
from weekly_report_agent.docx_renderer import DOCUMENT_PART, WORD_NS
from weekly_report_agent.input_parser import parse_input
from weekly_report_agent.profiles import ProfileStore
from weekly_report_agent.service import prepare_report, render_prepared_report
from weekly_report_agent.task_worker import TaskRunner
from weekly_report_agent.validator import ReportValidationError, validate_report
from weekly_report_agent.web import create_app
from weekly_report_agent.web_sessions import session_preset

TEMPLATE = Path("data/研发三部周报模板-v2.docx")
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


def test_optional_fields_can_be_omitted_and_kept_in_source(tmp_path):
    path, table, rule = proposal(tmp_path)
    rule["sections"] = [
        section for section in rule["sections"] if section["source_field"] == "content"
    ]
    prepared, _ = prepare_report(path, preset=validate_adaptation(rule, table))
    assert len(prepared.blocks) == 1
    assert len(prepared.source.sources) == 2
    assert prepared.source.sources[0].fields["owner"] == "测试人员"


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
        duplicate.find(f"{Q}rPr/{Q}sz").set(Q + "val", "12")
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


def test_written_requirements_override_observations_with_explicit_evidence():
    assert inspect_template(TEMPLATE)["written_requirements"]
    result = inspect_requirements(
        "全篇行间距1.5倍行间距；\n"
        "一级标题1. 微软雅黑加粗16号字体，段前段后6磅；\n"
        "正文使用默认正文字体10.5号，首行缩进2字符；\n"
        "研发三部从9开始写。"
    )
    assert result["styles"]["heading_1"]["size_pt"] == 16
    assert result["styles"]["heading_1"]["before_pt"] == 6
    assert result["styles"]["body_2"]["first_line_chars"] == 2
    assert result["heading_start"] == 9
    assert result["pending"] and result["evidence"]


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


def test_xlsx_sheet_and_nonfirst_header_row(tmp_path):
    path = tmp_path / "source.xlsx"
    workbook = Workbook()
    workbook.active.title = "说明"
    worksheet = workbook.create_sheet("进展")
    worksheet.append(["合成表格标题"])
    worksheet.merge_cells("A1:C1")
    worksheet.append(["任务名称", "本周进展", "任务状态"])
    worksheet.append(["合成事项", "完成0.81验证", "进行中"])
    workbook.save(path)
    table = inspect_table(path, "进展")
    assert table["header_rows"] == 2 and table["sheets"] == ("说明", "进展")
    rule = propose_rule(table, inspect_template(TEMPLATE))
    preset = validate_adaptation(rule, table)
    parsed = parse_input(path, preset)
    assert parsed.source_files[0].sheet == "进展"
    assert parsed.sources[0].row_number == 3


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


def test_bounded_assistant_sends_only_headers_and_rejects_invented_columns(tmp_path):
    path = source(tmp_path)
    table = inspect_table(path)

    class Mock:
        def __init__(self):
            self.calls = []

        def complete(self, messages, model):
            self.calls.append(json.dumps(messages, ensure_ascii=False))
            if len(self.calls) == 1:
                return '{"mapping":{"project":"invented"}}'
            return '{"mapping":{"project":"任务名称","content":"本周进展"}}'

    client = Mock()
    assert assist_mapping(table, client=client, model="mock")["project"] == "任务名称"
    assert len(client.calls) == 2
    assert all(
        "0.81" not in call and "仅在本地保存" not in call and "合成任务" not in call
        for call in client.calls
    )

    class Invalid:
        def complete(self, messages, model):
            return '{"mapping":{"execute":"shell"}}'

    with pytest.raises(ValueError, match="两次"):
        assist_mapping(table, client=Invalid(), model="mock")


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
    from weekly_report_agent.service import edit_report_block

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


def test_invalid_draft_and_unsupported_template_fail_before_publication(tmp_path):
    app = create_app()
    client = app.test_client()
    assert client.get("/profiles/drafts/invalid").status_code == 400
    path = tmp_path / "table.docx"
    modify_package(TEMPLATE, path, lambda root: ET.SubElement(root.find(Q + "body"), Q + "tbl"))
    response = client.post(
        "/profiles/analyze",
        data={
            "source": (io.BytesIO(SYNTHETIC.encode()), "s.csv"),
            "template": (io.BytesIO(path.read_bytes()), "table.docx"),
        },
    )
    directory = app.config["ADAPTATION_ROOT"] / response.location.rsplit("/", 1)[-1]
    rule = json.loads((directory / "analysis.json").read_text())["rule"]
    result = client.post(response.location + "/trial", json={"rule": rule})
    assert result.status_code == 400 and "不支持" in result.json["error"]
    assert app.config["PROFILE_STORE"].list() == []


def test_adaptation_cannot_change_layout_or_silently_ignore_unknown_rules(tmp_path):
    _, table, rule = proposal(tmp_path)
    rule["document"]["numbering"] = "arbitrary"
    with pytest.raises(ValueError, match="不支持"):
        validate_adaptation(rule, table)
    del rule["document"]["numbering"]
    rule["input"]["status_policy"]["unknown"] = "drop"
    with pytest.raises(ValueError, match="未知状态"):
        validate_adaptation(rule, table)


def test_metadata_assistant_is_queued_and_never_receives_source_rows(tmp_path):
    from weekly_report_agent.llm import LLMConfig, LLMService

    class Client:
        def __init__(self):
            self.calls = []

        def complete(self, messages, model):
            self.calls.append(json.dumps(messages, ensure_ascii=False))
            return '{"mapping":{"project":"任务名称","content":"本周进展"}}'

    mock = Client()
    app = create_app(llm_service=LLMService(LLMConfig(api_key="synthetic-only"), mock))
    client, draft_url, _ = draft_workflow(app)
    result = client.post(draft_url + "/assist", data={"allow_metadata": "yes"})
    assert result.status_code == 202
    assert mock.calls == []
    queue = app.config["TASK_QUEUE"]
    job = queue.claim("mock-assistant")
    assert job["kind"] == "adapt" and "source_name" not in job["payload"]
    assert "0.81" not in json.dumps(job["payload"], ensure_ascii=False)
    TaskRunner(queue, app.config["SNAPSHOT_STORE"], llm_service=app.config["LLM_SERVICE"]).run(
        job, "mock-assistant"
    )
    status = client.get(result.json["status_url"])
    assert status.status_code == 200 and status.json["state"] == "succeeded"
    assert status.json["result"]["mapping"]["project"] == "任务名称"
    assert len(mock.calls) == 1 and "仅在本地保存" not in mock.calls[0]


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


def test_admin_list_filter_and_explicit_delete(tmp_path, monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("manage_profiles", "scripts/manage_profiles.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _, _, rule = proposal(tmp_path)
    library = ProfileStore.from_environment()
    profile_id, _ = library.save(rule, TEMPLATE)
    assert module.main(["--no-env", "list"]) == 0
    assert profile_id in capsys.readouterr().out
    assert module.main(["--no-env", "list", "--unused-days", "90"]) == 0
    assert profile_id not in capsys.readouterr().out
    assert library.list()
    monkeypatch.setattr("builtins.input", lambda _: "not-confirmed")
    assert module.main(["--no-env", "delete", profile_id]) == 0
    assert library.list()
    monkeypatch.setattr("builtins.input", lambda _: profile_id)
    assert module.main(["--no-env", "delete", profile_id]) == 0
    assert library.list() == []
