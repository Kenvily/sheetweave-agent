"""Flask web interface for deterministic weekly report generation."""

from __future__ import annotations

import argparse
import io
import os
import tempfile
import zipfile
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

from flask import Flask, render_template, request, send_file
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import RequestEntityTooLarge

from .planner import validate_section_number
from .rules import RulePreset, list_rule_presets, load_rule_preset
from .service import generate_report, prepare_report

DEFAULT_TEMPLATE = Path("data/研发三部周报模板.docx")
DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_UPLOAD_SIZE = 16 * 1024 * 1024
DEFAULT_SECTION_NUMBER = 9
SUPPORTED_INPUT_SUFFIXES = {".csv", ".xlsx"}
FIELD_KEYS = ("project", "detail", "status", "content", "issues", "next_plan")


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("报告日期必须使用 YYYY-MM-DD 格式。") from exc


def _parse_section_number(value: str, preset: RulePreset) -> int:
    if not value.strip():
        return preset.document.heading_start_default
    try:
        section_number = int(value)
    except ValueError as exc:
        raise ValueError("起始章节号必须是整数。") from exc
    return validate_section_number(section_number)


def _require_extension(filename: str, extensions: set[str], label: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in extensions:
        allowed = "或 ".join(f"{item} 文件" for item in sorted(extensions))
        raise ValueError(f"{label}必须是 {allowed}。")
    return suffix


def _data_upload() -> FileStorage | None:
    """Accept the M1 field name while retaining the old Web contract."""
    return request.files.get("data_file") or request.files.get("csv_file")


def _field_mapping() -> dict[str, str]:
    return {
        key: request.form.get(f"field_{key}", "").strip()
        for key in FIELD_KEYS
        if request.form.get(f"field_{key}", "").strip()
    }


def create_app(template_path: Path | None = None) -> Flask:
    """Create and configure the weekly report web application."""
    app = Flask(__name__)
    configured_template = template_path or Path(
        os.environ.get("WEEKLY_REPORT_TEMPLATE", DEFAULT_TEMPLATE)
    )
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_UPLOAD_SIZE,
        REPORT_TEMPLATE_PATH=configured_template.resolve(),
    )

    def show_form(
        *,
        error: str | None = None,
        status: int = 200,
        prepared=None,
        selected_preset: RulePreset | None = None,
    ):
        return (
            render_template(
                "index.html",
                error=error,
                today=date.today().isoformat(),
                form=request.form,
                default_section_number=DEFAULT_SECTION_NUMBER,
                presets=list_rule_presets(),
                prepared=prepared,
                selected_preset=selected_preset,
            ),
            status,
        )

    def save_input(upload: FileStorage, workdir: Path) -> Path:
        if not upload.filename:
            raise ValueError("请选择周报 CSV 或 XLSX 文件。")
        suffix = _require_extension(upload.filename, SUPPORTED_INPUT_SUFFIXES, "周报数据")
        input_path = workdir / f"input{suffix}"
        upload.save(input_path)
        return input_path

    @app.get("/")
    def index():
        return show_form()

    @app.post("/preview")
    def preview():
        upload = _data_upload()
        try:
            if upload is None:
                raise ValueError("请选择周报 CSV 或 XLSX 文件。")
            preset = load_rule_preset(request.form.get("preset", "rd3"))
            with tempfile.TemporaryDirectory(prefix="weekly-report-preview-") as directory:
                input_path = save_input(upload, Path(directory))
                prepared, preset = prepare_report(
                    input_path,
                    preset_id=preset.id,
                    requested_week=request.form.get("week", "").strip() or None,
                    sheet=request.form.get("sheet", "").strip() or None,
                    field_mapping=_field_mapping(),
                    display_filename=upload.filename,
                )
            return show_form(prepared=prepared, selected_preset=preset)
        except (FileNotFoundError, OSError, ValueError, zipfile.BadZipFile, ET.ParseError) as exc:
            return show_form(error=str(exc), status=400)

    @app.post("/generate")
    def generate():
        upload = _data_upload()
        template_upload = request.files.get("template_file")
        try:
            if upload is None:
                raise ValueError("请选择周报 CSV 或 XLSX 文件。")
            preset = load_rule_preset(request.form.get("preset", "rd3"))
            report_date = _parse_date(request.form.get("report_date", ""))
            requested_week = request.form.get("week", "").strip() or None
            section_number = _parse_section_number(request.form.get("section_number", ""), preset)

            with tempfile.TemporaryDirectory(prefix="weekly-report-web-") as directory:
                workdir = Path(directory)
                input_path = save_input(upload, workdir)
                output_path = workdir / "report.docx"

                selected_template = app.config["REPORT_TEMPLATE_PATH"]
                if template_upload is not None and template_upload.filename:
                    _require_extension(template_upload.filename, {".docx"}, "报告模板")
                    selected_template = workdir / "template.docx"
                    template_upload.save(selected_template)

                generate_report(
                    input_path=input_path,
                    template_path=Path(selected_template),
                    output_path=output_path,
                    report_date=report_date,
                    requested_week=requested_week,
                    section_number=section_number,
                    preset_id=preset.id,
                    sheet=request.form.get("sheet", "").strip() or None,
                    field_mapping=_field_mapping(),
                    display_filename=upload.filename,
                )
                report_content = output_path.read_bytes()

            filename = f"{preset.name}-{report_date:%Y%m%d}.docx"
            return send_file(
                io.BytesIO(report_content),
                mimetype=DOCX_MIME_TYPE,
                as_attachment=True,
                download_name=filename,
                max_age=0,
            )
        except (FileNotFoundError, OSError, ValueError, zipfile.BadZipFile, ET.ParseError) as exc:
            return show_form(error=str(exc), status=400)

    @app.errorhandler(RequestEntityTooLarge)
    def upload_too_large(_error: RequestEntityTooLarge):
        limit_mb = MAX_UPLOAD_SIZE // (1024 * 1024)
        return show_form(error=f"上传文件总大小不能超过 {limit_mb} MB。", status=413)

    return app


def build_parser() -> argparse.ArgumentParser:
    """Build the web server command-line parser."""
    parser = argparse.ArgumentParser(description="启动规则化周报生成页面。")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    parser.add_argument("--template", type=Path, help="默认 DOCX 模板路径")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Start the local Flask development server."""
    args = build_parser().parse_args(argv)
    app = create_app(args.template)
    app.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
