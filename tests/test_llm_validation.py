"""Critical fact preservation and advisory differences."""

import pytest

from sheetweave_agent.llm.facts import FactKind, extract_facts
from sheetweave_agent.llm.validation import ValidationLevel, validate_generated_text


@pytest.mark.parametrize(
    ("original", "generated"),
    [
        ("TabPFN3.0调试", "tabpfn 3.0调试"),
        ("抽取5千样本", "抽取5000样本"),
        ("2026年10月12日复核", "2026-10-12复核"),
        ("部署已经完成，调试进行中", "部署已完成，调试正在进行"),
        ("部署和调试已完成，验证进行中", "部署与调试已完成，验证进行中"),
        (
            "模型在1万oot上效果ks是62.5%，lgb在1万oot上效果ks是56.5%。",
            "模型在 1 万 oot 上效果 KS 为 62.5%，LightGBM 在 1 万 oot 上效果 KS 为 56.5%。",
        ),
    ],
)
def test_normalized_equivalent_facts_pass(original: str, generated: str) -> None:
    assert validate_generated_text(original, generated, "polish").level is ValidationLevel.PASS


@pytest.mark.parametrize(
    ("original", "generated", "reason"),
    [
        ("TabPFN3.0调试", "TabPFN 3.1调试", "模型与版本"),
        ("AUC 0.81，KS 0.53", "AUC 0.53，KS 0.81", "指标"),
        ("提升3%", "提升3个百分点", "百分"),
        ("部署已完成，调试进行中", "部署进行中，调试已完成", "状态疑似"),
        ("抽取5000样本", "抽取6000样本", "数量"),
        ("在1万oot上验证", "在 2 万 oot 上验证", "数量"),
        ("2026年10月12日复核", "2026年10月13日复核", "日期"),
    ],
)
def test_changed_or_misattributed_fact_is_conflict(
    original: str, generated: str, reason: str
) -> None:
    result = validate_generated_text(original, generated, "polish")

    assert result.level is ValidationLevel.CONFLICT
    assert any(reason in issue.message for issue in result.issues)
    assert all(issue.original_excerpt and issue.generated_excerpt for issue in result.issues)


def test_polish_requires_details_metrics_and_status() -> None:
    optional = validate_generated_text(
        "10月12日完成1000条样本整理并使用LightGBM", "完成样本整理", "polish"
    )
    metric = validate_generated_text("AUC 0.81验证已完成", "验证已完成", "polish")

    assert optional.level is ValidationLevel.CONFLICT
    assert metric.level is ValidationLevel.CONFLICT


@pytest.mark.parametrize("mode", ["polish"])
def test_non_concise_modes_require_all_source_facts(mode: str) -> None:
    result = validate_generated_text("10月12日完成1000条样本整理", "完成样本整理", mode)

    assert result.level is ValidationLevel.CONFLICT


def test_multi_record_summary_may_merge_duplicate_facts_and_derive_count() -> None:
    original = "TabPFN相关性0.9\nTabPFN相关性0.9"
    generated = "两项与TabPFN的相关性均为0.9"

    assert validate_generated_text(original, generated, "polish").level is ValidationLevel.PASS


@pytest.mark.parametrize(
    "original,generated",
    [
        ("使用LightGBM进行验证。", "进行验证。"),
        ("编号5090的环境。", "相关环境。"),
    ],
)
def test_polish_advisory_difference_does_not_trigger_strict_fallback(original, generated):
    relaxed = validate_generated_text(original, generated, "polish")
    strict = validate_generated_text(original, generated, "original")
    assert relaxed.level is ValidationLevel.REVIEW and relaxed.accepted
    assert strict.level is ValidationLevel.CONFLICT and not strict.accepted


def test_polish_critical_conflict_overrides_advisory_omission():
    result = validate_generated_text("LightGBM，AUC 0.81。", "AUC 0.91。", "polish")
    assert result.level is ValidationLevel.CONFLICT and not result.accepted


def _facts(text: str, kind: FactKind):
    return [fact for fact in extract_facts(text) if fact.kind is kind]


def test_model_version_normalizes_spacing_case_and_version_marker() -> None:
    identities = {
        _facts(text, FactKind.MODEL_VERSION)[0].identity
        for text in ("tabpfn3.0", "TabPFN 3.0", "TABPFN v3.0")
    }

    assert identities == {("model_version", "tabpfn", "3.0", "", "")}
    assert not _facts("TabPFN3.0", FactKind.PLAIN_NUMBER)
    assert _facts("TabPFN 3.1", FactKind.MODEL_VERSION)[0].value == "3.1"


def test_normalizer_model_aliases_keep_fact_identity():
    from sheetweave_agent.normalizer import normalize_report_text

    for source in ("lgbm 4.5", "xgb 2.1", "使用lgbm和xgb验证，AUC 0.81。"):
        assert [f.identity for f in extract_facts(source)] == [
            f.identity for f in extract_facts(normalize_report_text(source))
        ]


def test_typed_numbers_do_not_overlap() -> None:
    facts = extract_facts("TabPFN3.0于2026年10月12日验证5000样本，AUC 0.81，提升3%，增加2个百分点")

    assert [(fact.name, fact.value) for fact in facts if fact.kind is FactKind.MODEL_VERSION] == [
        ("tabpfn", "3.0")
    ]
    assert [(fact.name, fact.value) for fact in facts if fact.kind is FactKind.METRIC] == [
        ("auc", "0.81")
    ]
    assert [fact.value for fact in facts if fact.kind is FactKind.DATE] == ["2026-10-12"]
    assert [fact.display for fact in facts if fact.kind is FactKind.QUANTITY] == ["5000样本"]
    assert [fact.display for fact in facts if fact.kind is FactKind.PERCENT] == ["3%"]
    assert [fact.display for fact in facts if fact.kind is FactKind.PERCENTAGE_POINT] == ["2pp"]
    assert not [fact for fact in facts if fact.kind is FactKind.PLAIN_NUMBER]


def test_quantity_date_and_status_equivalent_forms_are_normalized() -> None:
    source = extract_facts("5千样本，2026年10月12日，部署已经完成")
    generated = extract_facts("5000样本，2026-10-12，部署已完成")

    assert [fact.identity for fact in source] == [fact.identity for fact in generated]
    assert _facts("百擎部署", FactKind.PLAIN_NUMBER) == []


def test_spaced_scales_and_metric_copula_keep_numeric_identity() -> None:
    facts = extract_facts("在 1 万 oot 上效果 ks是62.5%")
    assert [(f.value, f.unit) for f in facts if f.kind is FactKind.QUANTITY] == [("10000", None)]
    assert [(f.name, f.value, f.unit) for f in facts if f.kind is FactKind.METRIC] == [
        ("ks", "62.5", "%")
    ]
    assert not [f for f in facts if f.kind in {FactKind.PLAIN_NUMBER, FactKind.PERCENT}]
