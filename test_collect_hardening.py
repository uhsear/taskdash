"""Tests for the collect-05/06/07/08/09 hardening items in taskdash_collect.py.

Pure-function coverage only, plus a couple of mocked-PowerShell checks: no ArcGIS Online
calls, no real PowerShell. Runs on system Python.

    cd <TaskDash folder>
    python -m pytest test_collect_hardening.py -q
"""

from __future__ import annotations

from datetime import timedelta

import pytest

import taskdash_collect as collect
from test_taskdash import NOW, _event, _task, iso

# ---------------------------------------------------------------- 1.1 event-log health

def test_check_event_log_health_passes_when_enabled(monkeypatch):
    monkeypatch.setattr(collect, "run_powershell_json", lambda script, timeout: [
        {"exists": True, "is_enabled": True, "record_count": 16300,
         "oldest_record_number": 1, "file_size": 1048576, "error_message": ""}
    ])
    info = collect.check_event_log_health()
    assert info["is_enabled"] is True


def test_check_event_log_health_raises_when_disabled(monkeypatch):
    monkeypatch.setattr(collect, "run_powershell_json", lambda script, timeout: [
        {"exists": True, "is_enabled": False, "record_count": 0,
         "oldest_record_number": None, "file_size": 0, "error_message": ""}
    ])
    with pytest.raises(SystemExit) as exc:
        collect.check_event_log_health()
    assert "wevtutil sl" in str(exc.value)
    assert "/e:true" in str(exc.value)


def test_check_event_log_health_raises_when_missing_or_access_denied(monkeypatch):
    monkeypatch.setattr(collect, "run_powershell_json", lambda script, timeout: [
        {"exists": False, "is_enabled": False, "record_count": None,
         "oldest_record_number": None, "file_size": None, "error_message": "Access is denied."}
    ])
    with pytest.raises(SystemExit) as exc:
        collect.check_event_log_health()
    assert "Access is denied" in str(exc.value)


def test_collect_run_events_checks_log_health_before_querying(monkeypatch):
    calls = []

    def fail_health(**kwargs):
        calls.append("health")
        raise SystemExit("log disabled")

    def spy_query(script, timeout):
        calls.append("query")
        return []

    monkeypatch.setattr(collect, "check_event_log_health", fail_health)
    monkeypatch.setattr(collect, "run_powershell_json", spy_query)
    with pytest.raises(SystemExit):
        collect.collect_run_events(30, 5000)
    assert calls == ["health"], "a disabled/unreadable log must stop the query, not just the checker"


def test_log_health_query_sets_utf8_output_encoding():
    """Without this, a localized non-ASCII error_message (decoded as UTF-8 by
    run_powershell_json) comes out garbled -- matching the other two PS payloads."""
    assert "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8" in collect.POWERSHELL_LOG_HEALTH_QUERY_TEMPLATE


def test_history_query_only_swallows_no_matching_events(monkeypatch):
    """The old -ErrorAction SilentlyContinue swallowed every failure; the FQEID catch must
    only forgive the well-known empty-result outcome, not e.g. access denied."""
    template = collect.POWERSHELL_HISTORY_QUERY_TEMPLATE
    assert "-ErrorAction SilentlyContinue" not in template
    assert "NoMatchingEventsFound" in template
    assert "throw" in template


# ---------------------------------------------------------------- 1.2 outcome events

def test_history_event_ids_include_the_verified_outcome_events():
    """Verified 2026-09-25 against wevtutil gp Microsoft-Windows-TaskScheduler /ge /gm:true
    and real TASKHOST-1-shaped events, not from memory."""
    for event_id in (100, 101, 102, 103, 111, 153, 200, 201, 202, 203, 322, 329, 330, 332):
        assert event_id in collect.HISTORY_EVENT_IDS


@pytest.fixture
def inventory():
    return collect.build_inventory_rows([_task()], host_name="TASKHOST-1",
                                        descriptions={}, audit_dt=NOW)


def test_launch_failure_has_no_instance_id_and_gets_its_own_row(inventory):
    """Real TASKHOST-1 event 101 carries only TaskName/UserContext/ResultCode -- no
    InstanceId, no TaskInstanceId."""
    events = [_event(101, 5, instance="", event_record_id=5001)]
    rows = collect.build_run_rows(events, inventory, host_name="TASKHOST-1")
    assert len(rows) == 1
    assert rows[0]["run_key"].startswith("TASKHOST-1|\\Parcel Refresh|rec5001|")
    assert rows[0]["run_status"] == "StartFailed"
    assert rows[0]["is_error"] == 1
    assert rows[0]["is_completed"] == 1


