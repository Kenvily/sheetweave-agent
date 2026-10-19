"""Generic provider settings and optional reasoning request fields."""

import io
import json

import pytest

from weekly_report_agent.llm import LLMConfig, OpenAICompatibleClient, load_llm_config


def test_generic_settings_override_legacy_without_exposing_key():
    config = load_llm_config(
        {
            "LLM_API_KEY": "generic-secret",
            "DEEPSEEK_API_KEY": "legacy-secret",
            "LLM_API_BASE": "https://provider.example/v1",
            "DEEPSEEK_API_BASE": "https://legacy.example",
            "LLM_MODEL": "model-a",
            "LLM_ALLOWED_MODELS": "model-a,model-b",
            "DEEPSEEK_MODEL": "legacy",
            "DEEPSEEK_MODELS": "legacy",
            "LLM_REASONING_EFFORT": "low",
        }
    )
    assert config.api_key == "generic-secret"
    assert config.api_base == "https://provider.example/v1"
    assert config.model == "model-a" and config.allowed_models == ("model-a", "model-b")
    assert config.reasoning_effort == "low"
    assert "generic-secret" not in repr(config)
    assert load_llm_config({"LLM_API_KEY": "", "DEEPSEEK_API_KEY": "legacy-secret"}).api_key == ""
    assert load_llm_config({"LLM_REASONING_EFFORT": ""}).reasoning_effort is None


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("effort", [None, "medium"])
def test_optional_reasoning_is_sent_for_both_request_paths(monkeypatch, streaming, effort):
    def open_response(request, *, timeout):
        payload = json.loads(request.data)
        if effort:
            assert payload["reasoning_effort"] == effort
        else:
            assert "reasoning_effort" not in payload
        if streaming:
            return io.BytesIO(
                b'data: {"choices":[{"delta":{"content":"result"},"finish_reason":"stop"}]}\n\n'
            )
        return io.BytesIO(b'{"choices":[{"message":{"content":"result"}}]}')

    monkeypatch.setattr("urllib.request.urlopen", open_response)
    client = OpenAICompatibleClient(LLMConfig(api_key="test-only", reasoning_effort=effort))
    result = "".join(client.stream([], "model")) if streaming else client.complete([], "model")
    assert result == "result"
