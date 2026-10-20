"""Word-native body lists; semantic report plans retain their readable prefixes."""

from __future__ import annotations

import copy
import re
import secrets
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from .document_style import paragraph_style
from .models import ParagraphKind, ReportPlan
from .numbering import CIRCLED, validate_numbering

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
NUMBERING_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering"
NUMBERING_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"
BODY_KINDS = tuple(ParagraphKind(f"body_{level}") for level in range(1, 4))


def qn(name: str) -> str:
    return f"{{{WORD_NS}}}{name}"


def prop(parent: ET.Element, name: str, **attrs: str) -> ET.Element:
    return ET.SubElement(parent, qn(name), {qn(key): value for key, value in attrs.items()})


def serialize(root: ET.Element) -> bytes:
    if root.tag in {f"{{{REL_NS}}}Relationships", f"{{{CONTENT_NS}}}Types"}:
        # LibreOffice's OPC reader requires these roots to use the default namespace.
        # Avoid register_namespace mutations: exports can run in concurrent threads.
        root = copy.deepcopy(root)
        namespace = root.tag.split("}")[0][1:]
        for node in root.iter():
            node.tag = node.tag.removeprefix(f"{{{namespace}}}")
        root.set("xmlns", namespace)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def split_prefix(text: str, kind: ParagraphKind, numbering: dict) -> tuple[int, str]:
    """Remove only the configured list prefix, leaving business text untouched."""
    pattern = numbering[kind.value]
    token = "{circle}" if "{circle}" in pattern else "{n}"
    number = f"([{CIRCLED}])" if token == "{circle}" else r"([1-9][0-9]*)"
    match = re.match("^" + re.escape(pattern).replace(re.escape(token), number), text)
    if match is None:
        raise ValueError(f"正文编号与方案不一致：{kind.value}")
    value = CIRCLED.index(match[1]) + 1 if token == "{circle}" else int(match[1])
    return value, text[match.end() :]


def level_format(pattern: str, level: int) -> tuple[str, str]:
    """Map the finite supported prefixes to standard OOXML list formats."""
    return (
        "decimalEnclosedCircle" if "{circle}" in pattern else "decimal",
        pattern.replace("{circle}", f"%{level + 1}").replace("{n}", f"%{level + 1}"),
    )


@dataclass(frozen=True)
class NativeLists:
    styles: bytes
    numbering: bytes
    paragraphs: dict[int, tuple[str, int, str, str]]


def _append_instance(root: ET.Element, num_id: str, abstract_id: int) -> None:
    instance = ET.Element(qn("num"), {qn("numId"): num_id})
    prop(instance, "abstractNumId", val=str(abstract_id))
    for level in range(3):
        override = prop(instance, "lvlOverride", ilvl=str(level))
        prop(override, "startOverride", val="1")
    cleanup = root.find(qn("numIdMacAtCleanup"))
    root.insert(list(root).index(cleanup) if cleanup is not None else len(root), instance)


