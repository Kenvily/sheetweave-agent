import json
from pathlib import Path

import pytest

from weekly_report_agent.rules import RulePresetError, load_rule_preset, validate_rule_preset
from weekly_report_agent.service import prepare_report


def _general_payload() -> dict:
    return {
        "id": "safe",
        "name": "安全规则",
        "schema_version": 1,
        "input": {
            "layout": "records",
            "header_rows": 1,
            "required_fields": ["project", "content"],
            "column_aliases": {
                "project": ["项目"],
                "content": ["本周工作"],
            },
        },
        "sections": [
            {
                "id": "weekly_work",
                "title": "本周工作",
                "source_field": "content",
                "group_by": "project",
            }
        ],
        "document": {
            "report_title": "周报",
            "template": "safe.docx",
            "heading_start_default": 1,
        },
    }


def test_rule_validation_rejects_bad_json_unknown_fields_and_unsafe_templates(
    tmp_path: Path,
) -> None:
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    with pytest.raises(RulePresetError, match="规则 JSON 无效"):
        load_rule_preset("broken", tmp_path)

    payload = _general_payload()
    payload["document"]["template"] = "../outside.docx"
    with pytest.raises(RulePresetError, match="受控目录"):
        validate_rule_preset(payload)

    payload = _general_payload()
    payload["sections"][0]["source_field"] = "missing"
    with pytest.raises(RulePresetError, match="未知字段"):
        validate_rule_preset(payload)

    payload = _general_payload()
    (tmp_path / "different.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RulePresetError, match="文件名与内部 id"):
        load_rule_preset("different", tmp_path)


def test_traceability_supports_one_source_to_many_blocks_and_many_sources_to_one_block(
    tmp_path: Path,
) -> None:
    source = tmp_path / "general.csv"
    source.write_text(
        "项目,本周工作,问题与风险,下周计划\n"
        "项目甲,完成模型训练,样本偏少,补充样本\n"
        "项目甲,完成指标验证,,继续评估\n",
        encoding="utf-8",
    )

    prepared, preset = prepare_report(source, preset_id="general")

    assert preset.id == "general"
    assert prepared.blocks_for_source("f1:CSV:2")
    assert {block.section_id for block in prepared.blocks_for_source("f1:CSV:2")} == {
        "weekly_work",
        "issues",
        "next_plan",
    }
    weekly = next(block for block in prepared.blocks if block.section_id == "weekly_work")
    assert weekly.source_ids == ("f1:CSV:2", "f1:CSV:3")
    assert [source.row_number for source in prepared.sources_for_block(weekly.block_id)] == [2, 3]
    assert weekly.generated_text is None
    assert weekly.edited_text is None
    assert weekly.mode == "original"
    assert weekly.effective_text == "完成模型训练\n完成指标验证"
