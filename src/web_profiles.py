"""Confirmed adaptation workflow and permanent report profile management UI."""

from __future__ import annotations

import fcntl
import hashlib
import json
import secrets
import shutil
import time
from datetime import date
from pathlib import Path

from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from .adaptation import (
    ensure_unmatched_section,
    inspect_requirements,
    inspect_table,
    inspect_template,
    propose_rule,
    validate_adaptation,
)
from .input_parser import list_xlsx_sheets
from .llm.config import load_llm_config
from .numbering import CHOICES
from .profiles import ProfileStore
from .rules import preset_payload
from .service import prepare_report, render_prepared_report
from .task_queue import QueueFullError
from .web_sessions import TOKEN_PATTERN

profiles_ui = Blueprint("profiles", __name__)


def store() -> ProfileStore:
    return current_app.config["PROFILE_STORE"]


def draft_path(token: str) -> Path:
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("适配草稿链接无效。")
    root = current_app.config["ADAPTATION_ROOT"]
    directory = root / token
    metadata = directory / "analysis.json"
    if not metadata.is_file() or time.time() - metadata.stat().st_mtime > 7200:
        raise ValueError("适配草稿已过期，请重新分析。已保存的方案不受影响。")
    return directory


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def rule_hash(rule: dict) -> str:
    return hashlib.sha256(json.dumps(rule, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@profiles_ui.get("/profiles")
def library():
    try:
        service = current_app.config.get("LLM_SERVICE")
        available = (service.config if service else load_llm_config()).available
    except ValueError:
        available = False
    return render_template(
        "profiles.html", profiles=store().list(), analysis=None, llm_available=available
    )


@profiles_ui.post("/profiles/input-options")
def input_options():
    try:
        upload = request.files.get("source")
        suffix = Path(upload.filename or "").suffix.lower() if upload else ""
        if suffix not in {".csv", ".xlsx"}:
            raise ValueError("请选择 CSV 或 XLSX。")
        import tempfile

        with tempfile.TemporaryDirectory(prefix="profile-sheets-") as temp:
            path = Path(temp) / ("input" + suffix)
            upload.save(path)
            sheets = list_xlsx_sheets(path) if suffix == ".xlsx" else ()
        return jsonify(sheets=sheets, selected_sheet=sheets[0] if sheets else None)
    except (ValueError, OSError) as exc:
        return jsonify(error=str(exc)), 400


@profiles_ui.post("/profiles/analyze")
def analyze():
    directory = None
    try:
        smart = request.form.get("smart") == "yes"
        if smart:
            if request.form.get("allow_analysis") != "yes":
                raise ValueError("请确认样例已脱敏，并允许将样例内容与参考文档发送到已配置模型。")
            service = current_app.config.get("LLM_SERVICE")
            (service.config if service else load_llm_config()).require_available()
        upload = request.files.get("source")
        if not upload or Path(upload.filename or "").suffix.lower() not in {".csv", ".xlsx"}:
            raise ValueError("请选择 CSV/XLSX 分析样例。")
        root = current_app.config["ADAPTATION_ROOT"]
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        token = secrets.token_urlsafe(32)
        # Only short-lived adaptation samples expire. The permanent profile store is separate.
        for old in root.iterdir():
            if not old.is_symlink() and old.is_dir() and TOKEN_PATTERN.fullmatch(old.name):
                metadata_path = old / "analysis.json"
                if metadata_path.is_file() and time.time() - metadata_path.stat().st_mtime > 7200:
                    try:
                        with metadata_path.open() as lock:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            shutil.rmtree(old)
                    except (OSError, BlockingIOError):
                        pass
        directory = root / token
        directory.mkdir(mode=0o700)
        source = directory / ("input" + Path(upload.filename).suffix.lower())
        upload.save(source)
        source.chmod(0o600)
        template = directory / "template.docx"
        profile_id = request.form.get("profile_id", "") or None
        version = None
        if profile_id:
            old_preset, saved_template, metadata = store().load(profile_id)
            version = metadata["version"]
        else:
            saved_template = current_app.config["REPORT_TEMPLATE_PATH"]
        uploaded_template = request.files.get("template")
        if uploaded_template and uploaded_template.filename:
            if Path(uploaded_template.filename).suffix.lower() != ".docx":
                raise ValueError("模板必须是 DOCX。")
            uploaded_template.save(template)
        else:
            shutil.copyfile(saved_template, template)
        template.chmod(0o600)
        table = inspect_table(
            source,
            request.form.get("sheet", "").strip() or None,
            int(request.form["header_rows"]) if request.form.get("header_rows") else None,
        )
        document = inspect_template(template)
        matches = []
        with store().locked():
            for saved in store().list():
                candidate = store().load(saved["id"])[0]
                if (
                    candidate.input.expected_headers == tuple(table["headers"])
                    and candidate.input.layout == table["layout"]
                ):
                    matches.append({"id": candidate.id, "name": candidate.name})
        proposal = propose_rule(table, document)
        if profile_id:
            # Keep confirmed business/format rules; adapt only the physical source signature.
            old = preset_payload(old_preset)
            old["id"] = "adapted"
            old["input"].update(
                expected_headers=list(table["headers"]),
                header_rows=table["header_rows"],
                layout=table["layout"],
                sheet_default=table["sheet"],
            )
            old["input"]["required_fields"] = (
                ["project", "content"] if table["layout"] == "records" else ["project"]
            )
            if table["layout"] == "records":
                old["input"]["historical_completion"] = None
            proposal = old
        ensure_unmatched_section(proposal)
        # Explicit instructions inside DOCX are evidence separate from incidental styles.
        # User-supplied instructions come last and remain subject to conflict confirmation.
        requirements_text = (
            "\n".join(document["written_requirements"])
            + "\n"
            + request.form.get("requirements", "")[:16000]
        ).strip()[:32000]
        requirements = inspect_requirements(requirements_text)
        for role, properties in requirements["styles"].items():
            observed = proposal["document"]["styles"].get(role, {})
            for key, value in properties.items():
                if key in observed and observed[key] != value:
                    document["pending"].append(
                        f"{role}.{key}：书面要求与样例冲突，已优先提出书面值，请确认。"
                    )
            proposal["document"]["styles"].setdefault(role, {}).update(properties)
        if requirements["heading_start"] is not None:
            proposal["document"]["heading_start_default"] = requirements["heading_start"]
        document["pending"].extend(requirements["pending"])
        document["requirements_evidence"] = requirements["evidence"]
        analysis = {
            "table": table,
            "matching_profiles": matches,
            "document": document,
            "source_name": source.name,
            "filename": Path(upload.filename).name,
            "profile_id": profile_id,
            "version": version,
            "description": metadata.get("description", "") if profile_id else "",
            "requirements": requirements_text,
            "desired_outline": request.form.get("desired_outline", "")[:12000],
            "rule": proposal,
        }
        if smart and document["supported"]:
            queue = current_app.config["TASK_QUEUE"]
            task, task_directory = queue.new_directory()
            try:
                shutil.copyfile(source, task_directory / source.name)
                shutil.copyfile(template, task_directory / "template.docx")
                write_json(task_directory / "analysis.json", analysis)
                for path in task_directory.iterdir():
                    path.chmod(0o600)
                queue.enqueue(task, "adapt", {"mode": "profile", "input_name": source.name})
                analysis["ai_task"] = task
            except Exception:
                shutil.rmtree(task_directory, ignore_errors=True)
                raise
        write_json(directory / "analysis.json", analysis)
        return redirect(url_for("profiles.draft", token=token))
    except (ValueError, OSError, KeyError) as exc:
        if directory:
            shutil.rmtree(directory, ignore_errors=True)
        return render_template(
            "profiles.html", profiles=store().list(), analysis=None, error=str(exc)
        ), 400


@profiles_ui.get("/profiles/drafts/<token>")
def draft(token):
    try:
        directory = draft_path(token)
        analysis = read_json(directory / "analysis.json")
        return render_template(
            "profiles.html",
            profiles=store().list(),
            analysis=analysis,
            token=token,
            numbering_options=CHOICES,
        )
    except (ValueError, OSError) as exc:
        return str(exc), 400


@profiles_ui.get("/profiles/drafts/<token>/status")
def analysis_status(token):
    try:
        directory = draft_path(token)
        with (directory / "analysis.json").open() as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            analysis = read_json(directory / "analysis.json")
            if not analysis.get("ai_task") or analysis.get("ai_applied"):
                return jsonify(state="ready")
            queue = current_app.config["TASK_QUEUE"]
            status = queue.status(analysis["ai_task"])
            if status["state"] == "succeeded":
                result = status["result"]
                analysis.update(
                    rule=result["rule"],
                    table=result["table"],
                    summary=result["summary"],
                    questions=result["questions"],
                    ai_applied=True,
                )
                write_json(directory / "analysis.json", analysis)
                (directory / "trial.json").unlink(missing_ok=True)
                return jsonify(state="ready")
            if status["state"] in {"failed", "cancelled"}:
                analysis.update(ai_applied=True, ai_error=status.get("error") or "自动识别已取消。")
                write_json(directory / "analysis.json", analysis)
                return jsonify(state="ready")
        return jsonify(state=status["state"], queue_position=status["queue_position"])
    except (ValueError, OSError) as exc:
        return jsonify(error=str(exc)), 400


def require_analysis_ready(analysis):
    if analysis.get("ai_task") and not analysis.get("ai_applied"):
        raise ValueError("自动识别尚未完成，请等待识别结果后再核对。")


@profiles_ui.post("/profiles/drafts/<token>/assist")
def assist(token):
    """Explicitly consented metadata-only assistance; original report text stays local."""
    try:
        if request.form.get("allow_metadata") != "yes":
            raise ValueError("需要确认允许发送表头列名；不会发送数据行、模板正文或书面要求。")
        analysis = read_json(draft_path(token) / "analysis.json")
        service = current_app.config.get("LLM_SERVICE")
        config = service.config if service else load_llm_config()
        config.require_available()
        queue = current_app.config["TASK_QUEUE"]
        task, directory = queue.new_directory()
        try:
            # Freeze only metadata needed by the assistant. Never enqueue report bodies here.
            queue.enqueue(
                task,
                "adapt",
                {
                    "table": {
                        "headers": analysis["table"]["headers"],
                        "layout": analysis["table"]["layout"],
                    }
                },
            )
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return jsonify(
            id=task,
            status_url=url_for("task_status", token=task),
            cancel_url=url_for("cancel_task", token=task),
        ), 202
    except (ValueError, OSError, RuntimeError) as exc:
        return jsonify(error=str(exc)), 429 if isinstance(exc, QueueFullError) else 400


@profiles_ui.post("/profiles/drafts/<token>/trial")
def trial(token):
    try:
        directory = draft_path(token)
        analysis = read_json(directory / "analysis.json")
        if not analysis["document"]["supported"]:
            raise ValueError(
                "模板包含当前不支持的版式：" + "、".join(analysis["document"]["unsupported"])
            )
        require_analysis_ready(analysis)
        rule = request.get_json()["rule"]
        preset = validate_adaptation(rule, analysis["table"])
        if preset.id != "adapted":
            raise ValueError("适配草稿 id 必须为 adapted，保存时分配正式方案 ID。")
        report_date = date.fromisoformat(
            request.get_json().get("report_date") or date.today().isoformat()
        )
        # Lock concurrent trial/save for this draft; no network calls in this critical section.
        with (directory / "analysis.json").open() as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            prepared, _ = prepare_report(
                directory / analysis["source_name"], preset=preset, sheet=analysis["table"]["sheet"]
            )
            plan = render_prepared_report(
                prepared, preset, directory / "template.docx", directory / "trial.docx", report_date
            )
            write_json(
                directory / "trial.json",
                {"hash": rule_hash(rule), "rule": rule, "date": report_date.isoformat()},
            )
        return jsonify(
            paragraphs=[{"kind": p.kind.value, "text": p.text} for p in plan.paragraphs],
            warnings=prepared.source.warnings,
            retained=len(prepared.source.sources),
            blocks=len(prepared.blocks),
            excluded=len(prepared.source.excluded_sources),
            excluded_rows=[
                {"row": r.row_number, "status": r.fields.get("status", "")}
                for r in prepared.source.excluded_sources
            ],
            download_url=url_for("profiles.trial_download", token=token),
        )
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return jsonify(error=str(exc)), 400


@profiles_ui.get("/profiles/drafts/<token>/trial.docx")
def trial_download(token):
    try:
        path = draft_path(token) / "trial.docx"
        if not path.is_file():
            raise ValueError("请先试生成。")
        return send_file(path, as_attachment=True, download_name="方案试生成.docx", max_age=0)
    except (ValueError, OSError) as exc:
        return str(exc), 400


@profiles_ui.post("/profiles/drafts/<token>/save")
def save(token):
    try:
        directory = draft_path(token)
        analysis = read_json(directory / "analysis.json")
        require_analysis_ready(analysis)
        payload = request.get_json()
        if payload.get("confirmed") is not True:
            raise ValueError("请核对字段、状态取舍、格式与试生成结果后确认。")
        with (directory / "analysis.json").open() as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            trial_result = read_json(directory / "trial.json")
            if rule_hash(payload["rule"]) != trial_result["hash"]:
                raise ValueError("配置已改变，请重新试生成再保存。")
            validate_adaptation(payload["rule"], analysis["table"])
            profile_id, version = store().save(
                payload["rule"],
                directory / "template.docx",
                description=payload.get("description", analysis.get("description", ""))[:1000],
                evidence={
                    "table": analysis["table"],
                    "document": analysis["document"],
                    "requirements": analysis["requirements"],
                    "confirmed": True,
                },
                profile_id=analysis["profile_id"],
                expected_version=analysis["version"],
            )
        return jsonify(id=profile_id, version=version, url=url_for("index", preset=profile_id))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
