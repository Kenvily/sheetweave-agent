"""Finite, validated numbering choices for semantic report paragraph levels."""

from __future__ import annotations

import re
from dataclasses import replace

from .models import ReportPlan

CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
CHOICES = {
    "heading_1": ["{h1}. ", "{h1}、", "第{h1}章 "],
    "heading_2": ["{h1}.{h2}. ", "{h1}.{h2} ", "（{h2}）"],
    "heading_3": ["{h1}.{h2}.{h3}. ", "{h1}.{h2}.{h3} ", "{h3}、"],
    "body_1": ["{n}、", "{n}. ", "（{n}）"],
    "body_2": ["（{n}）", "({n}) ", "{n}. "],
    "body_3": ["{circle}", "（{n}）", "{n}. "],
}


def validate_numbering(value=None) -> dict[str, str]:
    if value is None:
        return {key: values[0] for key, values in CHOICES.items()}
    if not isinstance(value, dict) or set(value) - set(CHOICES):
        raise ValueError("不支持的编号层级配置。")
    result = validate_numbering()
    for key, pattern in value.items():
        if pattern not in CHOICES[key]:
            raise ValueError(f"不支持的 {key} 编号格式。")
        result[key] = pattern
    return result


def renumber(plan: ReportPlan, numbering: dict) -> ReportPlan:
    """Change only canonical prefix characters, never source content or hierarchy."""
    paragraphs = []
    h1, h2, h3 = plan.section_number, 0, 0
    for paragraph in plan.paragraphs:
        role, text = paragraph.kind.value, paragraph.text
        if role.startswith("heading_"):
            match = re.match(r"^(\d+)\.(?:(\d+)\.)?(?:(\d+)\.)?\s+", text)
            if not match:
                raise ValueError("标题缺少规范编号。")
            h1 = int(match[1])
            h2 = int(match[2] or 0)
            h3 = int(match[3] or 0)
            text = numbering[role].format(h1=h1, h2=h2, h3=h3) + text[match.end() :]
        elif role.startswith("body_"):
            match = re.match(r"^(\d+)、|^（(\d+)）|^([" + CIRCLED + "])", text)
            if not match:
                raise ValueError("正文缺少规范编号。")
            n = int(match[1] or match[2]) if match[1] or match[2] else CIRCLED.index(match[3]) + 1
            circle = CIRCLED[n - 1] if n <= len(CIRCLED) else ""
            if "{circle}" in numbering[role] and not circle:
                raise ValueError("带圈编号最多支持20项。")
            text = numbering[role].format(n=n, circle=circle) + text[match.end() :]
        paragraphs.append(replace(paragraph, text=text))
    return replace(plan, paragraphs=tuple(paragraphs))


def format_body(paragraphs, numbering):
    """Use the same configured body prefixes in review and document rendering."""
    from datetime import date

    plan = ReportPlan(date.today(), "", 1, 0, tuple(paragraphs))
    return renumber(plan, numbering).paragraphs
