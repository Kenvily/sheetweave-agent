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
