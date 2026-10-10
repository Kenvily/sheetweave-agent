"""Flask web interface for weekly report generation."""

from __future__ import annotations

import argparse
import difflib
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
    MODE_OPTION_LABELS,
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
from .service import (
    edit_report_block,
    generate_report,
    prepare_report,
    process_report,
    render_prepared_report,
    select_block_version,
)
from .web_sessions import ReportSession, SessionConflictError, SnapshotStore

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


def _parse_revision(value: str) -> int:
    try:
        revision = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("报告版本号无效，请刷新页面后重试。") from exc
    if revision < 1:
        raise ValueError("报告版本号无效，请刷新页面后重试。")
    return revision


def _diff_segments(before: str, after: str) -> tuple[tuple[str, str], ...]:
    """Return an escaped-by-Jinja character diff without changing either text."""
    segments: list[tuple[str, str]] = []
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    for operation, left_start, left_end, right_start, right_end in matcher.get_opcodes():
        if operation == "equal":
            segments.append(("equal", before[left_start:left_end]))
        elif operation == "delete":
            segments.append(("delete", before[left_start:left_end]))
        elif operation == "insert":
            segments.append(("insert", after[right_start:right_end]))
        else:
            segments.append(("delete", before[left_start:left_end]))
            segments.append(("insert", after[right_start:right_end]))
    return tuple(segment for segment in segments if segment[1])


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
                content_modes=tuple((mode.value, MODE_OPTION_LABELS[mode]) for mode in ContentMode),
                mode_labels={mode.value: label for mode, label in MODE_LABELS.items()},
                selected_mode=request.form.get("content_mode", default_mode),
                models=models,
                default_model=default_model,
                llm_available=llm_available,
            ),
            status,
        )

    def show_workspace(
        session: ReportSession,
        *,
        error: str | None = None,
        notice: str | None = None,
        status: int = 200,
    ):
        models, default_model, llm_available = llm_page_settings()
        preset = load_rule_preset(session.preset_id)
        notices = {
            "edited": "人工编辑已保存，文档预览与下载文件已同步更新。",
            "selected": "有效内容版本已更新，文档预览与下载文件已同步更新。",
            "regenerated": "所选内容块已重新生成，其他内容未改变。",
        }
        return (
            render_template(
                "workspace.html",
                session=session,
                prepared=session.prepared,
                preset=preset,
                error=error,
                notice=notices.get(notice, notice),
                diff_segments=_diff_segments,
                content_modes=tuple(
                    (mode.value, MODE_OPTION_LABELS[mode])
                    for mode in ContentMode
                    if mode is not ContentMode.ORIGINAL
                ),
                models=models,
                default_model=default_model,
                llm_available=llm_available,
            ),
            status,
        )

    def render_session_revision(
        session: ReportSession,
        prepared,
        *,
        content_mode: str | None = None,
        custom_instruction: str | None = None,
        model: str | None = None,
    ) -> ReportSession:
        preset = load_rule_preset(session.preset_id)
        with tempfile.TemporaryDirectory(prefix="weekly-report-revision-") as directory:
            output_path = Path(directory) / "report.docx"
            render_prepared_report(
                prepared,
                preset,
                session.template_path,
                output_path,
                session.report_date,
                session.section_number,
            )
            return app.config["SNAPSHOT_STORE"].update_session(
                session.token,
                session.revision,
                prepared,
                output_path.read_bytes(),
                content_mode=content_mode,
                custom_instruction=custom_instruction,
                model=model,
            )

    def mutation_error(token: str, error: Exception, status: int):
        try:
            current = app.config["SNAPSHOT_STORE"].load_session(token)
        except ValueError:
            return show_form(error=str(error), status=status)
        return show_workspace(current, error=str(error), status=status)

    def current_session(token: str) -> ReportSession:
        session = app.config["SNAPSHOT_STORE"].load_session(token)
        expected = _parse_revision(request.form.get("revision", ""))
        if expected != session.revision:
            raise SessionConflictError(
                f"报告已在其他页面更新（当前版本 {session.revision}），"
                "请刷新后重新操作，当前修改未覆盖新版本。"
            )
        return session

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
                template = selected_template(workdir)
                render_prepared_report(
                    prepared,
                    preset,
                    template,
                    output_path,
                    report_date,
                    section_number,
                )
                filename = f"{preset.name}-{report_date:%Y%m%d}.docx"
                snapshot_token = app.config["SNAPSHOT_STORE"].create_session(
                    prepared,
                    preset_id=preset.id,
                    report_date=report_date,
                    section_number=section_number,
                    content_mode=mode.value,
                    custom_instruction=instruction,
                    model=model,
                    template_content=template.read_bytes(),
                    report_content=output_path.read_bytes(),
                    download_name=filename,
                )
            return show_workspace(app.config["SNAPSHOT_STORE"].load_session(snapshot_token))
        except (
            FileNotFoundError,
            OSError,
            ValueError,
            LLMClientError,
            zipfile.BadZipFile,
            ET.ParseError,
        ) as exc:
            return show_form(error=str(exc), status=400)

    @app.get("/workspace/<token>")
    def workspace(token: str):
        try:
            session = app.config["SNAPSHOT_STORE"].load_session(token)
            return show_workspace(session, notice=request.args.get("updated"))
        except ValueError as exc:
            return show_form(error=str(exc), status=404)

    @app.post("/workspace/<token>/blocks/<block_id>/edit")
    def edit_workspace_block(token: str, block_id: str):
        try:
            session = current_session(token)
            prepared = edit_report_block(
                session.prepared, block_id, request.form.get("edited_text", "")
            )
            render_session_revision(session, prepared)
            return redirect(
                f"{url_for('workspace', token=token, updated='edited')}#block-{block_id}"
            )
        except SessionConflictError as exc:
            return mutation_error(token, exc, 409)
        except (KeyError, OSError, ValueError, zipfile.BadZipFile, ET.ParseError) as exc:
            return mutation_error(token, exc, 400)

    @app.post("/workspace/<token>/blocks/<block_id>/select")
    def select_workspace_block(token: str, block_id: str):
        try:
            session = current_session(token)
            prepared = select_block_version(
                session.prepared, block_id, request.form.get("version", "")
            )
            render_session_revision(session, prepared)
            return redirect(
                f"{url_for('workspace', token=token, updated='selected')}#block-{block_id}"
            )
        except SessionConflictError as exc:
            return mutation_error(token, exc, 409)
        except (KeyError, OSError, ValueError, zipfile.BadZipFile, ET.ParseError) as exc:
            return mutation_error(token, exc, 400)

    @app.post("/workspace/<token>/blocks/<block_id>/regenerate")
    def regenerate_workspace_block(token: str, block_id: str):
        try:
            session = current_session(token)
            mode = parse_content_mode(request.form.get("content_mode", session.content_mode))
            if mode is ContentMode.ORIGINAL:
                raise ValueError("单条重新生成必须选择一种模型处理模式。")
            _require_external_consent(mode)
            instruction = request.form.get("custom_instruction", "").strip() or None
            model = request.form.get("model", "").strip() or session.model
            service = app.config.get("LLM_SERVICE") or LLMService(load_llm_config())
            prepared = service.regenerate_block(
                session.prepared,
                block_id,
                mode,
                custom_instruction=instruction,
                model=model,
                overwrite_edited=request.form.get("overwrite_edited") == "yes",
            )
            render_session_revision(
                session,
                prepared,
                content_mode=mode.value,
                custom_instruction=instruction,
                model=model,
            )
            return redirect(
                f"{url_for('workspace', token=token, updated='regenerated')}#block-{block_id}"
            )
        except SessionConflictError as exc:
            return mutation_error(token, exc, 409)
        except (
            KeyError,
            LLMClientError,
            OSError,
            ValueError,
            zipfile.BadZipFile,
            ET.ParseError,
        ) as exc:
            return mutation_error(token, exc, 400)

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
