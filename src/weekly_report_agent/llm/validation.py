"""Mode-aware, graded validation for LLM-generated weekly-report text."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from enum import IntEnum

from .facts import Fact, FactKind, extract_facts


class ValidationLevel(IntEnum):
    """Ordered validation severity."""

    PASS = 0
    REVIEW = 1
    CONFLICT = 2

    @property
    def label(self) -> str:
        """Return a user-facing Chinese label."""
        return {
            ValidationLevel.PASS: "通过",
            ValidationLevel.REVIEW: "待人工确认",
            ValidationLevel.CONFLICT: "严重冲突",
        }[self]


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One explainable fact mismatch with bounded source excerpts."""

    level: ValidationLevel
    code: str
    message: str
    original_excerpt: str
    generated_excerpt: str
    original_fact: Fact | None = None
    generated_fact: Fact | None = None


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Complete graded result for one generated content block."""

    level: ValidationLevel
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def accepted(self) -> bool:
        """Only fully validated text can flow into automatic DOCX export."""
        return self.level is ValidationLevel.PASS


ALWAYS_CRITICAL_KINDS = {
    FactKind.MODEL_VERSION,
    FactKind.METRIC,
    FactKind.PERCENT,
    FactKind.PERCENTAGE_POINT,
    FactKind.STATUS,
}
CONCISE_OPTIONAL_KINDS = {
    FactKind.DATE,
    FactKind.QUANTITY,
    FactKind.TECHNICAL_TERM,
    FactKind.PLAIN_NUMBER,
}
AGGREGATION_UNITS = {"项", "条", "组"}
FACT_LABELS = {
    FactKind.MODEL_VERSION: "模型与版本",
    FactKind.TECHNICAL_TERM: "模型或技术名",
    FactKind.DATE: "日期",
    FactKind.METRIC: "指标",
    FactKind.PERCENT: "百分比",
    FactKind.PERCENTAGE_POINT: "百分点",
    FactKind.QUANTITY: "数量",
    FactKind.STATUS: "任务状态",
    FactKind.PLAIN_NUMBER: "业务数字",
}


def _excerpt(text: str, fact: Fact | None, limit: int = 96) -> str:
    if fact is None:
        compact = text.strip().replace("\n", " ")
        return compact[:limit] + ("…" if len(compact) > limit else "")
    start = max(0, fact.start - 32)
    end = min(len(text), fact.end + 32)
    excerpt = text[start:end].strip().replace("\n", " ")
    return ("…" if start else "") + excerpt + ("…" if end < len(text) else "")


def _identity_map(facts: tuple[Fact, ...]) -> dict[tuple[str, str, str, str, str], Fact]:
    return {fact.identity: fact for fact in facts if fact.kind is not FactKind.STATUS}


def _quantity_compatible(left: Fact, right: Fact) -> bool:
    """Accept an explicitly supplied count noun when value and comparison stay unchanged."""
    return (
        left.kind is FactKind.QUANTITY
        and right.kind is FactKind.QUANTITY
        and left.value == right.value
        and left.comparison == right.comparison
        and (left.unit == right.unit or left.unit is None or right.unit is None)
    )


def _remove_compatible_quantities(
    missing: list[Fact], added: list[Fact]
) -> tuple[list[Fact], list[Fact]]:
    remaining_added = list(added)
    remaining_missing: list[Fact] = []
    for source in missing:
        match = next(
            (candidate for candidate in remaining_added if _quantity_compatible(source, candidate)),
            None,
        )
        if match is None:
            remaining_missing.append(source)
        else:
            remaining_added.remove(match)
    return remaining_missing, remaining_added


def _remove_derived_aggregation_counts(
    original: tuple[Fact, ...], generated: tuple[Fact, ...]
) -> tuple[Fact, ...]:
    original_counts = Counter(
        fact.identity for fact in original if fact.kind is not FactKind.STATUS
    )
    generated_counts = Counter(
        fact.identity for fact in generated if fact.kind is not FactKind.STATUS
    )
    duplicate_counts = {
        count
        for identity, count in original_counts.items()
        if count > 1 and generated_counts[identity] < count
    }
    if not duplicate_counts:
        return generated
    return tuple(
        fact
        for fact in generated
        if not (
            fact.kind is FactKind.QUANTITY
            and fact.unit in AGGREGATION_UNITS
            and fact.identity not in original_counts
            and fact.value.isdigit()
            and int(fact.value) in duplicate_counts
        )
    )


def _anchor_tokens(anchor: str | None) -> set[str]:
    if not anchor:
        return set()
    normalized = re.sub(r"\s+", "", anchor.lower())
    ascii_words = set(re.findall(r"[a-z][a-z0-9_.-]*", normalized))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    bigrams = {chinese[index : index + 2] for index in range(len(chinese) - 1)}
    return ascii_words | bigrams


def _anchor_similarity(left: str | None, right: str | None) -> float:
    left_tokens = _anchor_tokens(left)
    right_tokens = _anchor_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _status_issues(
    original_text: str,
    generated_text: str,
    original: tuple[Fact, ...],
    generated: tuple[Fact, ...],
) -> list[ValidationIssue]:
    before = [fact for fact in original if fact.kind is FactKind.STATUS]
    after = [fact for fact in generated if fact.kind is FactKind.STATUS]
    issues: list[ValidationIssue] = []
    before_values = {fact.value for fact in before}
    after_values = {fact.value for fact in after}
    for value in sorted(before_values - after_values):
        fact = next(item for item in before if item.value == value)
        issues.append(
            ValidationIssue(
                ValidationLevel.CONFLICT,
                "status_missing",
                f"生成内容缺少任务状态：{value}",
                _excerpt(original_text, fact),
                _excerpt(generated_text, None),
                original_fact=fact,
            )
        )
    for value in sorted(after_values - before_values):
        fact = next(item for item in after if item.value == value)
        issues.append(
            ValidationIssue(
                ValidationLevel.CONFLICT,
                "status_added",
                f"生成内容新增任务状态：{value}",
                _excerpt(original_text, None),
                _excerpt(generated_text, fact),
                generated_fact=fact,
            )
        )
    if issues or len(before_values) < 2:
        return issues

    for source_status in before:
        same_status = [fact for fact in after if fact.value == source_status.value]
        if any(
            _anchor_similarity(source_status.action_anchor, fact.action_anchor) >= 0.35
            for fact in same_status
        ):
            continue
        swapped = max(
            (
                fact
                for fact in after
                if fact.value != source_status.value
                and _anchor_similarity(source_status.action_anchor, fact.action_anchor) >= 0.45
            ),
            key=lambda fact: _anchor_similarity(source_status.action_anchor, fact.action_anchor),
            default=None,
        )
        if swapped is not None:
            issues.append(
                ValidationIssue(
                    ValidationLevel.CONFLICT,
                    "status_attribution_changed",
                    f"动作“{source_status.action_anchor}”的状态疑似由“{source_status.value}”"
                    f"改为“{swapped.value}”",
                    _excerpt(original_text, source_status),
                    _excerpt(generated_text, swapped),
                    source_status,
                    swapped,
                )
            )
    return issues


def _missing_level(kind: FactKind, mode: str) -> ValidationLevel:
    """Apply completeness policy without relaxing invention or fact mutation checks."""
    if kind in ALWAYS_CRITICAL_KINDS:
        return ValidationLevel.CONFLICT
    if mode == "concise" and kind in CONCISE_OPTIONAL_KINDS:
        return ValidationLevel.REVIEW
    return ValidationLevel.CONFLICT


def validate_generated_text(original: str, generated: str, mode: str) -> ValidationResult:
    """Compare normalized facts with mode-specific completeness and attribution rules."""
    original_facts = extract_facts(original)
    generated_facts = _remove_derived_aggregation_counts(original_facts, extract_facts(generated))
    before = _identity_map(original_facts)
    after = _identity_map(generated_facts)
    missing = [before[identity] for identity in before.keys() - after.keys()]
    added = [after[identity] for identity in after.keys() - before.keys()]
    missing, added = _remove_compatible_quantities(missing, added)
    issues: list[ValidationIssue] = []

    used_added: set[tuple[str, str, str, str, str]] = set()
    for fact in sorted(missing, key=lambda item: item.start):
        replacement = next(
            (
                candidate
                for candidate in added
                if candidate.identity not in used_added
                and candidate.kind is fact.kind
                and candidate.name == fact.name
            ),
            None,
        )
        if replacement is not None:
            used_added.add(replacement.identity)
            issues.append(
                ValidationIssue(
                    ValidationLevel.CONFLICT,
                    "fact_changed",
                    f"{FACT_LABELS[fact.kind]}由“{fact.display}”变为“{replacement.display}”",
                    _excerpt(original, fact),
                    _excerpt(generated, replacement),
                    fact,
                    replacement,
                )
            )
            continue
        issues.append(
            ValidationIssue(
                _missing_level(fact.kind, mode),
                "fact_missing",
                f"生成内容缺少{FACT_LABELS[fact.kind]}：{fact.display}",
                _excerpt(original, fact),
                _excerpt(generated, None),
                original_fact=fact,
            )
        )

    for fact in sorted(added, key=lambda item: item.start):
        if fact.identity not in used_added:
            issues.append(
                ValidationIssue(
                    ValidationLevel.CONFLICT,
                    "fact_added",
                    f"生成内容新增{FACT_LABELS[fact.kind]}：{fact.display}",
                    _excerpt(original, None),
                    _excerpt(generated, fact),
                    generated_fact=fact,
                )
            )

    issues.extend(_status_issues(original, generated, original_facts, generated_facts))
    level = max((issue.level for issue in issues), default=ValidationLevel.PASS)
    return ValidationResult(level, tuple(issues))
