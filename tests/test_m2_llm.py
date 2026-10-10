import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from weekly_report_agent.llm import (
    ContentMode,
    LLMConfig,
    LLMConfigurationError,
    LLMResponseError,
    LLMService,
    load_llm_config,
)
from weekly_report_agent.service import prepare_report, process_report


class FakeClient:
    def __init__(self, *, suffix: str = "，表述已整理", response: str | None = None) -> None:
        self.suffix = suffix
        self.response = response
        self.calls: list[tuple[Sequence[dict[str, str]], str]] = []

    def complete(self, messages: Sequence[dict[str, str]], model: str) -> str:
        self.calls.append((messages, model))
        if self.response is not None:
            return self.response
        request = json.loads(messages[-1]["content"])
        return json.dumps(
            {
                "blocks": [
                    {"block_id": block["block_id"], "text": block["text"] + self.suffix}
                    for block in request["blocks"]
                ]
            },
            ensure_ascii=False,
        )


def _prepared(tmp_path: Path):
    source = tmp_path / "weekly.csv"
    source.write_text(
        "项目,本周工作,问题与风险,下周计划\n"
        "模型平台,完成 TabPFN2.5 验证，AUC 0.81,进行中,2026年10月12日复核\n"
        "模型平台,完成 PSI 分析,,,\n",
        encoding="utf-8",
    )
    return prepare_report(source, preset_id="general")[0]


def _service(client: FakeClient, **overrides) -> LLMService:
    values = {
        "api_key": "test-only-key",
        "model": "deepseek-chat",
        "allowed_models": ("deepseek-chat", "deepseek-reasoner"),
        "max_batch_chars": 12000,
    }
    values.update(overrides)
    return LLMService(LLMConfig(**values), client)


def test_original_mode_is_offline_and_requires_no_key(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    client = FakeClient()
    service = LLMService(LLMConfig(api_key=""), client)

    result = process_report(prepared, ContentMode.ORIGINAL, llm_service=service)

    assert client.calls == []
    assert all(block.generated_text is None for block in result.blocks)
    assert all(block.mode == "original" for block in result.blocks)


@pytest.mark.parametrize("mode", ["polish", "concise", "expand"])
def test_llm_modes_preserve_ids_sources_and_facts(tmp_path: Path, mode: str) -> None:
    prepared = _prepared(tmp_path)
    client = FakeClient()

    result = process_report(prepared, mode, llm_service=_service(client))

    assert len(client.calls) == 1
    assert [block.block_id for block in result.blocks] == [
        block.block_id for block in prepared.blocks
    ]
    assert [block.source_ids for block in result.blocks] == [
        block.source_ids for block in prepared.blocks
    ]
    assert all(block.generated_text.endswith("，表述已整理") for block in result.blocks)
    assert all(block.mode == mode for block in result.blocks)


def test_custom_mode_passes_instruction_and_requires_it(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    client = FakeClient()
    service = _service(client)

    result = service.process(prepared, "custom", custom_instruction="按结论和动作整理")

    request = json.loads(client.calls[0][0][-1]["content"])
    assert request["instruction"] == "按结论和动作整理"
    assert all(block.mode == "custom" for block in result.blocks)

    with pytest.raises(ValueError, match="必须填写处理要求"):
        service.process(prepared, "custom")


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ("not json", "有效 JSON"),
        ('{"blocks":[]}', "遗漏"),
        (
            '{"blocks":[{"block_id":"unknown","text":"未改变"}]}',
            "未知块",
        ),
    ],
)
def test_invalid_structured_responses_are_rejected(
    tmp_path: Path, response: str, message: str
) -> None:
    prepared = _prepared(tmp_path)

    with pytest.raises(LLMResponseError, match=message):
        _service(FakeClient(response=response)).process(prepared, "polish")

    assert all(block.generated_text is None for block in prepared.blocks)


def test_fact_changes_are_rejected_without_partial_update(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    payload = {
        "blocks": [
            {
                "block_id": block.block_id,
                "text": block.original_text.replace("0.81", "0.91"),
            }
            for block in prepared.blocks
        ]
    }

    with pytest.raises(LLMResponseError, match="数字或指标"):
        _service(FakeClient(response=json.dumps(payload))).process(prepared, "polish")

    assert all(block.generated_text is None for block in prepared.blocks)


def test_model_allowlist_and_missing_key_are_enforced(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)

    with pytest.raises(LLMConfigurationError, match="API Key"):
        LLMService(LLMConfig(api_key=""), FakeClient()).process(prepared, "polish")
    with pytest.raises(LLMConfigurationError, match="允许列表"):
        _service(FakeClient()).process(prepared, "polish", model="unknown-model")


def test_regenerate_changes_only_selected_block_and_protects_edits(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    service = _service(FakeClient())
    target = prepared.blocks[0]

    result = service.regenerate_block(prepared, target.block_id, "polish")

    assert result.blocks[0].generated_text is not None
    assert result.blocks[1:] == prepared.blocks[1:]
    edited = replace(
        prepared, blocks=(replace(target, edited_text="人工版本"), *prepared.blocks[1:])
    )
    with pytest.raises(ValueError, match="人工修改"):
        service.regenerate_block(edited, target.block_id, "polish")


def test_environment_config_is_validated_without_exposing_key() -> None:
    config = load_llm_config(
        {
            "DEEPSEEK_API_KEY": "secret-value",
            "DEEPSEEK_API_BASE": "https://api.deepseek.com/",
            "DEEPSEEK_MODEL": "deepseek-reasoner",
            "DEEPSEEK_MODELS": "deepseek-chat,deepseek-reasoner",
            "LLM_TIMEOUT": "30",
        }
    )

    assert config.api_base == "https://api.deepseek.com"
    assert config.model == "deepseek-reasoner"
    assert config.timeout == 30
    assert "secret-value" not in repr(config)
    assert "secret-value" not in repr(config.select_model("deepseek-chat"))

    with pytest.raises(LLMConfigurationError, match="HTTP/HTTPS"):
        load_llm_config({"DEEPSEEK_API_BASE": "file:///tmp/model", "DEEPSEEK_API_KEY": "x"})
