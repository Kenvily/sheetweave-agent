"""Flask web interface for weekly report generation."""

from __future__ import annotations

import argparse
import difflib
import io
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import zipfile
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

from flask import (
    Flask,
    Response,
    copy_current_request_context,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    stream_with_context,
    url_for,
)
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import RequestEntityTooLarge

from .docx_security import inspect_docx_package
from .generation import generate_snapshot
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
from .normalizer import normalize_report_text
from .pdf_preview import PdfPreviewError, PdfPreviewService
from .planner import build_body_paragraphs, validate_section_number
from .rules import RulePreset, list_rule_presets, load_rule_preset
from .service import (
    edit_report_block,
    generate_report,
    render_prepared_report,
    select_block_version,
)
from .stats import UsageMetrics, UsageStats
from .task_queue import QueueFullError, TaskQueue
from .web_sessions import ReportSession, SessionConflictError, SnapshotStore
from .web_stream import GenerationCancelled, stream_events

DEFAULT_TEMPLATE = Path("data/研发三部周报模板.docx")
DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_UPLOAD_SIZE = 16 * 1024 * 1024
DEFAULT_SECTION_NUMBER = 9
SUPPORTED_INPUT_SUFFIXES = {".csv", ".xlsx"}
FIELD_KEYS = ("project", "detail", "status", "content", "issues", "next_plan")
VISITOR_COOKIE_NAME = "weekly_report_visitor"
VISITOR_COOKIE_MAX_AGE = 365 * 24 * 60 * 60
ANONYMOUS_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
EXAMPLE_FILES = {"general-weekly.csv", "general-weekly.xlsx"}


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


def _unified_diff(before: str, after: str) -> tuple[tuple[str, str], ...]:
    """Return a Git-style, line-oriented diff without changing source text."""
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    rows: list[tuple[str, str]] = []

    for operation, left_start, left_end, right_start, right_end in matcher.get_opcodes():
        if operation == "equal":
            rows.extend(("context", line) for line in before_lines[left_start:left_end])
        elif operation == "delete":
            rows.extend(("removed", line) for line in before_lines[left_start:left_end])
        elif operation == "insert":
            rows.extend(("added", line) for line in after_lines[right_start:right_end])
        else:
            rows.extend(("removed", line) for line in before_lines[left_start:left_end])
            rows.extend(("added", line) for line in after_lines[right_start:right_end])
    return tuple(rows)


def _unified_diff_header(before: str, after: str) -> str:
    """Return the single full-content hunk header used by the Web diff."""
    old_count = len(before.splitlines())
    new_count = len(after.splitlines())
    old_start = 1 if old_count else 0
    new_start = 1 if new_count else 0
    return f"@@ -{old_start},{old_count} +{new_start},{new_count} @@"


def _inline_diff(before: str, after: str) -> tuple[tuple[str, str], ...]:
    """Expose complete candidate text with word/character-level change markers."""
    tokens = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*|[^A-Za-z0-9_]", re.DOTALL)
    left, right = tokens.findall(before), tokens.findall(after)
    matcher = difflib.SequenceMatcher(a=left, b=right)
    parts: list[tuple[str, str]] = []
    for operation, left_start, left_end, right_start, right_end in matcher.get_opcodes():
        if operation == "equal":
            parts.append(("context", "".join(right[right_start:right_end])))
        else:
            if operation in {"delete", "replace"}:
                parts.append(("removed", "".join(left[left_start:left_end])))
            if operation in {"insert", "replace"}:
                parts.append(("added", "".join(right[right_start:right_end])))
    return tuple(parts)


def _warning_summary(warnings: tuple[str, ...]) -> tuple[str, ...]:
    """Deduplicate concise issue messages while retaining stored detail separately."""
    messages = []
    for warning in warnings:
        if "；原文：" not in warning:
            continue
        message = warning.split("；原文：", 1)[0]
        for prefix in ("谨慎使用：", "严重冲突：", "待人工确认：", "通过："):
            message = message.removeprefix(prefix)
        if message not in messages:
            messages.append(message)
    return tuple(messages)


