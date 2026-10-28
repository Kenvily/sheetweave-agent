"""Persistent queue, cancellation, independent execution and revision regressions."""

import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread

import pytest

from sheetweave_agent.llm import LLMClientError, LLMConfig, LLMService
from sheetweave_agent.task_queue import QueueFullError, TaskCancelled, TaskQueue
from sheetweave_agent.task_worker import TaskRunner
from sheetweave_agent.web import create_app
from sheetweave_agent.web_sessions import SnapshotStore

pytestmark = pytest.mark.usefixtures("saved_generation_profiles")


def _queue(tmp_path, **options):
    return TaskQueue(tmp_path / "queue.sqlite3", tmp_path / "files", **options)


def _enqueue(queue):
    token, _directory = queue.new_directory()
    queue.enqueue(token, "generate", {})
    return token


def _form(mode="original"):
    return {
        "csv_file": (
            io.BytesIO("项目,本周工作\n合成项目,完成验证，AUC 0.81\n".encode()),
            "synthetic.csv",
        ),
        "preset": "p_general",
        "report_date": "2026-10-12",
        "content_mode": mode,
    }


class BlockingClient:
    def __init__(self, *, broken=False):
        self.entered, self.release = Event(), Event()
        self.broken = broken
        self.calls = 0

    def complete(self, messages, model):
        self.calls += 1
        blocks = json.loads(messages[-1]["content"])["blocks"]
        value = json.dumps(
            {
                "blocks": [
                    {"block_id": b["block_id"], "text": b["editable_text"].replace("0.81", "0.91")}
                    for b in blocks
                ]
            }
        )
        self.entered.set()
        assert self.release.wait(3)
        if self.broken:
            raise LLMClientError("合成连接错误")
        return value


def _app(tmp_path, client=None, pdf=None):
    queue = _queue(tmp_path)
    snapshots = SnapshotStore(tmp_path / "snapshots")
    service = LLMService(LLMConfig(api_key="mock-only"), client) if client else None
    app = create_app(
        task_queue=queue, snapshot_store=snapshots, llm_service=service, pdf_preview_service=pdf
    )
    runner = TaskRunner(
        queue, snapshots, llm_service=service, pdf_service=pdf, stats=app.config["USAGE_STATS"]
    )
    return app, queue, snapshots, runner


def test_atomic_claim_across_queue_instances_and_global_cap(tmp_path):
    queue = _queue(tmp_path, concurrency=2)
    tokens = {_enqueue(queue) for _ in range(5)}
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(pool.map(lambda owner: _queue(tmp_path).claim(str(owner)), range(8)))
    claimed = [job["id"] for job in jobs if job]
    assert len(claimed) == len(set(claimed)) == 2
    assert set(claimed) <= tokens
    assert _queue(tmp_path).status(claimed[0])["state"] == "running"


def test_capacity_and_private_files(tmp_path):
    queue = _queue(tmp_path, capacity=1)
    token = _enqueue(queue)
    another, _ = queue.new_directory()
    with pytest.raises(QueueFullError):
        queue.enqueue(another, "generate", {})
    assert queue.database.stat().st_mode & 0o777 == 0o600
    assert queue.directory(token).stat().st_mode & 0o777 == 0o700
    assert queue.status(token)["queue_position"] == 1


def test_worker_crash_fails_without_replaying_external_call(tmp_path):
    queue = _queue(tmp_path, concurrency=1)
    first, second = _enqueue(queue), _enqueue(queue)
    assert queue.claim("old")["id"] == first
    with queue.connect() as db:
        db.execute("UPDATE tasks SET lease_until=? WHERE id=?", (time.time() - 1, first))
    assert queue.status(first)["state"] == "failed"
    assert queue.claim("new")["id"] == second
    assert not queue.finish(first, "old", result={"token": "stale"})


def test_cancel_retains_capacity_until_inflight_worker_stops(tmp_path):
    queue = _queue(tmp_path, concurrency=1)
    first, second = _enqueue(queue), _enqueue(queue)
    queue.claim("worker")
    assert queue.cancel(first)
    assert queue.claim("other") is None
    with pytest.raises(TaskCancelled):
        queue.emit(first, "worker", "progress", {"message": "late"})
    assert queue.finish(first, "worker", error="cancelled")
    assert queue.claim("other")["id"] == second


def test_publish_fences_cancellation_and_stale_owner(tmp_path):
    queue = _queue(tmp_path)
    token = _enqueue(queue)
    queue.claim("worker")
    with pytest.raises(TaskCancelled):
        queue.begin_publish(token, "intruder")
    queue.begin_publish(token, "worker")
    assert not queue.cancel(token)
    assert queue.finish(token, "worker", result={"token": "result"})
    assert not queue.cancel(token)
    assert queue.status(token)["state"] == "succeeded"


