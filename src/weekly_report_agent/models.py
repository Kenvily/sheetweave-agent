"""Domain models shared by the report generation pipeline."""

from dataclasses import dataclass
from datetime import date
from enum import Enum


@dataclass(frozen=True, slots=True)
class WeekColumn:
    """A weekly progress column discovered in the CSV headers."""

    index: int
    month: str
    week: str

    @property
    def label(self) -> str:
        """Return the fully qualified month and week label."""
        return f"{self.month}{self.week}"


@dataclass(frozen=True, slots=True)
class WeeklyItem:
    """One project progress item for a selected week."""

    project: str
    detail: str
    status: str
    progress: str


@dataclass(frozen=True, slots=True)
class WeeklyData:
    """Selected weekly data and its source period."""

    month: str
    week: str
    items: tuple[WeeklyItem, ...]

    @property
    def label(self) -> str:
        """Return the selected period label."""
        return f"{self.month}{self.week}"


class ParagraphKind(str, Enum):
    """Supported report paragraph styles."""

    TITLE = "title"
    DATE = "date"
    HEADING_1 = "heading_1"
    HEADING_2 = "heading_2"
    HEADING_3 = "heading_3"
    BODY_1 = "body_1"
    BODY_2 = "body_2"
    BODY_3 = "body_3"


@dataclass(frozen=True, slots=True)
class ReportParagraph:
    """A paragraph and the semantic style to apply to it."""

    text: str
    kind: ParagraphKind


@dataclass(frozen=True, slots=True)
class ReportPlan:
    """A deterministic, renderer-independent report plan."""

    report_date: date
    source_label: str
    section_number: int
    item_count: int
    paragraphs: tuple[ReportParagraph, ...]


@dataclass(frozen=True, slots=True)
class SourceFile:
    """Metadata for one parsed source file and optional worksheet."""

    file_id: str
    filename: str
    file_type: str
    sheet: str | None = None


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """One source row with a stable identity and physical location."""

    source_id: str
    file_id: str
    sheet: str | None
    row_number: int
    fields: dict[str, str]
    original_text: str
    raw_values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ParsedInput:
    """Normalized source records plus mapping and validation metadata."""

    source_files: tuple[SourceFile, ...]
    sources: tuple[SourceRecord, ...]
    field_mapping: dict[str, str]
    headers: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    available_sheets: tuple[str, ...] = ()
    month: str = ""
    week: str = ""

    @property
    def source_label(self) -> str:
        """Return the selected weekly label when one exists."""
        return f"{self.month}{self.week}"


@dataclass(frozen=True, slots=True)
class ContentBlock:
    """A traceable content block generated from one or more source rows."""

    block_id: str
    section_id: str
    section_title: str
    group_key: str
    source_ids: tuple[str, ...]
    original_text: str
    generated_text: str | None = None
    edited_text: str | None = None
    mode: str = "original"
    validation_warnings: tuple[str, ...] = ()

    @property
    def effective_text(self) -> str:
        """Return edited, generated, or original text in precedence order."""
        if self.edited_text is not None:
            return self.edited_text
        if self.generated_text is not None:
            return self.generated_text
        return self.original_text


@dataclass(frozen=True, slots=True)
class PreparedReport:
    """Current deterministic report state used by previews and export."""

    preset_id: str
    source: ParsedInput
    blocks: tuple[ContentBlock, ...]

    def source_by_id(self, source_id: str) -> SourceRecord:
        """Return a source record by stable ID."""
        for source in self.source.sources:
            if source.source_id == source_id:
                return source
        raise KeyError(f"未知来源 ID：{source_id}")

    def block_by_id(self, block_id: str) -> ContentBlock:
        """Return a content block by stable ID."""
        for block in self.blocks:
            if block.block_id == block_id:
                return block
        raise KeyError(f"未知内容块 ID：{block_id}")

    def blocks_for_source(self, source_id: str) -> tuple[ContentBlock, ...]:
        """Return every content block linked to a source record."""
        self.source_by_id(source_id)
        return tuple(block for block in self.blocks if source_id in block.source_ids)

    def sources_for_block(self, block_id: str) -> tuple[SourceRecord, ...]:
        """Return every source record linked to a content block."""
        block = self.block_by_id(block_id)
        return tuple(self.source_by_id(source_id) for source_id in block.source_ids)
