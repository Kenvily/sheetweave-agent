"""Build a deterministic, renderer-independent report hierarchy."""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import date

from .content_builder import block_heading_path
from .models import (
    ParagraphKind,
    PreparedReport,
    ReportParagraph,
    ReportPlan,
    WeeklyData,
    WeeklyItem,
)
from .normalizer import normalize_report_text
from .rules import RulePreset

TERMINAL_PUNCTUATION = re.compile(r"[。；：！？.!?]$")
TOP_LEVEL_NUMBER = re.compile(r"^(\d+)(?:、|[.．](?!\d))\s*(.*)$")
SUB_LEVEL_NUMBER = re.compile(r"^(?:[（(]?(\d+)[)）])\s*(.*)$")
CIRCLED_NUMBERS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
CIRCLED_NUMBER = re.compile(rf"^[{CIRCLED_NUMBERS}]\s*(.*)$")
INLINE_NUMBER = re.compile(r"(?<=[，,。；;：:])\s*(\d+)[.、](?!\d)\s*")
MIN_SECTION_NUMBER = 1
MAX_SECTION_NUMBER = 999
BULLET_PREFIX = re.compile(r"^(?:[•●▪◦‣·]+\s*|[-*]\s+)")


@dataclass(slots=True)
class _BodyItem:
    text: str
    grandchildren: list[str] = field(default_factory=list)
    is_label: bool = False


@dataclass(slots=True)
class _BodyGroup:
    text: str
    source_number: int
    children: list[_BodyItem] = field(default_factory=list)


def validate_section_number(section_number: int) -> int:
    """Validate and return a report section number."""
    if not MIN_SECTION_NUMBER <= section_number <= MAX_SECTION_NUMBER:
        raise ValueError(f"起始章节号必须在 {MIN_SECTION_NUMBER}～{MAX_SECTION_NUMBER} 之间。")
    return section_number


def _ensure_punctuation(text: str, *, label: bool = False) -> str:
    value = text.strip()
    if not value:
        return value
    if label:
        if value.endswith(("：", ":")):
            return value
        return f"{value.rstrip('，,；;。')}："
    if value.endswith(("；", ";", "：", ":", "，", ",")):
        return f"{value[:-1]}。"
    if not TERMINAL_PUNCTUATION.search(value):
        return f"{value}。"
    return value


def _split_inline_items(text: str) -> tuple[str, list[str]]:
    """Split inline numbered clauses while leaving decimals and versions intact."""
    matches = list(INLINE_NUMBER.finditer(text))
    if not matches:
        return text, []
    prefix = text[: matches[0].start()].strip("，,；;：:。 ")
    items = [
        text[match.end() : matches[index + 1].start() if index + 1 < len(matches) else None].strip()
        for index, match in enumerate(matches)
    ]
    return prefix, [item for item in items if item]


def _strip_bullet(text: str) -> str:
    return BULLET_PREFIX.sub("", text.strip()).strip()


def _parse_body(progress: str) -> list[_BodyGroup]:
    """Read explicit body markers; plain lines are siblings, never synthetic wrapper children."""
    groups: list[_BodyGroup] = []
    current: _BodyGroup | None = None
    active_child: _BodyItem | None = None
    orphan_root = False
    for raw in progress.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        bullet = bool(BULLET_PREFIX.match(raw))
        line = _strip_bullet(raw)
        if not line:
            continue
        top_match = TOP_LEVEL_NUMBER.match(line)
        sub_match = SUB_LEVEL_NUMBER.match(line)
        circled_match = CIRCLED_NUMBER.match(line)
        if top_match:
            orphan_root = False
            content = _strip_bullet(top_match.group(2))
            if not content:
                continue
            group_text, inline_items = _split_inline_items(content)
            if not group_text and inline_items:
                for item in inline_items:
                    current = _BodyGroup(_strip_bullet(item), len(groups) + 1)
                    groups.append(current)
                active_child = None
                continue
            current = _BodyGroup(group_text, int(top_match.group(1)))
            groups.append(current)
            active_child = None
            for item in inline_items:
                active_child = _BodyItem(_strip_bullet(item))
                current.children.append(active_child)
        elif sub_match and current is not None and not orphan_root:
            content = _strip_bullet(sub_match.group(2))
            if content:
                active_child = _BodyItem(content, is_label=content.endswith(("：", ":")))
                current.children.append(active_child)
        elif circled_match and current is not None and active_child is not None:
            content = _strip_bullet(circled_match.group(1))
            if content:
                active_child.grandchildren.append(content)
        elif bullet and current is not None and active_child and active_child.is_label:
            active_child.grandchildren.append(line)
        elif bullet and current is not None and current.text.endswith(("：", ":")):
            active_child = _BodyItem(line)
            current.children.append(active_child)
        else:
            # A trailing colon alone cannot prove the next independent topic is a child.
            orphan_root = bool(sub_match or circled_match)
            content = _strip_bullet(sub_match.group(2)) if sub_match else line
            if circled_match:
                content = _strip_bullet(circled_match.group(1))
            current = _BodyGroup(content, len(groups) + 1)
            groups.append(current)
            active_child = None
    return groups