def test_launch_failure_result_code_surfaces_the_real_reason(inventory):
    """101/103/202/203 carry a ResultCode too; without reading it the row gets result_code=None
    and the generic OUTCOME_EVENT_STATUS text instead of e.g. access denied."""
    events = [_event(101, 5, instance="", event_record_id=5010, result_code=0x80070005)]
    rows = collect.build_run_rows(events, inventory, host_name="TASKHOST-1")
    assert rows[0]["result_code"] == 0x80070005
    assert rows[0]["result_text"] != "Task Scheduler could not start the task (launch failure)."


def test_not_logged_on_has_no_instance_id_and_is_an_error(inventory):
    """Real TASKHOST-1 event 332 carries only TaskName/UserName."""
    events = [_event(332, 5, instance="", event_record_id=5002)]
    rows = collect.build_run_rows(events, inventory, host_name="TASKHOST-1")
    assert rows[0]["run_status"] == "NotLoggedOn"
    assert rows[0]["is_error"] == 1
    assert "|rec5002|" in rows[0]["run_key"]


def test_missed_start_is_a_benign_skip(inventory):
    events = [_event(153, 5, instance="", event_record_id=5003)]
    rows = collect.build_run_rows(events, inventory, host_name="TASKHOST-1")
    assert rows[0]["run_status"] == "Missed"
    assert rows[0]["is_error"] == 0
    assert rows[0]["is_completed"] == 1


def test_already_running_skip_is_not_an_error(inventory):
    events = [_event(322, 5, instance="i-skip")]
    rows = collect.build_run_rows(events, inventory, host_name="TASKHOST-1")
    assert rows[0]["run_status"] == "Skipped-AlreadyRunning"
    assert rows[0]["is_error"] == 0


def test_322_never_overrides_the_still_running_instance_it_names(inventory):
    """322's InstanceId is the instance that is ALREADY running, not the rejected launch
    (manifest: 'Task Scheduler did not launch task "%1" because instance "%2" of the same
    task is already running' -- verified via wevtutil gp against the real provider). A 322
    sharing that instance id must get its own row, not merge into and override the real run."""
    events = [
        _event(100, 30, instance="i-real"),
        _event(322, 20, instance="i-real"),
        _event(201, 10, instance="i-real", result_code=2147942402, event_record_id=6001),
        _event(102, 10, instance="i-real"),
    ]
    rows = collect.build_run_rows(events, inventory, host_name="TASKHOST-1")
    assert len(rows) == 2
    real_run = next(r for r in rows if r["run_key"] == "TASKHOST-1|i-real")
    skip_row = next(r for r in rows if r["run_key"] != "TASKHOST-1|i-real")
    assert real_run["run_status"] == "Error"
    assert real_run["is_error"] == 1
    assert skip_row["run_status"] == "Skipped-AlreadyRunning"
    assert skip_row["is_error"] == 0
    assert "|rec1020|" in skip_row["run_key"]


def test_322_alone_leaves_the_still_running_instance_running(inventory):
    """A 322 with no 201/102 for that instance yet must not close out or misclassify the run
    it names -- it becomes its own separate benign row instead."""
    events = [_event(100, 30, instance="i-real2"), _event(322, 20, instance="i-real2")]
    rows = collect.build_run_rows(events, inventory, host_name="TASKHOST-1")
    assert len(rows) == 2
    real_run = next(r for r in rows if r["run_key"] == "TASKHOST-1|i-real2")
    assert real_run["run_status"] == "Running"
    assert real_run["is_error"] == 0
    assert real_run["is_completed"] == 0


def test_killed_on_time_limit_beats_the_generic_terminated_event(inventory):
    """329 (stopping due to timeout) and 111 (terminated) both fire for a killed run; the
    specific reason must win, and the run must end up completed, not stuck Running."""
    events = [_event(100, 30, instance="i-kill"), _event(329, 5, instance="i-kill"),
             _event(111, 4, instance="i-kill")]
    rows = collect.build_run_rows(events, inventory, host_name="TASKHOST-1")
    assert len(rows) == 1
    assert rows[0]["run_status"] == "KilledTimeLimit"
    assert rows[0]["is_error"] == 1
    assert rows[0]["is_completed"] == 1


