"""Validate generated DOCX structure, numbering, wording, and formatting."""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from .docx_renderer import BODY_KINDS, DOCUMENT_PART, FONT_SETTINGS, HEADING_KINDS, WORD_NS
from .models import ParagraphKind
from .planner import CIRCLED_NUMBERS, validate_section_number

INFORMAL_NAME_PATTERN = re.compile(r"(?:[A-Za-z]{1,20}|[\u4e00-\u9fff]{1,3})[哥姐]")
RELATIVE_WEEKDAY_PATTERN = re.compile(r"(?:本周|下周|周)[一二三四五六日天]")
TERMINAL_PUNCTUATION = re.compile(r"[。；：！？.!?]$")


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
    if kind in HEADING_KINDS and (
        spacing.get(_qn("before")) != "120" or spacing.get(_qn("after")) != "120"
    ):
        raise ReportValidationError(f"标题段前段后间距不正确：{text}")
    if kind in BODY_KINDS:
        indent = paragraph_properties.find(_qn("ind"))
        if indent is None or indent.get(_qn("firstLineChars")) != "200":
            raise ReportValidationError(f"正文未首行缩进 2 字符：{text}")


def _paragraph_kind(text: str, report_title: str, expected_headings: set[str]) -> ParagraphKind:
    if text in expected_headings:
        return ParagraphKind.HEADING_1
    if re.match(r"^\d+\.\d+\.\d+\.\s", text):
        return ParagraphKind.HEADING_3
    if re.match(r"^\d+\.\d+\.\s", text):
        return ParagraphKind.HEADING_2
    if text == report_title:
        return ParagraphKind.TITLE
    if re.fullmatch(r"\d{8}", text):
        return ParagraphKind.DATE
    if re.match(r"^\d+、", text):
        return ParagraphKind.BODY_1
    if re.match(r"^（\d+）", text):
        return ParagraphKind.BODY_2
    if text.startswith(tuple(CIRCLED_NUMBERS)):
        return ParagraphKind.BODY_3
    raise ReportValidationError(f"正文编号格式不正确：{text}")


def _require_wording(text: str, kind: ParagraphKind) -> None:
    if INFORMAL_NAME_PATTERN.search(text):
        raise ReportValidationError(f"报告仍包含非正式称呼：{text}")
    if RELATIVE_WEEKDAY_PATTERN.search(text):
        raise ReportValidationError(f"报告仍包含相对星期：{text}")
    if kind in BODY_KINDS and not TERMINAL_PUNCTUATION.search(text):
        raise ReportValidationError(f"正文换行处缺少标点：{text}")


def _require_numbering_sequence(texts: list[str], section_number: int) -> None:
    """Require continuous heading and body numbering within each parent level."""
    heading_1_index = section_number - 1
    heading_2_index = 0
    heading_3_index = 0
    body_1_index = 0
    body_2_index = 0
    body_3_index = 0

    for text in texts:
        heading_3 = re.match(r"^(\d+)\.(\d+)\.(\d+)\.\s", text)
        heading_2 = re.match(r"^(\d+)\.(\d+)\.\s", text)
        heading_1 = re.match(r"^(\d+)\.\s", text)
        body_1 = re.match(r"^(\d+)、", text)
        body_2 = re.match(r"^（(\d+)）", text)

        if heading_3:
            if int(heading_3.group(1)) != heading_1_index:
                raise ReportValidationError(f"三级标题一级父编号不正确：{text}")
            if int(heading_3.group(2)) != heading_2_index:
                raise ReportValidationError(f"三级标题二级父编号不正确：{text}")
            heading_3_index += 1
            if int(heading_3.group(3)) != heading_3_index:
                raise ReportValidationError(f"三级标题编号不连续：{text}")
            body_1_index = body_2_index = body_3_index = 0
        elif heading_2:
            if int(heading_2.group(1)) != heading_1_index:
                raise ReportValidationError(f"二级标题父级编号不正确：{text}")
            heading_2_index += 1
            if int(heading_2.group(2)) != heading_2_index:
                raise ReportValidationError(f"二级标题编号不连续：{text}")
            heading_3_index = 0
        elif heading_1:
            heading_1_index += 1
            if int(heading_1.group(1)) != heading_1_index:
                raise ReportValidationError(f"一级标题编号不连续：{text}")
            heading_2_index = heading_3_index = 0
        elif body_1:
            body_1_index += 1
            if int(body_1.group(1)) != body_1_index:
                raise ReportValidationError(f"正文一级编号不连续：{text}")
            body_2_index = body_3_index = 0
        elif body_2:
            if body_1_index == 0:
                raise ReportValidationError(f"正文二级编号缺少父级：{text}")
            body_2_index += 1
            if int(body_2.group(1)) != body_2_index:
                raise ReportValidationError(f"正文二级编号不连续：{text}")
            body_3_index = 0
        elif text.startswith(tuple(CIRCLED_NUMBERS)):
            if body_2_index == 0:
                raise ReportValidationError(f"正文三级编号缺少父级：{text}")
            body_3_index += 1
            if body_3_index > len(CIRCLED_NUMBERS):
                raise ReportValidationError("正文三级编号超过支持范围。")
            expected = CIRCLED_NUMBERS[body_3_index - 1]
            if not text.startswith(expected):
                raise ReportValidationError(f"正文三级编号不连续：{text}")


def validate_report(
    path: Path,
    section_number: int = 9,
    *,
    report_title: str = "数据科学产品部周报",
    heading_titles: tuple[str, ...] = ("大模型产品研发",),
) -> ValidationResult:
    """Validate a readable DOCX against configured report formatting rules."""
    section_number = validate_section_number(section_number)
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
    expected_headings = {
        f"{section_number + offset}. {title}" for offset, title in enumerate(heading_titles)
    }
    missing_headings = expected_headings.difference(texts)
    if missing_headings:
        missing = "、".join(sorted(missing_headings))
        raise ReportValidationError(f"报告缺少一级标题：{missing}。")

    _require_numbering_sequence(texts, section_number)

    heading_count = 0
    for paragraph, text in zip(paragraphs, texts, strict=True):
        kind = _paragraph_kind(text, report_title, expected_headings)
        _require_style(paragraph, kind)
        _require_wording(text, kind)
        if kind in HEADING_KINDS:
            heading_count += 1

    return ValidationResult(paragraph_count=len(paragraphs), heading_count=heading_count)
