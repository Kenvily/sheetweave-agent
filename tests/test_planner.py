from datetime import date

from weekly_report_agent.models import WeeklyData, WeeklyItem
from weekly_report_agent.planner import build_report_plan


def test_builds_numbered_hierarchy_and_groups_projects() -> None:
    data = WeeklyData(
        month="2026年9月",
        week="第1周",
        items=(
            WeeklyItem("项目甲", "事项一", "进行中", "1、结论\n1）指标为72.5%"),
            WeeklyItem("项目甲", "事项二", "完成", "已完成"),
            WeeklyItem("项目乙", "事项三", "进行中", "继续推进"),
        ),
    )

    plan = build_report_plan(data, date(2026, 9, 24))
    texts = [paragraph.text for paragraph in plan.paragraphs]

    assert texts[:3] == ["数据科学产品部周报", "20260924", "9. 大模型产品研发"]
    assert "9.1 项目甲" in texts
    assert "9.1.2 事项二" in texts
    assert "9.2 项目乙" in texts
    assert "（1）指标为72.5%。" in texts
