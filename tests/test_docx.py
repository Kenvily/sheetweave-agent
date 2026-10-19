import zipfile
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from weekly_report_agent.docx_renderer import FONT_SETTINGS, WORD_NS, render_docx
from weekly_report_agent.models import ParagraphKind, WeeklyData, WeeklyItem
from weekly_report_agent.planner import build_report_plan
from weekly_report_agent.validator import validate_report
from weekly_report_agent.word_lists import read_lists

ROOT = Path(__file__).resolve().parents[1]


def _qn(name: str) -> str:
    return f"{{{WORD_NS}}}{name}"


def _text(paragraph: ET.Element) -> str:
    return "".join(node.text or "" for node in paragraph.iter(_qn("t")))


def test_renders_valid_docx_with_all_required_styles(tmp_path: Path) -> None:
    data = WeeklyData(
        month="2026年9月",
        week="第1周",
        items=(WeeklyItem("项目甲", "模型评估", "进行中", "1、结果\n（1）评估：\n①AUC为0.95"),),
    )
    plan = build_report_plan(data, date(2026, 9, 24), section_number=12)
    output = tmp_path / "report.docx"

    render_docx(plan, ROOT / "data/研发三部周报模板-v2.docx", output)
    result = validate_report(output, section_number=12)

    assert result.heading_count == 3
    with zipfile.ZipFile(output) as package:
        root = ET.fromstring(package.read("word/document.xml"))
        style_root = ET.fromstring(package.read("word/styles.xml"))
        numbering_root = ET.fromstring(package.read("word/numbering.xml"))
    paragraphs = root.findall(f".//{_qn('body')}/{_qn('p')}")
    definitions = {n.get(_qn("styleId")): n for n in style_root.findall(_qn("style"))}
    lists = read_lists(paragraphs, numbering_root, definitions)

    for index, (paragraph, planned) in enumerate(zip(paragraphs, plan.paragraphs, strict=True)):
        text = lists[index][0] if index in lists else _text(paragraph)
        assert text == planned.text
        kind = planned.kind
        font, size, bold = FONT_SETTINGS[kind]
        run_properties = paragraph.find(f"{_qn('r')}/{_qn('rPr')}")
        properties = paragraph.find(_qn("pPr"))
        if index in lists:
            reference = properties.find(_qn("pStyle")).get(_qn("val"))
            run_properties = definitions[reference].find(_qn("rPr"))
            style_properties = definitions[reference].find(_qn("pPr"))
        else:
            style_properties = properties
        assert run_properties is not None
        assert properties is not None
        assert run_properties.find(_qn("rFonts")).get(_qn("ascii")) == font
        assert run_properties.find(_qn("sz")).get(_qn("val")) == size
        bold_node = run_properties.find(_qn("b"))
        assert (bold_node is not None and bold_node.get(_qn("val"), "1") != "0") is bold
        spacing = style_properties.find(_qn("spacing"))
        assert spacing is not None
        assert spacing.get(_qn("line")) == "360"
        if kind in {
            ParagraphKind.HEADING_1,
            ParagraphKind.HEADING_2,
            ParagraphKind.HEADING_3,
        }:
            assert spacing.get(_qn("before")) == "120"
            assert spacing.get(_qn("after")) == "120"
        if kind in {
            ParagraphKind.BODY_1,
            ParagraphKind.BODY_2,
            ParagraphKind.BODY_3,
        }:
            assert lists[index][2].find(f"{_qn('pPr')}/{_qn('ind')}").get(
                _qn("firstLineChars")
            ) == ("400" if kind is ParagraphKind.BODY_3 else "200")


def test_heading_styles_define_navigation_levels_without_replacing_template_styles(tmp_path):
    data = WeeklyData(
        "2026年9月", "第1周", (WeeklyItem("合成项目", "合成任务", "进行中", "完成验证"),)
    )
    plan = build_report_plan(data, date(2026, 9, 24))
    first, second = tmp_path / "first.docx", tmp_path / "second.docx"
    render_docx(plan, ROOT / "data/研发三部周报模板-v2.docx", first)
    # An exported document can itself be uploaded as a template. Its styles must survive.
    render_docx(plan, first, second)
    with zipfile.ZipFile(first) as package:
        previous_styles = ET.fromstring(package.read("word/styles.xml"))
    with zipfile.ZipFile(second) as package:
        definitions = ET.fromstring(package.read("word/styles.xml"))
        document = ET.fromstring(package.read("word/document.xml"))
    by_id = {node.get(_qn("styleId")): node for node in definitions.findall(_qn("style"))}
    for node in previous_styles.findall(_qn("style")):
        assert ET.tostring(by_id[node.get(_qn("styleId"))]) == ET.tostring(node)
    paragraphs = document.findall(f"{_qn('body')}/{_qn('p')}")
    for paragraph, planned in zip(paragraphs, plan.paragraphs, strict=True):
        properties = paragraph.find(_qn("pPr"))
        expected = {
            ParagraphKind.HEADING_1: "0",
            ParagraphKind.HEADING_2: "1",
            ParagraphKind.HEADING_3: "2",
        }.get(planned.kind, "9")
        assert properties.find(_qn("outlineLvl")).get(_qn("val")) == expected
        if expected != "9":
            reference = properties.find(_qn("pStyle")).get(_qn("val"))
            assert reference.endswith("_2")
            assert (
                by_id[reference].find(f"{_qn('pPr')}/{_qn('outlineLvl')}").get(_qn("val"))
                == expected
            )
        elif planned.kind.value.startswith("body_"):
            reference = properties.find(_qn("pStyle")).get(_qn("val"))
            assert reference.startswith("DocAgentBody")
            assert reference not in {
                node.get(_qn("styleId")) for node in previous_styles.findall(_qn("style"))
            }
        else:
            assert properties.find(_qn("pStyle")) is None
    assert validate_report(second).heading_count == 3


