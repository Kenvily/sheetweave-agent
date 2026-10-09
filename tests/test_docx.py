import zipfile
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

from weekly_report_agent.docx_renderer import FONT_SETTINGS, WORD_NS, render_docx
from weekly_report_agent.models import ParagraphKind, WeeklyData, WeeklyItem
from weekly_report_agent.planner import build_report_plan
from weekly_report_agent.validator import validate_report

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

    render_docx(plan, ROOT / "data/研发三部周报模板.docx", output)
    result = validate_report(output, section_number=12)

    assert result.heading_count == 3
    with zipfile.ZipFile(output) as package:
        root = ET.fromstring(package.read("word/document.xml"))
    paragraphs = root.findall(f".//{_qn('body')}/{_qn('p')}")
    kinds_by_text = {paragraph.text: paragraph.kind for paragraph in plan.paragraphs}

    for paragraph in paragraphs:
        text = _text(paragraph)
        kind = kinds_by_text[text]
        font, size, bold = FONT_SETTINGS[kind]
        run_properties = paragraph.find(f"{_qn('r')}/{_qn('rPr')}")
        properties = paragraph.find(_qn("pPr"))
        assert run_properties is not None
        assert properties is not None
        assert run_properties.find(_qn("rFonts")).get(_qn("ascii")) == font
        assert run_properties.find(_qn("sz")).get(_qn("val")) == size
        assert (run_properties.find(_qn("b")) is not None) is bold
        spacing = properties.find(_qn("spacing"))
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
            assert properties.find(_qn("ind")).get(_qn("firstLineChars")) == "200"
