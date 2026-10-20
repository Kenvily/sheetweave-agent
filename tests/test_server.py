"""Unified launcher configuration and actual subprocess lifecycle tests."""

import os
import signal
import subprocess
import sys
import time

import pytest

from sheetweave_agent.server import load_environment


def test_env_loader_does_not_execute_values_or_expose_bad_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_TEST_VALUE", "inherited")
    config = tmp_path / ".env"
    marker = tmp_path / "must-not-exist"
    config.write_text(
        f'export SERVER_TEST_VALUE="from file"\nEMPTY=\nLITERAL="$(touch {marker})"\n'
    )
    values = load_environment(config)
    assert values["SERVER_TEST_VALUE"] == "from file" and values["EMPTY"] == ""
    assert values["LITERAL"].startswith("$(touch") and not marker.exists()
    assert load_environment(None)["SERVER_TEST_VALUE"] == "inherited"
    config.write_text('LLM_API_KEY="private-invalid-secret\n')
    with pytest.raises(ValueError) as captured:
        load_environment(config)
    assert "private-invalid-secret" not in str(captured.value)


def _launch(tmp_path, *, fail_web=False, exit_worker=False):
    for role in ("web", "worker"):
        path = tmp_path / f"{role}.py"
        code = (
            "import os,signal,sys,time\nfrom pathlib import Path\n"
            f"Path({str(tmp_path / (role + '.pid'))!r}).write_text(str(os.getpid()))\n"
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        )
        if role == "web" and fail_web:
            code += "raise SystemExit(2)\n"
        else:
            code += f"Path({str(tmp_path / (role + '.ready'))!r}).touch()\n"
            code += (
                "raise SystemExit(0)\n"
                if role == "worker" and exit_worker
                else "while True: time.sleep(.05)\n"
            )
        path.write_text(code)
    script = tmp_path / "launcher.py"
    script.write_text(
        "import os,sys\nfrom pathlib import Path\n"
        "from sheetweave_agent.server import run_services\n"
        f"root=Path({str(tmp_path)!r})\n"
        "raise SystemExit(run_services([sys.executable,str(root/'web.py')],"
        "[sys.executable,str(root/'worker.py')],cwd=root,environ=dict(os.environ),"
        "ready=lambda:(root/'web.ready').exists(),shutdown_timeout=2))\n"
    )
    return subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


def _assert_children_stopped(tmp_path):
    for path in tmp_path.glob("*.pid"):
        with pytest.raises(ProcessLookupError):
            os.kill(int(path.read_text()), 0)


def test_ctrl_c_stops_both_process_groups(tmp_path):
    process = _launch(tmp_path)
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "worker.ready").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert (tmp_path / "worker.ready").exists()
        process.send_signal(signal.SIGINT)
        process.communicate(timeout=5)
        assert process.returncode == 0
        _assert_children_stopped(tmp_path)
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)


@pytest.mark.parametrize("fail_web,exit_worker", [(True, False), (False, True)])
def test_child_exit_shuts_down_peer_and_returns_failure(tmp_path, fail_web, exit_worker):
    process = _launch(tmp_path, fail_web=fail_web, exit_worker=exit_worker)
    try:
        process.communicate(timeout=8)
        assert process.returncode == 1
        if fail_web:
            assert not (tmp_path / "worker.pid").exists()
        _assert_children_stopped(tmp_path)
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)
