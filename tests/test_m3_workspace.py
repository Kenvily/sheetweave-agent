import io
import json
import re
import zipfile
from collections.abc import Sequence
from pathlib import Path

from weekly_report_agent.llm import LLMConfig, LLMService
from weekly_report_agent.web import DOCX_MIME_TYPE, _unified_diff, create_app
from weekly_report_agent.web_sessions import SnapshotStore

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "data/研发三部周报模板.docx"
MULTI_SOURCE_CSV = (
    "项目,本周工作,问题与风险,下周计划\n"
    "项目甲,完成模型验证，AUC 0.81,样本偏少,补充样本\n"
    "项目甲,完成 PSI 分析,,继续评估\n"
).encode()
SINGLE_SOURCE_CSV = "项目,本周工作\n项目甲,完成模型验证，AUC 0.81\n".encode()


class SuffixClient:
    def __init__(self, suffix: str = "，表述已整理") -> None:
        self.suffix = suffix
        self.calls: list[tuple[Sequence[dict[str, str]], str]] = []

    def complete(self, messages: Sequence[dict[str, str]], model: str) -> str:
        self.calls.append((messages, model))
        payload = json.loads(messages[-1]["content"])
        return json.dumps(
            {
                "blocks": [
                    {
                        "block_id": block["block_id"],
                        "text": block["editable_text"] + self.suffix,
                    }
                    for block in payload["blocks"]
                ]
            },
            ensure_ascii=False,
        )


class ConflictingClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages: Sequence[dict[str, str]], model: str) -> str:
        self.calls += 1
        payload = json.loads(messages[-1]["content"])
        return json.dumps(
            {
                "blocks": [
                    {
                        "block_id": block["block_id"],
                        "text": block["editable_text"].replace("0.81", "0.91"),
                    }
                    for block in payload["blocks"]
                ]
            },
            ensure_ascii=False,
        )


def _application(tmp_path: Path, fake=None):
    service = None
    if fake is not None:
        service = LLMService(
            LLMConfig(
                api_key="test-key",
                model="deepseek-chat",
                allowed_models=("deepseek-chat",),
            ),
            fake,
        )
    store = SnapshotStore(tmp_path / "sessions")
    app = create_app(TEMPLATE, service, store)
    app.config["TESTING"] = True
    return app, store


def _preview(client, content: bytes = MULTI_SOURCE_CSV, *, mode: str = "original"):
    data = {
        "csv_file": (io.BytesIO(content), "weekly.csv"),
        "preset": "general",
        "report_date": "2026-10-10",
        "content_mode": mode,
        "model": "deepseek-chat",
    }
    if mode != "original":
        data["allow_external"] = "yes"
    response = client.post("/preview", data=data, content_type="multipart/form-data")
    assert response.status_code == 200
    token = re.search(r'href="/download/([^"/]+)"', response.text).group(1)
    return response, token


def _document_text(response) -> str:
    assert response.status_code == 200
    assert response.mimetype == DOCX_MIME_TYPE
    with zipfile.ZipFile(io.BytesIO(response.data)) as package:
        return package.read("word/document.xml").decode("utf-8")


def test_unified_diff_only_marks_changed_physical_line() -> None:
    rows = _unified_diff(
        "第一行保持不变\n第二行原内容\n第三行保持不变",
        "第一行保持不变\n第二行修改后内容\n第三行保持不变",
    )

    assert rows == (
        ("context", "第一行保持不变"),
        ("removed", "第二行原内容"),
        ("added", "第二行修改后内容"),
        ("context", "第三行保持不变"),
    )

    inserted = _unified_diff("第一行\n第三行", "第一行\n第二行\n第三行")
    assert inserted == (
        ("context", "第一行"),
        ("added", "第二行"),
        ("context", "第三行"),
    )


def test_workspace_has_three_columns_and_complete_bidirectional_ids(tmp_path: Path) -> None:
    app, store = _application(tmp_path)
    response, token = _preview(app.test_client())
    session = store.load_session(token)
    weekly = next(block for block in session.prepared.blocks if block.section_id == "weekly_work")
    first_source = session.prepared.source.sources[0]
    linked = session.prepared.blocks_for_source(first_source.source_id)

    assert 'aria-label="原始表格"' in response.text
    assert 'aria-label="内容审核"' in response.text
    assert 'aria-label="文档效果"' in response.text
    assert 'class="effective-badge"' in response.text
    assert "点击内容块可反向定位来源" not in response.text
    assert "展示当前版本的有效内容" not in response.text
    assert f'data-block-id="{weekly.block_id}"' in response.text
    assert f'data-source-ids="{" ".join(weekly.source_ids)}"' in response.text
    assert f'data-source-id="{first_source.source_id}"' in response.text
    assert f'data-block-ids="{" ".join(block.block_id for block in linked)}"' in response.text
    assert len(weekly.source_ids) == 2  # 多行合并为一块
    assert len(linked) >= 3  # 一行进入多个章节块


