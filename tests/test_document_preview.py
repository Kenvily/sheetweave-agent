import io
import re
import warnings
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from sheetweave_agent.docx_renderer import WORD_NS
from sheetweave_agent.docx_security import DocxSecurityError, inspect_docx_package
from sheetweave_agent.pdf_preview import PdfPreviewError
from sheetweave_agent.validator import ReportValidationError, validate_report
from sheetweave_agent.web import DOCX_MIME_TYPE, create_app
from sheetweave_agent.web_sessions import SessionConflictError, SnapshotStore

pytestmark = pytest.mark.usefixtures("saved_generation_profiles")

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "tests/fixtures/templates/reference.docx"
SIMPLE_CSV = "项目,本周工作\n项目甲,完成模型验证，AUC 0.81\n".encode()


class FakePdfService:
    available = True

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[Path] = []

    def convert(self, docx_path: Path, output_path: Path) -> Path:
        self.calls.append(docx_path)
        if self.fail:
            raise PdfPreviewError("测试转换失败，DOCX 下载不受影响。")
        output_path.write_bytes(b"%PDF-1.4\n% test preview\n")
        return output_path


class UnavailablePdfService:
    available = False

    def convert(self, docx_path: Path, output_path: Path) -> Path:  # pragma: no cover
        raise AssertionError("不可用的 PDF 服务不应被页面调用")


def _application(tmp_path: Path, pdf_service):
    store = SnapshotStore(tmp_path / "sessions")
    app = create_app(TEMPLATE, None, store, pdf_service)
    app.config["TESTING"] = True
    return app, store


