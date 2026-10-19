"""Keep semantic organization in the model and deterministic formatting in the renderer."""

import csv
import json
from datetime import date
from pathlib import Path

import pytest

from weekly_report_agent.llm import LLMConfig, LLMService
from weekly_report_agent.llm.service import LLMResponseError, _decode_response
from weekly_report_agent.models import ParagraphKind
from weekly_report_agent.planner import build_body_paragraphs
from weekly_report_agent.rules import load_rule_preset, preset_payload, validate_rule_preset
from weekly_report_agent.service import prepare_report, render_prepared_report

DAY = date(2026, 10, 19)


def test_duplicate_bullets_are_removed_without_rewriting_paths_or_numbers():
    body = build_body_paragraphs(
        "1、样本管理：\n（1）• 原始路径：work/003-样本；\n"
        "（2）▪ 分析路径：work/team/eval_data，4批样本。",
        DAY,
    )
    assert [p.text for p in body] == [
        "1、样本管理：",
        "（1）原始路径：work/003-样本。",
        "（2）分析路径：work/team/eval_data，4批样本。",
    ]


def test_plain_lines_do_not_invent_a_wrapper_or_inherit_an_orphan_colon():
    paragraphs = build_body_paragraphs("整理实验报告：\n完成培训材料 v2；\n陈述时间90分钟。", DAY)
    assert [p.kind for p in paragraphs] == [ParagraphKind.BODY_1] * 3
    assert [p.text for p in paragraphs] == [
        "1、整理实验报告。",
        "2、完成培训材料 v2。",
        "3、陈述时间90分钟。",
    ]


def test_explicit_siblings_after_a_colon_are_not_silently_demoted():
    body = build_body_paragraphs("1、样本管理\n（1）历史样本：\n（2）新样本路径为work/new；", DAY)
    assert [p.kind for p in body] == [
        ParagraphKind.BODY_1,
        ParagraphKind.BODY_2,
        ParagraphKind.BODY_2,
    ]
    assert body[-1].text == "（2）新样本路径为work/new。"


def test_only_real_bullet_children_use_a_colon_and_numeric_prefixes_are_preserved():
    body = build_body_paragraphs(
        "样本包括：\n• 路径work/003-old\n• 指标变化为-0.2%\n1.5倍耗时。", DAY
    )
    assert [p.kind for p in body] == [
        ParagraphKind.BODY_1,
        ParagraphKind.BODY_2,
        ParagraphKind.BODY_2,
        ParagraphKind.BODY_1,
    ]
    assert body[-1].text == "2、1.5倍耗时。"
    assert "-0.2%" in body[-2].text


@pytest.mark.parametrize(
    "paragraphs",
    [
        [{"level": 2, "text": "孤立子项"}],
        [{"level": 1, "text": "父项"}, {"level": 3, "text": "跳级"}],
        [{"level": True, "text": "无效级别"}],
        [{"level": 1, "text": "•"}],
    ],
)
def test_invalid_model_body_structure_is_rejected(paragraphs):
    with pytest.raises(LLMResponseError):
        _decode_response(
            json.dumps({"blocks": [{"block_id": "b1", "paragraphs": paragraphs}]}), ("b1",)
        )


def test_model_restructures_merged_rows_with_context_and_same_docx_html_plan(tmp_path):
    source = tmp_path / "synthetic.csv"
    training = "第一阶段完成57%，预计还需7天。\n全部训练预计还需2周。"
    material = "完成v2培训材料梳理，陈述时间90分钟。"
    with source.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerows(
            [
                ["项目", "事项", "本周工作"],
                ["合成项目", "模型实验", training],
                ["合成项目", "培训材料", material],
            ]
        )
    rule = preset_payload(load_rule_preset("general"))
    rule["input"]["column_aliases"]["detail"] = ["事项"]
    rule["sections"] = [
        {
            "id": "work",
            "title": "研发",
            "source_field": "content",
            "heading_path": ["研发"],
            "group_by": "section",
            "llm_editable": True,
        }
    ]
    preset = validate_rule_preset(rule)
    prepared, _ = prepare_report(source, preset=preset)
    assert len(prepared.blocks) == 1 and len(prepared.blocks[0].source_ids) == 2

    class SemanticClient:
        def complete(self, messages, model):
            self.payload = json.loads(messages[-1]["content"])
            self.system = messages[0]["content"]
            return json.dumps(
                {
                    "blocks": [
                        {
                            "block_id": prepared.blocks[0].block_id,
                            "paragraphs": [
                                {"level": 1, "text": training.replace("\n", "")},
                                {"level": 1, "text": material},
                            ],
                        }
                    ]
                }
            )

    client = SemanticClient()
    result = LLMService(LLMConfig(api_key="synthetic-only"), client).process(prepared, "polish")
    assert result.blocks[0].effective_version == "generated"
    assert result.blocks[0].validation_level == "pass"
    units = client.payload["blocks"][0]["source_units"]
    assert [unit["context"]["detail"] for unit in units] == ["模型实验", "培训材料"]
    assert "不要求保持段落数量" in client.payload["mode_contract"]["organization"]
    template = Path("data/研发三部周报模板-v2.docx")
    plan = render_prepared_report(result, preset, template, tmp_path / "structured.docx", DAY)
    paragraphs = [p for p in plan.paragraphs if p.kind.value.startswith("body")]
    assert len(paragraphs) == 2 and all(p.kind is ParagraphKind.BODY_1 for p in paragraphs)
    assert paragraphs[1].text == "2、" + material
    assert "进展内容" not in "\n".join(p.text for p in paragraphs)


def test_empty_inline_list_prefix_does_not_create_a_generic_parent():
    paragraphs = build_body_paragraphs("1、：1.完成验证。2.同步结果。", DAY)
    assert [p.text for p in paragraphs] == ["1、完成验证。", "2、同步结果。"]


def test_orphan_numbered_items_remain_siblings_without_a_fabricated_parent():
    paragraphs = build_body_paragraphs("（1）完成验证；\n（2）整理培训材料。", DAY)
    assert [p.text for p in paragraphs] == ["1、完成验证。", "2、整理培训材料。"]
    assert all(p.kind is ParagraphKind.BODY_1 for p in paragraphs)