def test_incremental_events_and_private_payload(tmp_path):
    queue = _queue(tmp_path)
    token, _ = queue.new_directory()
    queue.enqueue(token, "generate", {"custom_instruction": "private instruction"})
    queue.claim("worker")
    for number in range(105):
        queue.emit(token, "worker", "progress", {"message": str(number)})
    page = queue.status(token)
    assert len(page["events"]) == 100 and page["has_more"]
    remaining = queue.status(token, page["cursor"])
    assert len(remaining["events"]) == 5 and not remaining["has_more"]
    assert "private instruction" not in json.dumps(page)
    with pytest.raises(ValueError):
        queue.status("../queue.sqlite3")


def test_upload_returns_before_worker_and_original_runs_offline(tmp_path):
    app, queue, snapshots, runner = _app(tmp_path)
    response = app.test_client().post("/tasks", data=_form())
    assert response.status_code == 202
    token = response.json["id"]
    assert queue.status(token)["state"] == "queued"
    assert not list(snapshots.root.iterdir())
    # A fresh queue instance represents the separately launched worker process.
    claimed = _queue(tmp_path).claim("worker")
    runner.run(claimed, "worker")
    status = app.test_client().get(response.json["status_url"])
    assert status.json["state"] == "succeeded"
    download = app.test_client().get(status.json["result"]["download_url"])
    assert download.data.startswith(b"PK")
    assert app.config["USAGE_STATS"].metrics().total_generations == 1
    assert queue.status(token)["started_at"] >= queue.status(token)["created_at"]


def test_queued_llm_does_not_block_home_or_poll_and_keeps_fact_fallback(tmp_path):
    mock = BlockingClient()
    app, queue, snapshots, runner = _app(tmp_path, mock)
    response = app.test_client().post("/tasks", data=_form("polish"))
    assert response.status_code == 202 and mock.calls == 0
    job = queue.claim("worker")
    thread = Thread(target=runner.run, args=(job, "worker"))
    thread.start()
    assert mock.entered.wait(1)
    client = app.test_client()
    assert client.get("/").status_code == 200
    status = client.get(response.json["status_url"]).json
    assert status["state"] == "running" and status["events"]
    mock.release.set()
    thread.join(3)
    assert not thread.is_alive()
    result = client.get(response.json["status_url"]).json
    session = snapshots.load_session(result["result"]["token"])
    assert session.prepared.blocks[0].effective_version == "original"
    assert session.prepared.blocks[0].validation_level == "conflict"
    assert mock.calls == 1
    assert not any(event["kind"] == "delta" for event in result["events"])


@pytest.mark.parametrize("cancel", [False, True])
def test_failure_or_cancel_never_publishes_partial_snapshot(tmp_path, cancel):
    mock = BlockingClient(broken=not cancel)
    app, queue, snapshots, runner = _app(tmp_path, mock)
    response = app.test_client().post("/tasks", data=_form("polish"))
    job = queue.claim("worker")
    thread = Thread(target=runner.run, args=(job, "worker"))
    thread.start()
    assert mock.entered.wait(1)
    if cancel:
        assert app.test_client().post(response.json["cancel_url"]).json["cancelled"]
    mock.release.set()
    thread.join(3)
    status = queue.status(job["id"])
    assert status["state"] == ("cancelled" if cancel else "failed")
    assert status["result"] is None
    assert not list(snapshots.root.iterdir())
    assert app.config["USAGE_STATS"].metrics().total_generations == 0


def test_queued_regeneration_detects_revision_change_before_llm(tmp_path):
    app, queue, snapshots, runner = _app(tmp_path)
    response = app.test_client().post("/tasks", data=_form())
    runner.run(queue.claim("worker"), "worker")
    token = queue.status(response.json["id"])["result"]["token"]
    session = snapshots.load_session(token)
    block = session.prepared.blocks[0].block_id
    response = app.test_client().post(
        f"/workspace/{token}/blocks/{block}/tasks",
        data={"revision": 1, "content_mode": "polish", "allow_external": "yes"},
    )
    assert response.status_code == 202
    snapshots.update_session(token, 1, session.prepared, session.report_path.read_bytes())
    runner.run(queue.claim("worker"), "worker")
    status = queue.status(response.json["id"])
    assert status["state"] == "failed" and "版本已更新" in status["error"]
    assert snapshots.load_session(token).revision == 2


