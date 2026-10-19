"""Request-independent report generation and snapshot publication."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path

from .llm import ContentMode, LLMService
from .rules import load_rule_preset, preset_payload, validate_rule_preset
from .service import prepare_report, process_report, render_prepared_report
from .web_sessions import SnapshotStore


def generate_snapshot(
    input_path: Path,
    template_path: Path,
    options: dict,
    snapshots: SnapshotStore,
    *,
    llm_service: LLMService | None = None,
    on_delta: Callable[[int, str], None] | None = None,
    progress: Callable[[str], None] = lambda _message: None,
    before_publish: Callable[[], None] = lambda: None,
) -> str:
    """Run the existing deterministic/LLM pipeline without Flask request state."""
    preset = (
        validate_rule_preset(options["preset_payload"])
        if options.get("preset_payload")
        else load_rule_preset(options["preset"])
    )
    report_date = date.fromisoformat(options["report_date"])
    mode = ContentMode(options["content_mode"])
    progress("正在解析表格与规则。")
    prepared, preset = prepare_report(
        input_path,
        preset_id=preset.id,
        preset=preset,
        requested_week=options.get("week"),
        sheet=options.get("sheet"),
        field_mapping=options.get("field_mapping"),
        display_filename=options["filename"],
    )
    progress("正在规范化原文。" if mode is ContentMode.ORIGINAL else "正在生成候选内容，尚未校验。")
    prepared = process_report(
        prepared,
        mode,
        custom_instruction=options.get("custom_instruction"),
        model=options.get("model"),
        llm_service=llm_service,
        on_delta=on_delta,
    )
    progress("候选已完成校验，正在生成并验证 DOCX。")
    with tempfile.TemporaryDirectory(prefix="weekly-report-task-render-") as directory:
        output = Path(directory) / "report.docx"
        render_prepared_report(
            prepared,
            preset,
            template_path,
            output,
            report_date,
            options["section_number"],
        )
        progress("DOCX 验证通过，正在保存审核快照。")
        before_publish()
        return snapshots.create_session(
            prepared,
            preset_id=preset.id,
            report_date=report_date,
            section_number=options["section_number"],
            content_mode=mode.value,
            custom_instruction=options.get("custom_instruction"),
            model=options.get("model"),
            template_content=template_path.read_bytes(),
            report_content=output.read_bytes(),
            download_name=f"{preset.name}-{report_date:%Y%m%d}.docx",
            rule_payload=preset_payload(preset),
        )
