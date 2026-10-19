import io
import json
import urllib.error
from pathlib import Path
from threading import Event

import pytest

from weekly_report_agent.llm import LLMClientError, LLMConfig, LLMService, OpenAICompatibleClient
from weekly_report_agent.web import create_app
from weekly_report_agent.web_sessions import SnapshotStore
from weekly_report_agent.web_stream import GenerationCancelled, stream_events


def _packet(content: str, finish=None) -> bytes:
    return (
        "data: "
        + json.dumps({"choices": [{"delta": {"content": content}, "finish_reason": finish}]})
        + "\n\n"
    ).encode()


def test_provider_stream_yields_content_and_ignores_reasoning(monkeypatch) -> None:
    response = io.BytesIO(
        b": heartbeat\n\n"
        + b'data: {"choices":[{"delta":{"reasoning_content":"private reasoning"}}]}\n\n'
        + _packet("中文片段")
        + _packet("后续内容", "stop")
    )

    def open_response(request, *, timeout):
        assert json.loads(request.data)["stream"] is True
        assert request.get_header("Accept") == "text/event-stream"
        return response

    monkeypatch.setattr("urllib.request.urlopen", open_response)
    result = list(OpenAICompatibleClient(LLMConfig(api_key="test-only")).stream([], "model"))
    assert result == ["中文片段", "后续内容"]
    assert response.closed


def test_stream_disconnect_after_output_is_not_retried(monkeypatch) -> None:
    calls = 0

    def open_response(_request, *, timeout):
        nonlocal calls
        calls += 1
        return io.BytesIO(_packet("partial"))

    monkeypatch.setattr("urllib.request.urlopen", open_response)
    stream = OpenAICompatibleClient(LLMConfig(api_key="test-only")).stream([], "model")
    assert next(stream) == "partial"
    with pytest.raises(LLMClientError, match="提前结束"):
        next(stream)
    assert calls == 1


def test_stream_retries_transport_failure_only_before_output(monkeypatch) -> None:
    calls = 0

    def open_response(_request, *, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.URLError("unavailable")
        return io.BytesIO(_packet("complete", "stop"))

    monkeypatch.setattr("urllib.request.urlopen", open_response)
    monkeypatch.setattr("weekly_report_agent.llm.client.time.sleep", lambda _delay: None)
    assert list(OpenAICompatibleClient(LLMConfig(api_key="test-only")).stream([], "model")) == [
        "complete"
    ]
    assert calls == 2


class ChunkClient:
    def __init__(self, *, broken=False):
        self.broken = broken
        self.calls = 0
        self.entered = Event()
        self.release = Event()

    def stream(self, messages, model):
        self.calls += 1
        blocks = json.loads(messages[-1]["content"])["blocks"]
        content = json.dumps(
            {
                "blocks": [
                    {"block_id": b["block_id"], "text": b["editable_text"].replace("0.81", "0.91")}
                    for b in blocks
                ]
            }
        )
        yield content[:30]
        self.entered.set()
        assert self.release.wait(3)
        if self.broken:
            raise LLMClientError("synthetic disconnect")
        yield content[30:]


def _form():
    return {
        "csv_file": (
            io.BytesIO("项目,本周工作\n合成项目,验证 AUC 0.81\n".encode()),
            "synthetic.csv",
        ),
        "preset": "general",
        "report_date": "2026-10-12",
        "content_mode": "polish",
    }


def test_stream_delivers_delta_before_completion_and_retains_fallback(tmp_path) -> None:
    mock = ChunkClient()
    store = SnapshotStore(tmp_path / "sessions")
    app = create_app(
        llm_service=LLMService(LLMConfig(api_key="test-only"), mock), snapshot_store=store
    )
    response = app.test_client().post("/preview-stream", data=_form(), buffered=False)
    assert response.mimetype == "text/event-stream"
    assert response.headers["Cache-Control"] == "no-store"
    iterator = iter(response.response)
    frames = [next(iterator)]
    while b"event: delta" not in frames[-1]:
        frames.append(next(iterator))
    assert mock.entered.wait(1)
    assert not list(store.root.glob("*/session.json"))
    mock.release.set()
    frames.extend(iterator)
    text = b"".join(frames).decode()
    assert text.index("event: delta") < text.index("event: complete")
    payload = json.loads(text.split("event: complete\ndata: ", 1)[1].split("\n\n", 1)[0])
    token = payload["url"].rsplit("/", 1)[1]
    session = store.load_session(token)
    assert session.prepared.blocks[0].validation_level == "conflict"
    assert session.prepared.blocks[0].effective_version == "original"
    assert mock.calls == 1
    assert payload["download_url"].endswith(token)
    response.close()


def test_broken_stream_returns_error_without_snapshot(tmp_path) -> None:
    mock = ChunkClient(broken=True)
    mock.release.set()
    store = SnapshotStore(tmp_path / "sessions")
    app = create_app(
        llm_service=LLMService(LLMConfig(api_key="test-only"), mock), snapshot_store=store
    )
    response = app.test_client().post("/preview-stream", data=_form())
    assert b"event: error" in response.data and b"event: complete" not in response.data
    assert not list(store.root.iterdir())
    assert mock.calls == 1


def test_sse_cancellation_unblocks_producer() -> None:
    cancelled = Event()

    def work(emit):
        try:
            for _index in range(1000):
                emit("delta", {"text": "synthetic"})
        except GenerationCancelled:
            cancelled.set()
            raise

    generator = stream_events(work)
    next(generator)
    next(generator)
    generator.close()
    assert cancelled.wait(2)


def test_original_stream_is_offline(tmp_path) -> None:
    store = SnapshotStore(tmp_path / "sessions")
    app = create_app(snapshot_store=store)
    form = _form()
    form["content_mode"] = "original"
    form["template_file"] = (
        io.BytesIO(Path("data/研发三部周报模板-v2.docx").read_bytes()),
        "synthetic-template.docx",
    )
    response = app.test_client().post("/preview-stream", data=form)
    assert b"event: complete" in response.data and b"event: delta" not in response.data
