"""Application service orchestrating parsing, content processing, rendering, and validation."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

from .content_builder import build_content_blocks
from .docx_renderer import render_docx
from .input_parser import parse_input
from .llm import ContentMode, LLMService, load_llm_config, parse_content_mode
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


def process_report(
    prepared: PreparedReport,
    content_mode: str | ContentMode = ContentMode.ORIGINAL,
    *,
    custom_instruction: str | None = None,
    model: str | None = None,
    llm_service: LLMService | None = None,
) -> PreparedReport:
    """Apply a content mode while keeping original mode fully offline."""
    mode = parse_content_mode(content_mode)
    if mode is ContentMode.ORIGINAL:
        blocks = tuple(
            replace(
                block,
                generated_text=None,
                mode=ContentMode.ORIGINAL.value,
                validation_level="pass",
                validation_warnings=(),
            )
            for block in prepared.blocks
        )
        return replace(prepared, blocks=blocks)
    service = llm_service or LLMService(load_llm_config())
    return service.process(
        prepared,
        mode,
        custom_instruction=custom_instruction,
        model=model,
    )


def _legacy_rd3_plan(
    prepared: PreparedReport, report_date: date, section_number: int
) -> ReportPlan:
    """Adapt traceable blocks to the original planner without changing its structure."""
    source = prepared.source
    progress_by_source = {
        source_id: block.effective_text
        for block in prepared.blocks
        for source_id in block.source_ids
        if block.section_id == "weekly_work"
    }
    data = WeeklyData(
        month=source.month,
        week=source.week,
        items=tuple(
            WeeklyItem(
                project=record.fields["project"],
                detail=record.fields.get("detail") or "周度进展",
                status=record.fields.get("status", ""),
                progress=progress_by_source.get(record.source_id, record.fields["progress"]),
            )
            for record in source.sources
        ),
    )
    return build_report_plan(data, report_date, section_number)


def render_prepared_report(
    prepared: PreparedReport,
    preset: RulePreset,
    template_path: Path,
    output_path: Path,
    report_date: date,
    section_number: int | None = None,
) -> ReportPlan:
    """Render and validate an already prepared and optionally processed report."""
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
    content_mode: str | ContentMode = ContentMode.ORIGINAL,
    custom_instruction: str | None = None,
    model: str | None = None,
    llm_service: LLMService | None = None,
) -> ReportPlan:
    """Generate and validate one deterministic or LLM-assisted weekly report."""
    prepared, preset = prepare_report(
        input_path,
        preset_id=preset_id,
        requested_week=requested_week,
        sheet=sheet,
        field_mapping=field_mapping,
        display_filename=display_filename,
        rules_dir=rules_dir,
    )
    prepared = process_report(
        prepared,
        content_mode,
        custom_instruction=custom_instruction,
        model=model,
        llm_service=llm_service,
    )
    return render_prepared_report(
        prepared,
        preset,
        template_path,
        output_path,
        report_date,
        section_number,
    )
