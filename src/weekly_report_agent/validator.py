"""Validate generated DOCX package structure, hierarchy, and key formatting."""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from .docx_renderer import DOCUMENT_PART, FONT_SETTINGS, WORD_NS
from .models import ParagraphKind


class ReportValidationError(ValueError):
    """Raised when a generated report violates required structure or formatting."""


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Summary returned after successful report validation."""

    paragraph_count: int
    heading_count: int


def _qn(name: str) -> str:
    return f"{{{WORD_NS}}}{name}"


def _text(paragraph: ET.Element) -> str:
    return "".join(node.text or "" for node in paragraph.iter(_qn("t")))


def _require_style(paragraph: ET.Element, kind: ParagraphKind) -> None:
    text = _text(paragraph)
    run_properties = paragraph.find(f"{_qn('r')}/{_qn('rPr')}")
    paragraph_properties = paragraph.find(_qn("pPr"))
    if run_properties is None or paragraph_properties is None:
        raise ReportValidationError(f"段落缺少格式属性：{text}")

    font, size, bold = FONT_SETTINGS[kind]
    fonts = run_properties.find(_qn("rFonts"))
    size_node = run_properties.find(_qn("sz"))
    if fonts is None or fonts.get(_qn("ascii")) != font:
        raise ReportValidationError(f"段落字体不正确：{text}")
    if size_node is None or size_node.get(_qn("val")) != size:
        raise ReportValidationError(f"段落字号不正确：{text}")
    if bold and run_properties.find(_qn("b")) is None:
        raise ReportValidationError(f"标题未加粗：{text}")

    spacing = paragraph_properties.find(_qn("spacing"))
    if spacing is None or spacing.get(_qn("line")) != "360":
        raise ReportValidationError(f"段落不是 1.5 倍行距：{text}")
    if kind in {
        ParagraphKind.HEADING_1,
        ParagraphKind.HEADING_2,
        ParagraphKind.HEADING_3,
    } and (spacing.get(_qn("before")) != "120" or spacing.get(_qn("after")) != "120"):
        raise ReportValidationError(f"标题段前段后间距不正确：{text}")
    if kind is ParagraphKind.BODY:
        indent = paragraph_properties.find(_qn("ind"))
        if indent is None or indent.get(_qn("firstLineChars")) != "200":
            raise ReportValidationError(f"正文未首行缩进 2 字符：{text}")


def validate_report(path: Path) -> ValidationResult:
    """Validate that a report is a readable DOCX with compliant report content."""
    if not path.is_file():
        raise ReportValidationError(f"报告文件不存在：{path}")
    try:
        with zipfile.ZipFile(path, "r") as package:
            if package.testzip() is not None:
                raise ReportValidationError("DOCX 压缩包包含损坏条目。")
            if DOCUMENT_PART not in package.namelist():
                raise ReportValidationError("DOCX 缺少 word/document.xml。")
            root = ET.fromstring(package.read(DOCUMENT_PART))
    except (zipfile.BadZipFile, ET.ParseError) as exc:
        raise ReportValidationError(f"DOCX 文件无效：{exc}") from exc

    paragraphs = root.findall(f".//{_qn('body')}/{_qn('p')}")
    texts = [_text(paragraph) for paragraph in paragraphs]
    if "9. 大模型产品研发" not in texts:
        raise ReportValidationError("报告缺少“9. 大模型产品研发”一级标题。")

    heading_count = 0
    for paragraph, text in zip(paragraphs, texts, strict=True):
        if text == "9. 大模型产品研发":
            kind = ParagraphKind.HEADING_1
        elif re.match(r"^9\.\d+\.\d+\s", text):
            kind = ParagraphKind.HEADING_3
        elif re.match(r"^9\.\d+\s", text):
            kind = ParagraphKind.HEADING_2
        elif text == "数据科学产品部周报":
            kind = ParagraphKind.TITLE
        elif re.fullmatch(r"\d{8}", text):
            kind = ParagraphKind.DATE
        else:
            kind = ParagraphKind.BODY
        _require_style(paragraph, kind)
        if kind in {ParagraphKind.HEADING_1, ParagraphKind.HEADING_2, ParagraphKind.HEADING_3}:
            heading_count += 1

    return ValidationResult(paragraph_count=len(paragraphs), heading_count=heading_count)
