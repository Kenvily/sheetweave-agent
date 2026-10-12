import io
import json
import urllib.error
from concurrent.futures import ThreadPoolExecutor

import pytest

from weekly_report_agent.llm import LLMConfig, OpenAICompatibleClient
from weekly_report_agent.timing import TimingRecorder


def test_timing_keeps_failed_events_and_returns_detached_snapshots() -> None:
    recorder = TimingRecorder()
    with pytest.raises(ValueError), recorder.measure("stage", blocks=3):
        raise ValueError("private input must not be stored")
    events = recorder.snapshot()
    assert events[0]["success"] is False
    assert events[0]["blocks"] == 3
    assert events[0]["seconds"] >= 0
    events[0]["stage"] = "changed"
    assert recorder.snapshot()[0]["stage"] == "stage"
    assert "private input" not in json.dumps(recorder.snapshot())


def test_timing_counts_all_concurrent_stages() -> None:
    recorder = TimingRecorder()

    def record(index: int) -> None:
        with recorder.measure("batch", index=index):
            pass

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(record, range(24)))
    events = recorder.snapshot()
    assert len(events) == 24
    assert {event["index"] for event in events} == set(range(24))


def test_http_retry_timing_counts_attempts_without_recording_content(monkeypatch) -> None:
    recorder = TimingRecorder()
    calls = 0

    def urlopen(_request, *, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.URLError("private provider error")
        return io.BytesIO(
            json.dumps({"choices": [{"message": {"content": "private response"}}]}).encode()
        )

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr("weekly_report_agent.llm.client.time.sleep", lambda _delay: None)
    client = OpenAICompatibleClient(LLMConfig(api_key="private-secret"), timings=recorder)
    assert client.complete([{"role": "user", "content": "private prompt"}], "model") == (
        "private response"
    )
    events = recorder.snapshot()
    assert [(event["attempt"], event["success"]) for event in events] == [(1, False), (2, True)]
    assert all(event["stage"] == "http_attempt" for event in events)
    assert "private" not in json.dumps(events)
