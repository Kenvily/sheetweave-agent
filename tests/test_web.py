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


def test_get_only_form_targets_redirect_to_home() -> None:
    client = _client()

    for path in ("/preview", "/generate"):
        response = client.get(path)
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/")


def test_root_post_compatibly_generates_instead_of_returning_405() -> None:
    response = _client().post(
        "/",
        data={
            "csv_file": (io.BytesIO((ROOT / "data/周报.csv").read_bytes()), "周报.csv"),
            "week": "第1周",
            "report_date": "2026-09-24",
            "section_number": "9",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.mimetype == DOCX_MIME_TYPE


def test_web_page_is_simple_and_has_rule_parameters() -> None:
    response = _client().get("/")

    assert response.status_code == 200
    assert "研发周报生成器" in response.text
    assert 'name="csv_file"' in response.text
    assert 'name="section_number"' in response.text
    assert 'name="content_mode"' in response.text
    assert 'name="api_key"' not in response.text.lower()
    assert "规范润色（等长改写）" in response.text
    assert "精简总结（压缩去重）" in response.text
    assert "详细整理（结构扩写）" in response.text
    assert '<option value="original" selected>' in response.text
    assert 'id="custom-instruction-field" class="field" hidden' in response.text
    assert 'contentMode.addEventListener("change", updateCustomInstruction)' in response.text
    assert "数据处理说明" not in response.text
    assert 'name="allow_external"' not in response.text


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


def test_web_week_and_sheet_are_selects_and_section_is_text_input() -> None:
    response = _client().get("/")

    assert response.status_code == 200
    for name in ("week", "sheet"):
        assert f'<select name="{name}">' in response.text
    assert '<input type="text" name="section_number"' in response.text
    assert 'formaction="/generate"' in response.text
    advanced = response.text.split("<details>", 1)[1].split("</details>", 1)[0]
    assert "高级适配设置" in advanced
    assert 'name="section_number"' not in advanced
    assert "自定义模板（可选）" in advanced


def test_input_options_discovers_rd3_weeks() -> None:
    response = _client().post(
        "/input-options",
        data={
            "csv_file": (io.BytesIO((ROOT / "data/周报.csv").read_bytes()), "周报.csv"),
            "preset": "rd3",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["weeks"]
    assert payload["default_week"] in payload["weeks"]
    assert payload["sheets"] == []
    assert payload["section_default"] == 9


def test_input_options_returns_json_error_for_invalid_file() -> None:
    response = _client().post(
        "/input-options",
        data={"csv_file": (io.BytesIO(b"invalid"), "weekly.txt"), "preset": "rd3"},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert response.is_json
    assert "必须是 .csv 文件" in response.get_json()["error"]
