import io
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook

from weekly_report_agent import stats as stats_module
from weekly_report_agent.stats import UsageMetrics, UsageStats
from weekly_report_agent.web import DOCX_MIME_TYPE, create_app
from weekly_report_agent.web_sessions import SnapshotStore

pytestmark = pytest.mark.usefixtures("saved_generation_profiles")

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "data/研发三部周报模板-v2.docx"
GENERAL_CSV = "项目,本周工作\n项目甲,完成接口联调\n".encode()


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()


def _app(tmp_path: Path, stats=None):
    service = stats or UsageStats(tmp_path / "stats.sqlite3", hmac_key=b"m5-test-key-0123456789")
    app = create_app(
        TEMPLATE,
        snapshot_store=SnapshotStore(tmp_path / "sessions"),
        usage_stats=service,
    )
    app.config["TESTING"] = True
    return app, service


def _generation_form(task_id: str) -> dict:
    return {
        "csv_file": (io.BytesIO(GENERAL_CSV), "weekly.csv"),
        "preset": "p_general",
        "report_date": "2026-10-10",
        "content_mode": "original",
        "generation_task_id": task_id,
    }


def test_stats_are_idempotent_and_use_monday_week_boundary(tmp_path: Path) -> None:
    stats = UsageStats(tmp_path / "stats.sqlite3", hmac_key=b"m5-test-key-0123456789")
    sunday = _timestamp("2026-10-04T23:59:00")
    monday = _timestamp("2026-10-05T00:01:00")

    assert stats.record_generation("old-task", generated_at=sunday)
    assert stats.record_generation("new-task", generated_at=monday)
    assert not stats.record_generation("new-task", generated_at=monday)
    assert stats.record_visitor("browser-a", seen_at=monday)
    assert not stats.record_visitor("browser-a", seen_at=monday)
    assert stats.record_visitor("browser-b", seen_at=monday)

    assert stats.metrics(now=_timestamp("2026-10-10T12:00:00")) == UsageMetrics(2, 1, 2)
    with sqlite3.connect(stats.database_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        visitor_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(anonymous_visitors)")
        }
    assert {"generation_totals", "generation_tasks", "anonymous_visitors"} <= tables
    assert visitor_columns == {"visitor_hash", "first_seen"}


def test_stats_concurrently_create_one_shared_hmac_key(tmp_path: Path) -> None:
    path = tmp_path / "first-start.sqlite3"

    def initialize(_index: int) -> UsageStats:
        return UsageStats(path)

    with ThreadPoolExecutor(max_workers=6) as executor:
        stores = list(executor.map(initialize, range(6)))

    assert len({store._digest("test", "same-value") for store in stores}) == 1
    assert path.with_suffix(".sqlite3.key").stat().st_mode & 0o077 == 0


def test_stats_support_concurrent_shared_sqlite_writers(tmp_path: Path) -> None:
    path = tmp_path / "shared.sqlite3"
    stores = [UsageStats(path, hmac_key=b"m5-shared-key-0123456789") for _unused in range(4)]

    def record(index: int) -> bool:
        return stores[index % len(stores)].record_generation(f"task-{index}")

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert all(executor.map(record, range(24)))
    assert stores[0].metrics().total_generations == 24


def test_homepage_cookie_metrics_help_and_examples(tmp_path: Path) -> None:
    app, stats = _app(tmp_path)
    client = app.test_client()

    first = client.get("/")
    second = client.get("/")
    help_page = client.get("/help")

    assert first.status_code == second.status_code == help_page.status_code == 200
    assert "访客" in first.text
    assert stats.metrics().estimated_visitors == 1
    assert stats.metrics().page_views == 3
    assert "访问次数" in first.text
    assert "四步生成周报" in help_page.text
    assert "示例 CSV" in help_page.text
    csv_example = client.get("/examples/general-weekly.csv")
    xlsx_example = client.get("/examples/general-weekly.xlsx")
    assert csv_example.status_code == xlsx_example.status_code == 200
    assert "项目,本周工作" in csv_example.data.decode("utf-8-sig")
    workbook = load_workbook(io.BytesIO(xlsx_example.data), read_only=True)
    try:
        assert next(workbook.active.iter_rows(values_only=True))[:2] == ("项目", "本周工作")
    finally:
        workbook.close()