def test_validator_rejects_heading_with_visual_format_but_no_outline(tmp_path):
    import pytest

    from weekly_report_agent.validator import ReportValidationError

    data = WeeklyData(
        "2026年9月", "第1周", (WeeklyItem("合成项目", "合成任务", "进行中", "完成验证"),)
    )
    plan = build_report_plan(data, date(2026, 9, 24))
    original = tmp_path / "original.docx"
    render_docx(plan, ROOT / "data/研发三部周报模板-v2.docx", original)
    for fault in ("missing_outline", "wrong_outline", "missing_style", "missing_definition"):
        broken = tmp_path / f"{fault}.docx"
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(broken, "w") as target:
            for info in source.infolist():
                payload = source.read(info)
                if info.filename == "word/document.xml":
                    root = ET.fromstring(payload)
                    props = root.findall(f"{_qn('body')}/{_qn('p')}")[2].find(_qn("pPr"))
                    if fault == "missing_outline":
                        props.remove(props.find(_qn("outlineLvl")))
                    elif fault == "wrong_outline":
                        props.find(_qn("outlineLvl")).set(_qn("val"), "9")
                    elif fault == "missing_style":
                        props.remove(props.find(_qn("pStyle")))
                    payload = ET.tostring(root)
                elif info.filename == "word/styles.xml" and fault == "missing_definition":
                    root = ET.fromstring(payload)
                    for node in list(root):
                        if node.get(_qn("styleId")) == "DocAgentHeading1":
                            root.remove(node)
                    payload = ET.tostring(root)
                target.writestr(info, payload)
        with pytest.raises(ReportValidationError, match="大纲级别|标题样式"):
            validate_report(broken)


def test_third_body_indent_is_relative_to_second_even_for_existing_profiles(tmp_path):
    import pytest

    from weekly_report_agent.document_style import default_styles
    from weekly_report_agent.validator import ReportValidationError

    styles = default_styles()
    styles["body_2"]["first_line_chars"] = 3
    styles["body_3"]["first_line_chars"] = 2
    data = WeeklyData(
        "2026年10月",
        "第1周",
        (WeeklyItem("合成项目", "事项", "进行中", "1、父项\n（1）子项：\n①明细"),),
    )
    plan = build_report_plan(data, date(2026, 10, 19))
    output = tmp_path / "indented.docx"
    render_docx(plan, ROOT / "data/研发三部周报模板-v2.docx", output, styles=styles)
    validate_report(output, styles=styles)
    with zipfile.ZipFile(output) as source:
        parts = {item.filename: source.read(item.filename) for item in source.infolist()}
    numbering_root = ET.fromstring(parts["word/numbering.xml"])
    third_level = next(n for n in numbering_root.iter(_qn("lvl")) if n.get(_qn("ilvl")) == "2")
    indent = third_level.find(f"{_qn('pPr')}/{_qn('ind')}")
    assert indent.get(_qn("firstLineChars")) == "500"
    assert indent.get(_qn("firstLine")) == "1050"
    indent.set(_qn("firstLineChars"), "300")
    parts["word/numbering.xml"] = ET.tostring(numbering_root)
    with zipfile.ZipFile(output, "w") as target:
        for name, value in parts.items():
            target.writestr(name, value)
    with pytest.raises(ReportValidationError, match="首行缩进"):
        validate_report(output, styles=styles)


@pytest.mark.parametrize("second,third,expected", [(2, 2, 4), (3, 2, 5), (10, 2, 12), (2, 6, 6)])
def test_saved_style_contract_normalizes_third_indent_idempotently(second, third, expected):
    from weekly_report_agent.document_style import validate_styles

    styles = validate_styles(
        {"body_2": {"first_line_chars": second}, "body_3": {"first_line_chars": third}}
    )
    assert styles["body_3"]["first_line_chars"] == expected
    assert validate_styles(styles) == styles
