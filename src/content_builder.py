"""Build traceable deterministic content blocks from normalized source rows."""

from __future__ import annotations

import hashlib
from collections import OrderedDict

from .models import ContentBlock, ParsedInput
from .rules import RulePreset


def _block_id(section_id: str, source_ids: tuple[str, ...]) -> str:
    identity = f"{section_id}\0" + "\0".join(source_ids)
    digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
    return f"b-{section_id}-{digest}"


def record_heading_path(section, record) -> tuple[str, ...]:
    """Resolve fixed titles and field-backed titles without interpreting arbitrary templates."""
    fields = section.heading_fields or ("",) * len(section.heading_path)
    result = []
    for title, key in zip(section.heading_path, fields, strict=True):
        value = record.fields.get(key, "").strip() if key else title
        if not value:
            raise ValueError(f"来源行 {record.row_number} 的标题字段 {key} 为空，请核对方案。")
        result.append(value)
    return tuple(result)


def block_heading_path(prepared, section, block) -> tuple[str, ...]:
    """Resolve a block's heading from its traceable source records."""
    record = next(row for row in prepared.source.sources if row.source_id == block.source_ids[0])
    return record_heading_path(section, record)


def _matches(section, record) -> bool:
    return all(
        record.fields.get(key, "").strip() in values for key, values in section.match.items()
    )


def _source_text(section, record) -> str:
    fields = section.source_fields or (section.source_field,)
    text = "\n".join(
        record.fields.get(key, "").strip() for key in fields if record.fields.get(key, "").strip()
    )
    return text or ("附图。" if record.images else "")


def unmatched_source_ids(source: ParsedInput, preset: RulePreset) -> set[str]:
    """Identify unassigned rows before expanding the selected destination's scope."""
    return {
        record.source_id
        for record in source.sources
        if not any(
            not section.unmatched_only
            and _matches(section, record)
            and _source_text(section, record)
            for section in preset.sections
        )
    }


def build_content_blocks(source: ParsedInput, preset: RulePreset) -> tuple[ContentBlock, ...]:
    """Apply section/group rules while preserving complete source relationships."""
    blocks: list[ContentBlock] = []
    covered = set()
    unmatched = unmatched_source_ids(source, preset)
    # Ordinary routes always run first, regardless of editor ordering.
    for section in sorted(preset.sections, key=lambda item: item.unmatched_only):
        grouped: OrderedDict[tuple[str, bool, tuple[str, ...]], list[tuple[str, str]]] = (
            OrderedDict()
        )
        for record in source.sources:
            if section.unmatched_only:
                if record.source_id not in unmatched:
                    continue
            elif not _matches(section, record) and not (
                section.accept_unmatched and record.source_id in unmatched
            ):
                continue
            text = _source_text(section, record)
            if not text:
                continue
            covered.add(record.source_id)
            group_key = (
                record.fields.get("project", "未分类").strip() or "未分类"
                if section.group_by == "project"
                else record.source_id
            )
            if section.group_by == "section":
                group_key = section.title
            editable = section.llm_editable and not record.historical_completed
            grouped.setdefault(
                (group_key, editable, record_heading_path(section, record)), []
            ).append((record.source_id, text))
        for (group_key, editable, _path), values in grouped.items():
            source_ids = tuple(source_id for source_id, _text in values)
            original_text = "\n".join(text for _source_id, text in values)
            display_group = group_key
            if section.group_by == "row":
                record = next(item for item in source.sources if item.source_id == group_key)
                display_group = record.fields.get("project", "未分类")
            blocks.append(
                ContentBlock(
                    block_id=_block_id(section.id, source_ids),
                    section_id=section.id,
                    section_title=section.title,
                    group_key=display_group,
                    source_ids=source_ids,
                    original_text=original_text,
                    source_field=section.source_field,
                    source_fields=section.source_fields,
                    llm_editable=editable,
                )
            )
    if any(section.heading_path for section in preset.sections):
        missing = [
            str(record.row_number) for record in source.sources if record.source_id not in covered
        ]
        if missing:
            raise ValueError(
                "报告目录未覆盖来源行：" + "、".join(missing) + "。请补充匹配规则或确认排除策略。"
            )
    return tuple(blocks)
