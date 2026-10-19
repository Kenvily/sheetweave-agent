import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from threading import Barrier

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
                    {"block_id": block["block_id"], "text": block["editable_text"] + self.suffix}
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
    editable = [block for block in result.blocks if block.llm_editable]
    readonly = [block for block in result.blocks if not block.llm_editable]
    assert all(block.generated_text.endswith("，表述已整理") for block in editable)
    assert all(block.generated_text is None for block in readonly)
    assert all(block.mode == mode for block in result.blocks)


def test_preset_modes_send_distinct_writing_contracts_and_length_targets(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path)
    contracts = {}
    targets = {}

    for mode in ("polish", "concise", "expand"):
        client = FakeClient()
        _service(client).process(prepared, mode)
        request = json.loads(client.calls[0][0][-1]["content"])
        contracts[mode] = request["mode_contract"]
        targets[mode] = request["blocks"][0]["length_target"]

    assert "85%～115%" in contracts["polish"]["length_rule"]
    assert "50%～75%" in contracts["concise"]["length_rule"]
    assert "120%～160%" in contracts["expand"]["length_rule"]
    assert contracts["polish"]["organization"] != contracts["concise"]["organization"]
    assert contracts["concise"]["required_actions"] != contracts["expand"]["required_actions"]
    assert targets["concise"]["maximum_chars"] < targets["polish"]["minimum_chars"]
    assert targets["expand"]["minimum_chars"] > targets["polish"]["maximum_chars"]


