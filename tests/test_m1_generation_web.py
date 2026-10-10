import io
import zipfile
from datetime import date
from pathlib import Path

from openpyxl import Workbook

from weekly_report_agent.service import generate_report
from weekly_report_agent.validator import validate_report
from weekly_report_agent.web import DOCX_MIME_TYPE, create_app

ROOT = Path(__file__).resolve().parents[1]
GENERAL_CSV = (
    "项目,本周工作,问题与风险,下周计划,备注\n"
    "项目甲,完成模型验证，AUC 0.81,样本偏少,补充样本,原始备注\n"
    "项目甲,完成 PSI 分析,,继续评估,第二行\n"
).encode()


def _client():
    app = create_app(ROOT / "data/研发三部周报模板.docx")
    app.config["TESTING"] = True
    return app.test_client()


def test_general_preset_generates_configured_sections_with_default_start(tmp_path: Path) -> None:
    source = tmp_path / "general.csv"
    source.write_bytes(GENERAL_CSV)
    output = tmp_path / "general.docx"

    plan = generate_report(
        source,
        ROOT / "data/研发三部周报模板.docx",
        output,
        date(2026, 10, 10),
        preset_id="general",
        section_number=None,
    )

    assert plan.section_number == 1
    texts = [paragraph.text for paragraph in plan.paragraphs]
    assert "1. 本周工作" in texts
    assert "2. 问题与风险" in texts
    assert "3. 下周计划" in texts
    assert "1.1. 项目甲" in texts
    assert any("AUC 0.81" in text for text in texts)
    result = validate_report(
        output,
        section_number=1,
        report_title="通用周报",
        heading_titles=("本周工作", "问题与风险", "下周计划"),
    )
    assert result.heading_count >= 6


def test_web_previews_xlsx_raw_rows_mapping_and_blocks(tmp_path: Path) -> None:
    xlsx = tmp_path / "preview.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "数据"
    worksheet.append(["项目", "本周工作", "问题与风险", "下周计划", "原始备注"])
    worksheet.append(["项目甲", "完成接口联调", "", "发布验证", "必须显示"])
    workbook.save(xlsx)
    workbook.close()

    response = _client().post(
        "/preview",
        data={
            "csv_file": (io.BytesIO(xlsx.read_bytes()), "示例.xlsx"),
            "preset": "general",
            "sheet": "数据",
            "report_date": "2026-10-10",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert "来源表格" in response.text
    assert "内容结构与来源映射" in response.text
    assert "数据!2" in response.text
    assert "f1:数据:2" in response.text
    assert "原始备注" in response.text
    assert "必须显示" in response.text
    assert "原文模式 · 零 LLM" in response.text


def test_web_generates_general_xlsx_docx() -> None:
    stream = io.BytesIO()
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "周报"
    worksheet.append(["项目", "本周工作"])
    worksheet.append(["项目乙", "完成回归验证"])
    workbook.save(stream)
    workbook.close()
    stream.seek(0)

    response = _client().post(
        "/generate",
        data={
            "csv_file": (stream, "通用.xlsx"),
            "preset": "general",
            "sheet": "周报",
            "report_date": "2026-10-10",
            "section_number": "",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.mimetype == DOCX_MIME_TYPE
    with zipfile.ZipFile(io.BytesIO(response.data)) as package:
        document = package.read("word/document.xml").decode()
    assert "通用周报" in document
    assert "1. 本周工作" in document
    assert "1.1. 项目乙" in document


def test_input_options_discovers_xlsx_sheets(tmp_path: Path) -> None:
    xlsx = tmp_path / "options.xlsx"
    workbook = Workbook()
    first = workbook.active
    first.title = "周报一"
    first.append(["项目", "本周工作"])
    first.append(["项目甲", "完成验证"])
    second = workbook.create_sheet("周报二")
    second.append(["项目", "本周工作"])
    second.append(["项目乙", "完成复核"])
    workbook.save(xlsx)
    workbook.close()

    response = _client().post(
        "/input-options",
        data={
            "csv_file": (io.BytesIO(xlsx.read_bytes()), "周报.xlsx"),
            "preset": "general",
            "sheet": "周报二",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["sheets"] == ["周报一", "周报二"]
    assert payload["selected_sheet"] == "周报二"
    assert payload["weeks"] == []
    assert payload["section_default"] == 1
