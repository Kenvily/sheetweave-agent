"""Content-only LLM processing with stable IDs and graded fact protection."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from enum import Enum

from weekly_report_agent.models import ContentBlock, PreparedReport

from .client import LLMClient, OpenAICompatibleClient
from .config import LLMConfig
from .facts import protected_facts
from .validation import ValidationLevel, ValidationResult, validate_generated_text


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
    ContentMode.CONCISE: "去重并合并冗余表达，保留关键事实、指标、版本、日期和状态。",
    ContentMode.EXPAND: "仅丰富已有事实的表达和衔接，不补充原文没有的过程或结论。",
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
            if key != block.source_field and source.fields.get(key, "").strip()
        }
        if context:
            contexts.append(context)
    return tuple(contexts)


def _request_block(prepared: PreparedReport, block: ContentBlock) -> dict[str, object]:
    return {
        "block_id": block.block_id,
        "editable_text": block.original_text,
        "immutable_context": _immutable_context(prepared, block),
        "protected_facts": protected_facts(block.original_text),
    }


def _messages(
    prepared: PreparedReport,
    blocks: Sequence[ContentBlock],
    mode: ContentMode,
    custom_instruction: str | None,
) -> tuple[dict[str, str], dict[str, str]]:
    instruction = MODE_INSTRUCTIONS.get(mode, "")
    if mode is ContentMode.CUSTOM:
        instruction = (custom_instruction or "").strip()
        if not instruction:
            raise ValueError("自定义要求模式必须填写处理要求。")
    source = [_request_block(prepared, block) for block in blocks]
    system = (
        "你是公司周报文本整理器，只能改写 editable_text。"
        "immutable_context 中的项目、任务名称、结构状态等字段只用于理解上下文，"
        "不得复制、改写或作为返回字段。不得生成标题编号或 Word 格式。"
        "不得新增、删除、篡改或错误归属 protected_facts 中的指标、数字、日期、"
        "模型版本、技术名和任务状态；允许调整纯列表序号。"
        "必须为每个输入 block_id 返回且仅返回一项，禁止改变 block_id。"
        '只输出 JSON 对象，格式为 {"blocks":[{"block_id":"...","text":"..."}]}。'
    )
    user = json.dumps(
        {"mode": mode.value, "instruction": instruction, "blocks": source},
        ensure_ascii=False,
    )
    return {"role": "system", "content": system}, {"role": "user", "content": user}


def _correction_messages(
    prepared: PreparedReport,
    blocks: Sequence[ContentBlock],
    candidates: dict[str, str],
    results: dict[str, ValidationResult],
) -> tuple[dict[str, str], dict[str, str]]:
    payload = []
    for block in blocks:
        payload.append(
            {
                **_request_block(prepared, block),
                "unsafe_candidate": candidates[block.block_id],
                "conflicts": [issue.message for issue in results[block.block_id].issues],
            }
        )
    system = (
        "你正在修正周报候选文本中的严重事实冲突。只修正 unsafe_candidate，"
        "使其与 editable_text 和 protected_facts 一致；不得修改 immutable_context，"
        "不得添加新事实。每个 block_id 只返回一次，只输出约定的 JSON 对象。"
    )
    user = json.dumps({"blocks": payload}, ensure_ascii=False)
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
        return (*lines, "严重冲突定向修正后仍未通过，最终文档已自动使用原文。")
    if result.level is ValidationLevel.REVIEW:
        return (*lines, "该候选文本待人工确认，最终文档暂时使用原文。")
    return lines


class LLMService:
    """Process editable content through an injectable client and graded validator."""

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
        client = self._client or OpenAICompatibleClient(config)
        candidates: dict[str, str] = {}
        for batch in _batches(editable, config.max_batch_chars):
            response = client.complete(
                _messages(prepared, batch, selected_mode, custom_instruction), config.model
            )
            candidates.update(_decode_response(response, tuple(block.block_id for block in batch)))

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
        if conflicts:
            correction_attempted = {block.block_id for block in conflicts}
            try:
                response = client.complete(
                    _correction_messages(prepared, conflicts, candidates, validations), config.model
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