def test_successful_generation_counts_once_and_download_does_not_recount(tmp_path: Path) -> None:
    app, stats = _app(tmp_path)
    client = app.test_client()
    task_id = "same-generation-task-12345"

    first = client.post(
        "/generate", data=_generation_form(task_id), content_type="multipart/form-data"
    )
    retry = client.post(
        "/generate", data=_generation_form(task_id), content_type="multipart/form-data"
    )
    assert first.status_code == retry.status_code == 200
    assert first.mimetype == retry.mimetype == DOCX_MIME_TYPE
    assert stats.metrics().total_generations == 1

    preview_task_id = "preview-generation-task-12345"
    preview = client.post(
        "/preview",
        data=_generation_form(preview_task_id),
        content_type="multipart/form-data",
    )
    retry_preview = client.post(
        "/preview",
        data=_generation_form(preview_task_id),
        content_type="multipart/form-data",
    )
    assert preview.status_code == retry_preview.status_code == 200
    token = preview.text.split('href="/download/', 1)[1].split('"', 1)[0]
    assert stats.metrics().total_generations == 2
    assert client.get(f"/download/{token}").status_code == 200
    assert client.get(f"/workspace/{token}").status_code == 200
    assert stats.metrics().total_generations == 2


class BrokenStats:
    def metrics(self):
        raise sqlite3.OperationalError("unavailable")

    def record_visitor(self, _visitor_id):
        raise sqlite3.OperationalError("unavailable")

    def record_generation(self, _task_id):
        raise sqlite3.OperationalError("unavailable")


def test_statistics_failure_never_blocks_docx(tmp_path: Path) -> None:
    app, _stats = _app(tmp_path, BrokenStats())
    client = app.test_client()
    assert client.get("/").status_code == 200
    assert "统计暂不可用" in client.get("/").text
    response = client.post(
        "/generate",
        data=_generation_form("broken-stats-task-12345"),
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.mimetype == DOCX_MIME_TYPE


def test_statistics_survive_application_restart(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("STATS_DB_PATH", str(tmp_path / "persistent.sqlite3"))
    monkeypatch.delenv("STATS_HMAC_KEY")
    first_app = create_app(TEMPLATE)
    first_client = first_app.test_client()
    first_client.get("/")
    cookie = first_client.get_cookie("weekly_report_visitor").value
    response = first_client.post(
        "/generate",
        data=_generation_form("persistent-generation-task"),
        content_type="multipart/form-data",
    )
    assert response.status_code == 200

    second_app = create_app(TEMPLATE)
    second_client = second_app.test_client()
    second_client.set_cookie("weekly_report_visitor", cookie)
    second_client.get("/")
    metrics = second_app.config["USAGE_STATS"].metrics()
    assert metrics == UsageMetrics(1, 1, 1, 2)


def test_empty_snapshot_setting_uses_private_temp_directory(monkeypatch) -> None:
    monkeypatch.setenv("WEEKLY_REPORT_SNAPSHOT_DIR", "")
    app = create_app(TEMPLATE)
    assert app.config["SNAPSHOT_STORE"].root != Path.cwd()
    assert app.config["SNAPSHOT_STORE"].root.name == "weekly-report-agent-snapshots"


def test_project_database_migrates_legacy_counters_and_key(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    legacy_path = home / ".local/state/weekly-report-agent/stats.sqlite3"
    legacy = UsageStats(legacy_path)
    legacy.record_generation("legacy-task")
    legacy.record_visitor("same-browser")
    legacy.record_page_view()
    destination = tmp_path / "project/var/stats.sqlite3"
    monkeypatch.setattr(stats_module, "DEFAULT_STATS_PATH", destination)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("STATS_DB_PATH", "")
    monkeypatch.delenv("STATS_HMAC_KEY")
    monkeypatch.chdir(tmp_path)

    migrated = UsageStats.from_environment()
    assert migrated.database_path == destination
    assert migrated.metrics() == UsageMetrics(1, 1, 1, 1)
    assert not migrated.record_visitor("same-browser")
    assert legacy_path.is_file()
    migrated.record_generation("new-task")
    restarted = UsageStats.from_environment()
    assert restarted.metrics().total_generations == 2
    assert legacy.metrics().total_generations == 1
