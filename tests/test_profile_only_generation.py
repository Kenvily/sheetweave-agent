"""Public generation always uses a deliberately saved profile, without built-in fallbacks."""

import io
from pathlib import Path

import pytest

from weekly_report_agent.cli import main
from weekly_report_agent.rules import RulePresetError, list_rule_presets, load_rule_preset
from weekly_report_agent.service import prepare_report
from weekly_report_agent.web import create_app


def test_empty_library_directs_user_to_adaptation_and_disables_generation():
    response = create_app().test_client().get("/")
    assert response.status_code == 200
    assert "新建并保存方案" in response.text
    assert 'value="rd3"' not in response.text and 'value="general"' not in response.text
    assert 'name="preset" required disabled' in response.text
    assert 'type="submit" disabled formaction="/preview"' in response.text


@pytest.mark.parametrize("preset", ["", "rd3", "general"])
@pytest.mark.parametrize("route", ["/input-options", "/tasks", "/preview", "/generate"])
def test_public_generation_rejects_missing_or_builtin_rules(route, preset):
    app = create_app()
    response = app.test_client().post(
        route,
        data={
            "csv_file": (
                io.BytesIO("项目,本周工作\n合成项目,完成验证\n".encode()),
                "synthetic.csv",
            ),
            "preset": preset,
            "content_mode": "original",
            "report_date": "2026-10-19",
        },
    )
    assert response.status_code == 400
    assert app.config["TASK_QUEUE"].claim("must-not-generate") is None


def test_builtin_rd3_is_not_shipped_or_listed():
    assert "rd3" not in {preset.id for preset in list_rule_presets()}
    with pytest.raises(RulePresetError, match="找不到"):
        load_rule_preset("rd3")


def test_cli_and_internal_service_have_no_implicit_rule(capsys, tmp_path):
    with pytest.raises(SystemExit) as error:
        main(["--input", str(tmp_path / "not-read.csv")])
    assert error.value.code == 2 and "--profile" in capsys.readouterr().err
    with pytest.raises(ValueError, match="新建并保存方案"):
        prepare_report(Path("not-read.csv"))


def test_retired_rule_still_renders_existing_frozen_snapshot(tmp_path):
    from datetime import date

    from weekly_report_agent.rules import preset_payload
    from weekly_report_agent.service import render_prepared_report
    from weekly_report_agent.web_sessions import session_preset

    root = Path(__file__).resolve().parents[1]
    preset = load_rule_preset("rd3", root / "tests/fixtures/rules")
    source = tmp_path / "legacy.csv"
    source.write_text(
        "项目,具体内容,状态,2026年10月\n,,,第1周\n合成项目,合成事项,进行中,完成AUC 0.81验证\n"
    )
    prepared, _ = prepare_report(source, preset=preset)
    template = root / "data/研发三部周报模板-v2.docx"
    output = tmp_path / "legacy.docx"
    render_prepared_report(prepared, preset, template, output, date(2026, 10, 19))
    app = create_app()
    snapshots = app.config["SNAPSHOT_STORE"]
    token = snapshots.create_session(
        prepared,
        preset_id="rd3",
        report_date=date(2026, 10, 19),
        section_number=9,
        content_mode="original",
        custom_instruction=None,
        model=None,
        template_content=template.read_bytes(),
        report_content=output.read_bytes(),
        download_name="legacy.docx",
        rule_payload=preset_payload(preset),
    )
    assert session_preset(snapshots.load_session(token)).id == "rd3"
    assert app.test_client().get("/workspace/" + token).status_code == 200
