import io
import zipfile
from datetime import date
from pathlib import Path

from weekly_report_agent.web import DOCX_MIME_TYPE, create_app

ROOT = Path(__file__).resolve().parents[1]


def _client():
    app = create_app(ROOT / "data/研发三部周报模板.docx")
    app.config["TESTING"] = True
    return app.test_client()


def test_web_page_is_simple_and_has_rule_parameters() -> None:
    response = _client().get("/")

    assert response.status_code == 200
    assert "研发周报生成器" in response.text
    assert 'name="csv_file"' in response.text
    assert 'name="section_number"' in response.text
    assert "DeepSeek" not in response.text
    assert "API Key" not in response.text


def test_web_generates_download_with_custom_section_number() -> None:
    response = _client().post(
        "/generate",
        data={
            "csv_file": (io.BytesIO((ROOT / "data/周报.csv").read_bytes()), "周报.csv"),
            "week": "第1周",
            "report_date": "2026-09-24",
            "section_number": "12",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.mimetype == DOCX_MIME_TYPE
    assert "20260924" in response.headers["Content-Disposition"]
    with zipfile.ZipFile(io.BytesIO(response.data)) as package:
        document = package.read("word/document.xml").decode("utf-8")
    assert "12. 大模型产品研发" in document
    assert "12.1. BaaS数据评估" in document
    assert "72.5%" in document
    assert "DeepSeek" not in document


def test_web_rejects_invalid_upload() -> None:
    response = _client().post(
        "/generate",
        data={
            "csv_file": (io.BytesIO(b"invalid"), "weekly.txt"),
            "report_date": date(2026, 9, 24).isoformat(),
            "section_number": "9",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert "必须是 .csv 文件" in response.text


def test_web_rejects_invalid_section_number() -> None:
    response = _client().post(
        "/generate",
        data={
            "csv_file": (io.BytesIO((ROOT / "data/周报.csv").read_bytes()), "周报.csv"),
            "report_date": "2026-09-24",
            "section_number": "0",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert "起始章节号必须在 1～999 之间" in response.text
