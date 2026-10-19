"""Render report plans into DOCX packages using standard-library OOXML APIs."""

from __future__ import annotations

import copy
import os
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .document_style import default_styles, paragraph_style
from .docx_security import inspect_docx_package
from .models import ParagraphKind, ReportParagraph, ReportPlan

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
DOCUMENT_PART = "word/document.xml"
STYLES_PART = "word/styles.xml"
ET.register_namespace("w", WORD_NS)

# Compatibility export; the style contract is the single source of defaults.
FONT_SETTINGS: dict[ParagraphKind, tuple[str, str, bool]] = {
    kind: (values["font"], str(round(values["size_pt"] * 2)), values["bold"])
    for kind in ParagraphKind
    for values in [default_styles()[kind.value]]
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


def _paragraph_element(
    paragraph: ReportParagraph,
    styles: dict | None = None,
    heading_styles: dict[ParagraphKind, str] | None = None,
) -> ET.Element:
    node = ET.Element(_qn("p"))
    properties = ET.SubElement(node, _qn("pPr"))
    if paragraph.kind in HEADING_KINDS and heading_styles:
        _property(properties, "pStyle", val=heading_styles[paragraph.kind])

    if paragraph.kind in {ParagraphKind.TITLE, ParagraphKind.DATE}:
        _property(properties, "jc", val="center")
    style = paragraph_style(paragraph.kind, styles)
    spacing = style.attributes()
    _property(properties, "spacing", **spacing)
    if paragraph.kind in BODY_KINDS or style.first_line_chars:
        _property(properties, "ind", firstLineChars=str(round(style.first_line_chars * 100)))
    outline = (
        int(paragraph.kind.value.rsplit("_", 1)[1]) - 1 if paragraph.kind in HEADING_KINDS else 9
    )
    _property(properties, "outlineLvl", val=str(outline))

    run = ET.SubElement(node, _qn("r"))
    run_properties = ET.SubElement(run, _qn("rPr"))
    font, size, bold = style.font, str(round(style.size_pt * 2)), style.bold
    _property(
        run_properties,
        "rFonts",
        ascii=font,
        hAnsi=font,
        eastAsia={"Microsoft YaHei": "微软雅黑", "SimSun": "宋体"}.get(font, font),
        cs=font,
    )
    if bold:
        _property(run_properties, "b")
    elif bold is False and styles is not None:
        _property(run_properties, "b", val="0")
    if styles is not None and bold is not None:
        _property(run_properties, "bCs", val="1" if bold else "0")
    _property(run_properties, "sz", val=size)
    _property(run_properties, "szCs", val=size)
    text = ET.SubElement(run, _qn("t"), {f"{{{XML_NS}}}space": "preserve"})
    text.text = paragraph.text
    return node


def _heading_style_definitions(template_xml: bytes) -> tuple[bytes, dict[ParagraphKind, str]]:
    """Add outline-aware paragraph styles without replacing any template styles."""
    root = ET.fromstring(template_xml)
    existing_ids = {node.get(_qn("styleId")) for node in root.findall(_qn("style"))}
    existing_names = {
        node.get(_qn("val")) for node in root.findall(f"{_qn('style')}/{_qn('name')}")
    }
    mapping = {}
    for level in range(1, 4):
        style_id, name = f"DocAgentHeading{level}", f"docAgent 标题 {level}"
        suffix = 1
        while style_id in existing_ids or name in existing_names:
            suffix += 1
            style_id, name = f"DocAgentHeading{level}_{suffix}", f"docAgent 标题 {level} ({suffix})"
        node = ET.SubElement(
            root,
            _qn("style"),
            {_qn("type"): "paragraph", _qn("customStyle"): "1", _qn("styleId"): style_id},
        )
        _property(node, "name", val=name)
        _property(node, "uiPriority", val="9")
        _property(node, "qFormat")
        properties = ET.SubElement(node, _qn("pPr"))
        _property(properties, "outlineLvl", val=str(level - 1))
        mapping[ParagraphKind(f"heading_{level}")] = style_id
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), mapping


def _document_xml(
    plan: ReportPlan,
    template_xml: bytes,
    styles: dict | None = None,
    heading_styles: dict[ParagraphKind, str] | None = None,
) -> bytes:
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
        body.append(_paragraph_element(paragraph, styles, heading_styles))
    if section_properties is not None:
        body.append(section_properties)
    else:
        section = ET.SubElement(body, _qn("sectPr"))
        _property(section, "pgSz", w="11906", h="16838")
        _property(section, "pgMar", top="1440", right="1440", bottom="1440", left="1440")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def render_docx(
    plan: ReportPlan, template_path: Path, output_path: Path, *, styles: dict | None = None
) -> None:
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
            style_xml, heading_styles = _heading_style_definitions(source.read(STYLES_PART))
            replacements = {
                DOCUMENT_PART: _document_xml(
                    plan, source.read(DOCUMENT_PART), styles, heading_styles
                ),
                STYLES_PART: style_xml,
            }
            with tempfile.NamedTemporaryFile(
                dir=output_path.parent, suffix=".docx", delete=False
            ) as temporary:
                temporary_name = temporary.name
            with zipfile.ZipFile(temporary_name, "w") as destination:
                for info in source.infolist():
                    payload = (
                        replacements[info.filename]
                        if info.filename in replacements
                        else source.read(info)
                    )
                    destination.writestr(info, payload)
        os.replace(temporary_name, output_path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