def test_user_stop_is_not_an_error(inventory):
    events = [_event(100, 30, instance="i-stop"), _event(330, 5, instance="i-stop")]
    rows = collect.build_run_rows(events, inventory, host_name="TASKHOST-1")
    assert rows[0]["run_status"] == "Stopped"
    assert rows[0]["is_error"] == 0
    assert rows[0]["is_completed"] == 1


def test_bare_terminated_event_never_stays_running(inventory):
    """A 111 with no companion 329/330 is still a real, unexplained termination -- it must
    not be reported as still Running (the old 100/102/201-only reader would have)."""
    events = [_event(100, 30, instance="i-bare"), _event(111, 5, instance="i-bare")]
    rows = collect.build_run_rows(events, inventory, host_name="TASKHOST-1")
    assert rows[0]["run_status"] == "Terminated"
    assert rows[0]["is_error"] == 1
    assert rows[0]["is_completed"] == 1


# ---------------------------------------------------------------- 1.3 multi-action tasks

def test_multi_action_uses_the_last_actions_code_when_all_succeed(inventory):
    events = [
        _event(100, 30, instance="i-multi"),
        _event(201, 20, instance="i-multi", result_code=0x00041300, event_record_id=2001),
        _event(201, 10, instance="i-multi", result_code=0, event_record_id=2002),
        _event(102, 10, instance="i-multi"),
    ]
    rows = collect.build_run_rows(events, inventory, host_name="TASKHOST-1")
    assert rows[0]["result_code"] == 0, "the LAST action's (highest record id) code wins"


def test_multi_action_never_hides_an_earlier_failure(inventory):
    """First action fails, second (last) action succeeds -- the run must still report Error,
    not the later success. This is the bug: old code kept whichever 201 it saw last, which
    was the OLDEST one on a time-window (newest-first) fetch."""
    events = [
        _event(100, 30, instance="i-multi2"),
        _event(201, 20, instance="i-multi2", result_code=2147942401, event_record_id=3001),
        _event(201, 10, instance="i-multi2", result_code=0, event_record_id=3002),
        _event(102, 10, instance="i-multi2"),
    ]
    rows = collect.build_run_rows(events, inventory, host_name="TASKHOST-1")
    assert rows[0]["result_code"] == 2147942401
    assert rows[0]["run_status"] == "Error"
    assert rows[0]["is_error"] == 1


def test_multi_action_surfaces_a_later_failure_too(inventory):
    events = [
        _event(100, 30, instance="i-multi3"),
        _event(201, 20, instance="i-multi3", result_code=0, event_record_id=4001),
        _event(201, 10, instance="i-multi3", result_code=2147942402, event_record_id=4002),
        _event(102, 10, instance="i-multi3"),
    ]
    rows = collect.build_run_rows(events, inventory, host_name="TASKHOST-1")
    assert rows[0]["result_code"] == 2147942402
    assert rows[0]["run_status"] == "Error"


def test_multi_action_result_code_uses_record_id_not_list_position(inventory):
    """A real time-window Get-WinEvent read returns newest-first, so the highest record id is
    listed FIRST, not last. resolve_result_code sorts by record id internally, but build_run_rows
    must feed it (record_id, code) pairs keyed correctly regardless of list order -- a
    'whichever was processed last in the list' bug would pick the wrong action here."""
    events = [
        _event(100, 30, instance="i-multi4"),
        _event(201, 20, instance="i-multi4", result_code=2147942402, event_record_id=4002),  # newest, listed first
        _event(201, 10, instance="i-multi4", result_code=0, event_record_id=4001),  # oldest, listed last
        _event(102, 10, instance="i-multi4"),
    ]
    rows = collect.build_run_rows(events, inventory, host_name="TASKHOST-1")
    assert rows[0]["result_code"] == 2147942402, "highest record id (4002, listed first) is the last action"
    assert rows[0]["run_status"] == "Error"

    events_all_succeed = [
        _event(100, 30, instance="i-multi5"),
        _event(201, 20, instance="i-multi5", result_code=0x00041300, event_record_id=4102),  # newest, listed first
        _event(201, 10, instance="i-multi5", result_code=0, event_record_id=4101),  # oldest, listed last
        _event(102, 10, instance="i-multi5"),
    ]
    rows = collect.build_run_rows(events_all_succeed, inventory, host_name="TASKHOST-1")
    assert rows[0]["result_code"] == 0x00041300, "every action succeeded -- highest record id's code wins"


# ---------------------------------------------------------------- 1.4 result text