def create_app(
    template_path: Path | None = None,
    llm_service: LLMService | None = None,
    snapshot_store: SnapshotStore | None = None,
    pdf_preview_service: PdfPreviewService | None = None,
    usage_stats: UsageStats | None = None,
    task_queue: TaskQueue | None = None,
) -> Flask:
    """Create and configure the weekly report web application."""
    app = Flask(__name__)
    configured_template = template_path or Path(
        os.environ.get("WEEKLY_REPORT_TEMPLATE", DEFAULT_TEMPLATE)
    )
    snapshot_root = Path(
        os.environ.get("WEEKLY_REPORT_SNAPSHOT_DIR", "").strip()
        or str(Path(tempfile.gettempdir()) / "weekly-report-agent-snapshots")
    )
    configured_stats = usage_stats
    if configured_stats is None:
        try:
            configured_stats = UsageStats.from_environment()
        except (OSError, sqlite3.Error, ValueError) as exc:
            app.logger.warning(
                "统计持久化初始化失败（%s），请检查存储路径和配置。", type(exc).__name__
            )
            configured_stats = None
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_UPLOAD_SIZE,
        REPORT_TEMPLATE_PATH=configured_template.resolve(),
        LLM_SERVICE=llm_service,
        SNAPSHOT_STORE=snapshot_store or SnapshotStore(snapshot_root),
        PDF_PREVIEW_SERVICE=pdf_preview_service or PdfPreviewService.from_environment(),
        USAGE_STATS=configured_stats,
        TASK_QUEUE=task_queue or TaskQueue.from_environment(),
    )

    def safe_metrics() -> UsageMetrics:
        service = app.config.get("USAGE_STATS")
        if service is None:
            return UsageMetrics()
        try:
            return service.metrics()
        except Exception:  # Statistics must never interrupt report generation.
            g.stats_unavailable = True
            app.logger.warning("无法读取持久化统计，请检查统计数据库。")
            return UsageMetrics()

    def safe_record_visitor(visitor_id: str) -> None:
        service = app.config.get("USAGE_STATS")
        if service is not None:
            try:
                service.record_visitor(visitor_id)
            except Exception:  # Statistics are deliberately best effort.
                g.stats_unavailable = True
                app.logger.warning("访客统计持久化失败，请检查统计数据库。")

    def safe_record_generation(task_id: str) -> None:
        service = app.config.get("USAGE_STATS")
        if service is not None:
            try:
                service.record_generation(task_id)
            except Exception:  # A valid DOCX must still be delivered.
                g.stats_unavailable = True
                app.logger.warning("生成次数持久化失败，请检查统计数据库。")

    @app.before_request
    def identify_anonymous_visitor() -> None:
        if request.method != "GET" or request.endpoint not in {"index", "help_page", "workspace"}:
            return
        visitor_id = request.cookies.get(VISITOR_COOKIE_NAME, "")
        is_new = not ANONYMOUS_ID_PATTERN.fullmatch(visitor_id)
        if is_new:
            visitor_id = secrets.token_urlsafe(24)
        g.anonymous_visitor_id = visitor_id
        g.set_anonymous_visitor_cookie = is_new
        safe_record_visitor(visitor_id)

    @app.after_request
    def persist_anonymous_visitor(response):
        if (
            response.status_code == 200
            and request.method == "GET"
            and request.endpoint in {"index", "help_page", "workspace"}
        ):
            service = app.config.get("USAGE_STATS")
            if service is not None:
                try:
                    service.record_page_view()
                except Exception:
                    app.logger.warning("访问次数持久化失败，请检查统计数据库。")
        if getattr(g, "set_anonymous_visitor_cookie", False):
            response.set_cookie(
                VISITOR_COOKIE_NAME,
                g.anonymous_visitor_id,
                max_age=VISITOR_COOKIE_MAX_AGE,
                httponly=True,
                samesite="Lax",
            )
        return response

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
        default_mode = ContentMode.ORIGINAL.value
        metrics = safe_metrics()
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
                usage_metrics=metrics,
                stats_available=(
                    app.config.get("USAGE_STATS") is not None
                    and not getattr(g, "stats_unavailable", False)
                ),
                generation_task_id=(
                    request.form.get("generation_task_id", "").strip() or secrets.token_urlsafe(18)
                ),
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
            "edited": "人工编辑已确认并生效，文档预览与下载文件已同步更新。",
            "selected": "有效内容版本已更新，文档预览与下载文件已同步更新。",
            "regenerated": "所选内容块已重新生成，其他内容未改变。",
            "pdf": "当前版本的 PDF 版式预览已生成。",
        }
        return (
            render_template(
                "workspace.html",
                session=session,
                prepared=session.prepared,
                preset=preset,
                error=error,
                notice=notices.get(notice, notice),
                unified_diff=_unified_diff,
                unified_diff_header=_unified_diff_header,
                inline_diff=_inline_diff,
                warning_summary=_warning_summary,
                content_modes=tuple(
                    (mode.value, MODE_OPTION_LABELS[mode])
                    for mode in ContentMode
                    if mode is not ContentMode.ORIGINAL
                ),
                models=models,
                default_model=default_model,
                llm_available=llm_available,
                pdf_available=app.config["PDF_PREVIEW_SERVICE"].available,
                rendered_blocks={
                    block.block_id: "\n".join(
                        paragraph.text
                        for paragraph in build_body_paragraphs(
                            block.effective_text, session.report_date
                        )
                    )
                    for block in session.prepared.blocks
                },
                normalize_text=lambda text: normalize_report_text(text, session.report_date),
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
        inspect_docx_package(path)
        return path

    def process_options() -> tuple[ContentMode, str | None, str | None]:
        mode = _selected_mode()
        instruction = request.form.get("custom_instruction", "").strip() or None
        model = request.form.get("model", "").strip() or None
        return mode, instruction, model

    @app.get("/")
    def index():
        return show_form()

    @app.get("/help")
    def help_page():
        return render_template("help.html", usage_metrics=safe_metrics())

    @app.get("/examples/<filename>")
    def download_example(filename: str):
        if filename not in EXAMPLE_FILES:
            return show_form(error="示例文件不存在。", status=404)
        path = Path(__file__).with_name("examples") / filename
        return send_file(path, as_attachment=True, download_name=filename, max_age=3600)

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

    def generation_options(upload):
        preset = load_rule_preset(request.form.get("preset", "rd3"))
        mode, instruction, model = process_options()
        return {
            "preset": preset.id,
            "report_date": _parse_date(request.form.get("report_date", "")).isoformat(),
            "section_number": _parse_section_number(request.form.get("section_number", ""), preset),
            "content_mode": mode.value,
            "custom_instruction": instruction,
            "model": model,
            "week": request.form.get("week", "").strip() or None,
            "sheet": request.form.get("sheet", "").strip() or None,
            "field_mapping": _field_mapping(),
            "filename": upload.filename,
        }

    def build_preview(
        on_delta=None, progress=lambda _message: None, input_path=None, template_path=None
    ):
        """Compatibility synchronous preview; the homepage uses queued tasks."""
        upload = _data_upload()
        if upload is None:
            raise ValueError("请选择周报 CSV 或 XLSX 文件。")
        options = generation_options(upload)
        with tempfile.TemporaryDirectory(prefix="weekly-report-preview-") as directory:
            workdir = Path(directory)
            token = generate_snapshot(
                input_path or save_input(upload, workdir),
                template_path or selected_template(workdir),
                options,
                app.config["SNAPSHOT_STORE"],
                llm_service=app.config.get("LLM_SERVICE"),
                on_delta=on_delta,
                progress=progress,
            )
        submitted = request.form.get("generation_task_id", "").strip()
        safe_record_generation(
            f"preview:{submitted if ANONYMOUS_ID_PATTERN.fullmatch(submitted) else token}"
        )
        return token

    def task_submission(kind, payload, token, directory):
        try:
            app.config["TASK_QUEUE"].enqueue(token, kind, payload)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return jsonify(
            id=token,
            status_url=url_for("task_status", token=token),
            cancel_url=url_for("cancel_task", token=token),
        ), 202

    @app.post("/tasks")
    def submit_generation_task():
        """Persist uploads and enqueue work; never call the model in a Web thread."""
        directory = None
        try:
            upload = _data_upload()
            if upload is None:
                raise ValueError("请选择周报 CSV 或 XLSX 文件。")
            options = generation_options(upload)
            if options["content_mode"] != "original":
                service = app.config.get("LLM_SERVICE") or LLMService(load_llm_config())
                service.config.select_model(options["model"]).require_available()
                if options["content_mode"] == "custom" and not options["custom_instruction"]:
                    raise ValueError("自定义要求模式必须填写处理要求。")
            queue = app.config["TASK_QUEUE"]
            token, directory = queue.new_directory()
            source = save_input(upload, directory)
            source.chmod(0o600)
            template = selected_template(directory)
            destination = directory / "template.docx"
            if template != destination:
                shutil.copyfile(template, destination)
            destination.chmod(0o600)
            options["input_name"] = source.name
            return task_submission("generate", options, token, directory)
        except (OSError, ValueError, LLMClientError, zipfile.BadZipFile) as exc:
            if directory:
                shutil.rmtree(directory, ignore_errors=True)
            return jsonify(error=str(exc)), 429 if isinstance(exc, QueueFullError) else 400

    @app.post("/workspace/<token>/blocks/<block_id>/tasks")
    def submit_regeneration_task(token, block_id):
        try:
            session = current_session(token)
            mode, instruction, model = process_options()
            if mode is ContentMode.ORIGINAL:
                raise ValueError("单条重新生成必须选择模型处理模式。")
            _require_external_consent(mode)
            if not any(
                block.block_id == block_id and block.llm_editable
                for block in session.prepared.blocks
            ):
                raise ValueError("内容块不存在或不支持模型处理。")
            payload = {
                "token": token,
                "revision": session.revision,
                "block_id": block_id,
                "content_mode": mode.value,
                "custom_instruction": instruction,
                "model": model or session.model,
                "overwrite_edited": request.form.get("overwrite_edited") == "yes",
            }
            task, directory = app.config["TASK_QUEUE"].new_directory()
            return task_submission("regenerate", payload, task, directory)
        except (ValueError, OSError) as exc:
            return jsonify(error=str(exc)), 429 if isinstance(
                exc, QueueFullError
            ) else 409 if isinstance(exc, SessionConflictError) else 400

    @app.post("/workspace/<token>/pdf/tasks")
    def submit_pdf_task(token):
        try:
            session = current_session(token)
            if not app.config["PDF_PREVIEW_SERVICE"].available:
                raise ValueError("当前环境未安装 LibreOffice，PDF 版式预览不可用。")
            task, directory = app.config["TASK_QUEUE"].new_directory()
            return task_submission(
                "pdf", {"token": token, "revision": session.revision}, task, directory
            )
        except (ValueError, OSError) as exc:
            return jsonify(error=str(exc)), 429 if isinstance(
                exc, QueueFullError
            ) else 409 if isinstance(exc, SessionConflictError) else 400

    @app.get("/tasks/<token>")
    def task_status(token):
        try:
            result = app.config["TASK_QUEUE"].status(token, int(request.args.get("after", "0")))
            if result["result"]:
                outcome = result["result"]
                outcome["url"] = url_for(
                    "workspace", token=outcome["token"], updated=outcome.get("updated")
                )
                if outcome.get("block_id"):
                    outcome["url"] += f"#block-{outcome['block_id']}"
                outcome["download_url"] = url_for("download_snapshot", token=outcome["token"])
            return jsonify(result), 200, {"Cache-Control": "no-store"}
        except ValueError as exc:
            return jsonify(error=str(exc)), 404

    @app.post("/tasks/<token>/cancel")
    def cancel_task(token):
        try:
            cancelled = app.config["TASK_QUEUE"].cancel(token)
            return jsonify(cancelled=cancelled), 200 if cancelled else 409
        except ValueError as exc:
            return jsonify(error=str(exc)), 404

    @app.post("/preview")
    def preview():
        try:
            token = build_preview()
            return show_workspace(app.config["SNAPSHOT_STORE"].load_session(token))
        except (OSError, ValueError, LLMClientError, zipfile.BadZipFile, ET.ParseError) as exc:
            return show_form(error=str(exc), status=400)

    @app.post("/preview-stream")
    def preview_stream():
        """Stream provisional model content; publish only a completed validated snapshot."""
        upload_work = tempfile.TemporaryDirectory(prefix="weekly-report-stream-upload-")
        try:
            upload = _data_upload()
            if upload is None:
                raise ValueError("请选择周报 CSV 或 XLSX 文件。")
            saved_input = save_input(upload, Path(upload_work.name))
            saved_template = selected_template(Path(upload_work.name))
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            upload_work.cleanup()
            return jsonify(error=str(exc)), 400

        @copy_current_request_context
        def generate(emit):
            try:
                token = build_preview(
                    on_delta=lambda batch, text: emit("delta", {"batch": batch, "text": text}),
                    progress=lambda message: emit("progress", {"message": message}),
                    input_path=saved_input,
                    template_path=saved_template,
                )
                emit(
                    "complete",
                    {
                        "url": url_for("workspace", token=token),
                        "download_url": url_for("download_snapshot", token=token),
                    },
                )
            except GenerationCancelled:
                raise
            except (OSError, ValueError, LLMClientError, zipfile.BadZipFile, ET.ParseError) as exc:
                emit("error", {"message": str(exc)})
            except Exception as exc:
                app.logger.error("流式生成失败（%s）。", type(exc).__name__)
                emit("error", {"message": "生成失败，请重试或切换原文模式。"})

        def deliver():
            try:
                yield from stream_events(generate)
            finally:
                upload_work.cleanup()

        response = Response(
            stream_with_context(deliver()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
        response.call_on_close(upload_work.cleanup)
        return response

    @app.get("/workspace/<token>")
    def workspace(token: str):
        try:
            session = app.config["SNAPSHOT_STORE"].load_session(token)
            return show_workspace(session, notice=request.args.get("updated"))
        except ValueError as exc:
            return show_form(error=str(exc), status=404)

    @app.post("/workspace/<token>/blocks/<block_id>/edit-preview")
    def preview_workspace_edit(token: str, block_id: str):
        """Compare a human draft with current output without modifying the session."""
        try:
            session = current_session(token)
            block = session.prepared.block_by_id(block_id)
            draft = request.form.get("edited_text", "").strip()
            if not draft:
                raise ValueError("人工编辑内容不能为空。")
            before = "\n".join(
                item.text
                for item in build_body_paragraphs(block.effective_text, session.report_date)
            )
            after = "\n".join(
                item.text for item in build_body_paragraphs(draft, session.report_date)
            )
            response = jsonify(
                before=before,
                after=after,
                diff=_unified_diff(before, after),
                header=_unified_diff_header(before, after),
            )
            response.headers["Cache-Control"] = "no-store"
            return response
        except SessionConflictError as exc:
            return jsonify(error=str(exc)), 409
        except (KeyError, OSError, ValueError) as exc:
            return jsonify(error=str(exc)), 400

    @app.post("/workspace/<token>/blocks/<block_id>/edit")
    def edit_workspace_block(token: str, block_id: str):
        try:
            session = current_session(token)
            if request.form.get("confirm_edit") != "yes":
                raise ValueError("请查看本次修改对比并确认后保存。")
            prepared = edit_report_block(
                session.prepared,
                block_id,
                request.form.get("edited_text", ""),
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
                session.prepared,
                block_id,
                request.form.get("version", ""),
                confirm_warnings=request.form.get("confirm_warnings") == "yes",
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

    @app.post("/workspace/<token>/pdf")
    def generate_workspace_pdf(token: str):
        """Generate an optional PDF preview for exactly one current revision."""
        try:
            session = current_session(token)
            service = app.config["PDF_PREVIEW_SERVICE"]
            with tempfile.TemporaryDirectory(prefix="weekly-report-pdf-route-") as directory:
                output_path = Path(directory) / "preview.pdf"
                service.convert(session.report_path, output_path)
                app.config["SNAPSHOT_STORE"].publish_pdf(
                    token, session.revision, output_path.read_bytes()
                )
            return redirect(url_for("workspace", token=token, updated="pdf"))
        except SessionConflictError as exc:
            return mutation_error(token, exc, 409)
        except (OSError, PdfPreviewError, ValueError) as exc:
            return mutation_error(token, exc, 400)

    @app.get("/workspace/<token>/pdf")
    def workspace_pdf(token: str):
        """Serve only the PDF bound to the current report revision."""
        try:
            revision_value = request.args.get("revision", "").strip()
            expected_revision = _parse_revision(revision_value) if revision_value else None
            snapshot = app.config["SNAPSHOT_STORE"].get_pdf(token, expected_revision)
            return send_file(
                snapshot.path,
                mimetype="application/pdf",
                as_attachment=False,
                download_name=f"weekly-report-preview-{snapshot.revision}.pdf",
                max_age=0,
            )
        except SessionConflictError as exc:
            return mutation_error(token, exc, 409)
        except ValueError as exc:
            return mutation_error(token, exc, 404)

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
            submitted_task_id = request.form.get("generation_task_id", "").strip()
            task_id = (
                submitted_task_id
                if ANONYMOUS_ID_PATTERN.fullmatch(submitted_task_id)
                else secrets.token_urlsafe(24)
            )
            safe_record_generation(f"direct:{task_id}")
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
