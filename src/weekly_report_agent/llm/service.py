"""Content-only LLM processing with stable IDs and strict fact protection."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from enum import Enum

from weekly_report_agent.models import ContentBlock, PreparedReport

from .client import LLMClient, OpenAICompatibleClient
from .config import LLMConfig

MODEL_PATTERN = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_.-]*(?![A-Za-z0-9_])")
DATE_PATTERN = re.compile(r"(?:\d{4}[年/-]\d{1,2}[月/-]\d{1,2}日?|\d{1,2}月\d{1,2}日)")
LIST_MARKER_PATTERN = re.compile(
    r"(^|[\n\r，。；;：:])([ \t]*)"
    r"(?:[（(][1-9]\d?[）)]|[1-9]\d?[、）)]|[1-9]\d?[.．](?!\d))[ \t]*",
    re.MULTILINE,
)
NUMBER_TEXT = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
CHINESE_NUMBER_TEXT = r"[零〇一二两三四五六七八九十百千万亿]+"
UNIT_TEXT = (
    r"个百分点|个工作日|个月|%|％|pp|GB|MB|KB|万|千|亿|年|月|周|天|个|次|折|倍|点|条|项|轮|组|家|人"
)
NUMBER_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_.])(?P<comparison><=|>=|[<>≤≥])?[ \t]*"
    rf"(?P<number>{NUMBER_TEXT})[ \t]*(?P<unit>{UNIT_TEXT})?"
    r"(?![A-Za-z_.])",
    re.IGNORECASE,
)
RANGE_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_.])(?P<first>{NUMBER_TEXT})[ \t]*(?P<first_unit>{UNIT_TEXT})?"
    rf"[ \t]*(?:-|—|–|~|～|至|到)[ \t]*(?P<second>{NUMBER_TEXT})"
    rf"[ \t]*(?P<second_unit>{UNIT_TEXT})?(?![A-Za-z0-9_.])",
    re.IGNORECASE,
)
METRIC_NUMBER_PATTERN = re.compile(
    rf"(?P<metric>AUC|KS|PSI)[ \t]*(?P<comparison><=|>=|[<>≤≥])?[ \t]*"
    rf"(?P<number>{NUMBER_TEXT})[ \t]*(?P<unit>{UNIT_TEXT})?",
    re.IGNORECASE,
)
CHINESE_NUMBER_PATTERN = re.compile(
    rf"(?P<number>{CHINESE_NUMBER_TEXT})[ \t]*(?P<unit>{UNIT_TEXT})"
)
CHINESE_PERCENT_PATTERN = re.compile(rf"百分之(?P<number>{CHINESE_NUMBER_TEXT})")

KNOWN_MODEL_TERMS = {
    "auc",
    "bf16",
    "lgb",
    "lightgbm",
    "oom",
    "oot",
    "psi",
    "tabicl",
    "tabm",
    "tabpfn",
}
VERSIONED_MODEL_TERMS = ("lightgbm", "tabpfn", "tabicl", "tabm", "lgb")
MODEL_VERSION_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_])(?P<model>{'|'.join(VERSIONED_MODEL_TERMS)})"
    r"[ \t]*(?:v(?:ersion)?[ \t]*)?(?P<version>\d+(?:\.\d+)*)"
    r"(?![A-Za-z0-9_.])",
    re.IGNORECASE,
)
STATUS_PATTERNS = {
    "未开始": re.compile(r"未开始"),
    "进行中": re.compile(r"进行中"),
    "已完成": re.compile(r"(?:已经|现已|已)完成"),
    "暂停": re.compile(r"暂停"),
    "延期": re.compile(r"延期"),
    "阻塞": re.compile(r"阻塞"),
    "失败": re.compile(r"失败"),
}
AGGREGATION_UNITS = ("项", "条", "组")


class ContentMode(str, Enum):
    """Supported deterministic and LLM-backed content modes."""

    ORIGINAL = "original"
    POLISH = "polish"
    CONCISE = "concise"
    EXPAND = "expand"
    CUSTOM = "custom"


MODE_LABELS: dict[ContentMode, str] = {
    ContentMode.ORIGINAL: "原文保留",
    ContentMode.POLISH: "规范润色",
    ContentMode.CONCISE: "精简总结",
    ContentMode.EXPAND: "详细整理",
    ContentMode.CUSTOM: "自定义要求",
}
MODE_INSTRUCTIONS: dict[ContentMode, str] = {
    ContentMode.POLISH: "统一语气、术语、逻辑和语法，保持原有信息密度。",
    ContentMode.CONCISE: "去重并合并冗余表达，保留全部事实、指标和关键成果。",
    ContentMode.EXPAND: "仅丰富已有事实的表达和衔接，不补充原文没有的过程或结论。",
}


class LLMResponseError(ValueError):
    """Raised when structured output is incomplete, malformed, or factually unsafe."""


def parse_content_mode(value: str | ContentMode) -> ContentMode:
    """Parse a public mode value into a supported enum."""
    try:
        return value if isinstance(value, ContentMode) else ContentMode(value)
    except ValueError as exc:
        raise ValueError(f"不支持的内容处理模式：{value}。") from exc


def _canonical_technical_term(token: str) -> str:
    lowered = token.lower()
    aliases = {"lgb": "lightgbm"}
    return aliases.get(lowered, lowered)


def _model_versions(text: str) -> tuple[tuple[tuple[int, int], str], ...]:
    """Extract model/version facts independent of spacing and letter case."""
    return tuple(
        (
            match.span(),
            f"{_canonical_technical_term(match.group('model'))} {match.group('version')}",
        )
        for match in MODEL_VERSION_PATTERN.finditer(text)
    )


def _protected_models(text: str) -> Counter[str]:
    """Extract technical terms and preserve model versions as atomic facts."""
    versions = _model_versions(text)
    occupied = [span for span, _fact in versions]
    tokens = [fact for _span, fact in versions]
    metric_names = {"auc", "ks", "psi"}
    for match in MODEL_PATTERN.finditer(text):
        if _overlaps(match.span(), occupied):
            continue
        token = match.group()
        lowered = token.lower()
        metric_match = re.fullmatch(r"(auc|ks|psi)\d+(?:\.\d+)?", lowered)
        if metric_match:
            tokens.append(metric_match.group(1))
        elif lowered in KNOWN_MODEL_TERMS or any(character.isdigit() for character in token):
            tokens.append(_canonical_technical_term(lowered))
    # ``KS 2`` is tokenized as ``KS`` while ``KS2`` is normalized above.
    for metric in metric_names:
        if (
            re.search(rf"(?<![A-Za-z0-9_]){metric}(?![A-Za-z_])", text, re.IGNORECASE)
            and metric not in tokens
        ):
            tokens.append(metric)
    return Counter(set(tokens))


def _without_model_versions(text: str) -> str:
    """Blank model versions so they are not also treated as business metrics."""
    return MODEL_VERSION_PATTERN.sub(
        lambda match: match.group("model") + " " * (len(match.group()) - len(match.group("model"))),
        text,
    )


def _protected_statuses(text: str) -> Counter[str]:
    """Normalize explicit status phrases without treating ordinary verbs as statuses."""
    return Counter({status for status, pattern in STATUS_PATTERNS.items() if pattern.search(text)})


def _without_list_markers(text: str) -> str:
    """Remove presentation-only numeric list markers before extracting facts."""
    return LIST_MARKER_PATTERN.sub(lambda match: match.group(1), text)


def _canonical_dates(text: str) -> Counter[str]:
    values: list[str] = []
    for raw in DATE_PATTERN.findall(text):
        digits = re.findall(r"\d+", raw)
        if len(digits) == 3:
            year, month, day = (int(item) for item in digits)
            values.append(f"{year:04d}-{month:02d}-{day:02d}")
        elif len(digits) == 2:
            month, day = (int(item) for item in digits)
            values.append(f"--{month:02d}-{day:02d}")
    return Counter(values)


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


def _canonical_number(raw_number: str, unit: str = "", comparison: str = "") -> str:
    comparison_aliases = {"<=": "≤", ">=": "≥"}
    multipliers = {"千": Decimal("1000"), "万": Decimal("10000"), "亿": Decimal("100000000")}
    unit_aliases = {"％": "%", "百分点": "pp"}
    normalized_unit = unit_aliases.get(unit, unit)
    if unit == "个百分点":
        normalized_unit = "pp"
    try:
        number = (
            _chinese_integer(raw_number)
            if re.fullmatch(CHINESE_NUMBER_TEXT, raw_number)
            else Decimal(raw_number.replace(",", ""))
        )
    except InvalidOperation:  # pragma: no cover - regex only permits supported numbers
        return ""
    if normalized_unit in multipliers:
        number *= multipliers[normalized_unit]
        normalized_unit = ""
    normalized = format(number.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    normalized_comparison = comparison_aliases.get(comparison, comparison)
    return f"{normalized_comparison}{normalized}{normalized_unit.lower()}"


def _overlaps(span: tuple[int, int], occupied: list[tuple[int, int]]) -> bool:
    return any(span[0] < existing[1] and existing[0] < span[1] for existing in occupied)


def _canonical_numbers(text: str) -> Counter[str]:
    """Extract business numbers while accepting equivalent visual formatting."""
    fact_text = DATE_PATTERN.sub(" ", _without_list_markers(_without_model_versions(text)))
    comparison_words = (
        (r"不超过|小于等于|至多", "≤"),
        (r"不低于|不少于|大于等于|至少", "≥"),
        (r"小于|低于", "<"),
        (r"大于|高于", ">"),
    )
    for pattern, symbol in comparison_words:
        fact_text = re.sub(pattern + r"[ \t]*", symbol, fact_text)
    suffix_comparisons = ((r"以上|及以上", "≥"), (r"以下|及以下", "≤"))
    for suffix, symbol in suffix_comparisons:
        fact_text = re.sub(
            rf"(?P<value>{NUMBER_TEXT})[ \t]*(?P<unit>{UNIT_TEXT})?[ \t]*(?:{suffix})",
            lambda match, prefix=symbol: (
                f"{prefix}{match.group('value')}{match.group('unit') or ''}"
            ),
            fact_text,
            flags=re.IGNORECASE,
        )
    values: list[str] = []
    occupied: list[tuple[int, int]] = []

    for match in CHINESE_PERCENT_PATTERN.finditer(fact_text):
        values.append(_canonical_number(match.group("number"), "%"))
        occupied.append(match.span())

    for match in CHINESE_NUMBER_PATTERN.finditer(fact_text):
        if _overlaps(match.span(), occupied):
            continue
        values.append(_canonical_number(match.group("number"), match.group("unit")))
        occupied.append(match.span())

    # A range may write its unit only once: ``0.3-0.5pp`` equals ``0.3pp 至 0.5pp``.
    for match in RANGE_PATTERN.finditer(fact_text):
        first_unit = match.group("first_unit") or match.group("second_unit") or ""
        second_unit = match.group("second_unit") or match.group("first_unit") or ""
        values.extend(
            (
                _canonical_number(match.group("first"), first_unit),
                _canonical_number(match.group("second"), second_unit),
            )
        )
        occupied.append(match.span())

    # Metrics are often written without a space, for example ``KS2个点``.
    for match in METRIC_NUMBER_PATTERN.finditer(fact_text):
        if _overlaps(match.span(), occupied):
            continue
        values.append(
            _canonical_number(
                match.group("number"),
                match.group("unit") or "",
                match.group("comparison") or "",
            )
        )
        occupied.append(match.span())

    for match in NUMBER_PATTERN.finditer(fact_text):
        if _overlaps(match.span(), occupied):
            continue
        values.append(
            _canonical_number(
                match.group("number"),
                match.group("unit") or "",
                match.group("comparison") or "",
            )
        )
    return Counter(value for value in values if value)


def _unique_facts(values: Counter[str]) -> Counter[str]:
    """Compare fact presence while allowing concise text to merge duplicate mentions."""
    return Counter(set(values))


def _without_derived_aggregation_counts(
    original: Counter[str], generated: Counter[str]
) -> Counter[str]:
    """Allow counts such as “两项” only when they summarize duplicate facts."""
    duplicate_counts = {
        count for fact, count in original.items() if count > 1 and generated[fact] < count
    }
    if not duplicate_counts:
        return generated
    adjusted = generated.copy()
    unit_pattern = "|".join(AGGREGATION_UNITS)
    for fact in tuple(adjusted):
        match = re.fullmatch(rf"(?P<count>\d+)(?:{unit_pattern})", fact)
        if match and int(match.group("count")) in duplicate_counts and fact not in original:
            del adjusted[fact]
    return adjusted


def _protected_facts(text: str) -> dict[str, list[str]]:
    """Return normalized facts supplied to the model as explicit constraints."""
    return {
        "numbers": sorted(_canonical_numbers(text)),
        "dates": sorted(_canonical_dates(text)),
        "models_or_technical_terms": sorted(_protected_models(text)),
        "statuses": sorted(_protected_statuses(text)),
    }


def _fact_checks(
    original: str, generated: str
) -> tuple[tuple[str, Counter[str], Counter[str]], ...]:
    original_numbers = _canonical_numbers(original)
    generated_numbers = _without_derived_aggregation_counts(
        original_numbers, _canonical_numbers(generated)
    )
    return (
        (
            "数字或指标",
            _unique_facts(original_numbers),
            _unique_facts(generated_numbers),
        ),
        (
            "日期",
            _unique_facts(_canonical_dates(original)),
            _unique_facts(_canonical_dates(generated)),
        ),
        (
            "模型或技术名",
            _unique_facts(_protected_models(original)),
            _unique_facts(_protected_models(generated)),
        ),
        (
            "任务状态",
            _protected_statuses(original),
            _protected_statuses(generated),
        ),
    )


def _fact_warning_details(original: str, generated: str) -> tuple[str, ...]:
    details: list[str] = []
    for label, before, after in _fact_checks(original, generated):
        if before == after:
            continue
        missing = list((before - after).elements())
        added = list((after - before).elements())
        parts = []
        if missing:
            parts.append(f"缺少：{'、'.join(missing)}")
        if added:
            parts.append(f"新增：{'、'.join(added)}")
        details.append(f"{label}（{'；'.join(parts)}）")
    return tuple(details)


def _messages(
    blocks: Sequence[ContentBlock], mode: ContentMode, custom_instruction: str | None
) -> tuple[dict[str, str], dict[str, str]]:
    instruction = MODE_INSTRUCTIONS.get(mode, "")
    if mode is ContentMode.CUSTOM:
        instruction = (custom_instruction or "").strip()
        if not instruction:
            raise ValueError("自定义要求模式必须填写处理要求。")
    source = [
        {
            "block_id": block.block_id,
            "section": block.section_title,
            "group": block.group_key,
            "text": block.original_text,
            "protected_facts": _protected_facts(block.original_text),
        }
        for block in blocks
    ]
    system = (
        "你是公司周报文本整理器，只处理语义表达，不生成标题编号或 Word 格式。"
        "不得新增、删除或改动 protected_facts 中的数字、日期、指标、模型版本、状态和结论；"
        "可以调整纯列表序号，但不得把列表序号写入业务结论。"
        "必须为每个输入 block_id 返回且仅返回一项，禁止改变 block_id。"
        '只输出 JSON 对象，格式为 {"blocks":[{"block_id":"...","text":"..."}]}。'
    )
    user = json.dumps(
        {"mode": mode.value, "instruction": instruction, "blocks": source},
        ensure_ascii=False,
    )
    return {"role": "system", "content": system}, {"role": "user", "content": user}


def _decode_response(content: str, expected_ids: tuple[str, ...]) -> dict[str, str]:
    value = content.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            value = "\n".join(lines[1:-1])
            if value.lstrip().startswith("json"):
                value = value.lstrip()[4:].lstrip()
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise LLMResponseError("LLM 未返回有效 JSON，原文已保留。") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("blocks"), list):
        raise LLMResponseError("LLM 返回结构缺少 blocks 数组，原文已保留。")
    results: dict[str, str] = {}
    for item in payload["blocks"]:
        if not isinstance(item, dict):
            raise LLMResponseError("LLM 内容块格式无效，原文已保留。")
        block_id = item.get("block_id")
        text = item.get("text")
        if not isinstance(block_id, str) or not isinstance(text, str) or not text.strip():
            raise LLMResponseError("LLM 内容块 ID 或文本无效，原文已保留。")
        if block_id in results:
            raise LLMResponseError(f"LLM 重复返回内容块 {block_id}，原文已保留。")
        results[block_id] = text.strip()
    expected = set(expected_ids)
    actual = set(results)
    if expected != actual:
        missing = expected - actual
        unknown = actual - expected
        detail = []
        if missing:
            detail.append(f"遗漏 {len(missing)} 个块")
        if unknown:
            detail.append(f"包含 {len(unknown)} 个未知块")
        raise LLMResponseError(f"LLM 返回块不完整（{'，'.join(detail)}），原文已保留。")
    return results


def _batches(
    blocks: Sequence[ContentBlock], max_chars: int
) -> tuple[tuple[ContentBlock, ...], ...]:
    batches: list[list[ContentBlock]] = []
    current: list[ContentBlock] = []
    current_size = 0
    for block in blocks:
        size = len(block.original_text) + len(block.group_key) + len(block.section_title) + 100
        if current and current_size + size > max_chars:
            batches.append(current)
            current = []
            current_size = 0
        current.append(block)
        current_size += size
    if current:
        batches.append(current)
    return tuple(tuple(batch) for batch in batches)


class LLMService:
    """Process content blocks through an injectable client and validated configuration."""

    def __init__(self, config: LLMConfig, client: LLMClient | None = None) -> None:
        self.config = config
        self._client = client

    def process(
        self,
        prepared: PreparedReport,
        mode: str | ContentMode,
        *,
        custom_instruction: str | None = None,
        model: str | None = None,
    ) -> PreparedReport:
        """Return a new report with generated text, preserving all source mappings."""
        selected_mode = parse_content_mode(mode)
        if selected_mode is ContentMode.ORIGINAL:
            return replace(
                prepared,
                blocks=tuple(
                    replace(
                        block,
                        generated_text=None,
                        mode=ContentMode.ORIGINAL.value,
                        validation_warnings=(),
                    )
                    for block in prepared.blocks
                ),
            )
        config = self.config.select_model(model)
        config.require_available()
        client = self._client or OpenAICompatibleClient(config)
        generated: dict[str, str] = {}
        for batch in _batches(prepared.blocks, config.max_batch_chars):
            messages = _messages(batch, selected_mode, custom_instruction)
            response = client.complete(messages, config.model)
            generated.update(_decode_response(response, tuple(block.block_id for block in batch)))
        updated = []
        for block in prepared.blocks:
            text = generated[block.block_id]
            warnings = _fact_warning_details(block.original_text, text)
            if warnings:
                details = "；".join(warnings)
                raise LLMResponseError(
                    f"内容块 {block.block_id} 疑似改变事实：{details}。"
                    "模型结果未采用，请核对原文或调整生成要求。"
                )
            updated.append(
                replace(
                    block,
                    generated_text=text,
                    mode=selected_mode.value,
                    validation_warnings=(),
                )
            )
        return replace(prepared, blocks=tuple(updated))

    def regenerate_block(
        self,
        prepared: PreparedReport,
        block_id: str,
        mode: str | ContentMode,
        *,
        custom_instruction: str | None = None,
        model: str | None = None,
        overwrite_edited: bool = False,
    ) -> PreparedReport:
        """Regenerate exactly one block without changing any other block or mapping."""
        target = prepared.block_by_id(block_id)
        if target.edited_text is not None and not overwrite_edited:
            raise ValueError("该内容块包含人工修改，重新生成前必须确认覆盖。")
        target = replace(target, edited_text=None) if overwrite_edited else target
        partial = replace(prepared, blocks=(target,))
        processed = self.process(
            partial,
            mode,
            custom_instruction=custom_instruction,
            model=model,
        )
        replacement = processed.blocks[0]
        return replace(
            prepared,
            blocks=tuple(
                replacement if block.block_id == block_id else block for block in prepared.blocks
            ),
        )
