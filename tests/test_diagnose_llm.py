"""Independent diagnostic controls, native requests, deadlines and content privacy."""

import importlib.util
import io
import json
import os
import time
import urllib.error
from pathlib import Path

import pytest


@pytest.fixture
def tool(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/diagnose_llm.py"
    spec = importlib.util.spec_from_file_location("diagnose_llm", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key, value in {
        "LLM_API_BASE": "https://internal.invalid/v1",
        "LLM_API_KEY": "secret-key-not-for-output",
        "LLM_MODEL": "internal-model",
        "LLM_ALLOWED_MODELS": "internal-model",
        "LLM_TIMEOUT": "60",
        "LLM_MAX_RETRIES": "3",
        "LLM_MAX_BATCH_CHARS": "4000",
    }.items():
        monkeypatch.setenv(key, value)
    return module


def base_args():
    return ["--from-env", "--expect-model", "internal-model"]


def gateway(request, *, timeout):
    assert timeout <= 15
    payload = json.loads(request.data)
    try:
        blocks = json.loads(payload["messages"][-1]["content"])["blocks"]
        text = json.dumps(
            {
                "blocks": [
                    {"block_id": block["block_id"], "text": block["editable_text"]}
                    for block in blocks
                ]
            }
        )
    except (ValueError, KeyError):
        text = '{"ok":true}'
    if payload.get("stream"):
        events = [{"choices": [{"delta": {"reasoning_content": "private-reasoning"}}]}]
        for chunk in [text[: len(text) // 2], text[len(text) // 2 :]]:
            events.append({"choices": [{"delta": {"content": chunk}}]})
        events.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        raw = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
    else:
        raw = json.dumps({"choices": [{"message": {"content": text}}]}).encode()
    response = io.BytesIO(raw)
    response.status = 200
    return response


def test_default_preview_has_no_network(tool, monkeypatch, capsys):
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: pytest.fail("network"))
    assert tool.main(base_args()) == 0
    report = json.loads(capsys.readouterr().out)
    assert not report["executed"] and report["planned_requests"] == 3
    assert all(r["stage"] == "dry_run" for r in report["results"])


def test_six_native_calls_use_same_model_and_redacted_output(tool, monkeypatch, capsys, tmp_path):
    requests = []

    def open_response(request, *, timeout):
        payload = json.loads(request.data)
        assert payload["model"] == "internal-model"
        requests.append((payload, len(request.data)))
        return gateway(request, timeout=timeout)

    monkeypatch.setattr("urllib.request.urlopen", open_response)
    output = tmp_path / "summary.json"
    assert (
        tool.main(
            base_args()
            + ["--transport", "both", "--max-requests", "6", "--execute", "--output", str(output)]
        )
        == 0
    )
    printed = capsys.readouterr().out
    report = json.loads(printed)
    assert len(requests) == len(report["results"]) == 6
    assert all(r["success"] and r["http_calls"] == 1 for r in report["results"])
    assert [r["request_bytes"] for r in report["results"]] == [size for _, size in requests]
    report_payload, compact_payload = requests[2][0], requests[4][0]
    full = json.loads(report_payload["messages"][-1]["content"])["blocks"]
    compact = json.loads(compact_payload["messages"][-1]["content"])["blocks"]
    assert [(b["block_id"], b["editable_text"]) for b in full] == [
        (b["block_id"], b["editable_text"]) for b in compact
    ]
    assert requests[2][1] > requests[4][1]
    assert all(
        "first_content_seconds" in r and r["reasoning_chars"] > 0
        for r in report["results"]
        if r["transport"] == "stream"
    )
    assert all(
        secret not in printed + output.read_text()
        for secret in [
            "secret-key-not-for-output",
            "private-reasoning",
            "完成接口测试",
            "editable_text",
        ]
    )
    assert output.stat().st_mode & 0o777 == 0o600
    assert os.environ["LLM_MAX_RETRIES"] == "3"


@pytest.mark.parametrize("missing", ["LLM_MODEL", "LLM_API_BASE", "LLM_API_KEY"])
def test_missing_explicit_settings_do_not_fallback(tool, monkeypatch, capsys, missing):
    monkeypatch.delenv(missing)
    monkeypatch.delenv(
        {
            "LLM_MODEL": "DEEPSEEK_MODEL",
            "LLM_API_BASE": "DEEPSEEK_API_BASE",
            "LLM_API_KEY": "DEEPSEEK_API_KEY",
        }[missing],
        raising=False,
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: pytest.fail("network"))
    assert tool.main(base_args() + ["--execute"]) == 2
    assert "禁止回退" in capsys.readouterr().out


def test_expected_model_mismatch_stops_before_network(tool, capsys):
    assert tool.main(["--from-env", "--expect-model", "wrong-model", "--execute"]) == 2
    assert "--expect-model" in capsys.readouterr().out


def test_worker_environment_and_proxy_are_used_instead_of_shell(tool, monkeypatch, capsys):
    original = Path.read_bytes
    values = {
        "LLM_API_KEY": "worker-only-secret",
        "LLM_API_BASE": "https://worker.invalid/v1",
        "LLM_MODEL": "worker-model",
        "LLM_ALLOWED_MODELS": "worker-model",
        "HTTPS_PROXY": "http://proxy.invalid:8080",
    }

    def read_bytes(path):
        if str(path) == "/proc/12345/cmdline":
            return b"python\0-m\0sheetweave_agent.task_worker\0"
        if str(path) == "/proc/12345/environ":
            return b"\0".join(f"{k}={v}".encode() for k, v in values.items())
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    seen = []

    def open_response(request, *, timeout):
        seen.append(os.environ["HTTPS_PROXY"])
        assert json.loads(request.data)["model"] == "worker-model"
        return gateway(request, timeout=timeout)

    monkeypatch.setattr("urllib.request.urlopen", open_response)
    assert (
        tool.main(
            [
                "--worker-pid",
                "12345",
                "--expect-model",
                "worker-model",
                "--cases",
                "basic",
                "--execute",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert seen == ["http://proxy.invalid:8080"]
    assert "worker-only-secret" not in output
    assert os.environ["LLM_MODEL"] == "internal-model"


@pytest.mark.parametrize("phase", ["await_headers", "read_body"])
def test_socket_timeouts_preserve_phase_and_disable_retries(tool, monkeypatch, capsys, phase):
    calls = []

    class SlowResponse(io.BytesIO):
        status = 200

        def read(self, *args):
            raise TimeoutError("private provider details")

    def open_response(request, *, timeout):
        calls.append(request)
        if phase == "await_headers":
            raise urllib.error.URLError(TimeoutError("private network address"))
        return SlowResponse()

    monkeypatch.setattr("urllib.request.urlopen", open_response)
    assert tool.main(base_args() + ["--cases", "basic", "--execute"]) == 1
    printed = capsys.readouterr().out
    result = json.loads(printed)["results"][0]
    assert result["stage"] == phase and result["error_kind"] == "socket_timeout"
    assert result["cause_type"] == "TimeoutError"
    assert len(calls) == 1 and "private" not in printed


def test_stream_heartbeats_cannot_bypass_wall_deadline(tool, monkeypatch, capsys):
    class Heartbeats(io.BytesIO):
        status = 200

        def readline(self, *args):
            time.sleep(0.005)
            return b": heartbeat\n\n"

    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: Heartbeats())
    assert (
        tool.main(
            base_args()
            + ["--cases", "basic", "--transport", "stream", "--execute", "--deadline", ".03"]
        )
        == 1
    )
    result = json.loads(capsys.readouterr().out)["results"][0]
    assert result["error_kind"] == "wall_deadline"
    assert result["http_calls"] == 1 and result["total_seconds"] < 0.5


def test_reject_over_budget_and_unapproved_input(tool):
    with pytest.raises(SystemExit):
        tool.main(base_args() + ["--transport", "both", "--execute"])
    with pytest.raises(SystemExit):
        tool.main(base_args() + ["--input", "private.xlsx", "--profile", "p_test", "--execute"])


def test_production_prompt_matches_saved_profile(tool, tmp_path, saved_generation_profiles):
    source = tmp_path / "private.csv"
    source.write_text("项目,本周工作\n合成项目,内容不得出现在诊断输出中。\n")
    args = tool.parser().parse_args(
        base_args() + ["--input", str(source), "--profile", "p_general"]
    )
    values, config = tool.configuration(args)
    messages, metadata = tool.prepare_messages(args, config, tmp_path)
    preset, _, _ = tool.ProfileStore.from_environment().load("p_general")
    prepared, _ = tool.prepare_report(source, preset=preset)
    assert messages["report"] == tool._messages(
        prepared, prepared.blocks, tool.ContentMode.POLISH, None
    )
    assert metadata["batch_blocks"] == len(prepared.blocks)


def test_env_file_overrides_stale_terminal_model(tool, tmp_path, capsys):
    env = tmp_path / "worker.env"
    env.write_text(
        "LLM_API_BASE=https://company.invalid/v1\nLLM_API_KEY=file-secret\n"
        "LLM_MODEL=file-model\nLLM_ALLOWED_MODELS=file-model\n"
    )
    assert tool.main(["--env-file", str(env), "--expect-model", "file-model"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["model"] == "file-model"
    assert "file-secret" not in output


def test_total_budget_stops_before_second_http_call(tool, monkeypatch, capsys):
    calls = []

    class SlowHeaders:
        def __call__(self, request, *, timeout):
            calls.append(request)
            time.sleep(0.1)
            return gateway(request, timeout=timeout)

    monkeypatch.setattr("urllib.request.urlopen", SlowHeaders())
    assert tool.main(base_args() + ["--execute", "--total-timeout", ".03"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert len(calls) == 1
    assert report["results"][0]["error_kind"] == "wall_deadline"
    assert all(r["stage"] == "skipped_total_budget" for r in report["results"][1:])
    assert any("未发送" in line for line in report["analysis"])


def test_http_error_status_and_message_privacy(tool, monkeypatch, capsys):
    def rejected(request, *, timeout):
        raise urllib.error.HTTPError(
            "https://private.invalid", 503, "private backend response", {}, None
        )

    monkeypatch.setattr("urllib.request.urlopen", rejected)
    assert tool.main(base_args() + ["--cases", "basic", "--execute"]) == 1
    output = capsys.readouterr().out
    result = json.loads(output)["results"][0]
    assert result["http_status"] == 503 and result["http_calls"] == 1
    assert result["error_kind"] == "http_error"
    assert "private" not in output


def test_invalid_response_retains_only_exception_type(tool, monkeypatch, capsys):
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: io.BytesIO(b"private-invalid"))
    assert tool.main(base_args() + ["--cases", "basic", "--execute"]) == 1
    output = capsys.readouterr().out
    result = json.loads(output)["results"][0]
    assert result["stage"] == "decode_response"
    assert result["cause_type"] == "JSONDecodeError"
    assert "private-invalid" not in output
