"""Typed, position-aware fact extraction for weekly-report text."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum


class FactKind(str, Enum):
    """Fact categories that must not be compared as untyped numbers."""

    MODEL_VERSION = "model_version"
    TECHNICAL_TERM = "technical_term"
    DATE = "date"
    METRIC = "metric"
    PERCENT = "percent"
    PERCENTAGE_POINT = "percentage_point"
    QUANTITY = "quantity"
    STATUS = "status"
    PLAIN_NUMBER = "plain_number"


@dataclass(frozen=True, slots=True)
class Fact:
    """One normalized fact and its location in source text."""

    kind: FactKind
    value: str
    surface: str
    start: int
    end: int
    name: str | None = None
    unit: str | None = None
    comparison: str | None = None
    action_anchor: str | None = None

    @property
    def identity(self) -> tuple[str, str, str, str, str]:
        """Return the comparison identity, excluding presentation and location."""
        return (
            self.kind.value,
            self.name or "",
            self.value,
            self.unit or "",
            self.comparison or "",
        )

    @property
    def display(self) -> str:
        """Return a concise normalized representation for diagnostics."""
        if self.kind is FactKind.MODEL_VERSION:
            return f"{self.name} {self.value}"
        if self.kind is FactKind.METRIC:
            return f"{self.name.upper()} {self.comparison or ''}{self.value}{self.unit or ''}"
        if self.kind is FactKind.STATUS:
            return f"{self.action_anchor or '相关任务'}：{self.value}"
        return f"{self.comparison or ''}{self.value}{self.unit or ''}"


NUMBER_TEXT = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
CHINESE_NUMBER_TEXT = r"[零〇一二两三四五六七八九十百千万亿]+"
NUMBER_WITH_SCALE = rf"(?:{NUMBER_TEXT}(?:[ \t]*[千万亿])?|{CHINESE_NUMBER_TEXT})"
COMPARISON_PREFIX = (
    r"(?P<prefix><=|>=|[<>≤≥]|不超过|小于等于|至多|不低于|不少于|"
    r"大于等于|至少|小于|低于|大于|高于)?"
)
COMPARISON_SUFFIX = r"(?P<suffix>以上|及以上|以下|及以下)?"

DATE_PATTERN = re.compile(r"(?:\d{4}[年/-]\d{1,2}[月/-]\d{1,2}日?|\d{1,2}月\d{1,2}日)")
LIST_MARKER_PATTERN = re.compile(
    r"(^|[\n\r，。；;：:])\s*"
    r"(?P<marker>[（(][1-9]\d?[）)]|[1-9]\d?[、）)]|[1-9]\d?[.．](?!\d))",
    re.MULTILINE,
)

MODEL_ALIASES = {
    "lgb": "lightgbm",
    "lgbm": "lightgbm",
    "lightgbm": "lightgbm",
    "tabpfn": "tabpfn",
    "tabicl": "tabicl",
    "tabm": "tabm",
    "qwen": "qwen",
    "deepseek": "deepseek",
    "xgboost": "xgboost",
    "xgb": "xgboost",
    "catboost": "catboost",
    "bert": "bert",
    "glm": "glm",
}
MODEL_NAMES = "|".join(sorted(MODEL_ALIASES, key=len, reverse=True))
MODEL_VERSION_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_])(?P<model>{MODEL_NAMES})"
    r"[ \t]*(?:v(?:ersion)?[ \t]*)?(?P<version>\d+(?:\.\d+)+)"
    r"(?![A-Za-z0-9_.])",
    re.IGNORECASE,
)
TECHNICAL_ALIASES = {
    **MODEL_ALIASES,
    "auc": "auc",
    "ks": "ks",
    "psi": "psi",
    "bf16": "bf16",
    "oom": "oom",
    "oot": "oot",
}
TECHNICAL_NAMES = "|".join(sorted(TECHNICAL_ALIASES, key=len, reverse=True))
TECHNICAL_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_])(?:{TECHNICAL_NAMES})(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

METRIC_PATTERN = re.compile(
    rf"(?P<name>AUC|KS|PSI)[ \t]*(?:提升|下降|变化|达到|约为|为|是|约|[:：=])?[ \t]*"
    rf"{COMPARISON_PREFIX}[ \t]*(?P<first>{NUMBER_WITH_SCALE})"
    rf"(?:[ \t]*(?:-|—|–|~|～|至|到)[ \t]*(?P<second>{NUMBER_WITH_SCALE}))?"
    rf"[ \t]*(?P<unit>个百分点|个点|点|pp|%|％)?[ \t]*{COMPARISON_SUFFIX}",
    re.IGNORECASE,
)
PERCENTAGE_POINT_PATTERN = re.compile(
    rf"{COMPARISON_PREFIX}[ \t]*(?P<number>{NUMBER_WITH_SCALE})[ \t]*"
    rf"(?P<unit>个百分点|pp)[ \t]*{COMPARISON_SUFFIX}",
    re.IGNORECASE,
)
PERCENT_PATTERN = re.compile(
    rf"{COMPARISON_PREFIX}[ \t]*(?P<number>{NUMBER_WITH_SCALE})[ \t]*"
    rf"(?P<unit>%|％)[ \t]*{COMPARISON_SUFFIX}",
    re.IGNORECASE,
)
CHINESE_PERCENT_PATTERN = re.compile(rf"百分之(?P<number>{CHINESE_NUMBER_TEXT})")
QUANTITY_UNITS = (
    "个工作日",
    "个月",
    "样本",
    "条",
    "项",
    "轮",
    "组",
    "家",
    "人",
    "年",
    "月",
    "周",
    "天",
    "个",
    "次",
    "折",
    "倍",
    "点",
    "GB",
    "MB",
    "KB",
)
QUANTITY_PATTERN = re.compile(
    rf"{COMPARISON_PREFIX}[ \t]*(?P<number>{NUMBER_WITH_SCALE})[ \t]*"
    rf"(?P<unit>{'|'.join(QUANTITY_UNITS)})?[ \t]*{COMPARISON_SUFFIX}",
    re.IGNORECASE,
)
# Plain facts intentionally accept Arabic numerals only. Chinese numeral fragments such as
# “百” in a product name must not become business numbers; Chinese numbers with semantic
# units are already handled by the percentage and quantity patterns above.
PLAIN_NUMBER_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_.]){COMPARISON_PREFIX}[ \t]*(?P<number>{NUMBER_TEXT})"
    rf"[ \t]*{COMPARISON_SUFFIX}(?![A-Za-z0-9_.])",
    re.IGNORECASE,
)

STATUS_PATTERNS = (
    ("未开始", re.compile(r"尚未开始|未开始")),
    ("未完成", re.compile(r"尚未完成|未完成")),
    ("进行中", re.compile(r"正在进行|持续进行中|进行中")),
    ("已完成", re.compile(r"(?:已经|现已|已)完成")),
    ("暂停", re.compile(r"已暂停|暂停")),
    ("延期", re.compile(r"已延期|延期")),
    ("阻塞", re.compile(r"受阻|阻塞")),
    ("失败", re.compile(r"失败")),
)
CLAUSE_BOUNDARY_PATTERN = re.compile(r"[\n\r，,。；;：:]")

COMPARISON_ALIASES = {
    "<=": "≤",
    "不超过": "≤",
    "小于等于": "≤",
    "至多": "≤",
    "以下": "≤",
    "及以下": "≤",
    ">=": "≥",
    "不低于": "≥",
    "不少于": "≥",
    "大于等于": "≥",
    "至少": "≥",
    "以上": "≥",
    "及以上": "≥",
    "小于": "<",
    "低于": "<",
    "大于": ">",
    "高于": ">",
}


def _overlaps(span: tuple[int, int], occupied: list[tuple[int, int]]) -> bool:
    return any(span[0] < current[1] and current[0] < span[1] for current in occupied)


def _chinese_integer(raw: str) -> Decimal:
    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    small_units = {"十": 10, "百": 100, "千": 1000}
    large_units = {"万": 10000, "亿": 100000000}
    total = section = number = 0
    for character in raw:
        if character in digits:
            number = digits[character]
        elif character in small_units:
            section += (number or 1) * small_units[character]
            number = 0
        elif character in large_units:
            section += number
            total = (total + section) * large_units[character]
            section = number = 0
    return Decimal(total + section + number)


def canonical_number(raw: str) -> str:
    """Normalize Arabic/Chinese numbers and Chinese scale suffixes."""
    raw = re.sub(r"\s+", "", raw)
    multiplier = Decimal("1")
    if raw and raw[-1] in "千万亿" and raw[:-1] and raw[:-1][-1].isdigit():
        multiplier = {
            "千": Decimal("1000"),
            "万": Decimal("10000"),
            "亿": Decimal("100000000"),
        }[raw[-1]]
        raw = raw[:-1]
    try:
        value = (
            _chinese_integer(raw)
            if re.fullmatch(CHINESE_NUMBER_TEXT, raw)
            else Decimal(raw.replace(",", ""))
        )
    except InvalidOperation:  # pragma: no cover
        return raw
    value *= multiplier
    normalized = format(value.normalize(), "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def canonical_version(raw: str) -> str:
    """Normalize each version segment without discarding significant segment positions."""
    return ".".join(str(int(segment)) for segment in raw.split("."))


def _comparison(match: re.Match[str]) -> str | None:
    raw = match.groupdict().get("prefix") or match.groupdict().get("suffix") or ""
    return COMPARISON_ALIASES.get(raw, raw or None)


def _date_value(surface: str) -> str:
    parts = [int(item) for item in re.findall(r"\d+", surface)]
    if len(parts) == 3:
        return f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"
    return f"--{parts[0]:02d}-{parts[1]:02d}"


def _action_anchor(text: str, start: int, end: int) -> str | None:
    left_boundaries = list(CLAUSE_BOUNDARY_PATTERN.finditer(text, 0, start))
    left = left_boundaries[-1].end() if left_boundaries else 0
    right_match = CLAUSE_BOUNDARY_PATTERN.search(text, end)
    right = right_match.start() if right_match else len(text)
    clause = text[left:right].strip()
    clause = re.sub(r"^[（(]?\d+[）)、.．\s]*", "", clause)
    for _status, pattern in STATUS_PATTERNS:
        clause = pattern.sub("", clause)
    clause = re.sub(r"\s+", "", clause).strip("，。；;：:")
    return clause[-32:] or None


def _append_fact(
    facts: list[Fact],
    occupied: list[tuple[int, int]],
    match: re.Match[str],
    kind: FactKind,
    *,
    value: str,
    name: str | None = None,
    unit: str | None = None,
    comparison: str | None = None,
    occupy: bool = True,
    action_anchor: str | None = None,
) -> None:
    facts.append(
        Fact(
            kind=kind,
            value=value,
            surface=match.group(),
            start=match.start(),
            end=match.end(),
            name=name,
            unit=unit,
            comparison=comparison,
            action_anchor=action_anchor,
        )
    )
    if occupy:
        occupied.append(match.span())


def extract_facts(text: str) -> tuple[Fact, ...]:
    """Extract typed facts in semantic-priority order without double counting spans."""
    facts: list[Fact] = []
    occupied = [match.span("marker") for match in LIST_MARKER_PATTERN.finditer(text)]

    for match in DATE_PATTERN.finditer(text):
        _append_fact(facts, occupied, match, FactKind.DATE, value=_date_value(match.group()))

    for match in MODEL_VERSION_PATTERN.finditer(text):
        model = MODEL_ALIASES[match.group("model").lower()]
        _append_fact(
            facts,
            occupied,
            match,
            FactKind.MODEL_VERSION,
            name=model,
            value=canonical_version(match.group("version")),
        )

    for match in METRIC_PATTERN.finditer(text):
        if _overlaps(match.span(), occupied):
            continue
        raw_unit = (match.group("unit") or "").lower()
        unit = {"％": "%", "个百分点": "pp", "个点": "点"}.get(raw_unit, raw_unit or None)
        values = [match.group("first")]
        if match.group("second"):
            values.append(match.group("second"))
        for raw_value in values:
            _append_fact(
                facts,
                occupied,
                match,
                FactKind.METRIC,
                name=match.group("name").lower(),
                value=canonical_number(raw_value),
                unit=unit,
                comparison=_comparison(match),
                occupy=False,
            )
        occupied.append(match.span())

    for pattern, kind, normalized_unit in (
        (PERCENTAGE_POINT_PATTERN, FactKind.PERCENTAGE_POINT, "pp"),
        (PERCENT_PATTERN, FactKind.PERCENT, "%"),
    ):
        for match in pattern.finditer(text):
            if _overlaps(match.span(), occupied):
                continue
            _append_fact(
                facts,
                occupied,
                match,
                kind,
                value=canonical_number(match.group("number")),
                unit=normalized_unit,
                comparison=_comparison(match),
            )

    for match in CHINESE_PERCENT_PATTERN.finditer(text):
        if not _overlaps(match.span(), occupied):
            _append_fact(
                facts,
                occupied,
                match,
                FactKind.PERCENT,
                value=canonical_number(match.group("number")),
                unit="%",
            )

    for match in QUANTITY_PATTERN.finditer(text):
        if _overlaps(match.span(), occupied):
            continue
        raw_number = match.group("number")
        if raw_number in {"千", "万", "亿"}:
            continue
        unit = match.group("unit")
        scaled = bool(raw_number and raw_number[-1:] in "千万亿")
        if not unit and not scaled:
            continue
        normalized_unit = unit.upper() if unit and unit.lower() in {"gb", "mb", "kb"} else unit
        _append_fact(
            facts,
            occupied,
            match,
            FactKind.QUANTITY,
            value=canonical_number(raw_number),
            unit=normalized_unit,
            comparison=_comparison(match),
        )

    for status, pattern in STATUS_PATTERNS:
        for match in pattern.finditer(text):
            if not _overlaps(match.span(), occupied):
                _append_fact(
                    facts,
                    occupied,
                    match,
                    FactKind.STATUS,
                    value=status,
                    action_anchor=_action_anchor(text, match.start(), match.end()),
                )

    for match in TECHNICAL_PATTERN.finditer(text):
        if not _overlaps(match.span(), occupied):
            value = TECHNICAL_ALIASES[match.group().lower()]
            _append_fact(facts, occupied, match, FactKind.TECHNICAL_TERM, value=value)

    for match in PLAIN_NUMBER_PATTERN.finditer(text):
        if not _overlaps(match.span(), occupied):
            _append_fact(
                facts,
                occupied,
                match,
                FactKind.PLAIN_NUMBER,
                value=canonical_number(match.group("number")),
                comparison=_comparison(match),
            )

    return tuple(sorted(facts, key=lambda fact: (fact.start, fact.end, fact.kind.value)))


def fact_counts(facts: tuple[Fact, ...]) -> Counter[tuple[str, str, str, str, str]]:
    """Count normalized identities while retaining typed distinctions."""
    return Counter(fact.identity for fact in facts)


def protected_facts(text: str) -> dict[str, list[str]]:
    """Serialize normalized facts for the model prompt."""
    grouped: dict[str, list[str]] = {}
    for fact in extract_facts(text):
        grouped.setdefault(fact.kind.value, []).append(fact.display)
    return {kind: sorted(set(values)) for kind, values in grouped.items()}
