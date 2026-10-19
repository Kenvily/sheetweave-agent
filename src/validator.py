"""Validate generated DOCX structure, numbering, wording, and formatting."""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from .document_style import paragraph_style
from .docx_renderer import BODY_KINDS, DOCUMENT_PART, HEADING_KINDS, STYLES_PART, WORD_NS
from .docx_security import DocxSecurityError, inspect_docx_package
from .models import ParagraphKind, ReportPlan
from .numbering import validate_numbering
from .planner import CIRCLED_NUMBERS, validate_section_number
from .word_lists import NUMBERING_REL, NUMBERING_TYPE, numbering_part, read_lists

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


def _require_style(
    paragraph: ET.Element,
    kind: ParagraphKind,
    styles: dict | None = None,
    definition: ET.Element | None = None,
    list_level: ET.Element | None = None,
) -> None:
    text = _text(paragraph)
    style = paragraph_style(kind, styles)
    properties = paragraph.find(_qn("pPr"))
    runs = [run for run in paragraph.iter(_qn("r")) if _text(run)]
    if properties is None or not runs:
        raise ReportValidationError(f"段落缺少格式属性：{text}")
    inherited_p = definition.find(_qn("pPr")) if definition is not None else None
    inherited_r = definition.find(_qn("rPr")) if definition is not None else None

    def effective(direct, inherited, key):
        node = direct.find(_qn(key)) if direct is not None else None
        return (
            node
            if node is not None
            else (inherited.find(_qn(key)) if inherited is not None else None)
        )

    for run in runs:
        props = run.find(_qn("rPr"))
        fonts = effective(props, inherited_r, "rFonts")
        size = effective(props, inherited_r, "sz")
        east_asia = {"Microsoft YaHei": "微软雅黑", "SimSun": "宋体"}.get(style.font, style.font)
        if fonts is None or any(
            fonts.get(_qn(key)) != value
            for key, value in (
                ("ascii", style.font),
                ("hAnsi", style.font),
                ("eastAsia", east_asia),
            )
        ):
            raise ReportValidationError(f"段落字体不正确：{text}")
        if size is None or size.get(_qn("val")) != str(round(style.size_pt * 2)):
            raise ReportValidationError(f"段落字号不正确：{text}")
        bold = effective(props, inherited_r, "b")
        actual_bold = bold is not None and bold.get(_qn("val"), "1") not in {"0", "false", "off"}
        if style.bold is not None and actual_bold != style.bold:
            raise ReportValidationError(f"段落加粗不正确：{text}")
    spacing = effective(properties, inherited_p, "spacing")
    if spacing is None or any(
        spacing.get(_qn(key), "0") != value for key, value in style.attributes().items()
    ):
        raise ReportValidationError(f"段落行距或间距不正确：{text}")
    if kind in BODY_KINDS or style.first_line_chars:
        indent = properties.find(_qn("ind"))
        if indent is None and list_level is not None:
            indent = list_level.find(f"{_qn('pPr')}/{_qn('ind')}")
        if indent is None or any(
            indent.get(_qn(key)) != value for key, value in style.indent_attributes().items()
        ):
            raise ReportValidationError(f"正文首行缩进不正确：{text}")


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


def _require_outline(paragraph: ET.Element, kind: ParagraphKind, definitions: dict) -> None:
    """Require real heading semantics, not merely heading-like typography."""
    properties = paragraph.find(_qn("pPr"))
    outline = properties.find(_qn("outlineLvl")) if properties is not None else None
    expected = str(int(kind.value.rsplit("_", 1)[1]) - 1) if kind in HEADING_KINDS else "9"
    if outline is None or outline.get(_qn("val")) != expected:
        raise ReportValidationError(f"段落大纲级别不正确：{_text(paragraph)}")
    if kind in HEADING_KINDS:
        reference = properties.find(_qn("pStyle"))
        definition = definitions.get(reference.get(_qn("val"))) if reference is not None else None
        style_outline = (
            definition.find(f"{_qn('pPr')}/{_qn('outlineLvl')}") if definition is not None else None
        )
        if (
            definition is None
            or definition.get(_qn("type")) != "paragraph"
            or style_outline is None
            or style_outline.get(_qn("val")) != expected
        ):
            raise ReportValidationError(f"标题缺少有效的段落标题样式：{_text(paragraph)}")


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
            body_1_index = body_2_index = body_3_index = 0
        elif heading_1:
            heading_1_index += 1
            if int(heading_1.group(1)) != heading_1_index:
                raise ReportValidationError(f"一级标题编号不连续：{text}")
            heading_2_index = heading_3_index = 0
            body_1_index = body_2_index = body_3_index = 0
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


