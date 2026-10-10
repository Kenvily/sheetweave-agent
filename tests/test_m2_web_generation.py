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
                        "text": block["editable_text"] + "，模型已规范整理",
                    }
                    for block in request["blocks"]
                ]
            },
            ensure_ascii=False,
        )


def _client(fake: WebFakeClient, tmp_path: Path | None = None):
    service = LLMService(
        LLMConfig(api_key="test-key", allowed_models=("deepseek-chat",)),
        fake,
    )
    from weekly_report_agent.web_sessions import SnapshotStore

    store = SnapshotStore(tmp_path / "snapshots") if tmp_path is not None else None
    app = create_app(ROOT / "data/研发三部周报模板.docx", service, store)
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


def test_web_preview_displays_original_and_model_result(tmp_path: Path) -> None:
    fake = WebFakeClient()
    response = _client(fake, tmp_path).post(
        "/preview", data=_form(), content_type="multipart/form-data"
    )

    assert response.status_code == 200
    assert "内容结构与来源映射" in response.text
    assert "模型结果" in response.text
    assert "模型已规范整理" in response.text
    assert "无需重新选择文件" in response.text
    assert 'href="/download/' in response.text
    assert fake.calls == 1


def test_preview_snapshot_download_does_not_call_model_again(tmp_path: Path) -> None:
    fake = WebFakeClient()
    client = _client(fake, tmp_path)
    preview = client.post("/preview", data=_form(), content_type="multipart/form-data")
    marker = 'href="/download/'
    token = preview.text.split(marker, 1)[1].split('"', 1)[0]

    response = client.get(f"/download/{token}")

    assert response.status_code == 200
    assert response.mimetype == DOCX_MIME_TYPE
    with zipfile.ZipFile(io.BytesIO(response.data)) as package:
        document = package.read("word/document.xml").decode()
    assert "模型已规范整理" in document
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


class ConflictingWebClient:
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
                        "text": block["editable_text"].replace("0.81", "0.91"),
                    }
                    for block in request["blocks"]
                ]
            },
            ensure_ascii=False,
        )


def test_conflicting_candidate_is_explained_and_excluded_from_snapshot(tmp_path: Path) -> None:
    fake = ConflictingWebClient()
    client = _client(fake, tmp_path)

    preview = client.post("/preview", data=_form(), content_type="multipart/form-data")

    assert preview.status_code == 200
    assert "严重冲突" in preview.text
    assert "本次 DOCX 已自动使用原文" in preview.text
    assert "AUC 0.91" in preview.text
    assert "AUC 0.81" in preview.text
    assert fake.calls == 2
    marker = 'href="/download/'
    token = preview.text.split(marker, 1)[1].split('"', 1)[0]
    response = client.get(f"/download/{token}")
    with zipfile.ZipFile(io.BytesIO(response.data)) as package:
        document = package.read("word/document.xml").decode()
    assert "AUC 0.81" in document
    assert "AUC 0.91" not in document
    assert fake.calls == 2
