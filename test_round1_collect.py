"""Round-1 audit fixes in taskdash_collect: C1 monthly bucket + catch-up note, C3 repetition
buckets, C4 Windows description fallback, R1 exit codes, T1/X7 attention reason, X3 wording.

Pure functions and fixtures only: no PowerShell, no ArcGIS Online.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import taskdash_common as common

common.disable_arcpy_import()

import taskdash_collect as collect  # noqa: E402

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


def _trigger(**kw):
    base = {"type": "Daily", "start": "2024-01-05T02:00:00", "end": "", "enabled": True,
            "days_interval": "1", "days_of_week": "", "weeks_interval": "",
            "repetition_interval": "", "repetition_duration": "", "random_delay": "", "delay": ""}
    base.update(kw)
    return base


MONTHLY = _trigger(type="Scheduled", days_interval="", days_of_month="7")


def _task(name="T", triggers=None, **kw):
    base = {"task_name": name, "task_path": "\\", "task_full_name": "\\" + name, "state": "Ready",
            "enabled": True, "actions": "C:\\Jobs\\run.cmd", "primary_executable": "C:\\Jobs\\run.cmd",
            "primary_arguments": "", "primary_working_directory": "", "trigger_types": "Daily",
            "trigger_details": triggers if triggers is not None else [_trigger()],
            "run_as_user": "svc", "logon_type": "Interactive", "run_level": "Highest",
            "last_run_time": (NOW - timedelta(hours=6)).isoformat(),
            "next_run_time": (NOW + timedelta(hours=18)).isoformat(), "last_task_result": 0,
            "number_of_missed_runs": 0, "description": ""}
    base.update(kw)
    return base


def _row(**kw):
    return collect.build_inventory_rows([_task(**kw)], host_name="H", descriptions={},
                                        audit_dt=NOW, exists=lambda p: True)[0]


# ---------------------------------------------------------------- C1 / C3 schedule bucket

def test_a_monthly_trigger_buckets_as_monthly_and_gets_the_catch_up_note():
    row = _row(trigger_types="Scheduled", triggers=[MONTHLY], start_when_available=False)
    assert row["schedule_bucket"] == "Monthly"
    assert "Catch-up is off" in row["config_warnings"]


def test_a_capture_without_trigger_details_falls_back_to_trigger_types():
    assert collect.get_schedule_bucket("Scheduled") == "Scheduled"
    assert collect.get_schedule_bucket("Scheduled", []) == "Scheduled"


def test_repetition_sets_the_bucket_and_suppresses_the_catch_up_note():
    cases = {"PT15M": "Every 15 min", "PT1H": "Hourly", "PT4H": "Every 4 h", "PT30M": "Every 30 min"}
    for interval, expected in cases.items():
        weekly = _trigger(type="Weekly", days_of_week="127", repetition_interval=interval)
        assert collect.get_schedule_bucket("Weekly", [weekly]) == expected
    row = _row(trigger_types="Weekly", start_when_available=False,
               triggers=[_trigger(type="Weekly", days_of_week="127", repetition_interval="PT1H")])
    assert row["schedule_bucket"] == "Hourly"
    assert "Catch-up" not in row["config_warnings"]


def test_a_single_weekly_trigger_is_split_by_how_many_days_it_fires():
    """interactions-weekly-bucket-hides-frequency: 'Weekly' used to lump a once-a-week task in
    with one that runs 5 or 7 days a week."""
    def bucket(days_of_week):
        return collect.get_schedule_bucket(
            "Weekly", [_trigger(type="Weekly", days_of_week=str(days_of_week))])

    assert bucket(127) == "Daily"  # every day bit set
    assert bucket(124) == "5 days a week"  # Tue-Sat -- wording-13: not "Weekly (5 days)"
    assert bucket(17) == "2 days a week"  # Sun, Thu
    assert bucket(2) == "Weekly"  # a single day stays plain
    assert collect.get_schedule_bucket(  # every other week: the day count is not the frequency
        "Weekly", [_trigger(type="Weekly", days_of_week="127", weeks_interval="2")]) == "Weekly"
    # get_schedule_bucket's Daily+Weekly pairing (a separate Daily trigger) still matches on
    # the "Weekly" substring.
    assert "Weekly" in collect.get_schedule_bucket(
        "Daily | Weekly", [_trigger(type="Daily"), _trigger(type="Weekly", days_of_week="124")])
    # "N days a week" dropped the "Weekly" substring on purpose -- is_calendar_schedule_bucket
    # is the explicit check that replaced it (interactions-schedule-option-labels).
    assert collect.is_calendar_schedule_bucket("5 days a week")
    assert collect.is_calendar_schedule_bucket("2 days a week")
    assert not collect.is_calendar_schedule_bucket("Hourly")


def test_a_disabled_repeating_trigger_does_not_set_the_bucket():
    triggers = [_trigger(repetition_interval="PT15M", enabled=False), _trigger(type="Weekly")]
    assert collect.get_schedule_bucket("Daily | Weekly", triggers) == "Daily + Weekly"


def test_a_daily_task_keeps_the_catch_up_note():
    assert "StartWhenAvailable" in _row(start_when_available=False)["config_warnings"]


# ---------------------------------------------------------------- C4 description fallback

def test_the_windows_description_fills_a_missing_entry_and_the_json_entry_wins():
    assert _row(description="From Windows.")["description"] == "From Windows."
    rows = collect.build_inventory_rows(
        [_task(description="From Windows.")], host_name="H", descriptions={"t": "From JSON."},
        audit_dt=NOW, exists=lambda p: True)
    assert rows[0]["description"] == "From JSON."


# ---------------------------------------------------------------- R1 exit codes

def test_a_small_last_task_result_is_the_exit_code_and_a_launch_hresult_keeps_its_text():
    assert _row(last_task_result=1)["last_result_text"] == "Script exited with code 1."
    assert _row(last_task_result=1056)["last_result_text"] == "Script exited with code 1056."
    assert _row(last_task_result=2147942402)["last_result_text"] == "File not found."
    assert collect.describe_task_result(0) == "The operation completed successfully."
    assert collect.describe_task_result(None) == "No task result available."


def _event(event_id, minutes, **extra):
    row = {"event_id": event_id, "event_record_id": 1000 + minutes,
           "event_time_utc": (NOW - timedelta(minutes=minutes)).isoformat(),
           "task_full_name": "\\T", "instance_id": "i-1", "user_context": "svc",
           "computer_name": "H", "result_code": None}
    row.update(extra)
    return row


def test_a_201_wrapped_code_is_the_exit_code_but_a_launch_failure_is_not():
    inventory = collect.build_inventory_rows([_task()], host_name="H", descriptions={}, audit_dt=NOW)
    completed = [_event(100, 30), _event(201, 10, result_code=2147942401), _event(102, 10)]
    run = collect.build_run_rows(completed, inventory, host_name="H")[0]
    assert run["result_text"] == "Script exited with code 1." and run["is_error"] == 1
    truncated = [_event(100, 30), _event(201, 10, result_code=0x80070000), _event(102, 10)]
    assert collect.build_run_rows(truncated, inventory, host_name="H")[0]["result_text"] == \
        "Script exited with a non-zero code."
    launch = [_event(100, 30), _event(203, 29, result_code=2147942402), _event(102, 29)]
    assert collect.build_run_rows(launch, inventory, host_name="H")[0]["result_text"] == "File not found."


# ---------------------------------------------------------------- T1 / X7 attention reason

def test_the_reason_never_holds_a_success_or_running_sentence():
    ok = "The operation completed successfully."
    assert collect.build_attention_reason("Success", ok, 0, 5.0, False) == ""
    assert collect.build_attention_reason("Running", "Task is currently running.", 0, None, False) == ""
    assert _row()["attention_reason"] == ""


def test_the_reason_uses_one_hour_format_and_capitals():
    reason = collect.build_attention_reason("Warning", "", 0, -3.2249, False)
    assert reason == "Next run is overdue by 3.2 h"
    assert collect.build_attention_reason("NotYetRun", "", 0, None, False) == "Task has not yet run"
    assert collect.build_attention_reason("Disabled", "", 0, None, False) == "Task is disabled"
    warning_code = collect.describe_hresult(0x00041306)
    assert collect.build_attention_reason("Warning", warning_code, 0, None, False) == warning_code
    assert collect.build_attention_reason("Error", "Script exited with code 1.", 0, None, False) == \
        "Script exited with code 1."


# ---------------------------------------------------------------- X3 wording + acknowledgements

def _sentences(text):
    return [s for s in re.split(r"(?<=\.)\s+", text) if s]


def test_config_warning_sentences_are_20_words_or_fewer():
    warnings = [
        *collect.check_silent_skip_settings("IgnoreNew", "PT0S", False, "Daily"),
        *collect.check_silent_skip_settings("IgnoreNew", "PT72H", True, "Weekly", 3600.0),
        *collect.check_logon_type_drift(collect.DEFAULT_INTERACTIVE_REQUIRED_PATTERN + "\\python.exe",
                                        "Password"),
    ]
    assert len(warnings) == 4
    for warning in warnings:
        for sentence in _sentences(warning):
            assert len(sentence.split()) <= 20, sentence


def test_the_shipped_acknowledgements_drop_only_the_catch_up_note():
    loaded = collect.load_descriptions(Path(__file__).parent / "tests" / "fixtures" / "workflow_descriptions.json")
    for name in ("Server Restart Window", "Geodatabase Upkeep"):
        entry = loaded[name.lower()]
        assert entry["description"]
        raw = _task(name, multiple_instances_policy="IgnoreNew", execution_time_limit="PT0S",
                    start_when_available=False)
        rows = collect.build_inventory_rows([raw], host_name="H", descriptions=loaded,
                                            audit_dt=NOW, exists=lambda p: True)
        assert "Catch-up" not in rows[0]["config_warnings"]
        assert "hung run" in rows[0]["config_warnings"]


def test_script_arguments_are_the_tokens_after_the_script():
    py = r"C:\Pro\python.exe"
    assert collect.script_arguments(py, r'"C:\Jobs\run.py" --apply --target prod', r"C:\Jobs\run.py") == \
        "--apply --target prod"
    assert collect.script_arguments(py, r"C:\Jobs\run.py", r"C:\Jobs\run.py") == ""
    # The executable is the script: every argument belongs to it.
    assert collect.script_arguments(r"C:\Jobs\run.cmd", "/quiet", r"C:\Jobs\run.cmd") == "/quiet"
