import io
import json
import zipfile
from collections.abc import Sequence
from pathlib import Path

from weekly_report_agent.llm import LLMConfig, LLMService
from weekly_report_agent.web import DOCX_MIME_TYPE, create_app

ROOT = Path(__file__).resolve().parents[1]
CSV_CONTENT = "项目,本周工作\n项目甲,完成 AUC 0.81 验证\n".encode()


class WebFakeClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages: Sequence[dict[str, str]], model: str) -> str:
        self.calls += 1
        request = json.loads(messages[-1]["content"])
        return json.dumps(
            {
                "blocks": [
                    {
                        "block_id": block["block_id"],
                        "text": block["text"] + "，模型已规范整理",
                    }
                    for block in request["blocks"]
                ]
            },
            ensure_ascii=False,
        )


def _client(fake: WebFakeClient):
    service = LLMService(
        LLMConfig(api_key="test-key", allowed_models=("deepseek-chat",)),
        fake,
    )
    app = create_app(ROOT / "data/研发三部周报模板.docx", service)
    app.config["TESTING"] = True
    return app.test_client()


def _form(*, mode: str = "polish", consent: bool = True):
    form = {
        "csv_file": (io.BytesIO(CSV_CONTENT), "general.csv"),
        "preset": "general",
        "report_date": "2026-10-10",
        "content_mode": mode,
        "model": "deepseek-chat",
    }
    if consent:
        form["allow_external"] = "yes"
    return form


def test_web_requires_consent_before_model_call() -> None:
    fake = WebFakeClient()
    response = _client(fake).post(
        "/preview", data=_form(consent=False), content_type="multipart/form-data"
    )

    assert response.status_code == 400
    assert "确认允许" in response.text
    assert fake.calls == 0


def test_web_preview_displays_original_and_model_result() -> None:
    fake = WebFakeClient()
    response = _client(fake).post("/preview", data=_form(), content_type="multipart/form-data")

    assert response.status_code == 200
    assert "内容结构与来源映射" in response.text
    assert "模型结果" in response.text
    assert "模型已规范整理" in response.text
    assert fake.calls == 1


def test_web_llm_result_is_used_in_downloaded_docx() -> None:
    fake = WebFakeClient()
    response = _client(fake).post("/generate", data=_form(), content_type="multipart/form-data")

    assert response.status_code == 200
    assert response.mimetype == DOCX_MIME_TYPE
    with zipfile.ZipFile(io.BytesIO(response.data)) as package:
        document = package.read("word/document.xml").decode()
    assert "模型已规范整理" in document
    assert "AUC 0.81" in document
    assert fake.calls == 1


def test_original_web_mode_never_calls_model_or_needs_consent() -> None:
    fake = WebFakeClient()
    response = _client(fake).post(
        "/generate",
        data=_form(mode="original", consent=False),
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert fake.calls == 0
