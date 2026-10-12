import pytest

from weekly_report_agent.llm.validation import ValidationLevel, validate_generated_text


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


def test_concise_can_omit_noncritical_detail_but_not_metric_or_status() -> None:
    optional = validate_generated_text(
        "10月12日完成1000条样本整理并使用LightGBM", "完成样本整理", "concise"
    )
    metric = validate_generated_text("AUC 0.81验证已完成", "验证已完成", "concise")

    assert optional.level is ValidationLevel.REVIEW
    assert metric.level is ValidationLevel.CONFLICT


@pytest.mark.parametrize("mode", ["polish", "expand", "custom"])
def test_non_concise_modes_require_all_source_facts(mode: str) -> None:
    result = validate_generated_text("10月12日完成1000条样本整理", "完成样本整理", mode)

    assert result.level is ValidationLevel.CONFLICT


def test_multi_record_summary_may_merge_duplicate_facts_and_derive_count() -> None:
    original = "TabPFN相关性0.9\nTabPFN相关性0.9"
    generated = "两项与TabPFN的相关性均为0.9"

    assert validate_generated_text(original, generated, "concise").level is ValidationLevel.PASS
