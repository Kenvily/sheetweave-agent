import io
import json
import zipfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from sheetweave_agent.llm import LLMConfig, LLMService
from sheetweave_agent.web import DOCX_MIME_TYPE, create_app

pytestmark = pytest.mark.usefixtures("saved_generation_profiles")

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
    from sheetweave_agent.web_sessions import SnapshotStore

    store = SnapshotStore(tmp_path / "snapshots") if tmp_path is not None else None
    app = create_app(ROOT / "data/研发三部周报模板-v2.docx", service, store)
    app.config["TESTING"] = True
    return app.test_client()


def _form(*, mode: str = "polish", consent: bool = True):
    form = {
        "csv_file": (io.BytesIO(CSV_CONTENT), "general.csv"),
        "preset": "p_general",
        "report_date": "2026-10-10",
        "content_mode": mode,
        "model": "deepseek-chat",
    }
    if consent:
        form["allow_external"] = "yes"
    return form


def test_web_model_mode_does_not_require_removed_consent_field(tmp_path: Path) -> None:
    fake = WebFakeClient()
    response = _client(fake, tmp_path).post(
        "/preview", data=_form(consent=False), content_type="multipart/form-data"
    )

    assert response.status_code == 200
    assert fake.calls == 1


def test_web_preview_displays_original_and_model_result(tmp_path: Path) -> None:
    fake = WebFakeClient()
    response = _client(fake, tmp_path).post(
        "/preview", data=_form(), content_type="multipart/form-data"
    )

    assert response.status_code == 200
    assert "内容结构与来源映射" in response.text
    assert "当前内容" in response.text
    assert "模型候选 · 用于导出" in response.text
    assert response.text.count("查看变更对比") == 1
    assert 'class="unified-diff"' in response.text
    assert 'class="diff-line removed"' in response.text
    assert 'class="diff-line added"' in response.text
    assert "模型已规范整理" in response.text
    assert "周报审核工作区" in response.text
    assert "点击内容块可反向定位来源" not in response.text
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
    assert "谨慎使用" in preview.text
    assert "请核对事实后采用" in preview.text
    assert "原文 · 用于导出" in preview.text
    assert preview.text.count("查看变更对比") == 1
    assert "AUC 0.91" in preview.text
    assert "AUC 0.81" in preview.text
    assert fake.calls == 1
    marker = 'href="/download/'
    token = preview.text.split(marker, 1)[1].split('"', 1)[0]
    response = client.get(f"/download/{token}")
    with zipfile.ZipFile(io.BytesIO(response.data)) as package:
        document = package.read("word/document.xml").decode()
    assert "AUC 0.81" in document
    assert "AUC 0.91" not in document
    assert fake.calls == 1


@pytest.mark.parametrize("route", ["/preview", "/tasks"])
def test_polish_extra_requirements_reach_generation_and_regeneration(tmp_path, route):
    from sheetweave_agent.task_worker import TaskRunner

    class CapturingClient(WebFakeClient):
        def __init__(self):
            super().__init__()
            self.requests = []

        def complete(self, messages, model):
            self.requests.append(json.loads(messages[-1]["content"]))
            return super().complete(messages, model)

    fake = CapturingClient()
    client = _client(fake, tmp_path)
    app = client.application
    form = _form()
    form["custom_instruction"] = "按成果分条整理"
    response = client.post(route, data=form)
    queue = app.config["TASK_QUEUE"]
    runner = TaskRunner(queue, app.config["SNAPSHOT_STORE"], llm_service=app.config["LLM_SERVICE"])
    if route == "/tasks":
        assert response.status_code == 202
        job = queue.claim("initial")
        runner.run(job, "initial")
        token = queue.status(job["id"])["result"]["token"]
    else:
        assert response.status_code == 200
        import re

        token = re.search(r'href="/download/([^"/]+)"', response.text).group(1)
    assert fake.requests[0]["mode_contract"]["custom_instruction"] == "按成果分条整理"
    session = app.config["SNAPSHOT_STORE"].load_session(token)
    block_id = next(block.block_id for block in session.prepared.blocks if block.llm_editable)
    response = client.post(
        f"/workspace/{token}/blocks/{block_id}/tasks",
        data={
            "content_mode": "polish",
            "custom_instruction": "先写结论",
            "revision": session.revision,
            "allow_external": "yes",
        },
    )
    assert response.status_code == 202
    runner.run(queue.claim("regenerate"), "regenerate")
    assert fake.requests[-1]["mode_contract"]["custom_instruction"] == "先写结论"
