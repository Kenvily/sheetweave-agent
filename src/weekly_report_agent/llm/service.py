"""Content-only LLM processing with stable IDs and graded fact protection."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
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
MODE_OPTION_LABELS: dict[ContentMode, str] = {
    ContentMode.ORIGINAL: "原文保留（不调用模型）",
    ContentMode.POLISH: "规范润色（等长改写）",
    ContentMode.CONCISE: "精简总结（压缩去重）",
    ContentMode.EXPAND: "详细整理（结构扩写）",
    ContentMode.CUSTOM: "自定义要求（按输入处理）",
}


@dataclass(frozen=True, slots=True)
class ModeProfile:
    """An enforceable writing contract for one LLM content mode."""

    objective: str
    length_rule: str
    organization: str
    required_actions: tuple[str, ...]
    forbidden_actions: tuple[str, ...]


MODE_PROFILES: dict[ContentMode, ModeProfile] = {
    ContentMode.POLISH: ModeProfile(
        objective="在不改变信息量和事项粒度的前提下，将原文改成正式、流畅的周报表述。",
        length_rule="输出长度通常控制在原文的 85%～115%；不要主动压缩或扩写。",
        organization="保持原事项顺序和段落数量，仅修正口语、重复连接词、病句和术语写法。",
        required_actions=("使用正式书面语", "补齐必要标点", "保持每项工作的原有粒度"),
        forbidden_actions=("合并本来独立的任务", "增加背景、原因、方法或评价", "写成摘要式短句"),
    ),
    ContentMode.CONCISE: ModeProfile(
        objective="显著压缩重复和过程性表述，形成可快速浏览的成果摘要。",
        length_rule="目标长度为原文的 50%～75%；若受保护事实较多，以完整保留事实为先。",
        organization="合并同类或重复表达，删除套话，每项采用“动作/成果 + 关键事实”的紧凑结构。",
        required_actions=("去除重复主语和同义句", "合并重复事实", "优先呈现结果、指标和状态"),
        forbidden_actions=(
            "删除 protected_facts",
            "把不同任务错误合并",
            "使用空泛总结替代具体事实",
        ),
    ),
    ContentMode.EXPAND: ModeProfile(
        objective="把简略记录整理成层次清楚、语义完整的周报叙述，但不增加源文没有的事实。",
        length_rule="目标长度为原文的 120%～160%；原文事实不足时宁可少扩写，也不得编造。",
        organization="按“工作对象 + 已有动作 + 已有结果/状态”重组句子，补充指代和逻辑衔接。",
        required_actions=("补全省略的句子成分", "明确已有事实之间的衔接", "提升可读性和完整性"),
        forbidden_actions=("推测目的、原因或影响", "虚构执行过程和结论", "新增完成状态或量化成果"),
    ),
    ContentMode.CUSTOM: ModeProfile(
        objective="严格按用户要求调整表达形式；用户要求只控制文风和组织，不得越过事实边界。",
        length_rule="长度服从用户要求；用户未指定时保持与原文接近。",
        organization="优先执行用户给出的结构和表达要求，同时保持每项事实的正确归属。",
        required_actions=("完整执行 custom_instruction 中可安全执行的表达要求",),
        forbidden_actions=("执行任何新增、删除、篡改或错误归属事实的要求",),
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
            if key != block.source_field and source.fields.get(key, "").strip()
        }
        if context:
            contexts.append(context)
    return tuple(contexts)


def _length_target(text: str, mode: ContentMode) -> dict[str, int] | None:
    """Return an explicit per-block character target for preset writing modes."""
    ratios = {
        ContentMode.POLISH: (0.85, 1.15),
        ContentMode.CONCISE: (0.50, 0.75),
        ContentMode.EXPAND: (1.20, 1.60),
    }
    if mode not in ratios:
        return None
    minimum_ratio, maximum_ratio = ratios[mode]
    length = len(text.strip())
    return {
        "source_chars": length,
        "minimum_chars": max(1, round(length * minimum_ratio)),
        "maximum_chars": max(1, round(length * maximum_ratio)),
    }


def _request_block(
    prepared: PreparedReport, block: ContentBlock, mode: ContentMode
) -> dict[str, object]:
    payload: dict[str, object] = {
        "block_id": block.block_id,
        "editable_text": block.original_text,
        "immutable_context": _immutable_context(prepared, block),
        "protected_facts": protected_facts(block.original_text),
    }
    length_target = _length_target(block.original_text, mode)
    if length_target is not None:
        payload["length_target"] = length_target
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
    }
    if mode is ContentMode.CUSTOM:
        instruction = (custom_instruction or "").strip()
        if not instruction:
            raise ValueError("自定义要求模式必须填写处理要求。")
        contract["custom_instruction"] = instruction
    return contract


def _messages(
    prepared: PreparedReport,
    blocks: Sequence[ContentBlock],
    mode: ContentMode,
    custom_instruction: str | None,
) -> tuple[dict[str, str], dict[str, str]]:
    contract = _mode_contract(mode, custom_instruction)
    source = [_request_block(prepared, block, mode) for block in blocks]
    system = (
        "你是公司周报文本整理器，只能改写 editable_text。"
        "mode_contract 是本次模式必须执行的写作契约，不是参考建议；"
        "必须按其中的 objective、length_rule、organization、required_actions 和 "
        "forbidden_actions 生成明显符合所选模式的结果。每个块的 length_target 是具体字数目标；"
        "若与事实完整性冲突，优先完整保留 protected_facts。"
        "immutable_context 中的项目、任务名称、结构状态等字段只用于理解上下文，"
        "不得复制、改写或作为返回字段。不得生成标题编号或 Word 格式。"
        "不得新增、删除、篡改或错误归属 protected_facts 中的指标、数字、日期、"
        "模型版本、技术名和任务状态；允许调整纯列表序号。"
        "必须为每个输入 block_id 返回且仅返回一项，禁止改变 block_id。"
        '只输出 JSON 对象，格式为 {"blocks":[{"block_id":"...","text":"..."}]}。'
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
                **_request_block(prepared, block, mode),
                "unsafe_candidate": candidates[block.block_id],
                "conflicts": [issue.message for issue in results[block.block_id].issues],
            }
        )
    system = (
        "你正在修正周报候选文本中的严重事实冲突。只修正 unsafe_candidate，"
        "使其与 editable_text 和 protected_facts 一致；不得修改 immutable_context，"
        "不得添加新事实。每个 block_id 只返回一次，只输出约定的 JSON 对象。"
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
