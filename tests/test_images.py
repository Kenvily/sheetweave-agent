"""Pictures stay local, traceable and intact across parsing, editing and export."""

import io
import json
import struct
import time
import zipfile
import zlib
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from openpyxl import Workbook

from sheetweave_agent.docx_images import IMAGE_REL, A, D, P, R, W
from sheetweave_agent.excel_images import X, read_excel_images
from sheetweave_agent.input_parser import parse_input
from sheetweave_agent.llm import ContentMode
from sheetweave_agent.llm.service import _request_block
from sheetweave_agent.rules import load_rule_preset, preset_payload, validate_rule_preset
from sheetweave_agent.service import edit_report_block, prepare_report, render_prepared_report
from sheetweave_agent.validator import ReportValidationError, validate_report
from sheetweave_agent.web import create_app
from sheetweave_agent.web_sessions import SnapshotStore

TEMPLATE = Path("tests/fixtures/templates/reference.docx")


def png():
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\x00\xff\x00" * 2))
        + chunk(b"IEND", b"")
    )


def workbook(path, rows=None, anchors=((2, 1),), external=False):
    """Write standards-based picture anchors without a Pillow test dependency."""
    book = Workbook()
    book.active.title = "数据"
    for row in rows or [["项目", "本周工作", "状态"], ["平台", "完成验证。", "进行中"]]:
        book.active.append(row)
    book.create_sheet("其他")
    book.save(path)
    with zipfile.ZipFile(path) as src:
        parts = {name: src.read(name) for name in src.namelist()}
    main = ET.fromstring(parts["xl/worksheets/sheet1.xml"])
    ET.SubElement(
        main,
        "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}drawing",
        {f"{{{R}}}id": "rIdDrawing"},
    )
    parts["xl/worksheets/sheet1.xml"] = ET.tostring(main)
    rels = ET.Element(f"{{{P}}}Relationships")
    ET.SubElement(
        rels,
        f"{{{P}}}Relationship",
        {"Id": "rIdDrawing", "Type": R + "/drawing", "Target": "../drawings/drawing1.xml"},
    )
    parts["xl/worksheets/_rels/sheet1.xml.rels"] = ET.tostring(rels)
    drawing = ET.Element(f"{{{X}}}wsDr")
    for row, col in anchors:
        anchor = ET.SubElement(drawing, f"{{{X}}}oneCellAnchor")
        origin = ET.SubElement(anchor, f"{{{X}}}from")
        for key, value in [("col", col), ("colOff", 0), ("row", row - 1), ("rowOff", 0)]:
            ET.SubElement(origin, f"{{{X}}}{key}").text = str(value)
        ET.SubElement(anchor, f"{{{X}}}ext", {"cx": "9525000", "cy": "4762500"})
        picture = ET.SubElement(anchor, f"{{{X}}}pic")
        fill = ET.SubElement(picture, f"{{{X}}}blipFill")
        ET.SubElement(fill, f"{{{A}}}blip", {f"{{{R}}}{'link' if external else 'embed'}": "rIdPic"})
    parts["xl/drawings/drawing1.xml"] = ET.tostring(drawing)
    rels = ET.Element(f"{{{P}}}Relationships")
    attrs = {
        "Id": "rIdPic",
        "Type": IMAGE_REL,
        "Target": "https://invalid.example/picture.png" if external else "../media/image1.png",
    }
    if external:
        attrs["TargetMode"] = "External"
    ET.SubElement(rels, f"{{{P}}}Relationship", attrs)
    parts["xl/drawings/_rels/drawing1.xml.rels"] = ET.tostring(rels)
    parts["xl/media/image1.png"] = png()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as dst:
        for name, data in parts.items():
            dst.writestr(name, data)
    return path


def preset():
    rule = preset_payload(load_rule_preset("general"))
    rule["input"].update(
        layout="records",
        header_rows=1,
        required_fields=["project", "content"],
        column_aliases={"project": ["项目"], "content": ["本周工作"], "status": ["状态"]},
    )
    rule["sections"] = [
        {
            "id": "work",
            "title": "项目",
            "source_field": "content",
            "group_by": "section",
            "heading_path": ["报告", "项目"],
            "heading_fields": ["", "project"],
        }
    ]
    return validate_rule_preset(rule)