def test_workspace_refresh_preserves_session_and_diff_is_read_only(tmp_path: Path) -> None:
    fake = SuffixClient()
    app, store = _application(tmp_path, fake)
    client = app.test_client()
    preview, token = _preview(client, SINGLE_SOURCE_CSV, mode="polish")
    before = store.load_session(token).prepared

    refreshed = client.get(f"/workspace/{token}")
    after = store.load_session(token).prepared

    assert refreshed.status_code == 200
    assert "周报审核工作区" in refreshed.text
    assert refreshed.text.count("查看变更对比") == 1
    assert 'class="unified-diff"' in refreshed.text
    assert 'class="diff-line removed"' in refreshed.text
    assert 'class="diff-line added"' in refreshed.text
    assert "查看原文与模型候选 Diff" not in refreshed.text
    assert "查看模型/原文与人工编辑 Diff" not in refreshed.text
    assert preview.text.count("表述已整理") >= 1
    assert after == before
    assert len(fake.calls) == 1


def test_safe_manual_edit_persists_and_updates_downloaded_docx(tmp_path: Path) -> None:
    app, store = _application(tmp_path)
    client = app.test_client()
    _preview_response, token = _preview(client, SINGLE_SOURCE_CSV)
    session = store.load_session(token)
    block = session.prepared.blocks[0]
    edited = "完成模型验证（AUC 0.81）。"

    response = client.post(
        f"/workspace/{token}/blocks/{block.block_id}/edit",
        data={"revision": session.revision, "edited_text": edited},
    )

    assert response.status_code == 302
    current = store.load_session(token)
    assert current.revision == 2
    assert current.prepared.block_by_id(block.block_id).effective_text == edited
    refreshed = client.get(f"/workspace/{token}")
    assert edited in refreshed.text
    assert refreshed.text.count("查看变更对比") == 1
    assert refreshed.text.count('class="diff-line removed"') == 1
    assert refreshed.text.count('class="diff-line added"') == 1
    assert '<div class="version-heading"><b>原文</b>' not in refreshed.text
    document = _document_text(client.get(f"/download/{token}"))
    assert edited in document


