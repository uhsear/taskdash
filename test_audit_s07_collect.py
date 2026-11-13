"""Audit step "Fix collection accuracy": collector and publisher (TD-041..043, TD-147, TD-148, TD-153, TD-154)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

import taskdash_collect as collect
import taskdash_common as common
import taskdash_publish as publish
from test_taskdash import NOW, _event, _task

PRO = r"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\pythonw.exe"


def _action(execute, arguments="", working_directory=""):
    return {"execute": execute, "arguments": arguments, "working_directory": working_directory}


# ---------------------------------------------------------------- TD-041 relative paths

def test_td041_a_relative_script_is_checked_under_the_start_in_folder():
    row = _task(primary_executable=r"C:\Python\python.exe", primary_arguments=r"bin\run.py",
                primary_working_directory=r"C:\Work")
    seen = []

    def exists(path):
        seen.append(path)
        return path != r"C:\Work\bin\run.py"

    assert collect.check_broken_action_targets(row, exists=exists) == [
        r"script not found: C:\Work\bin\run.py"]
    assert r"bin\run.py" not in seen, "the bare relative path must never reach exists()"


def test_td041_a_moved_bare_script_name_is_flagged_when_the_start_in_folder_is_known():
    row = _task(primary_executable=r"C:\Python\python.exe", primary_arguments="run.py",
                primary_working_directory=r"C:\Work")
    warnings = collect.check_broken_action_targets(row, exists=lambda p: p != r"C:\Work\run.py")

    assert warnings == [r"script not found: C:\Work\run.py"]


def test_td041_a_relative_script_with_no_start_in_folder_is_not_checked_against_the_cwd():
    row = _task(primary_executable=r"C:\Python\python.exe", primary_arguments=r"bin\run.py",
                primary_working_directory="")
    asked = []
    collect.check_broken_action_targets(row, exists=lambda p: asked.append(p) or False)

    assert asked == [r"C:\Python\python.exe"]


def test_td041_an_absolute_script_is_checked_as_before():
    row = _task(primary_executable=r"C:\Python\python.exe", primary_arguments=r"D:\Jobs\run.py",
                primary_working_directory=r"C:\Work")

    assert collect.check_broken_action_targets(row, exists=lambda p: p != r"D:\Jobs\run.py") == [
        r"script not found: D:\Jobs\run.py"]


# ---------------------------------------------------------------- TD-042 every action

def _two_actions(**extra):
    return _task(
        action_details=[_action(r"C:\Python\python.exe", r"C:\Jobs\first.py"),
                        _action(r"C:\Python\python.exe", r"C:\Jobs\second.py", r"C:\Gone")],
        primary_executable=r"C:\Python\python.exe", primary_arguments=r"C:\Jobs\first.py", **extra)


def test_td042_the_second_action_gets_the_broken_target_checks():
    warnings = collect.check_broken_action_targets(
        _two_actions(), exists=lambda p: p not in (r"C:\Jobs\second.py", r"C:\Gone"))

    assert warnings == [r"script not found: C:\Jobs\second.py", r"working directory not found: C:\Gone"]


def test_td042_a_task_with_one_action_reports_it_once():
    row = _task(action_details=[_action(r"C:\Python\python.exe", r"C:\Jobs\only.py")],
                primary_executable=r"C:\Python\python.exe", primary_arguments=r"C:\Jobs\only.py")

    assert collect.check_broken_action_targets(row, exists=lambda p: False) == [
        r"interpreter/executable not found: C:\Python\python.exe", r"script not found: C:\Jobs\only.py"]


def test_td042_the_logon_note_covers_a_later_action_and_appears_once():
    first_only = _task(
        logon_type="Password", action_details=[_action(r"C:\Windows\notepad.exe"), _action(PRO, r"C:\Jobs\b.py")],
        primary_executable=r"C:\Windows\notepad.exe", primary_arguments="")
    both = _task(logon_type="Password", action_details=[_action(PRO, r"C:\Jobs\a.py"), _action(PRO, r"C:\Jobs\b.py")],
                 primary_executable=PRO, primary_arguments=r"C:\Jobs\a.py")

    assert "LogonType 'Password'" in collect.build_config_warnings(first_only, "Daily", exists=lambda p: True)
    assert collect.build_config_warnings(both, "Daily", exists=lambda p: True).count("LogonType 'Password'") == 1


def test_td042_a_row_without_action_details_still_uses_its_primary_action():
    row = _task(primary_executable=r"C:\Python\python.exe", primary_arguments=r"C:\Jobs\x.py")
    row.pop("action_details", None)

    assert collect.row_actions(row) == [(r"C:\Python\python.exe", r"C:\Jobs\x.py", None)]


# ---------------------------------------------------------------- TD-043 unreadable event log

def test_td043_a_failed_history_query_is_a_catchable_event_log_error(monkeypatch):
    monkeypatch.setattr(collect, "check_event_log_health", lambda **k: {})

    def denied(script, timeout):
        raise SystemExit("PowerShell collection failed (exit 1): Access is denied.")

    monkeypatch.setattr(collect, "run_powershell_json", denied)
    with pytest.raises(collect.EventLogUnreadable, match="Access is denied"):
        collect.collect_run_events(30, 100)


def test_td043_a_timeout_stays_a_timeout(monkeypatch):
    monkeypatch.setattr(collect, "check_event_log_health", lambda **k: {})

    def slow(script, timeout):
        raise common.PowerShellTimeout("too slow")

    monkeypatch.setattr(collect, "run_powershell_json", slow)
    with pytest.raises(common.PowerShellTimeout):
        collect.collect_run_events(30, 100)


def test_td043_a_log_that_cannot_be_opened_is_catchable(monkeypatch):
    monkeypatch.setattr(collect, "run_powershell_json", lambda script, timeout: [
        {"exists": False, "is_enabled": False, "error_message": "Access is denied."}])
    with pytest.raises(collect.EventLogUnreadable, match="Access is denied"):
        collect.check_event_log_health()


@pytest.mark.parametrize("failure", [
    collect.EventLogUnreadable("Cannot read event log 'X': Access is denied."),
    collect.EventLogDisabled("disabled"),
])
def test_td043_collect_everything_goes_on_with_zero_events(monkeypatch, tmp_path, failure):
    def failing(*args, **kwargs):
        raise failure

    monkeypatch.setattr(collect, "collect_tasks", lambda timeout, *_: [_task()])
    monkeypatch.setattr(collect, "collect_run_events", failing)
    monkeypatch.setattr(collect, "load_descriptions", lambda: {})
    collected = publish.collect_everything({"logging": {"log_dir": str(tmp_path)}}, None)

    assert [r["task_name"] for r in collected["inventory"]] == ["Parcel Refresh"]
    assert collected["raw_events"] == [] and collected["event_log_error"] is failure


# ---------------------------------------------------------------- TD-147 stored duration reaches the miner

def test_td147_the_miner_sees_the_last_run_duration(monkeypatch, tmp_path):
    events = [_event(100, 30), _event(201, 10, result_code=0), _event(102, 10)]
    monkeypatch.setattr(collect, "collect_tasks", lambda timeout, *_: [_task()])
    monkeypatch.setattr(collect, "collect_run_events", lambda *a, **k: events)
    monkeypatch.setattr(collect, "load_descriptions", lambda: {})
    seen = {}

    def mine(config, inventory, host_name, examined=None):
        seen.update({r["task_full_name"]: r.get("last_duration_seconds") for r in inventory})
        return [], {}

    monkeypatch.setattr(publish, "mine_metrics", mine)
    publish.collect_everything({"logging": {"log_dir": str(tmp_path)}}, None)

    assert seen == {"\\Parcel Refresh": 20 * 60.0}


def test_td147_a_pass_without_new_events_uses_the_stored_duration(monkeypatch, tmp_path):
    monkeypatch.setattr(collect, "collect_tasks", lambda timeout, *_: [_task(), _task(task_name="Other", task_full_name="\\Other")])
    monkeypatch.setattr(collect, "collect_run_events", lambda *a, **k: [])
    monkeypatch.setattr(collect, "load_descriptions", lambda: {})
    seen = {}

    def mine(config, inventory, host_name, examined=None):
        seen.update({r["task_full_name"]: r.get("last_duration_seconds") for r in inventory})
        return [], {}

    monkeypatch.setattr(publish, "mine_metrics", mine)
    publish.collect_everything({"logging": {"log_dir": str(tmp_path)}}, None,
                               stored_durations={"\\Parcel Refresh": 1200.0})

    assert seen["\\Parcel Refresh"] == 1200.0
    assert [v for k, v in seen.items() if k != "\\Parcel Refresh"] == [None]


def test_td147_the_stored_durations_read_names_only_this_host_and_survives_a_failure(monkeypatch):
    asked = {}

    def rows(table, where="", out_fields=""):
        asked.update(where=where, fields=out_fields)
        return [{"task_full_name": "\\A", "last_duration_seconds": 90.0},
                {"task_full_name": "\\B", "last_duration_seconds": None}]

    monkeypatch.setattr(publish, "query_all_rows", rows)
    assert publish._fetch_stored_durations("t", "H1") == {"\\A": 90.0}
    assert asked["where"] == "host_name = 'H1'"

    def broken(*a, **k):
        raise RuntimeError("offline")

    monkeypatch.setattr(publish, "query_all_rows", broken)
    assert publish._fetch_stored_durations("t", "H1") == {}


# ---------------------------------------------------------------- TD-153 bounded TaskChanges key read

def test_td153_the_taskchanges_key_read_is_bounded_by_this_pass(monkeypatch):
    calls = []

    def fake_upsert(table, rows, key_fields, *, chunk_size, label, order_field="", existing_where=""):
        calls.append((label, existing_where))
        return {"added": 0, "updated": 0, "deleted": 0}

    change = {"host_name": "H", "task_full_name": "\\T", "task_name": "T", "change_kind": "became_failing",
              "changed_utc": publish.utc_now().isoformat(), "old_summary": "", "new_summary": "boom"}
    tables = {publish.TABLE_INVENTORY: "i", publish.TABLE_RUNS: "r", publish.TABLE_METRICS: "m",
              publish.TABLE_TASK_CHANGES: "c"}
    monkeypatch.setattr(publish, "upsert_by_key", fake_upsert)
    monkeypatch.setattr(publish, "get_tables", lambda item: tables)
    monkeypatch.setattr(publish, "sync_inventory", lambda *a, **k: {"added": 0, "updated": 0, "deleted": 0})
    monkeypatch.setattr(publish, "summarize_runs_from_agol", lambda *a, **k: {})
    monkeypatch.setattr(publish, "fetch_other_host_counts", lambda *a, **k: [])
    monkeypatch.setattr(publish, "purge_older_than", lambda *a, **k: 0)
    monkeypatch.setattr(publish, "enrich_inventory", lambda *a, **k: None)
    monkeypatch.setattr(publish, "_fetch_prior_inventory_for_changes", lambda *a: {"\\T": {}})
    monkeypatch.setattr(publish, "detect_task_changes", lambda *a, **k: [change])
    collected = {"runs": [], "metrics": [], "host_name": "H", "log_summary": {},
                 "inventory": [{"task_full_name": "\\T", "host_name": "H"}]}
    publish.publish(None, None, collected, {})

    (where,) = [w for label, w in calls if label == publish.TABLE_TASK_CHANGES]
    assert where == "changed_utc >= TIMESTAMP '2026-09-25 12:00:00'"


# ---------------------------------------------------------------- TD-154 retried liveness query

def test_td154_a_transient_error_does_not_abort_the_liveness_query(monkeypatch):
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    started = publish.utc_now().timestamp() * 1000 - 60000
    attempts = []

    class Runs:
        def query(self, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise ConnectionError("reset")
            return SimpleNamespace(features=[SimpleNamespace(attributes={"started_utc": started})])

    assert publish._previous_name_is_live(Runs(), "OLD", 60) is True
    assert len(attempts) == 2
