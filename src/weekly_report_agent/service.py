"""Application service orchestrating parsing, planning, rendering, and validation."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from .csv_parser import load_weekly_data
from .docx_renderer import render_docx
from .models import ReportPlan
from .planner import build_report_plan
from .validator import validate_report


def generate_report(
    input_path: Path,
    template_path: Path,
    output_path: Path,
    report_date: date,
    requested_week: str | None = None,
    section_number: int = 9,
) -> ReportPlan:
    """Generate and validate one deterministic weekly report."""
    data = load_weekly_data(input_path, requested_week)
    plan = build_report_plan(data, report_date, section_number)
    render_docx(plan, template_path, output_path)
    validate_report(output_path, section_number)
    return plan
