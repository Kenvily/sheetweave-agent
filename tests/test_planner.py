from datetime import date

import pytest

from sheetweave_agent.models import ParagraphKind, WeeklyData, WeeklyItem
from sheetweave_agent.planner import build_report_plan


def test_builds_complete_numbered_hierarchy_and_groups_projects() -> None:
    data = WeeklyData(
        month="2026年9月",
        week="第1周",
        items=(
            WeeklyItem(
                "项目甲",
                "事项一",
                "进行中",
                "1、结论\n1）指标分析：\n①指标为72.5%",
            ),
            WeeklyItem("项目甲", "事项二", "完成", "已完成"),
            WeeklyItem("项目乙", "事项三", "进行中", "继续推进"),
        ),
    )

    plan = build_report_plan(data, date(2026, 9, 24))
    texts = [paragraph.text for paragraph in plan.paragraphs]

    assert texts[:3] == ["数据科学产品部周报", "20260924", "9. 大模型产品研发"]
    assert "9.1. 项目甲" in texts
    assert "9.1.2. 事项二" in texts
    assert "9.2. 项目乙" in texts
    assert "1、结论：" in texts
    assert "（1）指标分析：" in texts
    assert "①指标为72.5%。" in texts


def test_uses_configurable_section_number_without_synthetic_body_wrapper() -> None:
    data = WeeklyData(
        month="2026年9月",
        week="第1周",
        items=(WeeklyItem("项目甲", "事项", "进行中", "本周一同步强哥"),),
    )

    plan = build_report_plan(data, date(2026, 9, 24), section_number=12)
    paragraphs = {paragraph.text: paragraph.kind for paragraph in plan.paragraphs}

    assert "12. 大模型产品研发" in paragraphs
    assert "12.1. 项目甲" in paragraphs
    assert "12.1.1. 事项" in paragraphs
    assert "1、进展内容：" not in paragraphs
    assert paragraphs["1、2026年9月21日同步相关负责人。"] is ParagraphKind.BODY_1


def test_splits_inline_numbered_items_without_splitting_decimals() -> None:
    data = WeeklyData(
        month="2026年9月",
        week="第1周",
        items=(
            WeeklyItem(
                "项目甲",
                "事项",
                "进行中",
                "1、完成回访，1.效果为0.9。2.版本为TabPFN2.5。",
            ),
        ),
    )

    plan = build_report_plan(data, date(2026, 9, 24))
    texts = [paragraph.text for paragraph in plan.paragraphs]

    assert "1、完成回访：" in texts
    assert "（1）效果为0.9。" in texts
    assert "（2）版本为TabPFN2.5。" in texts


def test_rejects_invalid_section_number() -> None:
    data = WeeklyData("2026年9月", "第1周", ())

    with pytest.raises(ValueError, match="起始章节号"):
        build_report_plan(data, date(2026, 9, 24), section_number=0)
