"""Deterministic report text normalization without factual rewriting."""

from __future__ import annotations

import re
from datetime import date, timedelta

TERM_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"tabpfn", re.IGNORECASE), "TabPFN"),
    (re.compile(r"tabicl", re.IGNORECASE), "TabICL"),
    (re.compile(r"tabm", re.IGNORECASE), "TabM"),
    (re.compile(r"(?<![A-Za-z])lgb(?![A-Za-z])", re.IGNORECASE), "LightGBM"),
    (re.compile(r"(?<![A-Za-z])oot(?![A-Za-z])", re.IGNORECASE), "OOT"),
    (re.compile(r"(?<![A-Za-z])oom(?![A-Za-z])", re.IGNORECASE), "OOM"),
    (re.compile(r"(?<![A-Za-z])bf16(?![A-Za-z])", re.IGNORECASE), "BF16"),
)

WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
RELATIVE_WEEKDAY_PATTERN = re.compile(r"(本周|下周|周)([一二三四五六日天])")
ACTION_NAME_PATTERN = re.compile(
    r"(同步|联系|咨询|请教|告知|反馈给|发送给|交付给)([\u4e00-\u9fff]{1,3}[哥姐])"
)
STANDALONE_NAME_PATTERN = re.compile(
    r"(?<![A-Za-z\u4e00-\u9fff])(?:[A-Za-z]{1,20}|[\u4e00-\u9fff]{1,3})[哥姐]"
)


def _concrete_weekday(match: re.Match[str], report_date: date) -> str:
    qualifier, weekday = match.groups()
    monday = report_date - timedelta(days=report_date.weekday())
    week_offset = 7 if qualifier == "下周" else 0
    target = monday + timedelta(days=WEEKDAY_INDEX[weekday] + week_offset)
    return f"{target.year}年{target.month}月{target.day}日"


def normalize_report_text(text: str, report_date: date | None = None) -> str:
    """Normalize terminology and wording while preserving values and conclusions."""
    value = text.replace("\r\n", "\n").replace("\r", "\n")
    for pattern, replacement in TERM_REPLACEMENTS:
        value = pattern.sub(replacement, value)

    value = ACTION_NAME_PATTERN.sub(lambda match: f"{match.group(1)}相关负责人", value)
    value = STANDALONE_NAME_PATTERN.sub("相关负责人", value)
    if report_date is not None:
        value = RELATIVE_WEEKDAY_PATTERN.sub(
            lambda match: _concrete_weekday(match, report_date), value
        )

    value = value.replace("~", "～")
    value = re.sub(r"[ \t]+([，。；：！？])", r"\1", value)
    value = re.sub(r"(?m)^[ \t]+", "", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()
