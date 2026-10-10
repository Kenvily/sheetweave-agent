"""Application service orchestrating parsing, planning, rendering, and validation."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from .content_builder import build_content_blocks
from .docx_renderer import render_docx
from .input_parser import parse_input
from .models import PreparedReport, ReportPlan, WeeklyData, WeeklyItem
from .planner import build_prepared_report_plan, build_report_plan
from .rules import RulePreset, load_rule_preset
from .validator import validate_report


def prepare_report(
    input_path: Path,
    *,
    preset_id: str = "rd3",
    requested_week: str | None = None,
    sheet: str | None = None,
    field_mapping: dict[str, str] | None = None,
    display_filename: str | None = None,
    rules_dir: Path | None = None,
) -> tuple[PreparedReport, RulePreset]:
    """Parse an input and create traceable original-mode content blocks."""
    preset = load_rule_preset(preset_id, rules_dir)
    source = parse_input(
        input_path,
        preset,
        requested_week=requested_week,
        sheet=sheet,
        field_mapping=field_mapping,
        display_filename=display_filename,
    )
    blocks = build_content_blocks(source, preset)
    if not blocks:
        raise ValueError("当前规则没有匹配到可用内容。")
    return PreparedReport(preset.id, source, blocks), preset


def _legacy_rd3_plan(
    prepared: PreparedReport, report_date: date, section_number: int
) -> ReportPlan:
    """Adapt source records to the original planner without changing legacy output."""
    source = prepared.source
    data = WeeklyData(
        month=source.month,
        week=source.week,
        items=tuple(
            WeeklyItem(
                project=record.fields["project"],
                detail=record.fields.get("detail") or "周度进展",
                status=record.fields.get("status", ""),
                progress=record.fields["progress"],
            )
            for record in source.sources
        ),
    )
    return build_report_plan(data, report_date, section_number)


def generate_report(
    input_path: Path,
    template_path: Path,
    output_path: Path,
    report_date: date,
    requested_week: str | None = None,
    section_number: int | None = None,
    *,
    preset_id: str = "rd3",
    sheet: str | None = None,
    field_mapping: dict[str, str] | None = None,
    display_filename: str | None = None,
    rules_dir: Path | None = None,
) -> ReportPlan:
    """Generate and validate one deterministic weekly report."""
    prepared, preset = prepare_report(
        input_path,
        preset_id=preset_id,
        requested_week=requested_week,
        sheet=sheet,
        field_mapping=field_mapping,
        display_filename=display_filename,
        rules_dir=rules_dir,
    )
    effective_section = (
        preset.document.heading_start_default if section_number is None else section_number
    )
    if preset.id == "rd3":
        plan = _legacy_rd3_plan(prepared, report_date, effective_section)
    else:
        plan = build_prepared_report_plan(prepared, preset, report_date, effective_section)
    render_docx(plan, template_path, output_path)
    expected_headings = tuple(
        section.title
        for section in preset.sections
        if any(block.section_id == section.id for block in prepared.blocks)
    )
    validate_report(
        output_path,
        effective_section,
        report_title=preset.document.report_title,
        heading_titles=expected_headings,
    )
    return plan
