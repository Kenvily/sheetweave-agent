from datetime import date

import pytest

from sheetweave_agent.normalizer import normalize_report_text


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


@pytest.mark.parametrize("source", ["kv cache", "KV CACHE", "kv-cache", "kv_cache", "kvcache"])
def test_kv_cache_presentation_aliases(source):
    assert normalize_report_text(f"启用{source}后耗时下降。") == "启用KV Cache后耗时下降。"


@pytest.mark.parametrize(
    "source,expected",
    [
        ("lgbm、xgb、catboost", "LightGBM、XGBoost、CatBoost"),
        ("pytorch、tensorflow、sklearn、numpy", "PyTorch、TensorFlow、scikit-learn、NumPy"),
        ("vllm使用flash attention、cuda和cudnn", "vLLM使用FlashAttention、CUDA和cuDNN"),
        ("qlora、lora、llm、rag、moe", "QLoRA、LoRA、LLM、RAG、MoE"),
        ("gpu、cpu、npu、vram、oom", "GPU、CPU、NPU、VRAM、OOM"),
        ("bf16、fp16、fp32、fp8、int8、int4", "BF16、FP16、FP32、FP8、INT8、INT4"),
        ("api、sdk、json、yaml、csv、xlsx、docx", "API、SDK、JSON、YAML、CSV、XLSX、DOCX"),
        ("mysql、postgresql、redis、docker、k8s", "MySQL、PostgreSQL、Redis、Docker、Kubernetes"),
        ("auc、ks、psi、woe、iv、vif、gini", "AUC、KS、PSI、WOE、IV、VIF、Gini"),
        ("pr_auc、roc auc、rmse、mae", "PR-AUC、ROC-AUC、RMSE、MAE"),
        ("pd、lgd、ead、dpd30、fpd7、mob3", "PD、LGD、EAD、DPD30、FPD7、MOB3"),
        ("apr、irr、npv、ltv、rwa、kyc、aml", "APR、IRR、NPV、LTV、RWA、KYC、AML"),
    ],
)
def test_common_computer_and_risk_terminology(source, expected):
    assert normalize_report_text(source) == expected
    assert normalize_report_text(expected) == expected


def test_versions_numeric_metrics_and_business_definitions_remain_verbatim():
    source = (
        "tabpfn3.5、tabicl_v2、qwen2.5、deepseek-v3；"
        "5000样本，auc 0.81，ks 72.5%，psi 0.08，提升0.3-0.5pp；"
        "dpd30+，mob3，pd 1.2%，lgd 45%，ead 100万元，ltv 60%。"
    )
    expected = (
        "TabPFN3.5、TabICL_v2、Qwen2.5、DeepSeek-v3；"
        "5000样本，AUC 0.81，KS 72.5%，PSI 0.08，提升0.3-0.5pp；"
        "DPD30+，MOB3，PD 1.2%，LGD 45%，EAD 100万元，LTV 60%。"
    )
    assert normalize_report_text(source) == expected
    assert normalize_report_text(expected) == expected


def test_terms_do_not_rewrite_longer_words_or_identifiers():
    source = "rapid risk capital mobius gift numpy_array mytabm tabm_custom qwen2beta abc123"
    assert normalize_report_text(source) == source


def test_term_replacement_preserves_code_urls_and_common_resource_paths():
    source = (
        "`kv_cache = pd.read_csv('data/tabm.csv')`；"
        "```python\nkv_cache = numpy.array([1])\n```；"
        "https://host.test/models/tabm?format=json；"
        "work/tabm/model.pkl；/opt/cuda/；C:\\models\\tabpfn.bin；"
        "pd.read_csv；torch.cuda；在gpu上启用kv cache。"
    )
    assert normalize_report_text(source) == source.replace(
        "在gpu上启用kv cache", "在GPU上启用KV Cache"
    )