def _append_family(
    root: ET.Element,
    definitions: ET.Element,
    styles: dict | None,
    patterns: dict,
    abstract_id: int,
    num_id: str,
) -> dict[ParagraphKind, str]:
    """Keep list definitions and linked styles private to a single directory."""
    abstract = ET.Element(qn("abstractNum"), {qn("abstractNumId"): str(abstract_id)})
    existing_nsids = {n.get(qn("val")) for n in root.iter(qn("nsid"))}
    nsid = secrets.token_hex(4).upper()
    while nsid in existing_nsids:
        nsid = secrets.token_hex(4).upper()
    prop(abstract, "nsid", val=nsid)
    prop(abstract, "multiLevelType", val="multilevel")
    # All abstract definitions must precede concrete numbering instances.
    root.insert(
        next(
            (i for i, n in enumerate(root) if n.tag in {qn("num"), qn("numIdMacAtCleanup")}),
            len(root),
        ),
        abstract,
    )
    ids = {n.get(qn("styleId")) for n in definitions.findall(qn("style"))}
    names = {n.get(qn("val")) for n in definitions.findall(f"{qn('style')}/{qn('name')}")}
    body_styles = {}
    for level, kind in enumerate(BODY_KINDS):
        sid, name, suffix = f"SheetWeaveBody{level + 1}", f"SheetWeaveAgent 正文 {level + 1}", 1
        while sid in ids or name in names:
            suffix += 1
            sid, name = (
                f"SheetWeaveBody{level + 1}_{suffix}",
                f"SheetWeaveAgent 正文 {level + 1} ({suffix})",
            )
        body_styles[kind] = sid
        node = ET.SubElement(
            definitions,
            qn("style"),
            {qn("type"): "paragraph", qn("customStyle"): "1", qn("styleId"): sid},
        )
        prop(node, "name", val=name)
        prop(node, "next", val=sid)
        prop(node, "qFormat")
        pp = prop(node, "pPr")
        np = prop(pp, "numPr")
        prop(np, "ilvl", val=str(level))
        prop(np, "numId", val=num_id)
        style = paragraph_style(kind, styles)
        prop(pp, "spacing", **style.attributes())
        prop(pp, "outlineLvl", val="9")
        rp = prop(node, "rPr")
        prop(
            rp,
            "rFonts",
            ascii=style.font,
            hAnsi=style.font,
            eastAsia={"SimSun": "宋体", "Microsoft YaHei": "微软雅黑"}.get(style.font, style.font),
            cs=style.font,
        )
        if style.bold is not None:
            prop(rp, "b", val="1" if style.bold else "0")
            prop(rp, "bCs", val="1" if style.bold else "0")
        prop(rp, "sz", val=str(round(style.size_pt * 2)))
        prop(rp, "szCs", val=str(round(style.size_pt * 2)))
        lvl = prop(abstract, "lvl", ilvl=str(level))
        prop(lvl, "start", val="1")
        fmt, prefix = level_format(patterns[kind.value], level)
        prop(lvl, "numFmt", val=fmt)
        if level:
            prop(lvl, "lvlRestart", val=str(level))
        prop(lvl, "pStyle", val=sid)
        prop(lvl, "suff", val="nothing")
        prop(lvl, "lvlText", val=prefix)
        prop(lvl, "lvlJc", val="left")
        lp = prop(lvl, "pPr")
        prop(lp, "ind", left="0", **style.indent_attributes())
        # Number symbols inherit the same font and size as their paragraph style.
        lvl.append(ET.fromstring(ET.tostring(rp)))
    _append_instance(root, num_id, abstract_id)
    return body_styles


def build_lists(
    plan: ReportPlan,
    style_xml: bytes,
    numbering_xml: bytes | None,
    styles: dict | None,
    numbering: dict | None,
) -> NativeLists:
    """Append isolated definitions, instances and styles per heading's body."""
    patterns = validate_numbering(numbering)
    definitions = ET.fromstring(style_xml)
    root = ET.fromstring(numbering_xml) if numbering_xml else ET.Element(qn("numbering"))
    next_abstract = (
        max((int(n.get(qn("abstractNumId"))) for n in root.findall(qn("abstractNum"))), default=-1)
        + 1
    )
    next_num = max((int(n.get(qn("numId"))) for n in root.findall(qn("num"))), default=0) + 1
    paragraphs = {}
    active = None
    for index, paragraph in enumerate(plan.paragraphs):
        if paragraph.kind not in BODY_KINDS:
            active = None
            continue
        if active is None:
            active = str(next_num)
            body_styles = _append_family(root, definitions, styles, patterns, next_abstract, active)
            next_num += 1
            next_abstract += 1
        _, text = split_prefix(paragraph.text, paragraph.kind, patterns)
        paragraphs[index] = (
            active,
            BODY_KINDS.index(paragraph.kind),
            text,
            body_styles[paragraph.kind],
        )
    if not paragraphs:
        _append_family(root, definitions, styles, patterns, next_abstract, str(next_num))
    return NativeLists(serialize(definitions), serialize(root), paragraphs)


def numbering_part(relationships: ET.Element) -> str:
    """Resolve an existing internal numbering relationship, including custom paths."""
    import posixpath

    matches = [n for n in relationships if n.get("Type") == NUMBERING_REL]
    if len(matches) > 1:
        raise ValueError("DOCX 包含重复编号关系。")
    if not matches:
        return "word/numbering.xml"
    node = matches[0]
    if node.get("TargetMode") == "External":
        raise ValueError("不支持外部编号定义。")
    target = node.get("Target", "")
    path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "word/" + target)
    if not path.startswith("word/") or "\\" in path or not target:
        raise ValueError("DOCX 编号部件路径无效。")
    return path


def wire_numbering(relationships: ET.Element, content_types: ET.Element, part: str) -> None:
    """Register a numbering part without altering existing package relationships."""
    if not any(n.get("Type") == NUMBERING_REL for n in relationships):
        ids = {n.get("Id") for n in relationships}
        index = 1
        while f"rId{index}" in ids:
            index += 1
        ET.SubElement(
            relationships,
            f"{{{REL_NS}}}Relationship",
            {"Id": f"rId{index}", "Type": NUMBERING_REL, "Target": part.removeprefix("word/")},
        )
    override = next((n for n in content_types if n.get("PartName") == "/" + part), None)
    if override is None:
        ET.SubElement(
            content_types,
            f"{{{CONTENT_NS}}}Override",
            {"PartName": "/" + part, "ContentType": NUMBERING_TYPE},
        )
    elif override.get("ContentType") != NUMBERING_TYPE:
        raise ValueError("DOCX 编号部件类型无效。")


