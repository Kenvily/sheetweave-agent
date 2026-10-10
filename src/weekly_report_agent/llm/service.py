"""Content-only LLM processing with stable IDs and strict fact protection."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from enum import Enum

from weekly_report_agent.models import ContentBlock, PreparedReport

from .client import LLMClient, OpenAICompatibleClient
from .config import LLMConfig

NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])\d+(?:\.\d+)*(?:\s?(?:%|pp|万|亿|GB|MB|KB|天|个|次))?",
    re.IGNORECASE,
)
DATE_PATTERN = re.compile(r"(?:\d{4}[年/-]\d{1,2}[月/-]\d{1,2}日?|\d{1,2}月\d{1,2}日)")
MODEL_PATTERN = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_.-]*(?![A-Za-z0-9_])")
KNOWN_MODEL_TERMS = {
    "auc",
    "bf16",
    "lightgbm",
    "oom",
    "oot",
    "psi",
    "tabicl",
    "tabm",
    "tabpfn",
}
STATUS_TERMS = ("未开始", "进行中", "已完成", "完成", "暂停", "延期", "阻塞", "失败", "成功")


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


def _protected_models(text: str) -> Counter[str]:
    tokens = []
    for token in MODEL_PATTERN.findall(text):
        lowered = token.lower()
        if lowered in KNOWN_MODEL_TERMS or any(character.isdigit() for character in token):
            tokens.append(lowered)
    return Counter(tokens)


def _fact_warnings(original: str, generated: str) -> tuple[str, ...]:
    checks = (
        (
            "数字或指标",
            Counter(NUMBER_PATTERN.findall(original)),
            Counter(NUMBER_PATTERN.findall(generated)),
        ),
        ("日期", Counter(DATE_PATTERN.findall(original)), Counter(DATE_PATTERN.findall(generated))),
        ("模型或技术名", _protected_models(original), _protected_models(generated)),
        (
            "任务状态",
            Counter(term for term in STATUS_TERMS if term in original),
            Counter(term for term in STATUS_TERMS if term in generated),
        ),
    )
    return tuple(label for label, before, after in checks if before != after)


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
        }
        for block in blocks
    ]
    system = (
        "你是公司周报文本整理器，只处理语义表达，不生成标题编号或 Word 格式。"
        "不得新增、删除或改动数字、日期、指标、模型版本、状态、项目标识和结论。"
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
            warnings = _fact_warnings(block.original_text, text)
            if warnings:
                labels = "、".join(warnings)
                raise LLMResponseError(
                    f"内容块 {block.block_id} 疑似改变{labels}，结果未采用，原文已保留。"
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
