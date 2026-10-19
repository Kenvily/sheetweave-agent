"""Deterministic report text normalization without factual rewriting."""

from __future__ import annotations

import re
from datetime import date, timedelta


def _term(
    pattern: str, replacement: str, *, number_suffix: bool = False
) -> tuple[re.Pattern[str], str]:
    """Match prose terms next to Chinese text, without rewriting longer identifiers."""
    boundary = r"(?![A-Za-z0-9_])"
    if number_suffix:
        # Keep attached model versions (TabPFN2.5, TabICL_v2) and risk horizons (DPD30).
        boundary = (
            r"(?:(?![A-Za-z0-9_])|"
            r"(?=(?:_?v)?[0-9]+(?:\.[0-9]+)*(?![A-Za-z0-9_])))"
        )
    return re.compile(
        r"(?<![A-Za-z0-9_])(?:" + pattern + ")" + boundary, re.IGNORECASE
    ), replacement


# Presentation aliases only: never translate metrics, change units or infer business meanings.
TERM_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Models and tabular-learning products; attached versions remain verbatim.
    _term(r"tabpfn", "TabPFN", number_suffix=True),
    _term(r"tabicl", "TabICL", number_suffix=True),
    _term(r"tabm", "TabM", number_suffix=True),
    _term(r"kumo", "Kumo", number_suffix=True),
    _term(r"galaxy", "Galaxy"),
    _term(r"lightgbm|lgbm|lgb", "LightGBM", number_suffix=True),
    _term(r"xgboost|xgb", "XGBoost", number_suffix=True),
    _term(r"catboost", "CatBoost", number_suffix=True),
    _term(r"qwen", "Qwen", number_suffix=True),
    _term(r"deepseek", "DeepSeek", number_suffix=True),
    _term(r"bert", "BERT", number_suffix=True),
    _term(r"glm", "GLM", number_suffix=True),
    # Frameworks, inference optimizations and model architectures.
    _term(r"kv[ \t_-]*cache", "KV Cache"),
    _term(r"flash[ \t-]*attention", "FlashAttention", number_suffix=True),
    _term(r"pytorch", "PyTorch"),
    _term(r"tensorflow", "TensorFlow"),
    _term(r"scikit[ \t-]*learn|sklearn", "scikit-learn"),
    _term(r"numpy", "NumPy"),
    _term(r"pandas", "pandas"),
    _term(r"cuda", "CUDA"),
    _term(r"cudnn", "cuDNN"),
    _term(r"tensorrt", "TensorRT"),
    _term(r"onnx", "ONNX"),
    _term(r"vllm", "vLLM"),
    _term(r"deepspeed", "DeepSpeed"),
    _term(r"qlora", "QLoRA"),
    _term(r"lora", "LoRA"),
    _term(r"llm", "LLM"),
    _term(r"rag", "RAG"),
    _term(r"moe", "MoE"),
    _term(r"mlp", "MLP"),
    _term(r"cnn", "CNN"),
    _term(r"rnn", "RNN"),
    _term(r"lstm", "LSTM"),
    _term(r"gnn", "GNN"),
    _term(r"nlp", "NLP"),
    # Hardware and precision: do not convert memory units or change precision values.
    _term(r"gpu", "GPU"),
    _term(r"cpu", "CPU"),
    _term(r"npu", "NPU"),
    _term(r"tpu", "TPU"),
    _term(r"vram", "VRAM"),
    _term(r"ram", "RAM"),
    _term(r"oom", "OOM"),
    _term(r"bf16", "BF16"),
    _term(r"fp32", "FP32"),
    _term(r"fp16", "FP16"),
    _term(r"fp8", "FP8"),
    _term(r"int8", "INT8"),
    _term(r"int4", "INT4"),
    # Engineering interfaces, data formats and infrastructure.
    _term(r"api", "API"),
    _term(r"sdk", "SDK"),
    _term(r"https", "HTTPS"),
    _term(r"http", "HTTP"),
    _term(r"json", "JSON"),
    _term(r"yaml", "YAML"),
    _term(r"csv", "CSV"),
    _term(r"xlsx", "XLSX"),
    _term(r"docx", "DOCX"),
    _term(r"mysql", "MySQL"),
    _term(r"postgresql", "PostgreSQL"),
    _term(r"sql", "SQL"),
    _term(r"redis", "Redis"),
    _term(r"docker", "Docker"),
    _term(r"kubernetes|k8s", "Kubernetes"),
    # Evaluation and scorecard terminology: leave all numeric values untouched.
    _term(r"pr[ \t_-]*auc", "PR-AUC"),
    _term(r"roc[ \t_-]*auc", "ROC-AUC"),
    _term(r"auc", "AUC"),
    _term(r"roc", "ROC"),
    _term(r"ks", "KS"),
    _term(r"gini", "Gini"),
    _term(r"psi", "PSI"),
    _term(r"csi", "CSI"),
    _term(r"woe", "WOE"),
    _term(r"iv", "IV"),
    _term(r"vif", "VIF"),
    _term(r"oot", "OOT"),
    _term(r"oos", "OOS"),
    _term(r"rmse", "RMSE"),
    _term(r"mae", "MAE"),
    _term(r"mse", "MSE"),
    _term(r"mape", "MAPE"),
    # Financial/risk abbreviations: no expansion of ambiguous business definitions.
    _term(r"pd", "PD"),
    _term(r"lgd", "LGD"),
    _term(r"ead", "EAD"),
    _term(r"dpd", "DPD", number_suffix=True),
    _term(r"fpd", "FPD", number_suffix=True),
    _term(r"mob", "MOB", number_suffix=True),
    _term(r"apr", "APR"),
    _term(r"irr", "IRR"),
    _term(r"npv", "NPV"),
    _term(r"ltv", "LTV"),
    _term(r"rwa", "RWA"),
    _term(r"kyc", "KYC"),
    _term(r"aml", "AML"),
    _term(r"fico", "FICO", number_suffix=True),
)

