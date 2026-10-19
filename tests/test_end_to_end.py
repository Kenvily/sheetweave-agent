import zipfile
from pathlib import Path

from weekly_report_agent.cli import main

ROOT = Path(__file__).resolve().parents[1]


def test_cli_generates_report_from_repository_fixtures(tmp_path: Path) -> None:
    output = tmp_path / "weekly.docx"

    exit_code = main(
        [
            "--input",
            str(ROOT / "data/周报-v1.csv"),
            "--template",
            str(ROOT / "data/研发三部周报模板-v2.docx"),
            "--output",
            str(output),
            "--week",
            "第1周",
            "--report-date",
            "2026-09-24",
        ]
    )

    assert exit_code == 0
    with zipfile.ZipFile(output) as package:
        document = package.read("word/document.xml").decode("utf-8")
    for expected in (
        "数据科学产品部周报",
        "20260924",
        "9.1. BaaS数据评估",
        "TabPFN",
        "LightGBM",
        "相关负责人",
        "72.5%",
    ):
        assert expected in document
    assert "强哥" not in document
