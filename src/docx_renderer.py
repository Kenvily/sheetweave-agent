"""Render report plans into DOCX packages using standard-library OOXML APIs."""

from __future__ import annotations

import copy
import os
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .docx_security import inspect_docx_package
from .models import ParagraphKind, ReportParagraph, ReportPlan

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
DOCUMENT_PART = "word/document.xml"
ET.register_namespace("w", WORD_NS)

FONT_SETTINGS: dict[ParagraphKind, tuple[str, str, bool]] = {
    ParagraphKind.TITLE: ("Microsoft YaHei", "32", True),
    ParagraphKind.DATE: ("SimSun", "42", True),
    ParagraphKind.HEADING_1: ("Microsoft YaHei", "32", True),
    ParagraphKind.HEADING_2: ("Microsoft YaHei", "30", True),
    ParagraphKind.HEADING_3: ("Microsoft YaHei", "28", True),
    ParagraphKind.BODY_1: ("SimSun", "21", False),
    ParagraphKind.BODY_2: ("SimSun", "21", False),
    ParagraphKind.BODY_3: ("SimSun", "21", False),
}
BODY_KINDS = {
    ParagraphKind.BODY_1,
    ParagraphKind.BODY_2,
    ParagraphKind.BODY_3,
}
HEADING_KINDS = {
    ParagraphKind.HEADING_1,
    ParagraphKind.HEADING_2,
    ParagraphKind.HEADING_3,
}


def _qn(name: str) -> str:
    return f"{{{WORD_NS}}}{name}"


def _property(parent: ET.Element, name: str, **attributes: str) -> ET.Element:
    return ET.SubElement(parent, _qn(name), {_qn(key): value for key, value in attributes.items()})


def _paragraph_element(paragraph: ReportParagraph) -> ET.Element:
    node = ET.Element(_qn("p"))
    properties = ET.SubElement(node, _qn("pPr"))

    if paragraph.kind in {ParagraphKind.TITLE, ParagraphKind.DATE}:
        _property(properties, "jc", val="center")
    spacing = {"line": "360", "lineRule": "auto"}
    if paragraph.kind in HEADING_KINDS:
        spacing.update(before="120", after="120")
    _property(properties, "spacing", **spacing)
    if paragraph.kind in BODY_KINDS:
        _property(properties, "ind", firstLineChars="200")

    run = ET.SubElement(node, _qn("r"))
    run_properties = ET.SubElement(run, _qn("rPr"))
    font, size, bold = FONT_SETTINGS[paragraph.kind]
    _property(
        run_properties,
        "rFonts",
        ascii=font,
        hAnsi=font,
        eastAsia="微软雅黑" if font == "Microsoft YaHei" else "宋体",
        cs=font,
    )
    if bold:
        _property(run_properties, "b")
    _property(run_properties, "sz", val=size)
    _property(run_properties, "szCs", val=size)
    text = ET.SubElement(run, _qn("t"), {f"{{{XML_NS}}}space": "preserve"})
    text.text = paragraph.text
    return node


def _document_xml(plan: ReportPlan, template_xml: bytes) -> bytes:
    template_root = ET.fromstring(template_xml)
    template_body = template_root.find(_qn("body"))
    section_properties = None
    if template_body is not None:
        existing = template_body.find(_qn("sectPr"))
        if existing is not None:
            section_properties = copy.deepcopy(existing)

    root = ET.Element(_qn("document"))
    body = ET.SubElement(root, _qn("body"))
    for paragraph in plan.paragraphs:
        body.append(_paragraph_element(paragraph))
    if section_properties is not None:
        body.append(section_properties)
    else:
        section = ET.SubElement(body, _qn("sectPr"))
        _property(section, "pgSz", w="11906", h="16838")
        _property(section, "pgMar", top="1440", right="1440", bottom="1440", left="1440")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def render_docx(plan: ReportPlan, template_path: Path, output_path: Path) -> None:
    """Copy a template package and replace its main document with the report plan."""
    if not template_path.is_file():
        raise FileNotFoundError(f"模板文件不存在：{template_path}")
    if output_path.suffix.lower() != ".docx":
        raise ValueError("输出文件必须使用 .docx 扩展名。")
    if template_path.resolve() == output_path.resolve():
        raise ValueError("输出文件不能覆盖模板文件。")
    inspect_docx_package(template_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with zipfile.ZipFile(template_path, "r") as source:
            if DOCUMENT_PART not in source.namelist():
                raise ValueError("模板不是有效的 DOCX：缺少 word/document.xml。")
            replacement = _document_xml(plan, source.read(DOCUMENT_PART))
            with tempfile.NamedTemporaryFile(
                dir=output_path.parent, suffix=".docx", delete=False
            ) as temporary:
                temporary_name = temporary.name
            with zipfile.ZipFile(temporary_name, "w") as destination:
                for info in source.infolist():
                    payload = replacement if info.filename == DOCUMENT_PART else source.read(info)
                    destination.writestr(info, payload)
        os.replace(temporary_name, output_path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
