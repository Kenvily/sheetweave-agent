from datetime import date

from weekly_report_agent.normalizer import normalize_report_text


def test_normalizes_terms_names_and_weekdays_without_changing_metrics() -> None:
    source = (
        "tabpfn2.5 相对lgb的AUC有0.3-0.5pp优势，已同步强哥；本周一复核，tabicl发生oom后改为bf16。"
    )

    result = normalize_report_text(source, date(2026, 9, 24))

    assert "TabPFN2.5" in result
    assert "LightGBM" in result
    assert "0.3-0.5pp" in result
    assert "同步相关负责人" in result
    assert "强哥" not in result
    assert "2026年9月21日" in result
    assert "TabICL" in result
    assert "OOM" in result
    assert "BF16" in result