def test_row_pictures_survive_merge_human_edit_and_docx_validation(tmp_path):
    path = workbook(
        tmp_path / "pictures.xlsx",
        rows=[
            ["项目", "本周工作", "状态"],
            ["平台", "完成验证。", "进行中"],
            ["平台", "完成回归。", "进行中"],
        ],
        anchors=((2, 1), (3, 1)),
    )
    prepared, rule = prepare_report(path, preset=preset())
    assert len(prepared.blocks) == 1
    assert [len(row.images) for row in prepared.source.sources] == [1, 1]
    prepared = edit_report_block(prepared, prepared.blocks[0].block_id, "完成验证与回归。")
    output = tmp_path / "report.docx"
    plan = render_prepared_report(prepared, rule, TEMPLATE, output, date(2026, 11, 3))
    assert sum(len(p.images) for p in plan.paragraphs) == 2
    original, _ = prepare_report(path, preset=rule)
    original_plan = render_prepared_report(
        original, rule, TEMPLATE, tmp_path / "original.docx", date(2026, 11, 3)
    )
    assert [p.text for p in original_plan.paragraphs if p.images] == [
        "1、完成验证。",
        "2、完成回归。",
    ]
    with zipfile.ZipFile(output) as package:
        root = ET.fromstring(package.read("word/document.xml"))
        assert len(root.findall(f".//{{{A}}}blip")) == 2
        for extent in root.findall(f".//{{{D}}}extent"):
            size = root.find(f"{{{W}}}body/{{{W}}}sectPr/{{{W}}}pgSz")
            margins = root.find(f"{{{W}}}body/{{{W}}}sectPr/{{{W}}}pgMar")
            usable = (
                int(size.get(f"{{{W}}}w"))
                - int(margins.get(f"{{{W}}}left"))
                - int(margins.get(f"{{{W}}}right"))
            )
            assert 0 < int(extent.get("cx")) <= usable * 635
            assert abs(int(extent.get("cx")) / int(extent.get("cy")) - 2) < 0.01
        rels = ET.fromstring(package.read("word/_rels/document.xml.rels"))
        for rel in rels:
            if rel.get("Type") == IMAGE_REL:
                assert package.read("word/" + rel.get("Target")) == png()
    request = json.dumps(_request_block(prepared, prepared.blocks[0], ContentMode.POLISH))
    assert prepared.source.sources[0].images[0].data not in request
    assert "image/png" not in request


def test_selected_week_keeps_only_its_pictures_and_image_only_progress(tmp_path):
    rows = [
        ["项目", "状态", "2026年11月", ""],
        ["", "", "第1周", "第2周"],
        ["平台", "进行中", "", ""],
    ]
    path = workbook(tmp_path / "weeks.xlsx", rows=rows, anchors=((3, 2), (3, 3)))
    rule = preset_payload(preset())
    rule["input"].update(
        layout="weekly_matrix",
        header_rows=2,
        column_aliases={"project": ["项目"], "status": ["状态"], "content": ["2026年11月"]},
    )
    rule = validate_rule_preset(rule)
    first = parse_input(path, rule, requested_week="第1周")
    latest = parse_input(path, rule)
    assert first.sources[0].images[0].column == 2
    assert latest.sources[0].images[0].column == 3
    assert latest.week == "第2周"
    assert latest.sources[0].fields["content"] == "附图。"
    assert latest.sources[0].raw_values[3] == ""


def test_picture_filtering_and_external_images(tmp_path):
    path = workbook(
        tmp_path / "status.xlsx",
        rows=[["项目", "本周工作", "状态"], ["保留", "", "进行中"], ["排除", "完成工作。", "暂停"]],
        anchors=((2, 1), (3, 1)),
    )
    rule = preset_payload(preset())
    rule["input"]["status_policy"] = {
        "known": ["进行中", "暂停"],
        "include": ["进行中"],
        "unknown": "keep",
    }
    parsed = parse_input(path, validate_rule_preset(rule))
    assert len(parsed.sources) == len(parsed.excluded_sources) == 1
    assert parsed.sources[0].images
    assert parsed.excluded_sources[0].images
    other, _ = read_excel_images(path, "其他")
    assert not other
    path = workbook(tmp_path / "external.xlsx", external=True)
    pictures, warnings = read_excel_images(path, "数据")
    assert not pictures
    assert any("外部" in warning for warning in warnings)


