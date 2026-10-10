"""Flask web interface for weekly report generation."""

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

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import RequestEntityTooLarge

from .input_parser import inspect_input_options
from .llm import (
    MODE_LABELS,
    ContentMode,
    LLMClientError,
    LLMConfigurationError,
    LLMService,
    load_llm_config,
    parse_content_mode,
)
from .llm.config import DEFAULT_MODEL, DEFAULT_MODELS
from .planner import validate_section_number
from .rules import RulePreset, list_rule_presets, load_rule_preset
from .service import generate_report, prepare_report, process_report, render_prepared_report
from .web_sessions import SnapshotStore

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


def _selected_mode() -> ContentMode:
    return parse_content_mode(request.form.get("content_mode", ContentMode.ORIGINAL.value))


def _require_external_consent(mode: ContentMode) -> None:
    if mode is not ContentMode.ORIGINAL and request.form.get("allow_external") != "yes":
        raise ValueError("使用模型处理前，请确认允许将内容块发送到已配置的 DeepSeek 服务。")


def create_app(
    template_path: Path | None = None,
    llm_service: LLMService | None = None,
    snapshot_store: SnapshotStore | None = None,
) -> Flask:
    """Create and configure the weekly report web application."""
    app = Flask(__name__)
    configured_template = template_path or Path(
        os.environ.get("WEEKLY_REPORT_TEMPLATE", DEFAULT_TEMPLATE)
    )
    snapshot_root = Path(
        os.environ.get(
            "WEEKLY_REPORT_SNAPSHOT_DIR",
            str(Path(tempfile.gettempdir()) / "weekly-report-agent-snapshots"),
        )
    )
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_UPLOAD_SIZE,
        REPORT_TEMPLATE_PATH=configured_template.resolve(),
        LLM_SERVICE=llm_service,
        SNAPSHOT_STORE=snapshot_store or SnapshotStore(snapshot_root),
    )

    def llm_page_settings() -> tuple[tuple[str, ...], str, bool]:
        configured_service = app.config.get("LLM_SERVICE")
        if configured_service is not None:
            config = configured_service.config
            return config.allowed_models, config.model, config.available
        try:
            config = load_llm_config()
        except LLMConfigurationError:
            return DEFAULT_MODELS, DEFAULT_MODEL, False
        return config.allowed_models, config.model, config.available

    def show_form(
        *,
        error: str | None = None,
        status: int = 200,
        prepared=None,
        selected_preset: RulePreset | None = None,
        snapshot_token: str | None = None,
    ):
        models, default_model, llm_available = llm_page_settings()
        default_mode = ContentMode.POLISH.value if llm_available else ContentMode.ORIGINAL.value
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
                snapshot_token=snapshot_token,
                content_modes=tuple((mode.value, MODE_LABELS[mode]) for mode in ContentMode),
                mode_labels={mode.value: label for mode, label in MODE_LABELS.items()},
                selected_mode=request.form.get("content_mode", default_mode),
                models=models,
                default_model=default_model,
                llm_available=llm_available,
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

    def selected_template(workdir: Path) -> Path:
        template_upload = request.files.get("template_file")
        if template_upload is None or not template_upload.filename:
            return Path(app.config["REPORT_TEMPLATE_PATH"])
        _require_extension(template_upload.filename, {".docx"}, "报告模板")
        path = workdir / "template.docx"
        template_upload.save(path)
        return path

    def process_options() -> tuple[ContentMode, str | None, str | None]:
        mode = _selected_mode()
        _require_external_consent(mode)
        instruction = request.form.get("custom_instruction", "").strip() or None
        model = request.form.get("model", "").strip() or None
        return mode, instruction, model

    @app.get("/")
    def index():
        return show_form()

    @app.post("/input-options")
    def input_options():
        """Inspect a temporary upload and return selectable Sheet/week metadata."""
        upload = _data_upload()
        try:
            if upload is None:
                raise ValueError("请选择周报 CSV 或 XLSX 文件。")
            preset = load_rule_preset(request.form.get("preset", "rd3"))
            with tempfile.TemporaryDirectory(prefix="weekly-report-options-") as directory:
                input_path = save_input(upload, Path(directory))
                options = inspect_input_options(
                    input_path,
                    preset,
                    sheet=request.form.get("sheet", "").strip() or None,
                )
            return jsonify(
                sheets=options.sheets,
                selected_sheet=options.selected_sheet,
                weeks=options.weeks,
                default_week=options.default_week,
                section_default=preset.document.heading_start_default,
            )
        except (FileNotFoundError, OSError, ValueError, zipfile.BadZipFile) as exc:
            return jsonify(error=str(exc)), 400

    @app.get("/preview")
    def preview_entry():
        """Return stale or directly opened preview URLs to the upload form."""
        return redirect(url_for("index"))

    @app.post("/preview")
    def preview():
        upload = _data_upload()
        try:
            if upload is None:
                raise ValueError("请选择周报 CSV 或 XLSX 文件。")
            preset = load_rule_preset(request.form.get("preset", "rd3"))
            report_date = _parse_date(request.form.get("report_date", ""))
            section_number = _parse_section_number(request.form.get("section_number", ""), preset)
            mode, instruction, model = process_options()
            with tempfile.TemporaryDirectory(prefix="weekly-report-preview-") as directory:
                workdir = Path(directory)
                input_path = save_input(upload, workdir)
                prepared, preset = prepare_report(
                    input_path,
                    preset_id=preset.id,
                    requested_week=request.form.get("week", "").strip() or None,
                    sheet=request.form.get("sheet", "").strip() or None,
                    field_mapping=_field_mapping(),
                    display_filename=upload.filename,
                )
                prepared = process_report(
                    prepared,
                    mode,
                    custom_instruction=instruction,
                    model=model,
                    llm_service=app.config.get("LLM_SERVICE"),
                )
                output_path = workdir / "report.docx"
                render_prepared_report(
                    prepared,
                    preset,
                    selected_template(workdir),
                    output_path,
                    report_date,
                    section_number,
                )
                filename = f"{preset.name}-{report_date:%Y%m%d}.docx"
                snapshot_token = app.config["SNAPSHOT_STORE"].create(
                    output_path.read_bytes(), filename
                )
            return show_form(
                prepared=prepared,
                selected_preset=preset,
                snapshot_token=snapshot_token,
            )
        except (
            FileNotFoundError,
            OSError,
            ValueError,
            LLMClientError,
            zipfile.BadZipFile,
            ET.ParseError,
        ) as exc:
            return show_form(error=str(exc), status=400)

    @app.get("/download/<token>")
    def download_snapshot(token: str):
        try:
            snapshot = app.config["SNAPSHOT_STORE"].get(token)
            return send_file(
                snapshot.path,
                mimetype=DOCX_MIME_TYPE,
                as_attachment=True,
                download_name=snapshot.download_name,
                max_age=0,
            )
        except ValueError as exc:
            return show_form(error=str(exc), status=404)

    @app.get("/generate")
    def generate_entry():
        """Return directly opened generation URLs to the upload form."""
        return redirect(url_for("index"))

    @app.post("/")
    @app.post("/generate")
    def generate():
        upload = _data_upload()
        try:
            if upload is None:
                raise ValueError("请选择周报 CSV 或 XLSX 文件。")
            preset = load_rule_preset(request.form.get("preset", "rd3"))
            report_date = _parse_date(request.form.get("report_date", ""))
            section_number = _parse_section_number(request.form.get("section_number", ""), preset)
            mode, instruction, model = process_options()

            with tempfile.TemporaryDirectory(prefix="weekly-report-web-") as directory:
                workdir = Path(directory)
                input_path = save_input(upload, workdir)
                output_path = workdir / "report.docx"
                generate_report(
                    input_path=input_path,
                    template_path=selected_template(workdir),
                    output_path=output_path,
                    report_date=report_date,
                    requested_week=request.form.get("week", "").strip() or None,
                    section_number=section_number,
                    preset_id=preset.id,
                    sheet=request.form.get("sheet", "").strip() or None,
                    field_mapping=_field_mapping(),
                    display_filename=upload.filename,
                    content_mode=mode,
                    custom_instruction=instruction,
                    model=model,
                    llm_service=app.config.get("LLM_SERVICE"),
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
        except (
            FileNotFoundError,
            OSError,
            ValueError,
            LLMClientError,
            zipfile.BadZipFile,
            ET.ParseError,
        ) as exc:
            return show_form(error=str(exc), status=400)

    @app.errorhandler(RequestEntityTooLarge)
    def upload_too_large(_error: RequestEntityTooLarge):
        limit_mb = MAX_UPLOAD_SIZE // (1024 * 1024)
        if request.path == url_for("input_options"):
            return jsonify(error=f"上传文件总大小不能超过 {limit_mb} MB。"), 413
        return show_form(error=f"上传文件总大小不能超过 {limit_mb} MB。", status=413)

    return app


def build_parser() -> argparse.ArgumentParser:
    """Build the web server command-line parser."""
    parser = argparse.ArgumentParser(description="启动周报生成页面。")
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