def _body_paragraphs(progress: str) -> list[ReportParagraph]:
    paragraphs: list[ReportParagraph] = []
    for group_index, group in enumerate(_parse_body(progress), start=1):
        paragraphs.append(
            ReportParagraph(
                f"{group_index}、{_ensure_punctuation(group.text, label=bool(group.children))}",
                ParagraphKind.BODY_1,
            )
        )
        for child_index, child in enumerate(group.children, start=1):
            paragraphs.append(
                ReportParagraph(
                    f"（{child_index}）"
                    f"{_ensure_punctuation(child.text, label=bool(child.grandchildren))}",
                    ParagraphKind.BODY_2,
                )
            )
            if len(child.grandchildren) > len(CIRCLED_NUMBERS):
                raise ValueError("同一正文子项最多支持 20 个三级明细。")
            for grandchild_index, grandchild in enumerate(child.grandchildren, start=1):
                paragraphs.append(
                    ReportParagraph(
                        f"{CIRCLED_NUMBERS[grandchild_index - 1]}{_ensure_punctuation(grandchild)}",
                        ParagraphKind.BODY_3,
                    )
                )
    return paragraphs


def _group_by_project(items: tuple[WeeklyItem, ...]) -> list[tuple[str, list[WeeklyItem]]]:
    projects: OrderedDict[str, list[WeeklyItem]] = OrderedDict()
    for item in items:
        projects.setdefault(item.project, []).append(item)
    return list(projects.items())


def build_body_paragraphs(text: str, report_date: date) -> tuple[ReportParagraph, ...]:
    """Plan the same normalized, numbered body for DOCX and Web previews."""
    return tuple(_body_paragraphs(normalize_report_text(text, report_date)))


def build_report_plan(data: WeeklyData, report_date: date, section_number: int = 9) -> ReportPlan:
    """Create normalized content and a hierarchy beginning at ``section_number``."""
    section_number = validate_section_number(section_number)
    paragraphs = [
        ReportParagraph("数据科学产品部周报", ParagraphKind.TITLE),
        ReportParagraph(report_date.strftime("%Y%m%d"), ParagraphKind.DATE),
        ReportParagraph(f"{section_number}. 大模型产品研发", ParagraphKind.HEADING_1),
    ]

    for project_index, (project, items) in enumerate(_group_by_project(data.items), start=1):
        normalized_project = normalize_report_text(project, report_date)
        paragraphs.append(
            ReportParagraph(
                f"{section_number}.{project_index}. {normalized_project}",
                ParagraphKind.HEADING_2,
            )
        )
        for detail_index, item in enumerate(items, start=1):
            normalized_detail = normalize_report_text(item.detail, report_date)
            paragraphs.append(
                ReportParagraph(
                    f"{section_number}.{project_index}.{detail_index}. {normalized_detail}",
                    ParagraphKind.HEADING_3,
                )
            )
            paragraphs.extend(build_body_paragraphs(item.progress, report_date))

    return ReportPlan(
        report_date=report_date,
        source_label=data.label,
        section_number=section_number,
        item_count=len(data.items),
        paragraphs=tuple(paragraphs),
    )


def build_prepared_report_plan(
    prepared: PreparedReport,
    preset: RulePreset,
    report_date: date,
    section_number: int | None = None,
) -> ReportPlan:
    """Build a report plan from traceable blocks using a validated preset."""
    if not isinstance(prepared, PreparedReport) or not isinstance(preset, RulePreset):
        raise TypeError("prepared 和 preset 类型无效。")
    start = validate_section_number(
        section_number if section_number is not None else preset.document.heading_start_default
    )
    paragraphs = [
        ReportParagraph(preset.document.report_title, ParagraphKind.TITLE),
        ReportParagraph(report_date.strftime("%Y%m%d"), ParagraphKind.DATE),
    ]
    active_sections = [
        (section, [block for block in prepared.blocks if block.section_id == section.id])
        for section in preset.sections
    ]
    active_sections = [(section, blocks) for section, blocks in active_sections if blocks]
    for section_offset, (section, section_blocks) in enumerate(active_sections):
        section_number_value = start + section_offset
        validate_section_number(section_number_value)
        paragraphs.append(
            ReportParagraph(
                f"{section_number_value}. {section.title}",
                ParagraphKind.HEADING_1,
            )
        )
        groups: OrderedDict[str, list[object]] = OrderedDict()
        for block in section_blocks:
            groups.setdefault(block.group_key, []).append(block)
        for group_index, (group_key, blocks) in enumerate(groups.items(), start=1):
            paragraphs.append(
                ReportParagraph(
                    f"{section_number_value}.{group_index}. "
                    f"{normalize_report_text(group_key, report_date)}",
                    ParagraphKind.HEADING_2,
                )
            )
            for block_index, block in enumerate(blocks, start=1):
                paragraphs.append(
                    ReportParagraph(
                        f"{section_number_value}.{group_index}.{block_index}. {section.title}内容",
                        ParagraphKind.HEADING_3,
                    )
                )
                paragraphs.extend(build_body_paragraphs(block.effective_text, report_date))
    if len(paragraphs) == 2:
        raise ValueError("当前规则没有生成任何有效内容块。")
    return ReportPlan(
        report_date=report_date,
        source_label=prepared.source.source_label,
        section_number=start,
        item_count=len(prepared.source.sources),
        paragraphs=tuple(paragraphs),
    )