def test_snapshot_web_pictures_and_expiry(tmp_path):
    path = workbook(tmp_path / "pictures.xlsx")
    prepared, rule = prepare_report(path, preset=preset())
    output = tmp_path / "report.docx"
    render_prepared_report(prepared, rule, TEMPLATE, output, date(2026, 11, 3))
    store = SnapshotStore(tmp_path / "sessions")
    token = store.create_session(
        prepared,
        preset_id=rule.id,
        report_date=date(2026, 11, 3),
        section_number=rule.document.heading_start_default,
        content_mode="original",
        custom_instruction=None,
        model=None,
        template_content=TEMPLATE.read_bytes(),
        report_content=output.read_bytes(),
        download_name="report.docx",
        rule_payload=preset_payload(rule),
    )
    session = store.load_session(token)
    assert session.prepared == prepared
    image = session.prepared.source.sources[0].images[0]
    client = create_app(snapshot_store=store).test_client()
    page = client.get("/workspace/" + token)
    assert page.status_code == 200
    assert page.data.count(b'class="source-pictures"') == 3
    url = f"/workspace/{token}/images/{image.image_id}"
    result = client.get(url)
    assert result.status_code == 200
    assert result.data == png()
    assert result.mimetype == "image/png"
    assert client.get(f"/workspace/{token}/images/unknown").status_code == 404
    state_path = store.root / token / "state.json"
    state = json.loads(state_path.read_text())
    state["expires_at"] = time.time() - 1
    state_path.write_text(json.dumps(state))
    assert client.get(url).status_code == 404


def test_docx_missing_picture_is_rejected(tmp_path):
    path = workbook(tmp_path / "pictures.xlsx")
    prepared, rule = prepare_report(path, preset=preset())
    output = tmp_path / "report.docx"
    plan = render_prepared_report(prepared, rule, TEMPLATE, output, date(2026, 11, 3))
    with zipfile.ZipFile(output) as src:
        parts = {name: src.read(name) for name in src.namelist()}
    for name in list(parts):
        if name.startswith("word/media/sheetweave-"):
            del parts[name]
    with zipfile.ZipFile(output, "w") as dst:
        for name, data in parts.items():
            dst.writestr(name, data)
    with pytest.raises(ReportValidationError):
        validate_report(
            output,
            rule.document.heading_start_default,
            report_title=rule.document.report_title,
            styles=rule.document.styles,
            expected_plan=plan,
        )


def test_queued_generation_keeps_pictures_after_upload_cleanup(tmp_path, saved_generation_profiles):
    from sheetweave_agent.task_worker import TaskRunner

    path = workbook(tmp_path / "pictures.xlsx")
    app = create_app()
    client = app.test_client()
    response = client.post(
        "/tasks",
        data={
            "csv_file": (io.BytesIO(path.read_bytes()), path.name),
            "preset": "p_general",
            "report_date": "2026-11-03",
            "content_mode": "original",
        },
    )
    assert response.status_code == 202
    path.unlink()
    queue = app.config["TASK_QUEUE"]
    job = queue.claim("image-worker")
    TaskRunner(queue, app.config["SNAPSHOT_STORE"], stats=app.config["USAGE_STATS"]).run(
        job, "image-worker"
    )
    state = queue.status(response.json["id"])
    assert state["state"] == "succeeded", state["error"]
    session = app.config["SNAPSHOT_STORE"].load_session(state["result"]["token"])
    assert session.prepared.source.sources[0].images
    assert client.get("/workspace/" + session.token).status_code == 200


def test_llm_text_changes_do_not_send_or_remove_pictures(tmp_path):
    from sheetweave_agent.llm import LLMConfig, LLMService
    from sheetweave_agent.service import process_report

    class Mock:
        def complete(self, messages, model):
            payload = json.loads(messages[-1]["content"])
            return json.dumps(
                {
                    "blocks": [
                        {"block_id": b["block_id"], "text": b["editable_text"]}
                        for b in payload["blocks"]
                    ]
                }
            )

    path = workbook(tmp_path / "pictures.xlsx")
    prepared, rule = prepare_report(path, preset=preset())
    result = process_report(
        prepared, "polish", llm_service=LLMService(LLMConfig(api_key="mock-only"), Mock())
    )
    assert result.blocks[0].effective_version == "generated"
    assert result.source == prepared.source
    plan = render_prepared_report(
        result, rule, TEMPLATE, tmp_path / "model.docx", date(2026, 11, 3)
    )
    assert sum(len(p.images) for p in plan.paragraphs) == 1
