"""H3-0: the H1 carry-over fixes.

(a) the read-mark and first-run budget files are per --target; (b) an incremental event read
shrinks its page after a PowerShell timeout like a first-run read; (d) a failed import of
taskdash_common leaves a bootstrap_FAIL file. No network: PowerShell, AGOL and e-mail are faked.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

import taskdash_collect as collect
import taskdash_publish as publish
from test_first_run_timeout import HISTORY, _fake_powershell

HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------- (a) per-target state files

def test_the_prod_file_names_are_unchanged_and_a_test_target_gets_a_suffix():
    assert publish.target_file_name(publish.EVENT_READ_MARK_FILE, "prod") == "event_read_mark.json"
    assert publish.target_file_name(publish.EVENT_READ_MARK_FILE, "test") == "event_read_mark_test.json"
    assert publish.target_file_name(publish.FIRST_RUN_BUDGET_FILE, "test") == "first_run_budget_test.json"


@pytest.mark.parametrize("target, budget_name, skipped", [
    ("prod", "first_run_budget.json", "799"),
    ("test", "first_run_budget_test.json", "199"),
])
def test_collect_reads_the_state_files_of_its_own_target(monkeypatch, tmp_path, target, budget_name,
                                                        skipped):
    seen = {}

    def read(history, timeout, min_record_id, max_events, budget_path=None):
        seen["budget_path"] = budget_path
        collect.oldest_record_id = 900
        return [], False

    monkeypatch.setattr(collect, "collect_tasks", lambda timeout=180, *_: [])
    monkeypatch.setattr(collect, "load_descriptions", lambda: {})
    monkeypatch.setattr(publish, "read_run_events", read)
    (tmp_path / "event_read_mark.json").write_text('{"record_id": 100}', encoding="utf-8")
    (tmp_path / "event_read_mark_test.json").write_text('{"record_id": 700}', encoding="utf-8")
    config = {"logging": {"log_dir": str(tmp_path)}}

    collected = publish.collect_everything(config, None, min_record_id=50, target=target)

    assert seen["budget_path"] == tmp_path / budget_name
    # The gap warning compares the oldest record (900) with this target's own mark.
    assert collected["event_gap_warning"].startswith(f"{skipped} event record id(s)")


@pytest.fixture
def publish_world(monkeypatch, tmp_path):
    item = type("I", (), {"title": "x", "id": "svc", "access": "private"})()
    calls = []

    def collect_everything(*a, **k):
        calls.append(k)
        return {"runs": [{"max_event_record_id": 77}], "inventory": [], "event_read_mark": 1234}

    monkeypatch.setattr(publish, "connect_agol", lambda cfg: object())
    monkeypatch.setattr(publish, "ensure_service", lambda gis, cfg, target: item)
    monkeypatch.setattr(publish, "get_tables", lambda i: {})
    monkeypatch.setattr(publish, "check_schema_drift", lambda tables, *a: None)
    monkeypatch.setattr(publish, "check_source_sharing", lambda *a: None)
    monkeypatch.setattr(publish, "check_release_sharing", lambda *a: None)
    monkeypatch.setattr(publish, "collect_everything", collect_everything)
    monkeypatch.setattr(publish, "report", lambda collected: None)
    monkeypatch.setattr(publish, "publish", lambda *a, **k: {})
    monkeypatch.setattr(publish, "maybe_send_alert", lambda *a: False)
    monkeypatch.setattr(publish, "_write_collector_run_row", lambda *a, **k: None)

    def run(target, *flags):
        args = publish.parse_args([*flags, "--target", target])
        config = {"logging": {"log_dir": str(tmp_path)}}
        return publish._run_publish(args, config, tmp_path, publish.utc_now(),
                                    {"armed": False, "recorded": False})

    run.calls = calls
    return run


def test_a_test_publish_writes_only_its_own_read_mark_and_clears_only_its_own_budget(
        publish_world, tmp_path):
    prod_budget = tmp_path / "first_run_budget.json"
    prod_budget.write_text('{"configured": 30000, "max_events": 500}', encoding="utf-8")
    test_budget = tmp_path / "first_run_budget_test.json"
    test_budget.write_text('{"configured": 30000, "max_events": 500}', encoding="utf-8")

    assert publish_world("test", "--apply") == 0

    assert json.loads((tmp_path / "event_read_mark_test.json").read_text(encoding="utf-8")) == \
        {"record_id": 1234}
    assert not (tmp_path / "event_read_mark.json").exists()
    assert prod_budget.exists() and not test_budget.exists()


def test_a_prod_publish_keeps_the_default_file_names(publish_world, tmp_path):
    assert publish_world("prod", "--apply") == 0
    assert (tmp_path / "event_read_mark.json").exists()
    assert not (tmp_path / "event_read_mark_prod.json").exists()


# ---------------------------------------------------------------- (b) incremental read shrinks

def test_a_timed_out_incremental_read_halves_the_next_incremental_budget(monkeypatch, tmp_path):
    seen = _fake_powershell(monkeypatch, slow_above=2000)
    path = tmp_path / "budget.json"
    outcomes = []
    for _ in range(6):
        try:
            publish.read_run_events(HISTORY, 60, 500, 0, path)
            outcomes.append("ok")
            break
        except SystemExit:
            outcomes.append("timeout")

    assert outcomes == ["timeout", "timeout", "ok"], seen
    assert seen == [5000, 2500, 1250]
    assert json.loads(path.read_text(encoding="utf-8")) == {"configured": 5000, "max_events": 1250}


def test_the_incremental_budget_stops_at_the_floor_and_names_its_setting(monkeypatch, tmp_path):
    _fake_powershell(monkeypatch, slow_above=0)
    path = tmp_path / "budget.json"
    for _ in range(10):
        with pytest.raises(SystemExit) as exc:
            publish.read_run_events(HISTORY, 60, 500, 0, path)
        assert "history.max_events_per_run" in str(exc.value)
    assert json.loads(path.read_text(encoding="utf-8"))["max_events"] == publish.FIRST_RUN_BUDGET_FLOOR


def test_a_changed_max_events_per_run_ignores_the_saved_incremental_budget(monkeypatch, tmp_path):
    path = tmp_path / "budget.json"
    path.write_text('{"configured": 5000, "max_events": 500}', encoding="utf-8")
    seen = _fake_powershell(monkeypatch, slow_above=10 ** 9)
    publish.read_run_events({**HISTORY, "max_events_per_run": 8000}, 60, 500, 0, path)
    assert seen == [8000]


def test_an_incremental_publish_that_moves_the_watermark_clears_the_budget(
        publish_world, tmp_path, monkeypatch):
    budget = tmp_path / "first_run_budget.json"
    budget.write_text('{"configured": 5000, "max_events": 2500}', encoding="utf-8")
    monkeypatch.setattr(publish, "event_watermark", lambda *a: 10)
    monkeypatch.setattr(publish, "get_tables", lambda i: {publish.TABLE_RUNS: object()})

    assert publish_world("prod", "--apply") == 0

    assert not budget.exists()


def test_a_dry_run_and_a_test_target_pass_their_names_to_the_collection(publish_world):
    assert publish_world("test") == 0
    (kwargs,) = publish_world.calls
    assert kwargs["first_run_budget"] is False and kwargs["target"] == "test"


# ---------------------------------------------------------------- (d) taskdash_common import fails

def _load_cli_without_common(monkeypatch, install_dir, folder_deploy=False):
    """Execute a copy of taskdash_cli.py in install_dir while taskdash_common cannot be imported."""
    if folder_deploy:
        (install_dir / "rules").mkdir()
        (install_dir / "rules" / "default.json").write_text("{}", encoding="utf-8")
    copy = install_dir / "taskdash_cli.py"
    copy.write_text((HERE / "taskdash_cli.py").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setitem(sys.modules, "taskdash_common", None)
    spec = importlib.util.spec_from_file_location("taskdash_cli_import_probe", copy)
    spec.loader.exec_module(importlib.util.module_from_spec(spec))


@pytest.fixture
def dirs(tmp_path):
    home, install = tmp_path / "home", tmp_path / "install"
    home.mkdir()
    install.mkdir()
    return home, install


def test_a_failed_taskdash_common_import_leaves_a_fallback_file_and_reraises(monkeypatch, dirs):
    home, install = dirs
    monkeypatch.setenv("TASKDASH_HOME", str(home))
    with pytest.raises(ImportError):
        _load_cli_without_common(monkeypatch, install)
    (log,) = (home / "Logs").glob("bootstrap_FAIL_*.log")
    text = log.read_text(encoding="utf-8")
    assert "taskdash_common failed to import" in text and "Traceback" in text


def test_the_fallback_file_goes_to_the_venv_parent_when_home_dir_would_choose_it(monkeypatch, dirs, tmp_path):
    # home_dir() falls back to the folder that owns the running venv; the fallback writer must agree.
    home, install = dirs
    venv = home / "venv"
    venv.mkdir()
    (home / "TaskDash.config.json").write_text("{}", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.delenv("TASKDASH_HOME", raising=False)
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "base"))
    with pytest.raises(ImportError):
        _load_cli_without_common(monkeypatch, install)
    assert len(list((home / "Logs").glob("bootstrap_FAIL_*.log"))) == 1
    assert not (elsewhere / "Logs").exists()


def test_a_folder_install_writes_no_fallback_file(monkeypatch, dirs):
    home, install = dirs
    monkeypatch.setenv("TASKDASH_HOME", str(home))
    with pytest.raises(ImportError):
        _load_cli_without_common(monkeypatch, install, folder_deploy=True)
    assert not (home / "Logs").exists()


def test_the_fallback_writer_never_hides_the_import_error(monkeypatch, dirs, capsys):
    home, install = dirs
    (home / "Logs").write_text("a file where the folder should be", encoding="utf-8")
    monkeypatch.setenv("TASKDASH_HOME", str(home))
    with pytest.raises(ImportError):
        _load_cli_without_common(monkeypatch, install)
    assert "could not write bootstrap_FAIL log" in capsys.readouterr().err
