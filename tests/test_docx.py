import zipfile
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

from weekly_report_agent.docx_renderer import WORD_NS, render_docx
from weekly_report_agent.models import WeeklyData, WeeklyItem
from weekly_report_agent.planner import build_report_plan
from weekly_report_agent.validator import validate_report

ROOT = Path(__file__).resolve().parents[1]


def _qn(name: str) -> str:
    return f"{{{WORD_NS}}}{name}"


def test_renders_valid_docx_with_required_styles(tmp_path: Path) -> None:
    data = WeeklyData(
        month="2026年9月",
        week="第1周",
        items=(WeeklyItem("项目甲", "模型评估", "进行中", "AUC为0.95"),),
    )
    plan = build_report_plan(data, date(2026, 9, 24))
    output = tmp_path / "report.docx"

    render_docx(plan, ROOT / "data/研发三部周报模板.docx", output)
    result = validate_report(output)

    assert result.heading_count == 3
    with zipfile.ZipFile(output) as package:
        root = ET.fromstring(package.read("word/document.xml"))
    paragraphs = root.findall(f".//{_qn('body')}/{_qn('p')}")
    by_text = {
        "".join(node.text or "" for node in paragraph.iter(_qn("t"))): paragraph
        for paragraph in paragraphs
    }
    title_size = by_text["数据科学产品部周报"].find(f"{_qn('r')}/{_qn('rPr')}/{_qn('sz')}")
    date_size = by_text["20260924"].find(f"{_qn('r')}/{_qn('rPr')}/{_qn('sz')}")
    heading_spacing = by_text["9.1 项目甲"].find(f"{_qn('pPr')}/{_qn('spacing')}")
    body_indent = by_text["AUC为0.95。"].find(f"{_qn('pPr')}/{_qn('ind')}")
    assert title_size is not None
    assert title_size.get(_qn("val")) == "32"
    assert date_size is not None
    assert date_size.get(_qn("val")) == "42"
    assert heading_spacing is not None
    assert heading_spacing.get(_qn("before")) == "120"
    assert body_indent is not None
    assert body_indent.get(_qn("firstLineChars")) == "200"
