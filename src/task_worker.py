"""Independent task worker; Web processes never execute queued LLM work."""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import signal
import sqlite3
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Thread

from .generation import generate_snapshot
from .llm import LLMClientError, LLMService, load_llm_config
from .pdf_preview import PdfPreviewError, PdfPreviewService
from .service import render_prepared_report
from .stats import UsageStats
from .task_queue import TaskCancelled, TaskQueue
from .timing import diagnostic_event, diagnostic_scope, measure
from .web_sessions import SessionConflictError, SnapshotStore, session_preset

LOGGER = logging.getLogger(__name__)


class TaskRunner:
    """Execute queued tasks and persist progress without streaming report content."""

    def __init__(
        self,
        queue: TaskQueue,
        snapshots: SnapshotStore,
        *,
        llm_service: LLMService | None = None,
        pdf_service: PdfPreviewService | None = None,
        stats: UsageStats | None = None,
    ) -> None:
        self.queue, self.snapshots = queue, snapshots
        self.llm_service = llm_service
        self.pdf_service = pdf_service or PdfPreviewService.from_environment()
        self.stats = stats

    @classmethod
    def from_environment(cls, queue: TaskQueue) -> TaskRunner:
        snapshots = SnapshotStore.from_environment()
        try:
            stats = UsageStats.from_environment()
        except (OSError, ValueError, sqlite3.Error):
            stats = None
            LOGGER.warning("统计不可用；后台生成仍可继续。")
        return cls(queue, snapshots, stats=stats)

    def run(self, job: dict, owner: str) -> None:
        with diagnostic_scope(task_id=job["id"], task_kind=job["kind"]), measure(None, "task"):
            diagnostic_event("worker_enter")
            self._run(job, owner)
            status = self.queue.status(job["id"])
            diagnostic_event("task_result", state=status["state"])

    def _run(self, job: dict, owner: str) -> None:
        """Keep a lease while processing; never replay crashed external calls silently."""
        token = job["id"]
        stopped, lost = Event(), Event()

        def renew():
            while not stopped.wait(min(5, self.queue.lease_seconds / 3)):
                try:
                    if not self.queue.heartbeat(token, owner):
                        lost.set()
                        return
                except Exception:
                    lost.set()
                    return

        def emit(kind, payload):
            if lost.is_set():
                raise TaskCancelled()
            self.queue.emit(token, owner, kind, payload)

        def publish():
            if lost.is_set():
                raise TaskCancelled()
            self.queue.begin_publish(token, owner)

        heartbeat = Thread(target=renew, daemon=True, name="task-lease")
        heartbeat.start()
        try:
            emit("progress", {"message": "后台 Worker 已开始处理。"})
            options = job["payload"]
            directory = self.queue.directory(token)
            if job["kind"] == "adapt":
                from .adaptation import assist_mapping
                from .llm.client import OpenAICompatibleClient

                service = self.llm_service
                config = service.config if service else load_llm_config()
                config.require_available()
                client = (
                    service._client
                    if service and service._client is not None
                    else OpenAICompatibleClient(config)
                )
                if options.get("mode") == "profile":
                    import json

                    from .adaptation import suggest_profile

                    analysis = json.loads((directory / "analysis.json").read_text())
                    result = suggest_profile(
                        directory / options["input_name"],
                        directory / "template.docx",
                        analysis,
                        client=client,
                        model=config.model,
                    )
                    publish()
                else:
                    mapping = assist_mapping(options["table"], client=client, model=config.model)
                    publish()
                    result = {"mapping": mapping, "notice": "仅为待确认建议，未保存或改变方案。"}
            elif job["kind"] == "generate":
                snapshot = generate_snapshot(
                    directory / options["input_name"],
                    directory / "template.docx",
                    options,
                    self.snapshots,
                    llm_service=self.llm_service,
                    on_delta=None,
                    progress=lambda message: emit("progress", {"message": message}),
                    before_publish=publish,
                )
                result = {"token": snapshot, "updated": None}
            else:
                session = self.snapshots.load_session(options["token"])
                if session.revision != options["revision"]:
                    raise SessionConflictError(
                        "报告版本已更新，请刷新后重新提交，当前任务未覆盖新版本。"
                    )
                with tempfile.TemporaryDirectory(prefix="weekly-report-task-") as temp:
                    work = Path(temp)
                    if job["kind"] == "pdf":
                        emit("progress", {"message": "正在生成 PDF 版式预览。"})
                        output = work / "preview.pdf"
                        self.pdf_service.convert(session.report_path, output)
                        publish()
                        self.snapshots.publish_pdf(
                            session.token, session.revision, output.read_bytes()
                        )
                        result = {"token": session.token, "updated": "pdf"}
                    else:
                        emit("progress", {"message": "正在重新生成所选内容块。"})
                        service = self.llm_service or LLMService(load_llm_config())
                        prepared = service.regenerate_block(
                            session.prepared,
                            options["block_id"],
                            options["content_mode"],
                            custom_instruction=options.get("custom_instruction"),
                            model=options.get("model"),
                            overwrite_edited=options.get("overwrite_edited", False),
                        )
                        emit("progress", {"message": "正在生成并验证新的 DOCX 版本。"})
                        output = work / "report.docx"
                        render_prepared_report(
                            prepared,
                            session_preset(session),
                            session.template_path,
                            output,
                            session.report_date,
                            session.section_number,
                        )
                        publish()
                        self.snapshots.update_session(
                            session.token,
                            session.revision,
                            prepared,
                            output.read_bytes(),
                            content_mode=options["content_mode"],
                            custom_instruction=options.get("custom_instruction"),
                            model=options.get("model"),
                        )
                        result = {
                            "token": session.token,
                            "updated": "regenerated",
                            "block_id": options["block_id"],
                        }
            accepted = self.queue.finish(token, owner, result=result)
            if (
                accepted
                and job["kind"] == "generate"
                and options.get("preset", "").startswith("p_")
            ):
                try:
                    from .profiles import ProfileStore

                    ProfileStore.from_environment().used(options["preset"], successful=True)
                except (OSError, ValueError, sqlite3.Error):
                    LOGGER.warning("方案最后成功时间更新失败。")
            if accepted and job["kind"] == "generate" and self.stats:
                try:
                    self.stats.record_generation(f"task:{token}")
                except Exception:
                    LOGGER.warning("任务已完成，统计更新失败。")
        except TaskCancelled:
            self.queue.finish(token, owner, error="任务已取消或后台执行权已失效。")
        except (ValueError, LLMClientError, PdfPreviewError) as exc:
            diagnostic_event("task_failure", exception_type=type(exc).__name__)
            self.queue.finish(token, owner, error=str(exc))
        except Exception as exc:
            LOGGER.error("后台任务失败（%s）。", type(exc).__name__)
            self.queue.finish(token, owner, error="后台生成失败，请重试或切换原文模式。")
        finally:
            stopped.set()
            heartbeat.join(timeout=2)


def main() -> int:
    """Run a separate process with bounded local and shared task concurrency."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="处理一个已排队任务后退出")
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    queue = TaskQueue.from_environment()
    runner = TaskRunner.from_environment(queue)
    owner = secrets.token_urlsafe(24)
    stop = Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _sig, _frame: stop.set())
    queue.cleanup()
    if args.once:
        job = queue.claim(owner)
        if job:
            runner.run(job, owner)
        return 0
    last_cleanup = time.monotonic()
    with ThreadPoolExecutor(
        max_workers=queue.concurrency, thread_name_prefix="task-runner"
    ) as pool:
        pending = set()
        while not stop.is_set():
            for future in list(pending):
                if future.done():
                    try:
                        future.result()
                    except Exception as exc:
                        LOGGER.error("任务执行器异常（%s）。", type(exc).__name__)
                    pending.remove(future)
            while len(pending) < queue.concurrency and not stop.is_set():
                job = queue.claim(owner)
                if not job:
                    break
                pending.add(pool.submit(runner.run, job, owner))
            if time.monotonic() - last_cleanup > 60:
                queue.cleanup()
                last_cleanup = time.monotonic()
            stop.wait(0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
