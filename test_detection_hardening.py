"""Tests for detection-hardening items 3.3 (task configuration checks) and 3.4 (missed vs
failed + per-task overdue grace) in taskdash_collect.py.

Pure-function coverage only: no ArcGIS Online calls, no real PowerShell. Runs on system Python.

    cd <TaskDash folder>
    python -m pytest test_detection_hardening.py -q
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

import taskdash_collect as collect
from test_taskdash import NOW, _task, iso

# ---------------------------------------------------------------- 3.3(a) broken action targets

def test_broken_action_targets_flags_each_missing_path():
    row = _task(
        primary_executable=r"C:\Missing\python.exe",
        primary_arguments=r"C:\Missing\script.py",
        primary_working_directory=r"C:\Missing\wd",
    )
    warnings = collect.check_broken_action_targets(row, exists=lambda p: False)
    assert any("interpreter/executable not found" in w for w in warnings)
    assert any("script not found" in w for w in warnings)
    assert any("working directory not found" in w for w in warnings)


def test_broken_action_targets_silent_when_everything_exists():
    row = _task()
    assert collect.check_broken_action_targets(row, exists=lambda p: True) == []


def test_broken_action_targets_ignores_bare_command_name():
    """A bare "python" (no directory component) resolves through PATH and is not a path this
    check can evaluate -- it must not be reported as missing."""
    row = _task(primary_executable="python", primary_arguments="", primary_working_directory="")
    assert collect.check_broken_action_targets(row, exists=lambda p: False) == []


def test_broken_action_targets_expands_environment_variables(monkeypatch):
    """%SystemRoot%\\System32\\cmd.exe is a real, existing path -- the literal string with the
    unexpanded %VAR% in it is not, so exists() must see it expanded."""
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    row = _task(primary_executable=r"%SystemRoot%\System32\cmd.exe", primary_arguments="",
               primary_working_directory=r"%SystemRoot%\Temp")
    seen = []

    def exists(p):
        seen.append(p)
        return "%" not in p

    assert collect.check_broken_action_targets(row, exists=exists) == []
    assert all("%" not in p for p in seen)


# ---------------------------------------------------------------- 3.3(b) LogonType drift

PRO_PYTHON = r"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\pythonw.exe"


def test_logon_type_drift_warns_when_not_interactive():
    warnings = collect.check_logon_type_drift(PRO_PYTHON, "Password")
    assert warnings and "Interactive" in warnings[0]
    assert "Set-ScheduledTask -User -Password" in warnings[0]


def test_logon_type_drift_silent_when_interactive():
    assert collect.check_logon_type_drift(PRO_PYTHON, "Interactive") == []


def test_logon_type_drift_ignores_non_pro_interpreter():
    assert collect.check_logon_type_drift(r"C:\Python313\python.exe", "Password") == []


def test_logon_type_drift_pattern_is_configurable():
    warnings = collect.check_logon_type_drift(
        r"C:\Tools\myrunner.exe", "Password", interactive_required_pattern="\\Tools\\myrunner"
    )
    assert warnings


# ---------------------------------------------------------------- 3.3(c) silent-skip settings

def test_parse_iso8601_duration_seconds():
    assert collect.parse_iso8601_duration_seconds("PT0S") == 0.0
    assert collect.parse_iso8601_duration_seconds("PT72H") == 259200.0
    assert collect.parse_iso8601_duration_seconds("") is None
    assert collect.parse_iso8601_duration_seconds(None) is None


def test_silent_skip_warns_ignorenew_with_no_time_limit():
    warnings = collect.check_silent_skip_settings("IgnoreNew", "PT0S", True, "Daily")
    assert any("IgnoreNew" in w for w in warnings)


def test_silent_skip_silent_when_time_limit_is_set():
    assert collect.check_silent_skip_settings("IgnoreNew", "PT72H", True, "Daily") == []


def test_silent_skip_warns_start_when_available_false_on_daily():
    warnings = collect.check_silent_skip_settings("Parallel", "PT72H", False, "Daily")
    assert any("StartWhenAvailable" in w for w in warnings)


def test_silent_skip_silent_start_when_available_false_on_one_time():
    assert collect.check_silent_skip_settings("Parallel", "PT72H", False, "One Time") == []


# ---------------------------------------------------------------- 3.3 wired into inventory rows

def test_build_inventory_rows_sets_config_warnings_and_logon_type():
    row = collect.build_inventory_rows([_task()], host_name="H", descriptions={}, audit_dt=NOW)[0]
    assert row["logon_type"] == ""
    assert "Interactive" in row["config_warnings"]


def test_config_warnings_never_change_health_status_or_needs_attention():
    """3.3 checks are always Warning-severity data on their own field -- they must not flip an
    otherwise-healthy task's health_status/needs_attention."""
    row = collect.build_inventory_rows([_task()], host_name="H", descriptions={}, audit_dt=NOW)[0]
    assert row["config_warnings"] != ""
    assert row["health_status"] == "Success"
    assert row["needs_attention"] == 0


# ---------------------------------------------------------------- 3.4 per-task overdue grace

