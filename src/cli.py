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
    parser = argparse.ArgumentParser(description="使用已保存方案生成文档 DOCX。")
    parser.add_argument(
        "--input",
        type=Path,
        default=Path(__file__).with_name("examples") / "general-weekly.csv",
        help="输入 CSV/XLSX",
    )
    parser.add_argument("--output", type=Path, default=Path("outputs/文档.docx"), help="输出 DOCX")
    parser.add_argument("--week", help="周次，例如“第1周”或“2026年9月第1周”")
    parser.add_argument("--profile", required=True, help="已保存方案 ID；先在方案适配中创建并保存")
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
    parser.add_argument("--custom-instruction", help="规范润色的额外要求（可选，原文模式不使用）")
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
        from .profiles import ProfileStore
        from .rules import preset_payload, validate_rule_preset

        store = ProfileStore.from_environment()
        # Freeze locally while holding the same lock as administrator deletion.
        import shutil
        import tempfile

        with tempfile.TemporaryDirectory(prefix="weekly-profile-cli-") as temp:
            with store.locked():
                preset, saved_template, _ = store.load(args.profile)
                template = Path(temp) / "template.docx"
                shutil.copyfile(saved_template, template)
                frozen = validate_rule_preset(preset_payload(preset))
                store.used(args.profile)
            plan = generate_report(
                args.input,
                template,
                args.output,
                args.report_date,
                args.week,
                args.section_number,
                preset=frozen,
                sheet=args.sheet,
                content_mode=args.content_mode,
                custom_instruction=args.custom_instruction,
                model=args.model,
            )
            store.used(args.profile, successful=True)
        print(f"已生成：{args.output.resolve()}，有效事项：{plan.item_count}")
        return 0
    except (FileNotFoundError, OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    return 0