def test_queue_cleanup_only_removes_expired_owned_tasks(tmp_path):
    queue = _queue(tmp_path)
    token = _enqueue(queue)
    queue.cancel(token)
    unrelated = queue.root / "keep-me"
    unrelated.mkdir()
    with queue.connect() as db:
        db.execute("UPDATE tasks SET finished=? WHERE id=?", (time.time() - 10000, token))
    queue.cleanup()
    assert unrelated.is_dir() and not queue.directory(token).exists()
    with pytest.raises(ValueError):
        queue.status(token)


class FakePdf:
    available = True

    def convert(self, source, output):
        assert source.read_bytes().startswith(b"PK")
        output.write_bytes(b"%PDF-1.4\nsynthetic fixture\n%%EOF")


def test_queued_pdf_preserves_revision_and_matches_snapshot(tmp_path):
    app, queue, snapshots, runner = _app(tmp_path, pdf=FakePdf())
    response = app.test_client().post("/tasks", data=_form())
    runner.run(queue.claim("worker"), "worker")
    token = queue.status(response.json["id"])["result"]["token"]
    response = app.test_client().post(f"/workspace/{token}/pdf/tasks", data={"revision": 1})
    assert response.status_code == 202
    runner.run(queue.claim("worker"), "worker")
    assert queue.status(response.json["id"])["state"] == "succeeded"
    session = snapshots.load_session(token)
    assert session.revision == 1 and session.pdf_path.read_bytes().startswith(b"%PDF-")
    assert app.config["USAGE_STATS"].metrics().total_generations == 1


def test_expired_waiting_task_is_not_sent_to_model(tmp_path):
    queue = _queue(tmp_path)
    token = _enqueue(queue)
    with queue.connect() as db:
        db.execute("UPDATE tasks SET created=? WHERE id=?", (time.time() - 10000, token))
    assert queue.claim("worker") is None
    assert queue.status(token)["state"] == "failed"


def test_queued_regeneration_publishes_new_revision(tmp_path):
    class Client:
        def complete(self, messages, model):
            blocks = json.loads(messages[-1]["content"])["blocks"]
            return json.dumps(
                {
                    "blocks": [
                        {
                            "block_id": b["block_id"],
                            "text": b["editable_text"].replace("完成验证", "完成模型验证"),
                        }
                        for b in blocks
                    ]
                }
            )

    app, queue, snapshots, runner = _app(tmp_path, Client())
    response = app.test_client().post("/tasks", data=_form())
    runner.run(queue.claim("worker"), "worker")
    token = queue.status(response.json["id"])["result"]["token"]
    session = snapshots.load_session(token)
    block = session.prepared.blocks[0].block_id
    response = app.test_client().post(
        f"/workspace/{token}/blocks/{block}/tasks",
        data={"revision": 1, "content_mode": "polish", "allow_external": "yes"},
    )
    assert response.status_code == 202
    runner.run(queue.claim("worker"), "worker")
    assert queue.status(response.json["id"])["state"] == "succeeded"
    updated = snapshots.load_session(token)
    assert updated.revision == 2
    assert "完成模型验证" in updated.prepared.blocks[0].generated_text
    assert app.config["USAGE_STATS"].metrics().total_generations == 1


@pytest.mark.parametrize("mode", ["original", "polish"])
def test_native_client_generation_chain_logs_safe_stages(tmp_path, monkeypatch, caplog, mode):
    requests = []

    def open_response(request, *, timeout):
        requests.append(request)
        payload = json.loads(request.data)
        blocks = json.loads(payload["messages"][-1]["content"])["blocks"]
        content = json.dumps(
            {
                "blocks": [
                    {"block_id": block["block_id"], "text": block["editable_text"]}
                    for block in blocks
                ]
            }
        )
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

    monkeypatch.setattr("urllib.request.urlopen", open_response)
    app, queue, snapshots, runner = _app(tmp_path)
    runner.llm_service = LLMService(LLMConfig(api_key="never-log-secret"))
    app.config["LLM_SERVICE"] = runner.llm_service
    caplog.set_level("INFO")
    response = app.test_client().post("/tasks", data=_form(mode))
    job = queue.claim("diagnostic-worker")
    runner.run(job, "diagnostic-worker")
    status = queue.status(response.json["id"])
    assert status["state"] == "succeeded"
    assert snapshots.get(status["result"]["token"]).path.read_bytes().startswith(b"PK")
    assert len(requests) == (1 if mode == "polish" else 0)
    events = [
        json.loads(record.message) for record in caplog.records if record.message.startswith("{")
    ]
    stages = {event.get("stage") for event in events}
    assert {"prepare_ir", "llm_process", "docx_render_validate"} <= stages
    if mode == "polish":
        assert {"http_attempt", "response_parse", "fact_validation"} <= stages
    assert any(e["event"] == "task_result" and e["state"] == "succeeded" for e in events)
    assert "never-log-secret" not in caplog.text
    assert "合成项目" not in caplog.text
