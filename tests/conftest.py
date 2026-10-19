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

    monkeypatch.setenv("REPORT_PROFILES_DIR", str(tmp_path / "profiles"))


@pytest.fixture
def saved_generation_profiles(isolate_default_web_state):
    """Create saved schemes explicitly for generation tests; no production built-ins."""
    import json

    from weekly_report_agent.profiles import ProfileStore
    from weekly_report_agent.rules import load_rule_preset, preset_payload

    root = Path(__file__).resolve().parents[1]
    template = root / "data/研发三部周报模板-v2.docx"
    store = ProfileStore.from_environment()
    general = preset_payload(load_rule_preset("general"))
    store.save(general, template, profile_id="p_general")
    matrix = json.loads((root / "tests/fixtures/rules/rd3.json").read_text())
    matrix["sections"][0].update(
        heading_path=["大模型产品研发", "项目", "具体事项"],
        heading_fields=["", "project", "detail"],
    )
    store.save(matrix, template, profile_id="p_rd3")
