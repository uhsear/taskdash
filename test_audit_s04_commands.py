"""Audit step "Unify command text and help once" (TD-166 to TD-187).

One command form in every message, one program name in every usage line, no silent flag pairs,
a docs pointer on every refusal, and a local `status --offline` that judges the scheduled run.
No test signs in or reaches a network.
"""

from __future__ import annotations

import ast
import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import taskdash_cli as cli
import taskdash_collect as collect
import taskdash_common as common
import taskdash_dashboard as dash
import taskdash_demo as demo
import taskdash_preflight as preflight
import taskdash_publish as publish
import taskdash_remove as remove
import taskdash_setup as setup
import taskdash_signin as signin

ROOT = Path(__file__).resolve().parent
SHIPPED = [p for p in sorted(ROOT.glob("taskdash_*.py")) if p.name != "taskdash_synthetic.py"]


# ------------------------------------------------------------------ TD-166: the bare command

def test_bare_taskdash_lists_every_command_in_install_order(capsys):
    assert cli.main([]) == 2
    text = capsys.readouterr().out
    order = ["signin", "preflight", "setup", "collect", "dashboard", "status", "test-alert", "demo", "remove"]
    positions = [text.index(f"  {name} ") for name in order]
    assert positions == sorted(positions)
    assert set(order) == set(cli.SUBCOMMAND_MODULES) | {"status", "test-alert"}
    assert "docs/INSTALL.md" in text


def test_help_is_an_alias_for_the_list_of_commands(capsys):
    assert cli.main(["help"]) == 0
    assert capsys.readouterr().out.strip() == cli.USAGE


# ------------------------------------------------------------------ TD-167, TD-173: usage lines, --version

