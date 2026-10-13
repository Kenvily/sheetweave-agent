"""Shared test isolation for local Web state."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_default_web_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent application-factory tests from writing to the user state directory."""
    monkeypatch.setenv("STATS_DB_PATH", str(tmp_path / "default-stats.sqlite3"))
    monkeypatch.setenv("STATS_HMAC_KEY", "pytest-default-hmac-key-0123456789")
    monkeypatch.setenv("WEEKLY_REPORT_SNAPSHOT_DIR", str(tmp_path / "default-snapshots"))

    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setenv("TASK_FILES_DIR", str(tmp_path / "task-files"))
