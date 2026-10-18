"""Phase 3: wheel packaging -- runtime paths, the `taskdash` command, the offline dashboard run.

No test signs in or reaches a network: every AGOL entry point is replaced by a fake that fails
the test if it is called.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import taskdash_cli as cli
import taskdash_common as common
import taskdash_dashboard as dash

ROOT = Path(__file__).resolve().parent


# ------------------------------------------------------------------ runtime paths

def test_folder_deploy_resolves_everything_to_the_module_folder():
    assert common._folder_deploy() is True
    assert common.data_dir() == common.home_dir() == common.runtime_dir() == ROOT


@pytest.fixture
def installed(monkeypatch, tmp_path):
    monkeypatch.setattr(common, "_folder_deploy", lambda: False)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.delenv("TASKDASH_HOME", raising=False)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return tmp_path


def test_an_unrelated_rules_package_beside_the_module_is_not_a_folder_deploy(tmp_path, monkeypatch):
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(common, "__file__", str(tmp_path / "taskdash_common.py"))
    assert common._folder_deploy() is False
    (tmp_path / "rules" / "default.json").write_text("{}", encoding="utf-8")
    assert common._folder_deploy() is True


def test_installed_mode_reads_data_from_share_and_keeps_files_in_home(installed, monkeypatch):
    home = installed / "home"
    home.mkdir()
    monkeypatch.setenv("TASKDASH_HOME", str(home))
    assert common.data_dir() == installed / "venv" / "share" / "taskdash"
    assert common.home_dir() == home.resolve()
    assert common.runtime_dir() == home.resolve()


def test_installed_mode_without_taskdash_home_uses_the_working_directory(installed):
    assert common.home_dir() == (installed / "cwd").resolve()


def test_missing_config_message_names_the_real_folder(installed):
    with pytest.raises(SystemExit) as exc:
        common.load_config()
    assert str(common.home_dir()) in str(exc.value) and common.DEFAULT_CONFIG_FILE in str(exc.value)


def test_profile_rules_load_from_home_and_default_rules_from_data(installed, monkeypatch):
    import taskdash_metrics as metrics

    seen = []

    def stop(paths):
        seen.extend(paths)
        raise KeyError("stop after the paths are chosen")

    monkeypatch.setattr(metrics, "_merge_rule_files", stop)
    metrics.load_rules.cache_clear()
    with pytest.raises(KeyError):
        metrics.load_rules(("rules/org.json",))
    metrics.load_rules.cache_clear()
    assert seen == [metrics._RULES_BASE / "rules/default.json", common.home_dir() / "rules/org.json"]


# ------------------------------------------------------------------ CLI

@pytest.mark.parametrize("command, module", [
    ("collect", "taskdash_publish"), ("setup", "taskdash_setup"),
    ("preflight", "taskdash_preflight"), ("dashboard", "taskdash_dashboard"),
])
def test_each_subcommand_passes_the_rest_of_argv_through(monkeypatch, command, module):
    calls = []
    monkeypatch.setitem(sys.modules, module, SimpleNamespace(main=lambda argv: calls.append(argv) or 0))
    assert cli.main([command, "--apply", "--target", "prod", "x"]) == 0
    assert calls == [["--apply", "--target", "prod", "x"]]


def test_subcommand_exit_status_is_returned(monkeypatch):
    monkeypatch.setitem(sys.modules, "taskdash_setup", SimpleNamespace(main=lambda argv: 3))
    assert cli.main(["setup"]) == 3


def test_a_missing_subcommand_module_fails_only_that_subcommand(monkeypatch):
    monkeypatch.setitem(sys.modules, "taskdash_setup", None)
    with pytest.raises(SystemExit) as exc:
        cli.main(["setup"])
    assert "taskdash_setup.py is not part of this install" in str(exc.value)
    assert cli.main(["--version"]) == 0


def test_version_prints_the_package_version(capsys):
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"TaskDash {common.TASKDASH_VERSION}"


def test_no_subcommand_prints_usage_and_exits_2(capsys):
    assert cli.main([]) == 2
    assert "usage: taskdash" in capsys.readouterr().out


def test_unknown_subcommand_usage_goes_to_stderr(capsys):
    assert cli.main(["nonsense"]) == 2
    assert "unknown command 'nonsense'" in capsys.readouterr().err


def _fail_if_called(*args, **kwargs):
    raise AssertionError("signed in or reached AGOL during an offline run")


def test_status_offline_prints_the_newest_local_result_and_never_signs_in(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(common, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(common, "connect_agol", _fail_if_called)
    logs = tmp_path / "Logs"
    logs.mkdir()
    (logs / "taskdash_20260101_000000.log").write_text("old run\nold result\n", encoding="utf-8")
    newest = logs / "taskdash_20260102_000000.log"
    newest.write_text("started\nTaskDash publish complete.\n\n", encoding="utf-8")
    assert cli.main(["status", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "taskdash_20260102_000000.log: TaskDash publish complete." in out


def test_status_reads_the_newest_collector_run_through_the_service(monkeypatch, tmp_path, capsys):
    config = {"arcgis": {"item_id": "ITEM"}, "logging": {"log_dir": str(tmp_path / "Logs")}}
    monkeypatch.setattr(common, "load_config", lambda path=None: config)
    (tmp_path / "TaskDash.config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli, "runtime_dir", lambda: tmp_path)
    queries = []
    row = SimpleNamespace(attributes={"started_utc": 1767225600000, "exit_status": "success"})
    table = SimpleNamespace(query=lambda **kw: queries.append(kw) or SimpleNamespace(features=[row]))
    gis = SimpleNamespace(content=SimpleNamespace(get=lambda item_id: SimpleNamespace(id=item_id)))
    monkeypatch.setattr(common, "connect_agol", lambda cfg: gis)
    monkeypatch.setattr(common, "get_tables", lambda item: {common.TABLE_COLLECTOR_RUNS: table})
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "2026-01-01 00:00:00 UTC, exit_status success" in out
    assert queries[0]["order_by_fields"] == "started_utc DESC" and queries[0]["result_record_count"] == 1


# ------------------------------------------------------------------ offline dashboard

def test_dashboard_offline_builds_validates_and_never_signs_in(monkeypatch, tmp_path):
    monkeypatch.setattr(dash, "connect_agol", _fail_if_called)
    monkeypatch.setattr(dash, "load_config", _fail_if_called)
    monkeypatch.setattr(dash, "runtime_dir", lambda: tmp_path)
    assert dash.main(["--offline"]) == 0
    written = (tmp_path / dash.OUTPUT_JSON).read_text(encoding="utf-8")
    assert dash.OFFLINE_ITEM_ID in written


def test_dashboard_offline_refuses_apply(monkeypatch, tmp_path):
    monkeypatch.setattr(dash, "connect_agol", _fail_if_called)
    monkeypatch.setattr(dash, "runtime_dir", lambda: tmp_path)
    with pytest.raises(SystemExit) as exc:
        dash.main(["--offline", "--apply"])
    assert "cannot --apply" in str(exc.value)
    assert not (tmp_path / dash.OUTPUT_JSON).exists()


# ------------------------------------------------------------------ pyproject

def _pyproject():
    tomllib = pytest.importorskip("tomllib")
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_every_runtime_module_ships_and_no_test_or_tool_does():
    listed = set(_pyproject()["tool"]["setuptools"]["py-modules"])
    on_disk = {p.stem for p in ROOT.glob("taskdash_*.py")}
    assert listed == on_disk


def test_the_private_rules_profile_is_not_a_data_file():
    shipped = _pyproject()["tool"]["setuptools"]["data-files"]
    flat = [name for names in shipped.values() for name in names]
    assert "rules/default.json" in flat
    assert not any("rules/" in name and name != "rules/default.json" for name in flat)


def test_the_console_script_and_version_come_from_the_code():
    project = _pyproject()
    assert project["project"]["scripts"] == {"taskdash": "taskdash_cli:main"}
    assert project["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "taskdash_common.TASKDASH_VERSION"}