def read_lists(
    paragraphs: list[ET.Element],
    numbering_root: ET.Element | None,
    definitions: dict[str, ET.Element],
    numbering: dict | None = None,
) -> dict[int, tuple[str, ParagraphKind, ET.Element]]:
    """Resolve and check native numbering to recover the visible report text."""
    patterns = validate_numbering(numbering)
    result = {}
    counters = {}
    abstracts = (
        {}
        if numbering_root is None
        else {n.get(qn("abstractNumId")): n for n in numbering_root.findall(qn("abstractNum"))}
    )
    instances = (
        {}
        if numbering_root is None
        else {n.get(qn("numId")): n for n in numbering_root.findall(qn("num"))}
    )
    for index, paragraph in enumerate(paragraphs):
        pp = paragraph.find(qn("pPr"))
        np = pp.find(qn("numPr")) if pp is not None else None
        if np is None:
            continue
        try:
            level = int(np.find(qn("ilvl")).get(qn("val")))
            num_id = np.find(qn("numId")).get(qn("val"))
            if not 0 <= level < 3 or num_id == "0":
                raise ValueError("正文列表级别无效。")
            instance = instances[num_id]
            abstract_id = instance.find(qn("abstractNumId")).get(qn("val"))
            abstract = abstracts[abstract_id]
            if abstract.find(qn("multiLevelType")).get(qn("val")) != "multilevel":
                raise ValueError("正文缺少多级列表定义。")
            levels = {int(n.get(qn("ilvl"))): n for n in abstract.findall(qn("lvl"))}
            if len(levels) != 3:
                raise ValueError("正文列表必须包含三级定义。")
            for level_index, kind in enumerate(BODY_KINDS):
                lvl = levels[level_index]
                fmt, prefix = level_format(patterns[kind.value], level_index)
                for key, expected in (
                    ("start", "1"),
                    ("numFmt", fmt),
                    ("lvlText", prefix),
                    ("suff", "nothing"),
                ):
                    if lvl.find(qn(key)).get(qn("val")) != expected:
                        raise ValueError("正文列表编号格式不正确。")
                if level_index and lvl.find(qn("lvlRestart")).get(qn("val")) != str(level_index):
                    raise ValueError("正文列表重新起号规则不正确。")
                override = next(
                    (
                        n
                        for n in instance.findall(qn("lvlOverride"))
                        if n.get(qn("ilvl")) == str(level_index)
                    ),
                    None,
                )
                if (
                    override is None
                    or override.find(qn("lvl")) is not None
                    or override.find(qn("startOverride")).get(qn("val")) != "1"
                ):
                    raise ValueError("正文列表起始编号不正确。")
            lvl = levels[level]
            sid = pp.find(qn("pStyle")).get(qn("val"))
            if sid != lvl.find(qn("pStyle")).get(qn("val")) or sid not in definitions:
                raise ValueError("正文列表缺少关联段落样式。")
            style_np = definitions[sid].find(f"{qn('pPr')}/{qn('numPr')}")
            if style_np.find(qn("ilvl")).get(qn("val")) != str(level):
                raise ValueError("正文列表样式级别不正确。")
            if style_np.find(qn("numId")).get(qn("val")) != num_id:
                raise ValueError("正文列表样式错误关联到其他目录的编号。")
            count = counters.setdefault(num_id, [0, 0, 0])
            if level and count[level - 1] == 0:
                raise ValueError("正文列表缺少上级条目。")
            count[level] += 1
            count[level + 1 :] = [0] * (2 - level)
            n = count[level]
            if "{circle}" in patterns[BODY_KINDS[level].value] and n > len(CIRCLED):
                raise ValueError("带圈编号最多支持20项。")
            prefix = patterns[BODY_KINDS[level].value].format(
                n=n, circle=CIRCLED[n - 1] if n <= len(CIRCLED) else ""
            )
            text = "".join(n.text or "" for n in paragraph.iter(qn("t")))
            result[index] = (prefix + text, BODY_KINDS[level], lvl)
        except (AttributeError, KeyError, TypeError) as exc:
            raise ValueError("正文列表编号引用或定义无效。") from exc
    return result