def test_load_descriptions_accepts_string_and_object_entries(tmp_path):
    path = tmp_path / "workflow_descriptions.json"
    path.write_text(json.dumps({
        "tasks": {
            "Plain Task": "Plain text description.",
            "Grace Task": {"description": "Has grace.", "overdue_grace_minutes": 45},
        }
    }), encoding="utf-8")

    descriptions = collect.load_descriptions(path)

    assert descriptions["plain task"]["description"] == "Plain text description."
    assert descriptions["plain task"]["overdue_grace_minutes"] is None
    assert descriptions["grace task"]["description"] == "Has grace."
    assert descriptions["grace task"]["overdue_grace_minutes"] == 45.0


def test_a_tasks_value_that_is_not_an_object_uses_no_descriptions(tmp_path, capsys):
    path = tmp_path / "workflow_descriptions.json"
    path.write_text(json.dumps({"tasks": [{"name": "Job"}]}), encoding="utf-8")
    assert collect.load_descriptions(path) == {}
    assert '"tasks" must be an object' in capsys.readouterr().out


@pytest.mark.parametrize("grace", [True, "soon", float("nan"), float("inf"), -5, 10 ** 12])
def test_a_bad_grace_is_ignored_with_a_warning_and_the_rest_of_the_file_applies(tmp_path, capsys, grace):
    path = tmp_path / "workflow_descriptions.json"
    path.write_text(json.dumps({"tasks": {
        "Bad": {"description": "Keeps this.", "overdue_grace_minutes": grace},
        "Good": {"overdue_grace_minutes": 30}}}), encoding="utf-8")  # json.dumps writes NaN as a bare NaN
    descriptions = collect.load_descriptions(path)
    assert descriptions["bad"]["description"] == "Keeps this."
    assert descriptions["bad"]["overdue_grace_minutes"] is None
    assert descriptions["good"]["overdue_grace_minutes"] == 30.0
    assert "for task 'bad': overdue_grace_minutes must be a number" in capsys.readouterr().out


@pytest.mark.parametrize("acknowledged", [True, 7, {"a": 1}, [1, 2], [["x"]]])
def test_a_bad_acknowledgement_is_ignored_with_a_warning(tmp_path, capsys, acknowledged):
    path = tmp_path / "workflow_descriptions.json"
    path.write_text(json.dumps({"tasks": {
        "Job": {"description": "Keeps this.", "acknowledge_config_warnings": acknowledged}}}), encoding="utf-8")
    entry = collect.load_descriptions(path)["job"]
    assert entry["description"] == "Keeps this." and entry["acknowledge_config_warnings"] == []
    assert "acknowledge_config_warnings must be a text or a list of texts" in capsys.readouterr().out


def test_an_entry_that_is_neither_text_nor_object_is_skipped_with_a_warning(tmp_path, capsys):
    path = tmp_path / "workflow_descriptions.json"
    path.write_text(json.dumps({"tasks": {"Bad": ["x"], "Num": 5, "Ok": "Fine."}}), encoding="utf-8")
    assert list(collect.load_descriptions(path)) == ["ok"]
    out = capsys.readouterr().out
    assert "for task 'bad': the entry must be a text or an object" in out and "'num'" in out


def test_per_task_grace_override_changes_overdue():
    task = _task(next_run_time=iso(NOW - timedelta(minutes=5)))

    tight = collect.build_inventory_rows(
        [task], host_name="H",
        descriptions={"parcel refresh": {"description": "", "overdue_grace_minutes": 1}},
        audit_dt=NOW, overdue_grace_minutes=15,
    )[0]
    default = collect.build_inventory_rows(
        [task], host_name="H", descriptions={}, audit_dt=NOW, overdue_grace_minutes=15,
    )[0]

    assert tight["is_overdue"] == 1, "a 1-minute per-task grace must flag a 5-minute-late run"
    assert default["is_overdue"] == 0, "the 15-minute global grace absorbs the same 5 minutes"


def test_old_flat_string_descriptions_dict_still_works():
    """build_inventory_rows must keep accepting the pre-3.4 {name: "text"} shape passed
    directly (bypassing load_descriptions), not just the new {name: {...}} shape."""
    row = collect.build_inventory_rows(
        [_task()], host_name="H", descriptions={"parcel refresh": "Rebuilds parcels."},
        audit_dt=NOW,
    )[0]
    assert row["description"] == "Rebuilds parcels."



PRO_PY = r"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"


def test_a_pyodbc_only_script_may_use_a_password_logon():
    """Replica Lag Monitor on TASKHOST-1: Pro's python, Password logon, no arcpy."""
    assert collect.check_logon_type_drift(PRO_PY, "Password", imports_arcpy=False) == []


def test_an_arcpy_script_or_an_unreadable_one_is_still_flagged():
    assert collect.check_logon_type_drift(PRO_PY, "Password", imports_arcpy=True)
    assert collect.check_logon_type_drift(PRO_PY, "Password", imports_arcpy=None)


def test_script_imports_arcpy_reads_the_script(tmp_path):
    uses = tmp_path / "a.py"
    uses.write_text("import os\nfrom arcpy import da\n", encoding="utf-8")
    plain = tmp_path / "b.py"
    plain.write_text("import pyodbc  # not arcpy\n", encoding="utf-8")
    assert collect.script_imports_arcpy(str(uses)) is True
    assert collect.script_imports_arcpy(str(plain)) is False
    assert collect.script_imports_arcpy(str(tmp_path / "missing.py")) is None
    assert collect.script_imports_arcpy("run.cmd") is None