# Skip terminology replacement inside explicit code, URLs, attribute access and common paths.
# These spans may contain case-sensitive object names or resource identifiers.
_TERM_LITERAL_PATTERN = re.compile(
    r"```[\s\S]*?```|`[^`\n]*`"
    r"|https?://[^\s<>\"'，。；！？]+"
    r"|(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/]|\.\.?/|~/|/)[^\s，。；！？<>\"']+"
    r"|(?<![A-Za-z0-9_])(?:work|data|models|src|tests|var|outputs)/[^\s，。；！？<>\"']+"
    r"|(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+",
    re.IGNORECASE,
)


def _normalize_terms(text: str) -> str:
    def replace_terms(value: str) -> str:
        for pattern, replacement in TERM_REPLACEMENTS:
            value = pattern.sub(replacement, value)
        return value

    pieces = []
    start = 0
    for match in _TERM_LITERAL_PATTERN.finditer(text):
        pieces.extend((replace_terms(text[start : match.start()]), match.group()))
        start = match.end()
    pieces.append(replace_terms(text[start:]))
    return "".join(pieces)


WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
RELATIVE_WEEKDAY_PATTERN = re.compile(r"(本周|下周|周)([一二三四五六日天])")
WEEKDAY_LIST_PATTERN = re.compile(r"(本周|下周|周)([一二三四五六日天](?:[/、][一二三四五六日天])+)")
ACTION_NAME_PATTERN = re.compile(
    r"(同步|联系|咨询|请教|告知|反馈给|发送给|交付给)([A-Za-z]{1,20}|[\u4e00-\u9fff]{1,3})[哥姐]"
)
PARTICLE_NAME_PATTERN = re.compile(
    r"(?<=[给向找与和由请让同跟])(?:[A-Za-z]{1,20}|[\u4e00-\u9fff]{1,3})[哥姐]"
)
STANDALONE_NAME_PATTERN = re.compile(
    r"(?<![A-Za-z\u4e00-\u9fff])(?:[A-Za-z]{1,20}|[\u4e00-\u9fff]{1,3})[哥姐]"
)


def _weekday_date(qualifier: str, weekday: str, report_date: date) -> str:
    monday = report_date - timedelta(days=report_date.weekday())
    week_offset = 7 if qualifier == "下周" else 0
    target = monday + timedelta(days=WEEKDAY_INDEX[weekday] + week_offset)
    return f"{target.year}年{target.month}月{target.day}日"


def _concrete_weekday(match: re.Match[str], report_date: date) -> str:
    return _weekday_date(match.group(1), match.group(2), report_date)


def _concrete_weekday_list(match: re.Match[str], report_date: date) -> str:
    qualifier, weekdays = match.groups()
    return "、".join(
        _weekday_date(qualifier, weekday, report_date) for weekday in re.split(r"[/、]", weekdays)
    )


def normalize_report_text(text: str, report_date: date | None = None) -> str:
    """Normalize terminology and wording while preserving values and conclusions."""
    value = text.replace("\r\n", "\n").replace("\r", "\n")
    value = _normalize_terms(value)

    value = ACTION_NAME_PATTERN.sub(lambda match: f"{match.group(1)}相关负责人", value)
    value = PARTICLE_NAME_PATTERN.sub("相关负责人", value)
    value = STANDALONE_NAME_PATTERN.sub("相关负责人", value)
    if report_date is not None:
        value = WEEKDAY_LIST_PATTERN.sub(
            lambda match: _concrete_weekday_list(match, report_date), value
        )
        value = RELATIVE_WEEKDAY_PATTERN.sub(
            lambda match: _concrete_weekday(match, report_date), value
        )

    value = value.replace("~", "～")
    value = re.sub(r"[ \t]+([，。；：！？])", r"\1", value)
    value = re.sub(r"(?m)^[ \t]+", "", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()