def test_conflicting_manual_edit_is_retained_but_not_exported(tmp_path: Path) -> None:
    app, store = _application(tmp_path)
    client = app.test_client()
    _response, token = _preview(client, SINGLE_SOURCE_CSV)
    session = store.load_session(token)
    block = session.prepared.blocks[0]
    unsafe = "完成模型验证，AUC 0.91。"

    response = client.post(
        f"/workspace/{token}/blocks/{block.block_id}/edit",
        data={"revision": 1, "edited_text": unsafe},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "人工编辑：严重冲突" in response.text
    current = store.load_session(token).prepared.block_by_id(block.block_id)
    assert current.edited_text == unsafe
    assert current.effective_text == block.original_text
    document = _document_text(client.get(f"/download/{token}"))
    assert "0.81" in document
    assert "0.91" not in document


def test_version_selection_rerenders_same_current_snapshot(tmp_path: Path) -> None:
    fake = SuffixClient()
    app, store = _application(tmp_path, fake)
    client = app.test_client()
    _response, token = _preview(client, SINGLE_SOURCE_CSV, mode="polish")
    block = store.load_session(token).prepared.blocks[0]

    original = client.post(
        f"/workspace/{token}/blocks/{block.block_id}/select",
        data={"revision": 1, "version": "original"},
    )
    assert original.status_code == 302
    assert "表述已整理" not in _document_text(client.get(f"/download/{token}"))

    generated = client.post(
        f"/workspace/{token}/blocks/{block.block_id}/select",
        data={"revision": 2, "version": "generated"},
    )
    assert generated.status_code == 302
    current = store.load_session(token)
    assert current.revision == 3
    assert current.prepared.blocks[0].effective_version == "generated"
    assert "表述已整理" in _document_text(client.get(f"/download/{token}"))
    assert len(fake.calls) == 1  # 选择版本和下载均不会重新调用模型


def test_conflicting_model_candidate_cannot_be_selected(tmp_path: Path) -> None:
    fake = ConflictingClient()
    app, store = _application(tmp_path, fake)
    client = app.test_client()
    _response, token = _preview(client, SINGLE_SOURCE_CSV, mode="polish")
    session = store.load_session(token)
    block = session.prepared.blocks[0]
    assert block.validation_level == "conflict"

    response = client.post(
        f"/workspace/{token}/blocks/{block.block_id}/select",
        data={"revision": 1, "version": "generated"},
    )

    assert response.status_code == 400
    assert "严重冲突的模型候选不能直接采用" in response.text
    current = store.load_session(token)
    assert current.revision == 1
    assert current.prepared.blocks[0].effective_version == "original"


def test_single_block_regeneration_changes_only_target(tmp_path: Path) -> None:
    fake = SuffixClient("，单块已重生成")
    app, store = _application(tmp_path, fake)
    client = app.test_client()
    _response, token = _preview(client)
    before = store.load_session(token)
    target = next(block for block in before.prepared.blocks if block.llm_editable)
    untouched = tuple(
        block for block in before.prepared.blocks if block.block_id != target.block_id
    )

    response = client.post(
        f"/workspace/{token}/blocks/{target.block_id}/regenerate",
        data={
            "revision": 1,
            "content_mode": "polish",
            "model": "deepseek-chat",
            "allow_external": "yes",
        },
    )

    assert response.status_code == 302
    current = store.load_session(token)
    assert current.revision == 2
    assert current.prepared.block_by_id(target.block_id).generated_text.endswith("单块已重生成")
    assert (
        tuple(block for block in current.prepared.blocks if block.block_id != target.block_id)
        == untouched
    )
    assert len(fake.calls) == 1


def test_regeneration_requires_explicit_edit_overwrite(tmp_path: Path) -> None:
    fake = SuffixClient("，重新整理")
    app, store = _application(tmp_path, fake)
    client = app.test_client()
    _response, token = _preview(client, SINGLE_SOURCE_CSV)
    block = store.load_session(token).prepared.blocks[0]
    edit = client.post(
        f"/workspace/{token}/blocks/{block.block_id}/edit",
        data={"revision": 1, "edited_text": "完成模型验证（AUC 0.81）。"},
    )
    assert edit.status_code == 302

    protected = client.post(
        f"/workspace/{token}/blocks/{block.block_id}/regenerate",
        data={
            "revision": 2,
            "content_mode": "polish",
            "model": "deepseek-chat",
            "allow_external": "yes",
        },
    )
    assert protected.status_code == 400
    assert "必须确认覆盖" in protected.text
    assert store.load_session(token).revision == 2
    assert len(fake.calls) == 0

    overwritten = client.post(
        f"/workspace/{token}/blocks/{block.block_id}/regenerate",
        data={
            "revision": 2,
            "content_mode": "polish",
            "model": "deepseek-chat",
            "allow_external": "yes",
            "overwrite_edited": "yes",
        },
    )
    assert overwritten.status_code == 302
    current = store.load_session(token)
    assert current.revision == 3
    assert current.prepared.blocks[0].edited_text is None
    assert len(fake.calls) == 1


def test_stale_revision_returns_409_without_overwriting_newer_edit(tmp_path: Path) -> None:
    app, store = _application(tmp_path)
    client = app.test_client()
    _response, token = _preview(client, SINGLE_SOURCE_CSV)
    block = store.load_session(token).prepared.blocks[0]
    first_edit = "完成模型验证（AUC 0.81）。"

    first = client.post(
        f"/workspace/{token}/blocks/{block.block_id}/edit",
        data={"revision": 1, "edited_text": first_edit},
    )
    stale = client.post(
        f"/workspace/{token}/blocks/{block.block_id}/edit",
        data={"revision": 1, "edited_text": "完成模型验证，AUC 0.81；旧标签修改。"},
    )

    assert first.status_code == 302
    assert stale.status_code == 409
    assert "已在其他页面更新" in stale.text
    current = store.load_session(token)
    assert current.revision == 2
    assert current.prepared.blocks[0].edited_text == first_edit


def test_session_rejects_path_traversal_and_preserves_tuple_fields(tmp_path: Path) -> None:
    app, store = _application(tmp_path)
    _response, token = _preview(app.test_client())

    session = store.load_session(token)

    assert isinstance(session.prepared.blocks[0].source_ids, tuple)
    assert isinstance(session.prepared.source.sources[0].raw_values, tuple)
    try:
        store.load_session("../outside")
    except ValueError as exc:
        assert "无效或已经过期" in str(exc)
    else:  # pragma: no cover - security regression guard
        raise AssertionError("path traversal token should be rejected")

    state_path = store.root / token / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["schema_version"] = 999
    state_path.write_text(json.dumps(state), encoding="utf-8")
    try:
        store.load_session(token)
    except ValueError as exc:
        assert "会话无效" in str(exc)
    else:  # pragma: no cover - corrupted-state regression guard
        raise AssertionError("unsupported session schema should be rejected")
