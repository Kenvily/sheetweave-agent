"""Content-only LLM processing with stable IDs and graded fact protection."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from enum import Enum

from sheetweave_agent.models import ContentBlock, PreparedReport
from sheetweave_agent.timing import TimingRecorder, measure

from .client import LLMClient, OpenAICompatibleClient
from .config import LLMConfig
from .facts import protected_facts
from .validation import ADVISORY_KINDS, ValidationLevel, ValidationResult, validate_generated_text


class ContentMode(str, Enum):
    """Supported deterministic and LLM-backed content modes."""

    ORIGINAL = "original"
    POLISH = "polish"


MODE_LABELS: dict[ContentMode, str] = {
    ContentMode.ORIGINAL: "原文保留",
    ContentMode.POLISH: "规范润色",
}
MODE_OPTION_LABELS: dict[ContentMode, str] = {
    ContentMode.ORIGINAL: "原文保留（不调用模型）",
    ContentMode.POLISH: "规范润色（语义分条与表达整理）",
}


@dataclass(frozen=True, slots=True)
class ModeProfile:
    """Writing instructions for an LLM content mode."""

    objective: str
    length_rule: str
    organization: str
    required_actions: tuple[str, ...]
    forbidden_actions: tuple[str, ...]


MODE_PROFILES: dict[ContentMode, ModeProfile] = {
    ContentMode.POLISH: ModeProfile(
        objective="在保留来源事实与正确归属的前提下，纠正分段和层级，将原文整理成正式、清晰的周报表述。",
        length_rule="不设置长度比例；允许压缩重复、补全衔接和重新组织，以信息准确、清晰完整为准。",
        organization="依据事项语义重新分条、合并断行并纠正父子层级，原始换行、序号和标点不可靠，不要求保持段落数量。",
        required_actions=(
            "使用正式书面语",
            "区分独立任务与说明子项",
            "纠正错误分段与父子层级",
            "普通句末用句号，真实父项引出子项用冒号",
        ),
        forbidden_actions=("编造背景、原因、过程或结论", "篡改关键事实或错误归属不同任务"),
    ),
}
READ_ONLY_CONTEXT_FIELDS = ("project", "detail", "status", "date", "report_date")


class LLMResponseError(ValueError):
    """Raised when structured output is incomplete or malformed."""


def parse_content_mode(value: str | ContentMode) -> ContentMode:
    """Parse a public mode value into a supported enum."""
    try:
        return value if isinstance(value, ContentMode) else ContentMode(value)
    except ValueError as exc:
        raise ValueError(f"不支持的内容处理模式：{value}。") from exc


def _immutable_context(prepared: PreparedReport, block: ContentBlock) -> tuple[dict[str, str], ...]:
    """Return structural source fields that the model may read but never rewrite."""
    contexts: list[dict[str, str]] = []
    for source in prepared.sources_for_block(block.block_id):
        context = {
            key: source.fields[key]
            for key in READ_ONLY_CONTEXT_FIELDS
            if key not in (block.source_fields or (block.source_field,))
            and source.fields.get(key, "").strip()
        }
        if context:
            contexts.append(context)
    return tuple(contexts)


def _request_block(
    prepared: PreparedReport,
    block: ContentBlock,
    mode: ContentMode,
    custom_instruction: str | None = None,
) -> dict[str, object]:
    facts = protected_facts(block.original_text)
    advisory = {kind.value for kind in ADVISORY_KINDS}
    payload: dict[str, object] = {
        "block_id": block.block_id,
        "editable_text": block.original_text,
        "immutable_context": _immutable_context(prepared, block),
        "protected_facts": {key: values for key, values in facts.items() if key not in advisory},
        "reference_facts": {key: values for key, values in facts.items() if key in advisory},
    }
    fields = block.source_fields or (block.source_field,)
    payload["primary_field"] = "content" if "content" in fields else fields[0]
    payload["source_units"] = [
        {
            "source_id": row.source_id,
            "fields": {key: row.fields[key] for key in fields if row.fields.get(key, "").strip()},
            "context": {
                key: row.fields[key]
                for key in READ_ONLY_CONTEXT_FIELDS
                if key not in fields and row.fields.get(key, "").strip()
            },
        }
        for row in prepared.sources_for_block(block.block_id)
    ]
    return payload


def _mode_contract(mode: ContentMode, custom_instruction: str | None) -> dict[str, object]:
    """Build a machine-readable mode contract instead of relying on a short style hint."""
    profile = MODE_PROFILES[mode]
    contract: dict[str, object] = {
        "objective": profile.objective,
        "length_rule": profile.length_rule,
        "organization": profile.organization,
        "required_actions": profile.required_actions,
        "forbidden_actions": profile.forbidden_actions,
        "validation_policy": {
            "structure": "允许重分段、调整顺序、合并同类表达，不按原文段落数量或长短验收",
            "critical": "指标、百分比、模型版本、日期、明确数量与任务状态须准确保留",
            "advisory": "术语提及和无单位数字差异提示人工核对，不因此单独回退",
        },
    }
    instruction = (custom_instruction or "").strip()
    if instruction:
        contract["custom_instruction"] = instruction
        contract["organization"] = (
            "优先按用户补充要求组织表达，同时纠正断行和任务归属；不得新增、删除、篡改或错误归属来源事实。"
        )
        contract["length_rule"] = (
            "用户明确提出篇幅要求时按要求处理，否则不限制长度比例；关键事实准确性优先。"
        )
    return contract


def _messages(
    prepared: PreparedReport,
    blocks: Sequence[ContentBlock],
    mode: ContentMode,
    custom_instruction: str | None,
) -> tuple[dict[str, str], dict[str, str]]:
    contract = _mode_contract(mode, custom_instruction)
    source = [_request_block(prepared, block, mode, custom_instruction) for block in blocks]
    system = (
        "你是公司周报文本整理器，只能改写 editable_text。"
        "primary_field表示主要正文字段，source_units保留原始行边界及事项上下文。"
        "围绕主要正文整合补充字段，不遗漏事实，不因合并多行就将不同事项变成上下级。"
        "mode_contract 是本次模式必须执行的写作契约，不是参考建议；"
        "必须按其中的 objective、length_rule、organization、required_actions 和 "
        "forbidden_actions 生成符合所选模式的结果，不限制段落数量、顺序或长度比例。"
        "validation_policy区分关键事实与提示性差异，表达和结构可以充分重组。"
        "immutable_context 中的项目、任务名称、结构状态等字段只用于理解上下文，"
        "不得复制、改写或作为返回字段。不得改变报告标题或生成 Word 格式。"
        "必须重新判断正文结构：换行不等于新事项，冒号不等于后续所有行都属于子项。"
        "说明、结论、修正点应跟随其所属工作；培训材料等独立事项不得挂到方法验证下。"
        "不得添加空泛的进展内容/具体事项父项；不得输出重复项目符号。"
        "包括/如下之后若列举多个路径、对象或子工作，应由父项引出level=2/3的清单，"
        "不得只去掉项目符号后将整个清单用分号挤回同一段。"
        "独立完整工作句末用句号；冒号只引出真实子项，分号用于段内并列分句。"
        "protected_facts中的指标、百分比、日期、明确数量、模型版本与状态须准确保留，"
        "不得编造、篡改或错误归属；reference_facts为术语与无单位数字提示，允许语义简化，"
        "不要求逐字保留，不能凭空替换模型或猜测数字；允许调整列表序号。"
        "必须为每个输入 block_id 返回且仅返回一项，禁止改变 block_id。"
        '只输出 JSON 对象，格式为 {"blocks":[{"block_id":"...",'
        '"paragraphs":[{"level":1,"text":"..."}]}]}。'
        "level仅为正文层级1/2/3，第一项必须是1，不能跳级。每项text是一段完整文字，不带序号或项目符号。"
        "独立事项用level=1，真实明细用level=2/3；同一任务的说明合并到所属段落，编号由本地规则生成。"
    )
    user = json.dumps(
        {"mode": mode.value, "mode_contract": contract, "blocks": source},
        ensure_ascii=False,
    )
    return {"role": "system", "content": system}, {"role": "user", "content": user}


def _correction_messages(
    prepared: PreparedReport,
    blocks: Sequence[ContentBlock],
    candidates: dict[str, str],
    results: dict[str, ValidationResult],
    mode: ContentMode,
    custom_instruction: str | None,
) -> tuple[dict[str, str], dict[str, str]]:
    payload = []
    for block in blocks:
        payload.append(
            {
                **_request_block(prepared, block, mode, custom_instruction),
                "unsafe_candidate": candidates[block.block_id],
                "conflicts": [issue.message for issue in results[block.block_id].issues],
            }
        )
    system = (
        "你正在修正周报候选文本中的严重事实冲突。只修正 unsafe_candidate，"
        "使其与 editable_text 和 protected_facts 一致；不得修改 immutable_context，"
        "不得添加新事实。每个 block_id 只返回一次，保留正确的语义分条及父子归属。"
        '只输出JSON {"blocks":[{"block_id":"...","paragraphs":[{"level":1,"text":"..."}]}]}。'
        "level为正文层级1/2/3，不跳级，text不带编号。"
    )
    user = json.dumps(
        {
            "mode": mode.value,
            "mode_contract": _mode_contract(mode, custom_instruction),
            "blocks": payload,
        },
        ensure_ascii=False,
    )
    return {"role": "system", "content": system}, {"role": "user", "content": user}


def _structured_body(value: object) -> str:
    """Serialize model-selected semantic levels into deterministic body markers."""
    from ..planner import CIRCLED_NUMBERS, _strip_bullet

    if not isinstance(value, list) or not value or len(value) > 500:
        raise LLMResponseError("LLM 正文段落数组无效。")
    counters, previous, lines = [0, 0, 0], 0, []
    for paragraph in value:
        if not isinstance(paragraph, dict):
            raise LLMResponseError("LLM 正文段落格式无效。")
        level, text = paragraph.get("level"), paragraph.get("text")
        if type(level) is not int or not 1 <= level <= 3 or level > previous + 1:
            raise LLMResponseError("LLM 正文层级无效或跳级。")
        if not isinstance(text, str) or not text.strip() or "\n" in text or "\r" in text:
            raise LLMResponseError("LLM 正文须为非空单段文字。")
        counters[level - 1] += 1
        counters[level:] = [0] * (3 - level)
        if level == 3 and counters[2] > len(CIRCLED_NUMBERS):
            raise LLMResponseError("正文三级子项超过支持数量。")
        prefix = (
            f"{counters[0]}、"
            if level == 1
            else f"（{counters[1]}）"
            if level == 2
            else CIRCLED_NUMBERS[counters[2] - 1]
        )
        text = _strip_bullet(text)
        if not text:
            raise LLMResponseError("LLM 正文不能仅包含项目符号。")
        lines.append(prefix + text)
        previous = level
    return "\n".join(lines)


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
        text = _structured_body(item["paragraphs"]) if "paragraphs" in item else item.get("text")
        if not isinstance(block_id, str) or not isinstance(text, str) or not text.strip():
            raise LLMResponseError("LLM 内容块 ID 或文本无效，原文已保留。")
        if block_id in results:
            raise LLMResponseError(f"LLM 重复返回内容块 {block_id}，原文已保留。")
        results[block_id] = text.strip()
    expected = set(expected_ids)
    actual = set(results)
    if expected != actual:
        detail = []
        if expected - actual:
            detail.append(f"遗漏 {len(expected - actual)} 个块")
        if actual - expected:
            detail.append(f"包含 {len(actual - expected)} 个未知块")
        raise LLMResponseError(f"LLM 返回块不完整（{'，'.join(detail)}），原文已保留。")
    return results


def _batches(
    blocks: Sequence[ContentBlock], max_chars: int
) -> tuple[tuple[ContentBlock, ...], ...]:
    batches: list[list[ContentBlock]] = []
    current: list[ContentBlock] = []
    current_size = 0
    for block in blocks:
        size = len(block.original_text) + len(block.group_key) + len(block.section_title) + 300
        if current and current_size + size > max_chars:
            batches.append(current)
            current = []
            current_size = 0
        current.append(block)
        current_size += size
    if current:
        batches.append(current)
    return tuple(tuple(batch) for batch in batches)


def _warning_lines(result: ValidationResult, *, correction_failed: bool = False) -> tuple[str, ...]:
    lines = tuple(
        f"{issue.level.label}：{issue.message}；原文：{issue.original_excerpt}；"
        f"生成：{issue.generated_excerpt}"
        for issue in result.issues
    )
    if correction_failed:
        return (*lines, "定向修正后仍有事实告警，请核对后谨慎采用。")
    if result.level is ValidationLevel.CONFLICT:
        return (*lines, "候选需谨慎使用，可编辑修正或确认告警后采用。")
    if result.level is ValidationLevel.REVIEW:
        return (*lines, "提示性差异不阻止采用，建议核对。")
    return lines


class LLMService:
    """Process editable content through an injectable client and graded validator."""

    def __init__(
        self,
        config: LLMConfig,
        client: LLMClient | None = None,
        *,
        timings: TimingRecorder | None = None,
    ) -> None:
        self.config = config
        self._client = client
        self._timings = timings

    def process(
        self,
        prepared: PreparedReport,
        mode: str | ContentMode,
        *,
        custom_instruction: str | None = None,
        model: str | None = None,
        on_delta: Callable[[int, str], None] | None = None,
    ) -> PreparedReport:
        """Return generated candidates while unsafe blocks automatically use source text."""
        selected_mode = parse_content_mode(mode)
        if selected_mode is ContentMode.ORIGINAL:
            return replace(
                prepared,
                blocks=tuple(
                    replace(
                        block,
                        generated_text=None,
                        mode=ContentMode.ORIGINAL.value,
                        validation_level="pass",
                        validation_warnings=(),
                        selected_version="auto",
                    )
                    for block in prepared.blocks
                ),
            )
        editable = tuple(block for block in prepared.blocks if block.llm_editable)
        if not editable:
            return replace(
                prepared,
                blocks=tuple(replace(block, mode=selected_mode.value) for block in prepared.blocks),
            )
        config = self.config.select_model(model)
        config.require_available()
        client = self._client or OpenAICompatibleClient(config, timings=self._timings)
        candidates: dict[str, str] = {}

        def generate_batch(item: tuple[int, tuple[ContentBlock, ...]]) -> dict[str, str]:
            index, batch = item
            with measure(self._timings, "llm_batch", blocks=len(batch)):
                messages = _messages(prepared, batch, selected_mode, custom_instruction)
                stream = getattr(client, "stream", None)
                if on_delta is not None and callable(stream):
                    chunks = []
                    for chunk in stream(messages, config.model):
                        on_delta(index, chunk)
                        chunks.append(chunk)
                    response = "".join(chunks)
                else:
                    response = client.complete(messages, config.model)
                    if on_delta is not None:
                        on_delta(index, response)
                return _decode_response(response, tuple(block.block_id for block in batch))

        batches = _batches(editable, config.max_batch_chars)
        if len(batches) > 1 and config.max_concurrency > 1:
            with ThreadPoolExecutor(max_workers=config.max_concurrency) as executor:
                for generated in executor.map(generate_batch, enumerate(batches, start=1)):
                    candidates.update(generated)
        else:
            for item in enumerate(batches, start=1):
                candidates.update(generate_batch(item))

        with measure(self._timings, "fact_validation", blocks=len(editable)):
            validations = {
                block.block_id: validate_generated_text(
                    block.original_text, candidates[block.block_id], selected_mode.value
                )
                for block in editable
            }
        conflicts = tuple(
            block
            for block in editable
            if validations[block.block_id].level is ValidationLevel.CONFLICT
        )
        correction_attempted: set[str] = set()
        if conflicts and config.auto_correct:
            correction_attempted = {block.block_id for block in conflicts}
            try:
                with measure(self._timings, "llm_correction", blocks=len(conflicts)):
                    response = client.complete(
                        _correction_messages(
                            prepared,
                            conflicts,
                            candidates,
                            validations,
                            selected_mode,
                            custom_instruction,
                        ),
                        config.model,
                    )
                corrected = _decode_response(response, tuple(block.block_id for block in conflicts))
            except (LLMResponseError, RuntimeError):
                corrected = {}
            for block in conflicts:
                text = corrected.get(block.block_id)
                if text is None:
                    continue
                corrected_result = validate_generated_text(
                    block.original_text, text, selected_mode.value
                )
                if corrected_result.accepted:
                    candidates[block.block_id] = text
                    validations[block.block_id] = corrected_result

        updated = []
        for block in prepared.blocks:
            if not block.llm_editable:
                updated.append(
                    replace(
                        block,
                        generated_text=None,
                        mode=selected_mode.value,
                        validation_level="pass",
                        validation_warnings=(),
                        selected_version="auto",
                    )
                )
                continue
            result = validations[block.block_id]
            correction_failed = block.block_id in correction_attempted and not result.accepted
            updated.append(
                replace(
                    block,
                    generated_text=candidates[block.block_id],
                    mode=selected_mode.value,
                    validation_level={
                        ValidationLevel.PASS: "pass",
                        ValidationLevel.REVIEW: "review",
                        ValidationLevel.CONFLICT: "conflict",
                    }[result.level],
                    validation_warnings=_warning_lines(result, correction_failed=correction_failed),
                    selected_version="generated" if result.accepted else "auto",
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
        """Regenerate exactly one editable block without changing other blocks."""
        target = prepared.block_by_id(block_id)
        if not target.llm_editable:
            raise ValueError("该内容块来自只读结构字段，不能使用 LLM 改写。")
        if target.edited_text is not None and not overwrite_edited:
            raise ValueError("该内容块包含人工修改，重新生成前必须确认覆盖。")
        target = (
            replace(
                target,
                edited_text=None,
                edited_validation_level=None,
                edited_validation_warnings=(),
                selected_version="auto",
            )
            if overwrite_edited
            else target
        )
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
