"""Command-line interface for weekly report generation."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from .llm import ContentMode
from .service import generate_report


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须使用 YYYY-MM-DD 格式。") from exc


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(description="生成研发周报 DOCX。")
    parser.add_argument("--input", type=Path, default=Path("data/周报.csv"), help="输入 CSV/XLSX")
    parser.add_argument(
        "--template",
        type=Path,
        default=Path("data/研发三部周报模板.docx"),
        help="DOCX 模板",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("outputs/研发三部周报.docx"), help="输出 DOCX"
    )
    parser.add_argument("--week", help="周次，例如“第1周”或“2026年9月第1周”")
    parser.add_argument("--preset", choices=("rd3", "general"), default="rd3", help="周报规则预设")
    parser.add_argument("--sheet", help="XLSX 工作表名称，默认使用第一个工作表")
    parser.add_argument("--report-date", type=_iso_date, default=date.today(), help="报告日期")
    parser.add_argument(
        "--section-number",
        type=int,
        default=None,
        help="一级标题起始章节号；留空使用所选预设默认值",
    )
    parser.add_argument(
        "--content-mode",
        choices=tuple(mode.value for mode in ContentMode),
        default=ContentMode.ORIGINAL.value,
        help="内容模式；除 original 外会调用服务端配置的模型",
    )
    parser.add_argument("--model", help="模型名称，必须在服务端允许列表中")
    parser.add_argument("--custom-instruction", help="custom 模式的补充要求")
    parser.add_argument(
        "--allow-external",
        action="store_true",
        help="确认允许将内容块发送到服务端配置的模型；非 original 模式必填",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the report generator CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.content_mode != ContentMode.ORIGINAL.value and not args.allow_external:
            parser.error("非原文模式必须使用 --allow-external 确认允许发送内容块。")
        plan = generate_report(
            input_path=args.input,
            template_path=args.template,
            output_path=args.output,
            report_date=args.report_date,
            requested_week=args.week,
            section_number=args.section_number,
            preset_id=args.preset,
            sheet=args.sheet,
            content_mode=args.content_mode,
            custom_instruction=args.custom_instruction,
            model=args.model,
        )
    except (FileNotFoundError, OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(f"已生成：{args.output.resolve()}")
    print(
        f"数据范围：{plan.source_label}，起始章节：{plan.section_number}，"
        f"有效事项：{plan.item_count}"
    )
    return 0
