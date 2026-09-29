"""Command-line interface for weekly report generation."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from .service import generate_report


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须使用 YYYY-MM-DD 格式。") from exc


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(description="从多行表头 CSV 生成研发三部周报 DOCX。")
    parser.add_argument("--input", type=Path, default=Path("data/周报.csv"), help="输入 CSV")
    parser.add_argument(
        "--template",
        type=Path,
        default=Path("data/研发三部周报模板.docx"),
        help="DOCX 模板",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("output/研发三部周报.docx"), help="输出 DOCX"
    )
    parser.add_argument("--week", help="周次，例如“第1周”或“2026年9月第1周”")
    parser.add_argument("--report-date", type=_iso_date, default=date.today(), help="报告日期")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the report generator CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        plan = generate_report(
            input_path=args.input,
            template_path=args.template,
            output_path=args.output,
            report_date=args.report_date,
            requested_week=args.week,
        )
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    print(f"已生成：{args.output.resolve()}")
    print(f"数据范围：{plan.source_label}，有效事项：{plan.item_count}")
    return 0
