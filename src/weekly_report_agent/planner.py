"""Build a renderer-independent report hierarchy."""

from __future__ import annotations

import re
from collections import OrderedDict
from datetime import date

from .models import ParagraphKind, ReportParagraph, ReportPlan, WeeklyData, WeeklyItem
from .normalizer import normalize_report_text

TERMINAL_PUNCTUATION = re.compile(r"[。；：！？.!?]$")
TOP_LEVEL_NUMBER = re.compile(r"^(\d+)[、.]\s*")
SUB_LEVEL_NUMBER = re.compile(r"^(?:[（(](\d+)[)）]|(\d+)[)）])\s*")


def _normalize_body_line(line: str) -> str:
    value = line.strip()
    top_match = TOP_LEVEL_NUMBER.match(value)
    if top_match:
        value = TOP_LEVEL_NUMBER.sub(f"{top_match.group(1)}、", value, count=1)
    else:
        sub_match = SUB_LEVEL_NUMBER.match(value)
        if sub_match:
            number = sub_match.group(1) or sub_match.group(2)
            value = SUB_LEVEL_NUMBER.sub(f"（{number}）", value, count=1)
    if value and not TERMINAL_PUNCTUATION.search(value):
        value += "。"
    return value


def _group_by_project(items: tuple[WeeklyItem, ...]) -> list[tuple[str, list[WeeklyItem]]]:
    projects: OrderedDict[str, list[WeeklyItem]] = OrderedDict()
    for item in items:
        projects.setdefault(item.project, []).append(item)
    return list(projects.items())


def build_report_plan(data: WeeklyData, report_date: date) -> ReportPlan:
    """Create titles, hierarchy numbering, and normalized body paragraphs."""
    paragraphs = [
        ReportParagraph("数据科学产品部周报", ParagraphKind.TITLE),
        ReportParagraph(report_date.strftime("%Y%m%d"), ParagraphKind.DATE),
        ReportParagraph("9. 大模型产品研发", ParagraphKind.HEADING_1),
    ]

    for project_index, (project, items) in enumerate(_group_by_project(data.items), start=1):
        paragraphs.append(ReportParagraph(f"9.{project_index} {project}", ParagraphKind.HEADING_2))
        for detail_index, item in enumerate(items, start=1):
            paragraphs.append(
                ReportParagraph(
                    f"9.{project_index}.{detail_index} {item.detail}",
                    ParagraphKind.HEADING_3,
                )
            )
            normalized = normalize_report_text(item.progress, report_date)
            for line in normalized.splitlines():
                body = _normalize_body_line(line)
                if body:
                    paragraphs.append(ReportParagraph(body, ParagraphKind.BODY))

    return ReportPlan(
        report_date=report_date,
        source_label=data.label,
        item_count=len(data.items),
        paragraphs=tuple(paragraphs),
    )