def _preview(client) -> tuple[str, str]:
    response = client.post(
        "/preview",
        data={
            "csv_file": (io.BytesIO(SIMPLE_CSV), "weekly.csv"),
            "preset": "p_general",
            "report_date": "2026-10-10",
            "content_mode": "original",
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    token = re.search(r'href="/download/([^"/]+)"', response.text).group(1)
    return response.text, token


def _copy_package(source: Path, target: Path, replacements: dict[str, bytes]) -> None:
    with zipfile.ZipFile(source) as package, zipfile.ZipFile(target, "w") as output:
        for info in package.infolist():
            if info.filename not in replacements:
                output.writestr(info, package.read(info))
        for name, content in replacements.items():
            if content:
                output.writestr(name, content)


def test_default_template_passes_bounded_package_inspection() -> None:
    info = inspect_docx_package(TEMPLATE)

    assert info.entry_count > 0
    assert info.uncompressed_bytes > 0


@pytest.mark.parametrize(
    ("entry_name", "message"),
    (("../escape.xml", "不安全路径"), ("word/document.xml", "重复部件")),
)
def test_docx_inspection_rejects_unsafe_or_duplicate_entries(
    tmp_path: Path, entry_name: str, message: str
) -> None:
    path = tmp_path / "unsafe.docx"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(TEMPLATE) as package, zipfile.ZipFile(path, "w") as output:
            for info in package.infolist():
                output.writestr(info, package.read(info))
            output.writestr(entry_name, b"<invalid/>")

    with pytest.raises(DocxSecurityError, match=message):
        inspect_docx_package(path)


def test_docx_inspection_rejects_missing_part_and_invalid_xml(tmp_path: Path) -> None:
    missing = tmp_path / "missing.docx"
    _copy_package(TEMPLATE, missing, {"word/settings.xml": b""})
    with pytest.raises(DocxSecurityError, match="缺少必要部件"):
        inspect_docx_package(missing)

    invalid = tmp_path / "invalid.docx"
    _copy_package(TEMPLATE, invalid, {"word/document.xml": b"<broken"})
    with pytest.raises(DocxSecurityError, match="XML 无效"):
        inspect_docx_package(invalid)


def test_report_validation_requires_explicit_page_margins(tmp_path: Path) -> None:
    app, store = _application(tmp_path, UnavailablePdfService())
    _page, token = _preview(app.test_client())
    session = store.load_session(token)
    invalid = tmp_path / "no-margins.docx"
    with zipfile.ZipFile(session.report_path) as package:
        root = ET.fromstring(package.read("word/document.xml"))
    namespace = f"{{{WORD_NS}}}"
    section = root.find(f"{namespace}body/{namespace}sectPr")
    section.remove(section.find(f"{namespace}pgMar"))
    _copy_package(
        session.report_path,
        invalid,
        {"word/document.xml": ET.tostring(root, encoding="utf-8", xml_declaration=True)},
    )

    with pytest.raises(ReportValidationError, match="缺少页边距"):
        validate_report(
            invalid,
            section_number=1,
            report_title="通用文档",
            heading_titles=("本周工作",),
        )


def test_pdf_preview_is_generated_for_exact_revision_without_changing_it(
    tmp_path: Path,
) -> None:
    pdf_service = FakePdfService()
    app, store = _application(tmp_path, pdf_service)
    client = app.test_client()
    page, token = _preview(client)

    assert 'title="生成当前版本的 PDF 预览"' in page
    assert 'class="pdf-preview-form"' in page
    assert "重新生成 PDF 预览" not in page
    response = client.post(f"/workspace/{token}/pdf", data={"revision": 1})

    assert response.status_code == 302
    session = store.load_session(token)
    assert session.revision == 1
    assert session.pdf_path is not None
    assert len(pdf_service.calls) == 1
    refreshed = client.get(f"/workspace/{token}")
    assert "PDF 版式预览" in refreshed.text
    assert 'data-preview-mode="html"' in refreshed.text
    assert 'data-preview-mode="pdf"' in refreshed.text
    assert 'data-preview-panel="html" hidden' in refreshed.text
    assert 'data-preview-panel="pdf"' in refreshed.text
    assert "通用文档" in refreshed.text  # HTML preview remains rendered after PDF generation.
    assert "panel.hidden = panel.dataset.previewPanel !== selectedMode" in refreshed.text
    assert f"/workspace/{token}/pdf?revision=1" in refreshed.text.replace("&amp;", "&")
    pdf = client.get(f"/workspace/{token}/pdf?revision=1")
    assert pdf.status_code == 200
    assert pdf.mimetype == "application/pdf"
    assert pdf.data.startswith(b"%PDF-")


def test_edit_invalidates_old_pdf_and_stale_pdf_cannot_be_published(tmp_path: Path) -> None:
    app, store = _application(tmp_path, FakePdfService())
    client = app.test_client()
    _page, token = _preview(client)
    assert client.post(f"/workspace/{token}/pdf", data={"revision": 1}).status_code == 302
    session = store.load_session(token)
    block = session.prepared.blocks[0]

    edited = client.post(
        f"/workspace/{token}/blocks/{block.block_id}/edit",
        data={"confirm_edit": "yes", "revision": 1, "edited_text": "完成模型验证（AUC 0.81）。"},
    )

    assert edited.status_code == 302
    current = store.load_session(token)
    assert current.revision == 2
    assert current.pdf_path is None
    stale = client.get(f"/workspace/{token}/pdf?revision=1")
    assert stale.status_code == 409
    with pytest.raises(SessionConflictError, match="转换期间更新"):
        store.publish_pdf(token, 1, b"%PDF-1.4\nold")


def test_pdf_failure_or_unavailable_converter_does_not_block_docx(tmp_path: Path) -> None:
    unavailable_app, _store = _application(tmp_path / "unavailable", UnavailablePdfService())
    unavailable_client = unavailable_app.test_client()
    page, token = _preview(unavailable_client)
    assert "当前环境未安装 LibreOffice" in page
    assert 'class="pdf-preview-form"' not in page
    docx = unavailable_client.get(f"/download/{token}")
    assert docx.status_code == 200
    assert docx.mimetype == DOCX_MIME_TYPE

    failing_app, _store = _application(tmp_path / "failing", FakePdfService(fail=True))
    failing_client = failing_app.test_client()
    _page, token = _preview(failing_client)
    failed = failing_client.post(
        f"/workspace/{token}/pdf", data={"revision": 1}, follow_redirects=False
    )
    assert failed.status_code == 400
    assert "DOCX 下载不受影响" in failed.text
    assert failing_client.get(f"/download/{token}").status_code == 200


@pytest.mark.parametrize(
    ("value", "seconds", "label"),
    [
        ("2h", 7200, "2 小时"),
        ("3d", 259200, "3 天"),
        ("1w", 604800, "1 周"),
        ("24小时", 86400, "1 天"),
        ("2天", 172800, "2 天"),
        ("2周", 1209600, "2 周"),
        (" 3 H ", 10800, "3 小时"),
        ("", 7200, "2 小时"),
    ],
)
def test_session_retention_readable_units(tmp_path, monkeypatch, value, seconds, label):
    monkeypatch.setenv("REPORT_SESSION_TTL", value)
    monkeypatch.setenv("REPORT_SESSION_TTL_SECONDS", "60")
    store = SnapshotStore.from_environment(tmp_path / "sessions")
    assert store.ttl_seconds == seconds
    assert store.retention_label == label


@pytest.mark.parametrize("value", ["0h", "-1d", "1.5w", "24", "invalid"])
def test_session_retention_invalid_configuration(tmp_path, monkeypatch, value):
    monkeypatch.setenv("REPORT_SESSION_TTL", value)
    with pytest.raises(ValueError, match="REPORT_SESSION_TTL"):
        SnapshotStore.from_environment(tmp_path / "sessions")


def test_session_retention_legacy_and_default(tmp_path, monkeypatch):
    assert SnapshotStore.from_environment(tmp_path / "sessions").ttl_seconds == 7200
    monkeypatch.setenv("REPORT_SESSION_TTL_SECONDS", "90")
    assert SnapshotStore.from_environment(tmp_path / "sessions").ttl_seconds == 90
    monkeypatch.setenv("REPORT_SESSION_TTL_SECONDS", "0")
    with pytest.raises(ValueError, match="REPORT_SESSION_TTL_SECONDS"):
        SnapshotStore.from_environment(tmp_path / "sessions")


def test_session_retention_shared_by_web_worker_and_displayed(monkeypatch):
    from sheetweave_agent.task_worker import TaskRunner

    monkeypatch.setenv("REPORT_SESSION_TTL", "3d")
    app = create_app(TEMPLATE)
    app.config["TESTING"] = True
    store = app.config["SNAPSHOT_STORE"]
    runner = TaskRunner.from_environment(app.config["TASK_QUEUE"])
    assert runner.snapshots.root == store.root
    assert runner.snapshots.ttl_seconds == store.ttl_seconds == 259200
    client = app.test_client()
    assert "3 天" in client.get("/").text
    assert "3 天" in client.get("/help").text
    html, token = _preview(client)
    assert "有效至" in html
    session = store.load_session(token)
    assert session.expires_at > 0
    assert store.get(token).path.is_file()


@pytest.mark.parametrize("editable", [False, True])
def test_cleanup_respects_saved_expiry_after_configuration_change(tmp_path, monkeypatch, editable):
    import json

    store = SnapshotStore(tmp_path / "sessions", ttl_seconds=86400)
    if editable:
        app = create_app(TEMPLATE, None, store, UnavailablePdfService())
        app.config["TESTING"] = True
        _, token = _preview(app.test_client())
        metadata = store.root / token / "state.json"
    else:
        token = store.create(b"synthetic", "report.docx")
        metadata = store.root / token / "metadata.json"
    expires_at = json.loads(metadata.read_text())["expires_at"]
    shorter = SnapshotStore(store.root, ttl_seconds=60)
    shorter.cleanup_expired(now=expires_at - 1)
    assert metadata.is_file()
    longer = SnapshotStore(store.root, ttl_seconds=604800)
    longer.cleanup_expired(now=expires_at + 1)
    assert not metadata.parent.exists()