def _require_page_layout(root: ET.Element) -> None:
    """Require a usable final section with explicit page size and margins."""
    body = root.find(_qn("body"))
    section = body.find(_qn("sectPr")) if body is not None else None
    if section is None:
        raise ReportValidationError("DOCX 缺少文档分节与页面设置。")

    page_size = section.find(_qn("pgSz"))
    if page_size is None:
        raise ReportValidationError("DOCX 缺少页面尺寸设置。")
    for attribute in ("w", "h"):
        try:
            value = int(page_size.get(_qn(attribute), ""))
        except ValueError as exc:
            raise ReportValidationError("DOCX 页面尺寸设置无效。") from exc
        if value <= 0:
            raise ReportValidationError("DOCX 页面尺寸设置无效。")

    margins = section.find(_qn("pgMar"))
    if margins is None:
        raise ReportValidationError("DOCX 缺少页边距设置。")
    for attribute in ("top", "right", "bottom", "left"):
        try:
            value = int(margins.get(_qn(attribute), ""))
        except ValueError as exc:
            raise ReportValidationError("DOCX 页边距设置无效。") from exc
        if value < 0:
            raise ReportValidationError("DOCX 页边距设置无效。")


def validate_report(
    path: Path,
    section_number: int = 9,
    *,
    report_title: str = "数据科学产品部周报",
    heading_titles: tuple[str, ...] = ("大模型产品研发",),
    styles: dict | None = None,
    numbering: dict | None = None,
    expected_plan: ReportPlan | None = None,
) -> ValidationResult:
    """Validate a readable DOCX against configured report formatting rules."""
    section_number = validate_section_number(section_number)
    if not path.is_file():
        raise ReportValidationError(f"报告文件不存在：{path}")
    try:
        inspect_docx_package(path)
    except DocxSecurityError as exc:
        raise ReportValidationError(str(exc)) from exc
    try:
        with zipfile.ZipFile(path, "r") as package:
            if package.testzip() is not None:
                raise ReportValidationError("DOCX 压缩包包含损坏条目。")
            if DOCUMENT_PART not in package.namelist():
                raise ReportValidationError("DOCX 缺少 word/document.xml。")
            root = ET.fromstring(package.read(DOCUMENT_PART))
            style_root = ET.fromstring(package.read(STYLES_PART))
            rel_part = "word/_rels/document.xml.rels"
            rels = ET.fromstring(package.read(rel_part)) if rel_part in package.namelist() else None
            list_root = None
            if rels is not None and any(n.get("Type") == NUMBERING_REL for n in rels):
                part = numbering_part(rels)
                types = ET.fromstring(package.read("[Content_Types].xml"))
                if not any(
                    n.get("PartName") == "/" + part and n.get("ContentType") == NUMBERING_TYPE
                    for n in types
                ):
                    raise ReportValidationError("DOCX 编号部件类型无效。")
                list_root = ET.fromstring(package.read(part))
    except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError) as exc:
        raise ReportValidationError(f"DOCX 文件无效：{exc}") from exc

    _require_page_layout(root)
    paragraphs = root.findall(f".//{_qn('body')}/{_qn('p')}")
    definitions = {node.get(_qn("styleId")): node for node in style_root.findall(_qn("style"))}
    try:
        lists = read_lists(paragraphs, list_root, definitions, numbering)
    except ValueError as exc:
        raise ReportValidationError(str(exc)) from exc
    texts = [lists[i][0] if i in lists else _text(p) for i, p in enumerate(paragraphs)]
    expected_headings = {
        f"{section_number + offset}. {title}" for offset, title in enumerate(heading_titles)
    }
    if expected_plan is not None:
        if texts != [p.text for p in expected_plan.paragraphs]:
            raise ReportValidationError("导出正文与已确认报告计划不一致。")
        expected_headings = {
            p.text for p in expected_plan.paragraphs if p.kind is ParagraphKind.HEADING_1
        }
    missing_headings = expected_headings.difference(texts)
    if missing_headings:
        missing = "、".join(sorted(missing_headings))
        raise ReportValidationError(f"报告缺少一级标题：{missing}。")

    if validate_numbering(numbering) == validate_numbering():
        _require_numbering_sequence(texts, section_number)
    elif expected_plan is None:
        raise ReportValidationError("自定义编号校验需要报告计划。")

    heading_count = 0
    for index, (paragraph, text) in enumerate(zip(paragraphs, texts, strict=True)):
        kind = (
            expected_plan.paragraphs[index].kind
            if expected_plan is not None
            else _paragraph_kind(text, report_title, expected_headings)
        )
        reference = paragraph.find(f"{_qn('pPr')}/{_qn('pStyle')}")
        definition = definitions.get(reference.get(_qn("val"))) if reference is not None else None
        if index in lists and lists[index][1] is not kind:
            raise ReportValidationError("正文列表级别与已确认报告计划不一致。")
        _require_style(
            paragraph,
            kind,
            styles,
            definition,
            lists[index][2] if index in lists else None,
        )
        _require_outline(paragraph, kind, definitions)
        _require_wording(text, kind)
        if kind in HEADING_KINDS:
            heading_count += 1

    return ValidationResult(paragraph_count=len(paragraphs), heading_count=heading_count)