def prepared_body_paragraphs(prepared, preset, report_date):
    """Return each block's actual body numbering, including shared directory leaves."""
    from .numbering import format_body

    result, offsets = {}, {}
    sections = {section.id: section for section in preset.sections}
    for block in prepared.blocks:
        section = sections[block.section_id]
        key = block_heading_path(prepared, section, block) or (block.block_id,)
        start = offsets.get(key, 0)
        texts = [block.effective_text]
        if block.effective_version == "original" and len(block.source_ids) > 1:
            # Separate source lists may both restart at 1; keep them as sibling lists.
            records = {row.source_id: row for row in prepared.source.sources}
            fields = block.source_fields or (block.source_field,)
            texts = [
                "\n".join(records[source_id].fields.get(key, "").strip() for key in fields)
                for source_id in block.source_ids
            ]
        paragraphs = []
        local_offset = 0
        for text in texts:
            segment = list(build_body_paragraphs(text, report_date))
            for paragraph in segment:
                if paragraph.kind is ParagraphKind.BODY_1:
                    match = TOP_LEVEL_NUMBER.match(paragraph.text)
                    paragraph = replace(
                        paragraph, text=f"{int(match[1]) + local_offset}、{match[2]}"
                    )
                paragraphs.append(paragraph)
            local_offset += sum(p.kind is ParagraphKind.BODY_1 for p in segment)
        adjusted = []
        for paragraph in paragraphs:
            if paragraph.kind is ParagraphKind.BODY_1:
                match = TOP_LEVEL_NUMBER.match(paragraph.text)
                paragraph = ReportParagraph(f"{int(match[1]) + start}、{match[2]}", paragraph.kind)
            adjusted.append(paragraph)
        offsets[key] = start + sum(p.kind is ParagraphKind.BODY_1 for p in paragraphs)
        result[block.block_id] = tuple(
            replace(p, block_id=block.block_id)
            for p in format_body(adjusted, preset.document.numbering)
        )
    return result


def build_directory_plan(prepared, preset, report_date, section_number):
    """Route source blocks into an explicit ordered hierarchy independent of Excel columns."""

    from .numbering import validate_numbering

    # Build canonical prefixes first; the service applies configured numbering exactly once.
    canonical_preset = replace(
        preset, document=replace(preset.document, numbering=validate_numbering())
    )
    bodies = prepared_body_paragraphs(prepared, canonical_preset, report_date)
    tree = {}
    for section in preset.sections:
        blocks = [block for block in prepared.blocks if block.section_id == section.id]
        if not blocks:
            continue
        for block in blocks:
            node = tree
            for title in block_heading_path(prepared, section, block):
                child = node.setdefault(title, {"children": {}, "blocks": []})
                node = child["children"]
            child["blocks"].append(block)
    paragraphs = [
        ReportParagraph(preset.document.report_title, ParagraphKind.TITLE),
        ReportParagraph(report_date.strftime("%Y%m%d"), ParagraphKind.DATE),
    ]

    def walk(nodes, parents=()):
        for index, (title, node) in enumerate(nodes.items(), section_number if not parents else 1):
            numbers = parents + (index,)
            if index > 999:
                raise ValueError("标题编号超过999。")
            prefix = ".".join(str(n) for n in numbers) + ". "
            paragraphs.append(
                ReportParagraph(
                    prefix + normalize_report_text(title, report_date),
                    ParagraphKind(f"heading_{len(numbers)}"),
                )
            )
            for block in node["blocks"]:
                paragraphs.extend(bodies[block.block_id])
            walk(node["children"], numbers)

    walk(tree)
    return ReportPlan(
        report_date,
        prepared.source.source_label,
        section_number,
        len(prepared.source.sources),
        tuple(paragraphs),
    )
