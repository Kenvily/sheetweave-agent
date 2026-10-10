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


def build_content_blocks(source: ParsedInput, preset: RulePreset) -> tuple[ContentBlock, ...]:
    """Apply section/group rules while preserving complete source relationships."""
    blocks: list[ContentBlock] = []
    for section in preset.sections:
        grouped: OrderedDict[str, list[tuple[str, str]]] = OrderedDict()
        for record in source.sources:
            text = record.fields.get(section.source_field, "").strip()
            if not text:
                continue
            group_key = (
                record.fields.get("project", "未分类").strip() or "未分类"
                if section.group_by == "project"
                else record.source_id
            )
            grouped.setdefault(group_key, []).append((record.source_id, text))
        for group_key, values in grouped.items():
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
                )
            )
    return tuple(blocks)