def test_ctypes_formaterror_fills_an_unmapped_win32_hresult():
    """0x80070050 (FACILITY_WIN32 + ERROR_FILE_EXISTS) is not in HRESULT_TEXT."""
    text = collect.describe_hresult(2147942480)
    assert "Unmapped" not in text
    assert text  # real OS text, not empty


def test_bare_exit_code_is_not_read_as_a_win32_message():
    """A completed action's result code is the process's own exit code, not a Win32 error --
    exit 3 must not read as ctypes.FormatError(3) ('The system cannot find the path
    specified.'). Only the FACILITY_WIN32 (0x8007xxxx) wrapped form gets unwrapped."""
    text = collect.describe_hresult(3)
    assert "Unmapped" in text
    assert "path" not in text.lower()


def test_ntstatus_control_c_exit_is_mapped():
    # wording-terminated-result-jargon: plain text, not console jargon a scheduled task never
    # sees ("Ctrl+C / console close").
    text = collect.describe_hresult(0xC000013A)
    assert "stopped before it finished" in text.lower()
    assert "ctrl+c" not in text.lower()


@pytest.mark.parametrize("code", [-1, -(2 ** 40), 2 ** 40, 10 ** 20, -(10 ** 20), 0, 2 ** 63])
def test_describe_hresult_never_raises(code):
    text = collect.describe_hresult(code)
    assert isinstance(text, str) and text


# ---------------------------------------------------------------- 1.5 event time (PS-only)

def test_event_time_reads_the_xml_system_time_not_a_local_conversion():
    """No pytest execution path runs the PowerShell itself (see module docstring), so this
    is a content check that the fix landed, matching the existing
    test_incremental_event_query_reads_oldest_first convention."""
    template = collect.POWERSHELL_HISTORY_QUERY_TEMPLATE
    assert "System.TimeCreated.SystemTime" in template
    assert "$eventTimeUtc = if ($systemTimeRaw)" in template


# ---------------------------------------------------------------- 1.6 overdue grace

def test_overdue_grace_absorbs_small_lateness():
    late = iso(NOW - timedelta(minutes=5))
    assert collect.get_health_status(True, "Ready", 0, 0, late, NOW, "Daily") == "Success"


def test_overdue_beyond_grace_is_still_a_warning():
    late = iso(NOW - timedelta(minutes=20))
    assert collect.get_health_status(True, "Ready", 0, 0, late, NOW, "Daily") == "Warning"


def test_overdue_grace_is_configurable():
    late = iso(NOW - timedelta(minutes=5))
    assert collect.get_health_status(
        True, "Ready", 0, 0, late, NOW, "Daily", overdue_grace_minutes=0
    ) == "Warning"


def test_inventory_is_overdue_respects_the_default_grace():
    rows = collect.build_inventory_rows(
        [_task(next_run_time=iso(NOW - timedelta(minutes=10)))],
        host_name="H", descriptions={}, audit_dt=NOW)
    assert rows[0]["is_overdue"] == 0
    assert rows[0]["health_status"] == "Success"


def test_inventory_is_overdue_beyond_the_default_grace():
    rows = collect.build_inventory_rows(
        [_task(next_run_time=iso(NOW - timedelta(minutes=30)))],
        host_name="H", descriptions={}, audit_dt=NOW)
    assert rows[0]["is_overdue"] == 1
    assert rows[0]["health_status"] == "Warning"


def test_attention_reason_respects_the_grace_threshold():
    within_grace = collect.build_attention_reason(
        "Success", "", 0, -5.0 / 60.0, False, overdue_grace_hours=15.0 / 60.0)
    assert "overdue" not in within_grace
    beyond_grace = collect.build_attention_reason(
        "Warning", "", 0, -30.0 / 60.0, False, overdue_grace_hours=15.0 / 60.0)
    assert "overdue" in beyond_grace


# ---------------------------------------------------------------- 1.7 command classification

@pytest.mark.parametrize("executable,arguments,expected_type", [
    ("python", "-m pip list", "Python"),
    ("python3.exe", r"C:\a\b.py", "PythonScript"),
    ("python3.11.exe", r"C:\a\b.py", "PythonScript"),
    ("pythonw3.exe", "", "Python"),
    ("python3", "", "Python"),
    ("powershell", r'-File "C:\a\b.ps1"', "PowerShellScript"),
    ("pwsh", "", "PowerShell"),
    ("pwsh.exe", "", "PowerShell"),
])
def test_extensionless_and_versioned_interpreters_are_classified(executable, arguments, expected_type):
    _, target_type = collect.classify_command_target(executable, arguments)
    assert target_type == expected_type
    assert target_type != "Executable"