@pytest.mark.parametrize("command, entry", [
    ("collect", publish.main), ("setup", setup.main), ("preflight", preflight.main),
    ("dashboard", dash.main), ("demo", demo.main), ("remove", remove.main), ("signin", signin.main),
    ("status", lambda argv: cli.main(["status", *argv])), ("test-alert", lambda argv: cli.main(["test-alert", *argv])),
])
def test_every_command_names_itself_in_its_usage_line(command, entry, capsys):
    with pytest.raises(SystemExit) as stop:
        entry(["--help"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.startswith(f"usage: taskdash {command} ")


@pytest.mark.parametrize("command", ["status", "test-alert"])
def test_status_and_test_alert_accept_version(command, capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main([command, "--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == f"TaskDash {common.TASKDASH_VERSION}"


def test_the_docs_do_not_say_that_status_lacks_version():
    text = (ROOT / "docs" / "CONFIGURE.md").read_text(encoding="utf-8")
    assert "has no `--version` flag" not in text


# ------------------------------------------------------------------ TD-168, TD-174, TD-176, TD-180: help text

def test_help_hides_internal_flags_and_the_setup_duplicates(capsys):
    with pytest.raises(SystemExit):
        preflight.main(["--help"])
    assert "--consume-handoff" not in capsys.readouterr().out
    assert preflight.parse_args(["--consume-handoff"]).consume_handoff is True
    with pytest.raises(SystemExit):
        publish.main(["--help"])
    out = capsys.readouterr().out
    for flag in ("--create-service", "--create-view", "--service-name"):
        assert flag not in out
    assert "taskdash setup" in out


def test_preflight_offline_help_names_every_network_check(capsys):
    with pytest.raises(SystemExit):
        preflight.main(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    for name, needs_network, _ in preflight.CHECKS:
        if needs_network:
            assert name in out
    assert "Read-only" not in out


def test_every_config_option_uses_the_one_help_string(capsys):
    for entry in (publish.main, setup.main, preflight.main, dash.main, demo.main, remove.main, signin.main):
        if entry is demo.main:
            continue  # it also says the file is never written
        with pytest.raises(SystemExit):
            entry(["--help"])
        out = " ".join(capsys.readouterr().out.split())
        assert common.CONFIG_ARG_HELP in out, entry.__module__
        assert "next to the scripts" not in out
    with pytest.raises(SystemExit):
        cli.main(["status", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "--target {prod,test} Which configured service to read." in out


def test_create_service_plan_lists_every_table(tmp_path, capsys):
    assert publish._main_dispatch(publish.parse_args(["--create-service"]), {}, tmp_path) == 0
    out = capsys.readouterr().out
    for name in publish.TABLE_SPECS:
        assert name in out


# ------------------------------------------------------------------ TD-169, TD-171, TD-059: command text

def test_command_text_builds_the_one_form():
    assert common.command_text("collect", "--migrate", "--apply") == "`taskdash collect --migrate --apply`"
    assert common.command_text("setup") == "`taskdash setup`"


def _message_strings(path: Path):
    """Every string constant of a module that is not a docstring or a comment."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body
                  and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value


BARE_FLAG = re.compile(r"\b(?:[Rr]un|[Rr]e-run|[Tt]hen run|[Tt]ry) (?:it )?--[a-z]")
SCRIPT_FORM = re.compile(r"python taskdash_\w+\.py|folder install")


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.name)
def test_no_message_names_a_bare_flag_or_a_script_file(path):
    """`Run --migrate` names no command, and `python taskdash_publish.py` does not exist in a wheel install."""
    bad = [(line, text[:80]) for line, text in _message_strings(path)
           if (BARE_FLAG.search(text) or SCRIPT_FORM.search(text)) and "argparse" not in text]
    assert not bad, f"{path.name}: name the command with taskdash_common.command_text: {bad}"


def test_a_missing_table_points_at_migrate_and_joins_the_names(monkeypatch):
    monkeypatch.setattr(publish, "get_tables", lambda item: {publish.TABLE_RUNS: object()})
    with pytest.raises(SystemExit) as stop:
        publish.publish(None, SimpleNamespace(), {}, {})
    text = str(stop.value)
    assert "[" not in text and f"{publish.TABLE_INVENTORY}, {publish.TABLE_METRICS}" in text
    assert "Run `taskdash collect --migrate --target prod --apply` to create them." in text
    assert "--create-service" not in text


def test_a_test_run_refusal_names_migrate_with_target_test(monkeypatch):
    """A refusal reached by `--target test` must not send the user to write schema to prod."""
    monkeypatch.setattr(publish, "get_tables", lambda item: {publish.TABLE_RUNS: object()})
    with pytest.raises(SystemExit) as stop:
        publish.publish(None, SimpleNamespace(), {}, {}, target="test")
    assert "`taskdash collect --migrate --target test --apply`" in str(stop.value)
    messages = []
    monkeypatch.setattr(publish, "emit", messages.append)
    publish.check_schema_drift({}, "test")
    assert any("--migrate --target test --apply" in m and "--target prod" not in m for m in messages)
    monkeypatch.setattr(publish, "ensure_service", lambda gis, config, target: None)
    monkeypatch.setattr(publish, "connect_agol", lambda config: None)
    monkeypatch.setattr(publish, "get_tables", lambda item: {})
    args = publish.parse_args(["--retire-host", "OTHER", "--target", "test"])
    with pytest.raises(SystemExit) as stop:
        publish.run_retire_host({"host_name_override": "H"}, args)
    assert "--migrate --target test --apply" in str(stop.value)


def test_a_prod_rebuild_is_always_written_with_target_prod_and_apply():
    full = "`taskdash dashboard --target prod --apply`"
    assert full in " ".join((ROOT / "docs" / "TROUBLESHOOTING.md").read_text(encoding="utf-8").split())
    assert common.command_text("dashboard", "--target", "prod", "--apply") == full
    source = Path(publish.__file__).read_text(encoding="utf-8")
    assert "to switch it" in source and "command_text('dashboard', '--target', target, '--apply')" in source


# ------------------------------------------------------------------ TD-170: dashboard messages

def test_publish_failure_shows_the_code_and_message_not_the_dict():
    gis = SimpleNamespace(
        _portal=SimpleNamespace(resturl="https://x/"), url="https://x", users=SimpleNamespace(me=SimpleNamespace(username="u")),
        _con=SimpleNamespace(post=lambda url, params: {"error": {"code": 400, "message": "Item name in use",
                                                                   "details": ["a", "b"]}}))
    with pytest.raises(SystemExit) as stop:
        dash.post_dashboard(gis, {}, "T", None)
    text = str(stop.value)
    assert "error 400: Item name in use (a; b)" in text and "{'error'" not in text
    assert "Check that the account may create items" in text and "The dashboard could not be published" in text


def test_the_missing_table_refusal_joins_the_names():
    item = SimpleNamespace(properties=SimpleNamespace())
    gis = SimpleNamespace(content=SimpleNamespace(get=lambda item_id: item))
    original = dash.get_tables
    dash.get_tables = lambda item: {}
    try:
        with pytest.raises(SystemExit) as stop:
            dash.resolve_layer_ids(gis, "SVC", "prod")
    finally:
        dash.get_tables = original
    text = str(stop.value)
    assert "[" not in text and "taskdash collect --migrate --target prod --apply" in text
    assert "The service has no tables" in text


# ------------------------------------------------------------------ TD-175: flag pairs that do nothing

@pytest.mark.parametrize("argv, fragment", [
    (["--service-name", "X"], "--service-name works only with --create-service"),
    (["--capture-fixture", "f.json", "--apply"], "--capture-fixture writes only the file"),
])
def test_collect_rejects_flag_pairs_that_do_nothing(argv, fragment, capsys):
    with pytest.raises(SystemExit) as stop:
        publish.parse_args(argv)
    assert stop.value.code == 2
    assert fragment in capsys.readouterr().err


def test_collect_still_accepts_service_name_with_create_service():
    args = publish.parse_args(["--create-service", "--service-name", "Mine"])
    assert args.service_name == "Mine"
    assert publish.parse_args([]).service_name == "TaskDash"


def test_status_offline_rejects_host(capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(["status", "--offline", "--host", "X"])
    assert stop.value.code == 2
    assert "--host selects a CollectorRuns host" in capsys.readouterr().err


# ------------------------------------------------------------------ TD-179: status --offline judges the scheduled run

@pytest.fixture
def logs(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(common, "runtime_dir", lambda: tmp_path)
    folder = tmp_path / "Logs"
    folder.mkdir()
    return folder


def _write_log(folder, name, text, age_minutes=0):
    path = folder / name
    path.write_text(text, encoding="utf-8")
    stamp = time.time() - age_minutes * 60
    os.utime(path, (stamp, stamp))
    return path


def test_offline_status_passes_a_fresh_clean_log(logs, capsys):
    _write_log(logs, "taskdash_20260101_000000.log", "x - INFO - started\nx - INFO - TaskDash publish complete.\n")
    assert cli.main(["status", "--offline"]) == 0
    assert "TaskDash publish complete." in capsys.readouterr().out


def test_offline_status_exits_1_when_the_log_holds_an_error_line(logs, capsys):
    _write_log(logs, "taskdash_20260101_000000.log", "x - INFO - started\nx - ERROR - TaskDash exiting: boom\n")
    assert cli.main(["status", "--offline"]) == 1
    assert "The newest scheduled run failed" in capsys.readouterr().out


def test_offline_status_exits_2_when_the_log_is_older_than_two_intervals(logs, capsys):
    _write_log(logs, "taskdash_20260101_000000.log", "x - INFO - TaskDash publish complete.\n", age_minutes=121)
    assert cli.main(["status", "--offline"]) == 2
    assert "more than 120 min" in capsys.readouterr().out


def test_offline_status_skips_the_log_of_a_manual_run(logs, capsys):
    _write_log(logs, "taskdash_20260101_000000.log", "x - ERROR - TaskDash exiting: boom\n", age_minutes=30)
    _write_log(logs, f"{common.MANUAL_LOG_PREFIX}_20260101_010000.log", "x - INFO - DRY RUN\n")
    assert cli.main(["status", "--offline"]) == 1
    out = capsys.readouterr().out
    assert "taskdash_20260101_000000.log" in out and "taskdash_manual" not in out


def test_online_status_prints_no_verdict_or_advice_from_the_local_log(logs, monkeypatch, capsys):
    """The service row decides. This computer may not be the collector, so a missing, failed or
    stale local log must not tell the user to run `collect --apply` here."""
    monkeypatch.setattr(cli, "_status_log_dir", lambda config_path, offline: (logs, {}))
    monkeypatch.setattr(cli, "_remote_status", lambda config, target, host, unsent=0: 0)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "no scheduled run log" in out and "--apply" not in out
    _write_log(logs, "taskdash_20260101_000000.log", "x - ERROR - boom\n", age_minutes=500)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Local: taskdash_20260101_000000.log" in out
    assert "failed" not in out and "old" not in out


def test_offline_status_with_only_manual_logs_finds_no_scheduled_run(logs, capsys):
    _write_log(logs, f"{common.MANUAL_LOG_PREFIX}_20260101_010000.log", "x - INFO - DRY RUN\n")
    assert cli.main(["status", "--offline"]) == 2
    assert "no scheduled run log" in capsys.readouterr().out


@pytest.mark.parametrize("argv, prefix", [
    ([], "taskdash_manual_"),
    (["--target", "test"], "taskdash_manual_"),
    (["--migrate"], "taskdash_manual_"),
    (["--apply"], "taskdash_2"),
])
def test_only_the_hourly_prod_publish_keeps_the_plain_log_prefix(tmp_path, monkeypatch, argv, prefix):
    monkeypatch.setattr(publish, "run_publish", lambda *a, **k: 0)
    monkeypatch.setattr(publish, "run_migration", lambda *a, **k: 0)
    monkeypatch.setattr(publish, "ping_healthcheck", lambda *a, **k: None)
    monkeypatch.setattr(publish, "_run_under_lock", lambda log_dir, action: action())
    config = tmp_path / "c.json"
    config.write_text(json.dumps({"logging": {"log_dir": str(tmp_path)}, "rules": {"profiles": []}}), encoding="utf-8")
    publish.main(["--config", str(config), *argv])
    (log,) = tmp_path.glob("taskdash_*.log")
    assert log.name.startswith(prefix)


def test_a_nonzero_result_leaves_an_error_line_for_status(tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "run_publish", lambda *a, **k: 3)
    config = tmp_path / "c.json"
    config.write_text(json.dumps({"logging": {"log_dir": str(tmp_path)}, "rules": {"profiles": []}}), encoding="utf-8")
    assert publish.main(["--config", str(config)]) == 3
    (log,) = tmp_path.glob("taskdash_manual_*.log")
    assert " - ERROR - TaskDash exiting with code 3." in log.read_text(encoding="utf-8")


# ------------------------------------------------------------------ TD-178: dashboard --apply records the new id

def test_a_new_prod_dashboard_id_is_written_to_the_config_after_a_backup(tmp_path, capsys):
    path = tmp_path / "TaskDash.config.json"
    path.write_text(json.dumps({"arcgis": {"item_id": "SVC"}, "keep": 1}), encoding="utf-8")
    config = {"arcgis": {"item_id": "SVC"}}
    dash._record_dashboard_id(SimpleNamespace(config=str(path)), config, "NEWID")
    assert json.loads(path.read_text(encoding="utf-8")) == {"arcgis": {"item_id": "SVC", "dashboard_id": "NEWID"}, "keep": 1}
    assert config["arcgis"]["dashboard_id"] == "NEWID"
    (backup,) = tmp_path.glob("TaskDash.config.json.bak_setup_*")
    assert json.loads(backup.read_text(encoding="utf-8"))["arcgis"] == {"item_id": "SVC"}


def test_an_unwritable_config_prints_the_manual_step(tmp_path, capsys):
    dash._record_dashboard_id(SimpleNamespace(config=str(tmp_path / "missing.json")), {}, "NEWID")
    out = capsys.readouterr().out
    assert "Put NEWID in arcgis.dashboard_id by hand" in out


# ------------------------------------------------------------------ TD-181, TD-186, TD-187: wording

def test_a_disabled_event_log_names_the_preflight_fix(monkeypatch):
    monkeypatch.setattr(collect, "run_powershell_json", lambda script, timeout: [
        {"exists": True, "is_enabled": False, "error_message": ""}])
    with pytest.raises(SystemExit) as stop:
        collect.check_event_log_health()
    text = str(stop.value)
    assert "wevtutil" not in text and "taskdash preflight --enable-event-log --apply" in text
    assert "The Task Scheduler log is turned off" in text


def test_a_missing_dependency_prints_the_fix_and_keeps_the_traceback_in_the_log(monkeypatch):
    saved = []
    monkeypatch.setattr(cli, "write_bootstrap_fail", saved.append)

    def broken_import(name):
        raise ModuleNotFoundError("No module named 'arcgis'", name="arcgis")

    monkeypatch.setattr(cli.importlib, "import_module", broken_import)
    with pytest.raises(SystemExit) as stop:
        cli._run_module("taskdash_setup", [])
    text = str(stop.value)
    assert "needs the Python package 'arcgis'" in text and "--force-reinstall" in text
    assert "A package that TaskDash needs is missing" in text
    assert "Traceback" in saved[0]


def test_remove_prints_the_finishing_steps_only_when_nothing_was_skipped(monkeypatch, capsys):
    steps = []
    monkeypatch.setattr(remove, "print_next_steps", steps.append)
    monkeypatch.setattr(remove, "classify", lambda gis, entry, me, gone: (object(), "skip", "kept"))
    gis = SimpleNamespace(users=SimpleNamespace(me=SimpleNamespace(username="u")))
    manifest = {"items": [{"order": 1, "id": "A", "type": "Dashboard", "title": "T"}]}
    assert remove.remove(gis, manifest, False, "TaskDash Collector") == 0
    assert steps == []
    monkeypatch.setattr(remove, "classify", lambda gis, entry, me, gone: (object(), "gone", ""))
    assert remove.remove(gis, manifest, False, "TaskDash Collector") == 0
    assert steps == ["TaskDash Collector"]


def test_a_corrupt_demo_config_is_a_message_not_a_traceback(tmp_path):
    path = tmp_path / "TaskDash.config.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit) as stop:
        demo.read_demo_arcgis(path)
    assert "cannot be read: JSONDecodeError" in str(stop.value)


def test_the_status_failure_message_points_to_the_docs_once_and_offers_offline(monkeypatch, capsys):
    config = {"arcgis": {"item_id": "ITEM"}}
    monkeypatch.setattr(common, "load_config", lambda path=None: config)
    monkeypatch.setattr(common, "connect_agol", lambda cfg: SimpleNamespace(
        content=SimpleNamespace(get=lambda item_id: SimpleNamespace(id=item_id))))
    row = SimpleNamespace(attributes={"started_utc": 1767225600000, "exit_status": "failed"})
    table = SimpleNamespace(query=lambda **kw: SimpleNamespace(features=[row]))
    monkeypatch.setattr(common, "get_tables", lambda item: {common.TABLE_COLLECTOR_RUNS: table})
    assert cli._remote_status(config, "prod", None) == 1
    out = capsys.readouterr().out
    assert out.count("TROUBLESHOOTING.md") == 1 and "taskdash status --offline" in out


# ------------------------------------------------------------------ TD-183: a docs pointer on every refusal

# Every other `raise SystemExit(<message>)` in a shipped module must carry see_troubleshooting.
# These do not, on purpose: the dashboard's definition checks get theirs from the two decorated
# entry points (build, validate_definition), and _run_cli adds the scrubbed text of an exit that
# already carries its own.
NO_POINTER = {
    ("taskdash_publish.py", "_run_cli"),
    ("taskdash_dashboard.py", "field_filter_type"),
    ("taskdash_dashboard.py", "route_fields_through_arcade"),
    ("taskdash_dashboard.py", "_check_target"),
    ("taskdash_dashboard.py", "_check_field"),
    ("taskdash_dashboard.py", "check_chart_series"),
    ("taskdash_dashboard.py", "check_table_columns"),
    ("taskdash_dashboard.py", "check_events"),
    ("taskdash_dashboard.py", "check_required_sources"),
    ("taskdash_dashboard.py", "check_caps_and_help"),
    ("taskdash_dashboard.py", "check_filter_types"),
    ("taskdash_dashboard.py", "check_arcade_sources"),
    ("taskdash_dashboard.py", "check_data_item"),
    ("taskdash_dashboard.py", "check_hidden_fields"),
    ("taskdash_dashboard.py", "validate_definition"),
}


def _exits_without_a_pointer():
    found = set()
    for path in SHIPPED:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                    and getattr(node.exc.func, "id", "") == "SystemExit" and node.exc.args):
                continue
            arg = node.exc.args[0]
            if isinstance(arg, ast.Call) and getattr(arg.func, "id", "") == "main":
                continue
            if any(isinstance(c, ast.Call) and getattr(c.func, "id", getattr(c.func, "attr", "")) == "see_troubleshooting"
                   for c in ast.walk(node.exc)):
                continue
            owner = node
            while owner in parents and not isinstance(owner, ast.FunctionDef):
                owner = parents[owner]
            found.add((path.name, owner.name if isinstance(owner, ast.FunctionDef) else "<module>"))
    return found


def test_every_refusal_names_its_section_or_is_on_the_allow_list():
    unlisted = _exits_without_a_pointer() - NO_POINTER
    assert not unlisted, (f"add see_troubleshooting(<heading>) to these exits, or list them in NO_POINTER "
                          f"with the reason: {sorted(unlisted)}")


def test_the_allow_list_holds_only_exits_that_still_exist():
    assert NO_POINTER - _exits_without_a_pointer() == set()


def test_a_definition_check_refusal_gets_the_shared_section():
    assert hasattr(dash.build, "__wrapped__") and hasattr(dash.validate_definition, "__wrapped__")
    with pytest.raises(SystemExit) as stop:
        dash.validate_definition({"filter": "'TESTHOST'"}, config={"host_name_override": "TESTHOST"})
    text = str(stop.value)
    assert "hardcoded host filter" in text
    assert text.endswith("See TROUBLESHOOTING.md, section The definition check refuses the dashboard.")
