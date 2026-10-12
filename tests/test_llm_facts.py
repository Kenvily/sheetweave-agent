from weekly_report_agent.llm.facts import FactKind, extract_facts


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
