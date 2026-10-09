"""Build a deterministic, renderer-independent report hierarchy."""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date

from .models import ParagraphKind, ReportParagraph, ReportPlan, WeeklyData, WeeklyItem
from .normalizer import normalize_report_text

TERMINAL_PUNCTUATION = re.compile(r"[。；：！？.!?]$")
TOP_LEVEL_NUMBER = re.compile(r"^(\d+)[、.．]\s*(.*)$")
SUB_LEVEL_NUMBER = re.compile(r"^(?:[（(]?(\d+)[)）])\s*(.*)$")
CIRCLED_NUMBERS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
CIRCLED_NUMBER = re.compile(rf"^[{CIRCLED_NUMBERS}]\s*(.*)$")
INLINE_NUMBER = re.compile(r"(?<=[，,。；;：:])\s*(\d+)[.、](?!\d)\s*")
MIN_SECTION_NUMBER = 1
MAX_SECTION_NUMBER = 999


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
    if not TERMINAL_PUNCTUATION.search(value):
        return f"{value}。"
    return value


def _split_inline_items(text: str) -> tuple[str, list[str]]:
    """Split inline numbered clauses while leaving decimals and versions intact."""
    matches = list(INLINE_NUMBER.finditer(text))
    if not matches:
        return text, []
    prefix = text[: matches[0].start()].strip() or "进展内容"
    items = [
        text[match.end() : matches[index + 1].start() if index + 1 < len(matches) else None].strip()
        for index, match in enumerate(matches)
    ]
    return prefix, [item for item in items if item]


def _new_default_group() -> _BodyGroup:
    return _BodyGroup("进展内容", 1)


def _parse_body(progress: str) -> list[_BodyGroup]:
    """Infer up to three body levels from source markers without rewriting facts."""
    lines = [line.strip() for line in progress.splitlines() if line.strip()]
    groups: list[_BodyGroup] = []
    current: _BodyGroup | None = None
    active_child: _BodyItem | None = None

    for line in lines:
        top_match = TOP_LEVEL_NUMBER.match(line)
        sub_match = SUB_LEVEL_NUMBER.match(line)
        circled_match = CIRCLED_NUMBER.match(line)

        if top_match:
            source_number = int(top_match.group(1))
            content = top_match.group(2).strip() or "进展内容"
            group_text, inline_items = _split_inline_items(content)
            if current is None or source_number > current.source_number:
                current = _BodyGroup(group_text, source_number)
                groups.append(current)
                active_child = None
                for inline_item in inline_items:
                    active_child = _BodyItem(inline_item)
                    current.children.append(active_child)
            elif active_child is not None and active_child.is_label:
                active_child.grandchildren.append(content)
            else:
                child = _BodyItem(content)
                current.children.append(child)
                active_child = child
            continue

        if current is None:
            current = _new_default_group()
            groups.append(current)

        if circled_match:
            content = circled_match.group(1).strip()
            if active_child is None:
                active_child = _BodyItem("具体事项", is_label=True)
                current.children.append(active_child)
            active_child.grandchildren.append(content)
            continue

        if sub_match:
            content = sub_match.group(2).strip() or "进展内容"
            if active_child is not None and active_child.is_label:
                active_child.grandchildren.append(content)
            else:
                active_child = _BodyItem(content)
                current.children.append(active_child)
            continue

        is_label = line.endswith(("：", ":"))
        if active_child is not None and active_child.is_label and not is_label:
            active_child.grandchildren.append(line)
        else:
            active_child = _BodyItem(line, is_label=is_label)
            current.children.append(active_child)

    return groups or [_new_default_group()]


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
            normalized_progress = normalize_report_text(item.progress, report_date)
            paragraphs.extend(_body_paragraphs(normalized_progress))

    return ReportPlan(
        report_date=report_date,
        source_label=data.label,
        section_number=section_number,
        item_count=len(data.items),
        paragraphs=tuple(paragraphs),
    )