def test_custom_mode_passes_instruction_and_requires_it(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    client = FakeClient()
    service = _service(client)

    result = service.process(prepared, "custom", custom_instruction="按结论和动作整理")

    request = json.loads(client.calls[0][0][-1]["content"])
    assert request["mode_contract"]["custom_instruction"] == "按结论和动作整理"
    assert "length_target" not in request["blocks"][0]
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


def test_fact_conflict_is_retained_for_review_and_falls_back_per_block(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    block = next(item for item in prepared.blocks if item.llm_editable)
    unsafe = block.original_text.replace("0.81", "0.91")
    payload = {"blocks": [{"block_id": block.block_id, "text": unsafe}]}
    client = FakeClient(response=json.dumps(payload))

    result = _service(client, auto_correct=True).process(prepared, "polish")
    checked = result.block_by_id(block.block_id)

    assert len(client.calls) == 2  # one normal call and at most one targeted correction
    assert checked.generated_text == unsafe
    assert checked.validation_level == "conflict"
    assert checked.effective_text == checked.original_text
    assert any(
        "AUC 0.81" in warning and "AUC 0.91" in warning for warning in checked.validation_warnings
    )
    assert all(item.generated_text is None for item in result.blocks if not item.llm_editable)


def test_default_processing_does_not_request_second_pass_correction(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    block = prepared.blocks[0]
    unsafe = block.original_text.replace("0.81", "0.91")
    client = FakeClient(
        response=json.dumps({"blocks": [{"block_id": block.block_id, "text": unsafe}]})
    )
    result = _service(client).process(prepared, "polish")
    assert len(client.calls) == 1
    assert result.blocks[0].validation_level == "conflict"
    assert result.blocks[0].effective_text == block.original_text


def test_batches_run_concurrently_and_keep_block_identity(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    block = prepared.blocks[0]
    prepared = replace(prepared, blocks=(block, replace(block, block_id="second-block")))
    barrier = Barrier(2)

    class ParallelClient(FakeClient):
        def complete(self, messages, model):
            barrier.wait(timeout=3)
            return super().complete(messages, model)

    client = ParallelClient()
    result = _service(client, max_batch_chars=1, max_concurrency=2).process(prepared, "polish")
    assert len(client.calls) == 2
    assert tuple(item.block_id for item in result.blocks) == (block.block_id, "second-block")
    assert all(item.validation_level == "pass" for item in result.blocks)


def test_equivalent_numeric_formatting_and_list_renumbering_are_accepted(tmp_path: Path) -> None:
    source = tmp_path / "numeric.csv"
    source.write_text(
        "项目,本周工作\n"
        '模型平台,"1、样本量不超过10000；2、AUC提升0.3-0.5pp；3、KS2个点；4、命中率72.5%"\n',
        encoding="utf-8",
    )
    prepared = prepare_report(source, preset_id="general")[0]
    block = prepared.blocks[0]
    generated = "样本量<=1万，AUC提升0.3pp至0.5pp，KS 2 个点，命中率72.5％。"
    payload = {"blocks": [{"block_id": block.block_id, "text": generated}]}

    result = _service(FakeClient(response=json.dumps(payload, ensure_ascii=False))).process(
        prepared, "polish"
    )

    assert result.blocks[0].generated_text == generated


def test_business_number_remains_protected_when_list_numbers_change(tmp_path: Path) -> None:
    source = tmp_path / "numeric.csv"
    source.write_text('项目,本周工作\n模型平台,"1、AUC 0.81；2、完成验证"\n', encoding="utf-8")
    prepared = prepare_report(source, preset_id="general")[0]
    block = prepared.blocks[0]
    payload = {"blocks": [{"block_id": block.block_id, "text": "AUC 0.91，完成验证。"}]}

    result = _service(FakeClient(response=json.dumps(payload, ensure_ascii=False))).process(
        prepared, "polish"
    )

    assert result.blocks[0].validation_level == "conflict"
    assert result.blocks[0].effective_text == block.original_text


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


def test_realistic_summary_can_merge_duplicate_metrics_and_normalize_units(
    tmp_path: Path,
) -> None:
    source = tmp_path / "numeric-summary.csv"
    source.write_text(
        "项目,本周工作\n"
        '模型平台,"对方用lgb充分搜参且五折交叉验证；5千10%的浓度下效果稳定；'
        "5千以上效果不及lgb；同样的变量与tabpfn相关性0.9；"
        '百擎的结果与tabpfn的结果相关性0.9"\n',
        encoding="utf-8",
    )
    prepared = prepare_report(source, preset_id="general")[0]
    block = prepared.blocks[0]
    generated = (
        "对方使用LightGBM充分搜参并采用5折交叉验证；5000样本、10%浓度下效果稳定；"
        "至少5000样本时效果不及LightGBM；两项与TabPFN的相关性均为0.9。"
    )
    payload = {"blocks": [{"block_id": block.block_id, "text": generated}]}

    result = _service(FakeClient(response=json.dumps(payload, ensure_ascii=False))).process(
        prepared, "concise"
    )

    assert result.blocks[0].generated_text == generated


def test_aggregation_count_is_not_ignored_without_duplicate_source_fact(tmp_path: Path) -> None:
    source = tmp_path / "numeric-summary.csv"
    source.write_text("项目,本周工作\n模型平台,相关性0.9\n", encoding="utf-8")
    prepared = prepare_report(source, preset_id="general")[0]
    block = prepared.blocks[0]
    payload = {"blocks": [{"block_id": block.block_id, "text": "两项相关性均为0.9。"}]}

    result = _service(FakeClient(response=json.dumps(payload, ensure_ascii=False))).process(
        prepared, "concise"
    )

    assert result.blocks[0].validation_level == "conflict"
    assert result.blocks[0].effective_text == block.original_text
    assert any("新增数量：2项" in warning for warning in result.blocks[0].validation_warnings)


def test_prompt_exposes_only_editable_text_and_readonly_context(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    client = FakeClient()

    _service(client).process(prepared, "polish")

    request = json.loads(client.calls[0][0][-1]["content"])
    assert len(request["blocks"]) == 1
    payload = request["blocks"][0]
    assert set(payload) == {
        "block_id",
        "editable_text",
        "immutable_context",
        "protected_facts",
        "length_target",
        "primary_body",
    }
    assert payload["primary_body"] == [
        row.fields["content"] for row in prepared.sources_for_block(prepared.blocks[0].block_id)
    ]
    assert set(payload["length_target"]) == {
        "source_chars",
        "minimum_chars",
        "maximum_chars",
    }
    assert "text" not in payload
    assert payload["editable_text"] == prepared.blocks[0].original_text
    assert payload["immutable_context"] == [{"project": "模型平台"}, {"project": "模型平台"}]


def test_model_version_spacing_and_completed_status_are_equivalent(tmp_path: Path) -> None:
    source = tmp_path / "model-version.csv"
    source.write_text(
        "项目,本周工作\n"
        '模型平台,"1、Tabm自动化搜参建模代码封装打包，部署代码以及验证样本已同步强哥。'
        "2、tabicl以及tabpfn3.0百擎部署代码已经完成，部署代码以及验证样本已同步强哥。"
        'tabpfn 3.0本地工程优化调试进行中"\n',
        encoding="utf-8",
    )
    prepared = prepare_report(source, preset_id="general")[0]
    block = prepared.blocks[0]
    generated = (
        "TabM自动化搜参建模代码已完成封装打包，部署代码及验证样本已同步相关负责人；"
        "TabICL及TabPFN 3.0百擎部署代码已完成，部署代码及验证样本已同步相关负责人，"
        "TabPFN3.0本地工程优化调试进行中。"
    )
    payload = {"blocks": [{"block_id": block.block_id, "text": generated}]}

    result = _service(FakeClient(response=json.dumps(payload, ensure_ascii=False))).process(
        prepared, "polish"
    )

    assert result.blocks[0].generated_text == generated


@pytest.mark.parametrize(
    "generated",
    (
        "TabM与TabICL工作已完成，TabPFN 3.1本地工程优化调试进行中。",
        "TabM与TabICL工作已完成，本地工程优化调试进行中。",
    ),
)
def test_model_version_change_or_removal_is_rejected(tmp_path: Path, generated: str) -> None:
    source = tmp_path / "model-version.csv"
    source.write_text(
        "项目,本周工作\n模型平台,TabM与TabICL工作已经完成，TabPFN3.0本地工程优化调试进行中\n",
        encoding="utf-8",
    )
    prepared = prepare_report(source, preset_id="general")[0]
    block = prepared.blocks[0]
    payload = {"blocks": [{"block_id": block.block_id, "text": generated}]}

    result = _service(FakeClient(response=json.dumps(payload, ensure_ascii=False))).process(
        prepared, "polish"
    )

    assert result.blocks[0].validation_level == "conflict"
    assert result.blocks[0].effective_text == block.original_text
    assert any("模型与版本" in warning for warning in result.blocks[0].validation_warnings)
