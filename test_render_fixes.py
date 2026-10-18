"""Collector/publisher fixes found by rendering TaskDash_TEST: readable TaskChanges summaries and
no false "not found" config notes when a capture from another host is replayed."""

from __future__ import annotations

import json

import taskdash_common

taskdash_common.disable_arcpy_import()

import taskdash_publish as publish  # noqa: E402

PYTHONW = r"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\pythonw.exe"
SCRIPT = r"C:\NoSuchHost\AddressDataScript\Stage1.py"


def test_definition_summary_is_plain_text():
    row = {"actions": f'"{PYTHONW}" {SCRIPT}', "run_as_user": "svc_gisjobs",
           "logon_type": "Interactive", "enabled": 1}
    summary = publish._definition_summary(row)
    assert summary == f"Actions: {PYTHONW} {SCRIPT} | Runs as: svc_gisjobs (Interactive) | Enabled: yes"
    assert "\\\\" not in summary and "'" not in summary
    bare = publish._definition_summary({"actions": "", "run_as_user": "", "logon_type": "", "enabled": 0})
    assert bare == "Actions: none | Runs as: unknown | Enabled: no"
    assert len(publish._definition_summary({"actions": "x" * 5000})) <= 1000


def test_a_fixture_replay_skips_only_the_path_existence_checks(tmp_path):
    task = {
        "task_name": "T", "task_path": "\\", "task_full_name": "\\T", "state": "Ready",
        "enabled": True, "actions": "", "primary_executable": PYTHONW, "primary_arguments": SCRIPT,
        "primary_working_directory": r"C:\NoSuchHost", "trigger_types": "Daily",
        "run_as_user": "svc", "logon_type": "Password", "last_run_time": None,
        "next_run_time": None, "last_task_result": None, "number_of_missed_runs": 0,
    }
    fixture = tmp_path / "f.json"
    fixture.write_text(json.dumps({"host_name": "H", "captured_at_utc": "2026-09-25T00:00:00Z",
                                   "tasks": [task], "events": []}), encoding="utf-8")
    warnings = publish.collect_everything({}, fixture)["inventory"][0]["config_warnings"]
    assert "not found" not in warnings
    assert "LogonType" in warnings  # captured data only, so it still runs
