"""Tests for the TaskDash collector, metric miner and row preparation.

Pure-function coverage only: no ArcGIS Online calls, no PowerShell. Runs on system Python.

    cd <TaskDash folder>
    python -m pytest test_taskdash.py -q
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import taskdash_collect as collect
import taskdash_metrics as metrics
import taskdash_publish as publish
from taskdash_common import normalize_text, parse_iso_datetime, to_epoch_millis
from conftest import REFERENCE_PROFILES

NOW = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


# ---------------------------------------------------------------- result codes

def test_hresult_success_and_known_failure():
    assert collect.describe_hresult(0) == "The operation completed successfully."
    assert collect.describe_hresult(2147942401) == "Incorrect function."


def test_hresult_previously_unmapped_code_now_resolves():
    """2147943691 appeared in TASKHOST-1 run history as 'Unmapped task result code'."""
    text = collect.describe_hresult(2147943691)
    assert "Unmapped" not in text
    assert "timely fashion" in text


def test_hresult_signed_and_unsigned_are_equivalent():
    signed = -2147024895  # same value as 2147942401 seen as int32
    assert collect.normalize_hresult_code(signed) == 2147942401
    assert collect.describe_hresult(signed) == "Incorrect function."


def test_hresult_unknown_code_reports_hex():
    assert "0x" in collect.describe_hresult(123456789)


def test_hresult_none_is_handled():
    assert collect.describe_hresult(None) == "No task result available."


# ---------------------------------------------------------------- command classification

@pytest.mark.parametrize("executable,arguments,expected_path,expected_type", [
    (r'"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\pythonw.exe"',
     r"C:\Jobs\AddressDataScript\Stage1.py", r"C:\Jobs\AddressDataScript\Stage1.py", "PythonScript"),
    ("powershell.exe", r'-ExecutionPolicy Bypass -File "C:\Jobs\a\b.ps1"',
     r"C:\Jobs\a\b.ps1", "PowerShellScript"),
    ("cmd.exe", r"/c C:\tools\run.bat", r"C:\tools\run.bat", "BatchScript"),
    ("cscript.exe", r"C:\tools\legacy.vbs", r"C:\tools\legacy.vbs", "VBScript"),
    (r"C:\tools\standalone.exe", "", r"C:\tools\standalone.exe", "Executable"),
    (r"C:\tools\direct.py", "", r"C:\tools\direct.py", "PythonScript"),
])
def test_classify_command_target(executable, arguments, expected_path, expected_type):
    path, target_type = collect.classify_command_target(executable, arguments)
    assert path == expected_path
    assert target_type == expected_type


def test_classify_python_without_script_falls_back_to_interpreter():
    path, target_type = collect.classify_command_target("python.exe", "-m pip list")
    assert target_type == "Python"
    assert path == "python.exe"


def test_classify_handles_unparseable_arguments():
    path, target_type = collect.classify_command_target("python.exe", 'C:\\bad"quote.py')
    assert path and target_type  # must not raise


# ---------------------------------------------------------------- schedule buckets

def test_base_trigger_class_is_not_dropped():
    """MSFT_TaskTrigger used to reduce to '' and bucket real triggers as 'No Triggers'."""
    assert collect.get_schedule_bucket("Scheduled") == "Scheduled"
    assert collect.expects_next_run("Scheduled") is True


@pytest.mark.parametrize("trigger_types,expected", [
    ("Daily", "Daily"),
    ("Weekly", "Weekly"),
    ("Time", "One Time"),
    ("Boot", "Startup"),
    ("MonthlyDOW", "Monthly"),
    ("", "No Triggers"),
    ("Daily | Weekly", "Daily + Weekly"),
    ("Daily | Logon", "Daily + Logon"),
    ("Daily | Weekly | Logon", "Multiple / Other"),
])
def test_schedule_bucket(trigger_types, expected):
    assert collect.get_schedule_bucket(trigger_types) == expected


@pytest.mark.parametrize("bucket,expected", [
    ("Daily", True), ("Weekly", True), ("Monthly", True), ("Scheduled", True),
    ("No Triggers", False), ("One Time", False), ("Logon", False), ("Event", False),
])
def test_expects_next_run(bucket, expected):
    assert collect.expects_next_run(bucket) is expected


# ---------------------------------------------------------------- health

def test_disabled_task_is_disabled_regardless_of_result():
    assert collect.get_health_status(False, "Ready", 0, 0, iso(NOW), NOW, "Daily") == "Disabled"


def test_successful_task_is_success():
    future = iso(NOW + timedelta(hours=6))
    assert collect.get_health_status(True, "Ready", 0, 0, future, NOW, "Daily") == "Success"


def test_failed_task_is_error():
    future = iso(NOW + timedelta(hours=6))
    assert collect.get_health_status(True, "Ready", 2147942401, 0, future, NOW, "Daily") == "Error"


def test_running_task_is_running():
    assert collect.get_health_status(True, "Running", 267009, 0, None, NOW, "Daily") == "Running"


def test_overdue_next_run_is_warning():
    past = iso(NOW - timedelta(hours=3))
    assert collect.get_health_status(True, "Ready", 0, 0, past, NOW, "Daily") == "Warning"


def test_missed_runs_is_warning():
    future = iso(NOW + timedelta(hours=6))
    assert collect.get_health_status(True, "Ready", 0, 2, future, NOW, "Daily") == "Warning"


def test_scheduled_task_without_next_run_is_warning():
    assert collect.get_health_status(True, "Ready", 0, 0, None, NOW, "Daily") == "Warning"


def test_event_task_without_next_run_is_not_warning():
    assert collect.get_health_status(True, "Ready", 0, 0, None, NOW, "Event") == "Success"


def test_attention_reason_mentions_missed_runs_and_overdue():
    reason = collect.build_attention_reason("Warning", "Incorrect function.", 2, -5.0, True)
    assert "2 missed runs" in reason
    assert "overdue by 5.0 h" in reason
    assert "no next scheduled run" in reason
    # wording-13: singular reads "1 missed run", not "1 missed run(s)".
    assert "1 missed run" in collect.build_attention_reason("Warning", "", 1, None, False)
    assert "1 missed runs" not in collect.build_attention_reason("Warning", "", 1, None, False)


# ---------------------------------------------------------------- filtering

def _task(**overrides):
    base = {
        "task_name": "Parcel Refresh",
        "task_path": "\\",
        "task_full_name": "\\Parcel Refresh",
        "state": "Ready",
        "enabled": True,
        "actions": "pythonw.exe C:\\Jobs\\PropertyAppraiser\\ParcelRefresh.py",
        "primary_executable": r"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\pythonw.exe",
        "primary_arguments": r"C:\Jobs\PropertyAppraiser\ParcelRefresh.py",
        "trigger_types": "Daily",
        "trigger_count": 1,
        "run_as_user": "svc_gisjobs",
        "last_run_time": iso(NOW - timedelta(hours=6)),
        "next_run_time": iso(NOW + timedelta(hours=18)),
        "last_task_result": 0,
        "number_of_missed_runs": 0,
    }
    base.update(overrides)
    return base


def test_microsoft_tasks_excluded():
    row = _task(task_path="\\Microsoft\\Windows\\Defrag\\")
    assert collect.should_include_task(row, {"exclude_microsoft_tasks": True}) is False


def test_exclude_by_name_regex():
    cfg = {"exclude_task_name_regex": "^Parcel Refresh$"}
    assert collect.should_include_task(_task(), cfg) is False
    assert collect.should_include_task(_task(task_name="Other"), cfg) is True


def test_exclude_by_command_target_type():
    cfg = {"exclude_command_target_types": ["Executable"]}
    exe = _task(primary_executable=r"C:\tools\thing.exe", primary_arguments="")
    assert collect.should_include_task(exe, cfg) is False
    assert collect.should_include_task(_task(), cfg) is True


def test_disabled_excluded_when_configured():
    cfg = {"include_disabled": False}
    assert collect.should_include_task(_task(enabled=False), cfg) is False


# ---------------------------------------------------------------- inventory rows

def test_inventory_row_shape_and_health():
    rows = collect.build_inventory_rows([_task()], host_name="TASKHOST-1",
                                        descriptions={"parcel refresh": "Rebuilds parcels."},
                                        audit_dt=NOW)
    assert len(rows) == 1
    row = rows[0]
    assert row["host_name"] == "TASKHOST-1"
    assert row["description"] == "Rebuilds parcels."
    assert row["health_status"] == "Success"
    assert row["last_run_failed"] == 0
    assert row["needs_attention"] == 0
    assert row["command_target_type"] == "PythonScript"
    assert row["command_target_name"] == "ParcelRefresh.py"
    assert row["hours_since_last_run"] == pytest.approx(6.0, abs=0.01)
    assert row["next_run_in_hours"] == pytest.approx(18.0, abs=0.01)


def test_inventory_marks_failure():
    rows = collect.build_inventory_rows([_task(last_task_result=2147942401)],
                                        host_name="H", descriptions={}, audit_dt=NOW)
    assert rows[0]["last_run_failed"] == 1
    assert rows[0]["needs_attention"] == 1
    assert "Incorrect function." in rows[0]["attention_reason"]


def test_inventory_marks_overdue():
    rows = collect.build_inventory_rows([_task(next_run_time=iso(NOW - timedelta(hours=2)))],
                                        host_name="H", descriptions={}, audit_dt=NOW)
    assert rows[0]["is_overdue"] == 1
    assert rows[0]["health_status"] == "Warning"


def test_disabled_task_with_stale_next_run_is_not_overdue():
    """A disabled task has no obligation to run; flagging it overdue contradicts its health."""
    rows = collect.build_inventory_rows(
        [_task(enabled=False, next_run_time=iso(NOW - timedelta(hours=2)))],
        host_name="H", descriptions={}, audit_dt=NOW)
    assert rows[0]["health_status"] == "Disabled"
    assert rows[0]["is_overdue"] == 0
    assert rows[0]["needs_attention"] == 0


def test_inventory_sorted_by_name():
    rows = collect.build_inventory_rows(
        [_task(task_name="Zeta", task_full_name="\\Zeta"),
         _task(task_name="Alpha", task_full_name="\\Alpha")],
        host_name="H", descriptions={}, audit_dt=NOW)
    assert [r["task_name"] for r in rows] == ["Alpha", "Zeta"]


# ---------------------------------------------------------------- run rows

def _event(event_id, minutes, instance="i-1", task="\\Parcel Refresh", **extra):
    row = {
        "event_id": event_id,
        "event_record_id": 1000 + minutes,
        "event_time_utc": iso(NOW - timedelta(minutes=minutes)),
        "task_full_name": task,
        "instance_id": instance,
        "user_context": "svc_gisjobs",
        "computer_name": "TASKHOST-1",
        "result_code": None,
    }
    row.update(extra)
    return row


@pytest.fixture
def inventory():
    return collect.build_inventory_rows([_task()], host_name="TASKHOST-1",
                                        descriptions={}, audit_dt=NOW)


def test_run_pairing_computes_duration(inventory):
    events = [_event(100, 30), _event(201, 10, result_code=0), _event(102, 10)]
    rows = collect.build_run_rows(events, inventory, host_name="TASKHOST-1")
    assert len(rows) == 1
    run = rows[0]
    assert run["duration_seconds"] == pytest.approx(1200.0)
    assert run["duration_minutes"] == pytest.approx(20.0)
    assert run["is_success"] == 1 and run["is_error"] == 0
    assert run["start_time_estimated"] == 0
    assert run["run_key"] == "TASKHOST-1|i-1"


def test_run_failure_is_flagged(inventory):
    events = [_event(100, 30), _event(201, 10, result_code=2147942401), _event(102, 10)]
    rows = collect.build_run_rows(events, inventory, host_name="TASKHOST-1")
    assert rows[0]["is_error"] == 1
    assert rows[0]["run_status"] == "Error"
    assert rows[0]["result_text"] == "Script exited with code 1."


def test_missing_start_event_does_not_produce_null_start(inventory):
    """57 rows in the old table had NULL start_time when event 100 fell outside the window."""
    events = [_event(201, 10, result_code=0), _event(102, 10)]
    rows = collect.build_run_rows(events, inventory, host_name="TASKHOST-1")
    assert len(rows) == 1
    assert rows[0]["start_time_utc"] is not None
    assert rows[0]["start_time_estimated"] == 1
    assert rows[0]["duration_seconds"] is None, "a duration from an estimated start is ~0 s and wrong"


def test_still_running_task_has_no_end(inventory):
    rows = collect.build_run_rows([_event(100, 5)], inventory, host_name="TASKHOST-1")
    assert rows[0]["is_completed"] == 0
    assert rows[0]["run_status"] == "Running"
    assert rows[0]["duration_seconds"] is None


def test_events_for_unknown_task_are_ignored(inventory):
    events = [_event(100, 30, task="\\Not In Inventory"), _event(102, 10, task="\\Not In Inventory")]
    assert collect.build_run_rows(events, inventory, host_name="TASKHOST-1") == []


def test_one_row_per_run_instance(inventory):
    events = []
    for idx, minutes in enumerate((300, 200, 100)):
        events += [_event(100, minutes, instance=f"i-{idx}"),
                   _event(102, minutes - 10, instance=f"i-{idx}", result_code=0)]
    rows = collect.build_run_rows(events, inventory, host_name="TASKHOST-1")
    assert len(rows) == 3
    assert {r["instance_id"] for r in rows} == {"i-0", "i-1", "i-2"}


def test_run_rows_are_deduplicated_by_instance(inventory):
    events = [_event(100, 30), _event(100, 30), _event(102, 10, result_code=0)]
    rows = collect.build_run_rows(events, inventory, host_name="TASKHOST-1")
    assert len(rows) == 1
    assert rows[0]["event_count"] == 3


# ---------------------------------------------------------------- metric mining

@pytest.mark.parametrize("line,key,value", [
    ("2026-08-10 08:17:33 | INFO | update_updates | Records checked: 47285", "checked_record_count", 47285.0),
    ("INFO - Records updated: 69", "updated_record_count", 69.0),
    ("INFO - Total execution time: 970.13 seconds", "duration_seconds", 970.13),
    ("INFO - Found 43 new segments to add", "new_segment_count", 43.0),
    ("INFO - Loaded 47285 source records", "source_record_count", 47285.0),
])
def test_metric_extraction_and_canonicalization(line, key, value):
    found = {}
    for raw_key, raw_value in metrics.extract_metrics_from_line(line):
        canonical = metrics.canonicalize_metric_key(raw_key, line)
        if canonical:
            found[canonical] = raw_value
    assert found.get(key) == pytest.approx(value)


def test_duration_synonyms_collapse_to_one_key():
    for label in ("Total execution time", "Processing time", "Duration", "Elapsed time"):
        assert metrics.canonicalize_metric_key(label.replace(" ", "_")) == "duration_seconds"


def test_updated_synonyms_collapse_to_one_key():
    for raw in ("records_updated", "updated_records", "updated_total", "updated_in_5"):
        assert metrics.canonicalize_metric_key(raw) == "updated_record_count"


@pytest.mark.parametrize("raw,expected", [
    # noun split collapsed onto one concept per pair
    ("exported_features_to_temp_fgdb", "exported_record_count"),
    ("total_records_exported", "exported_record_count"),
    ("deleted_features", "deleted_record_count"),
    ("total_deleted", "deleted_record_count"),
    ("deleted", "deleted_record_count"),
    # bare *_count forms
    ("updated_count", "updated_record_count"),
    ("deleted_count", "deleted_record_count"),
    ("source_count", "source_record_count"),
    ("target_count", "target_record_count"),
])
def test_canonical_metric_rules_collapse_nouns_and_bare_count_forms(raw, expected):
    """exported/deleted used to split into *_feature_count vs *_record_count series;
    the bare *_count labels (updated_count etc.) must also resolve, not fall through as noise."""
    assert metrics.canonicalize_metric_key(raw) == expected


def test_hms_duration_is_converted_to_seconds():
    found = dict(metrics.extract_metrics_from_line("Total time: 1:02:30"))
    assert found["duration_seconds"] == pytest.approx(3750.0)


@pytest.mark.parametrize("line", [
    "2026-08-11 17:45:13,116 - INFO - Duration: 0:00:07.133764",
    "2026-08-11 17:45:13 | INFO     | Duration: 0:00:07.133764",
    "2026-08-11 17:45:13 INFO - Duration: 0:00:07.133764",
    "[2026-08-11 17:45:13] Duration: 0:00:07.133764",
])
def test_log_level_prefix_does_not_shadow_the_real_duration(line):
    """The level word must not become part of a metric label.

    Regression: "- INFO - Duration: 0:00:07" mined BOTH duration_seconds=7.13 and a junk
    info_duration=0.0 (the "0" of "0:00:07"). info_duration canonicalizes onto
    duration_seconds as well, so the zero overwrote the real value and three tasks charted
    a permanently flat 0. Anything canonicalizing to duration_seconds must carry the
    real duration, and no info_* key may survive.
    """
    pairs = metrics.extract_metrics_from_line(line)
    assert not [k for k, _ in pairs if k.startswith("info_")], pairs
    durations = {metrics.canonicalize_metric_key(k): v for k, v in pairs}
    assert durations["duration_seconds"] == pytest.approx(7.133764)


def test_log_preamble_stripping_does_not_eat_real_metrics():
    """Guard the fix itself: stripping the preamble must not swallow the payload."""
    found = dict(metrics.extract_metrics_from_line(
        "2026-08-12 08:21:36,932 - INFO -   Verification: 15,562 features in hosted layer"))
    assert found["verification"] == pytest.approx(15562.0)


@pytest.mark.parametrize("noise", ["start_time", "time", "finished", "runtimeerror", "cpp"])
def test_noise_keys_are_rejected(noise):
    assert metrics.canonicalize_metric_key(noise) is None


def test_label_without_meaningful_token_is_rejected():
    assert metrics.canonicalize_metric_key("some_random_label") is None


def test_title_case_metric():
    assert metrics.title_case_metric("updated_record_count") == "Updated Record Count"


def test_scan_log_file_counts_and_metrics(tmp_path):
    log = tmp_path / "run.log"
    log.write_text(
        "2026-08-10 08:00:00 - INFO - Starting\n"
        "2026-08-10 08:01:00 - INFO - Records checked: 100\n"
        "2026-08-10 08:02:00 - INFO - Records updated: 7\n"
        "2026-08-10 08:03:00 - WARNING - slow response\n"
        "2026-08-10 08:04:00 - INFO - Completed successfully\n",
        encoding="utf-8")
    result = metrics.scan_log_file(log, max_lines=0)
    assert result["lines_scanned"] == 5
    assert result["warning_lines"] == 1
    assert result["success_lines"] == 1
    assert result["metrics"]["checked_record_count"][0] == 100.0
    assert result["metrics"]["updated_record_count"][0] == 7.0


def test_scan_log_file_last_value_wins(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("Records updated: 1\nRecords updated: 2\nRecords updated: 3\n", encoding="utf-8")
    assert metrics.scan_log_file(log, max_lines=0)["metrics"]["updated_record_count"][0] == 3.0


def test_scan_log_file_respects_max_lines(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("\n".join(f"line {i}" for i in range(100)), encoding="utf-8")
    assert metrics.scan_log_file(log, max_lines=10)["lines_scanned"] == 10


def test_discover_log_directories_finds_logs_subdir(tmp_path):
    script_dir = tmp_path / "MyWorkflow"
    (script_dir / "Logs").mkdir(parents=True)
    script = script_dir / "run.py"
    script.write_text("", encoding="utf-8")
    found = metrics.discover_log_directories(str(script))
    assert script_dir in found
    assert (script_dir / "Logs") in found


def test_discover_log_directories_missing_path_is_empty():
    assert metrics.discover_log_directories(r"C:\does\not\exist\x.py") == []


def test_discover_log_directories_includes_working_directory(tmp_path):
    """Task Scheduler lets an action's WorkingDirectory differ from where the script lives;
    a Logs/ folder beside the working directory must be found even though it has no Logs of
    its own beside the script."""
    script_dir = tmp_path / "ScriptHome"
    script_dir.mkdir()
    script = script_dir / "run.py"
    script.write_text("", encoding="utf-8")
    wd = tmp_path / "TaskWorkingDir"
    (wd / "Logs").mkdir(parents=True)

    found = metrics.discover_log_directories(str(script), str(wd))
    assert wd in found
    assert (wd / "Logs") in found
    assert script_dir in found  # the script's own folder is still a root


def test_discover_log_directories_working_directory_only(tmp_path):
    """Discovery must still work when the script path itself doesn't resolve on this host."""
    wd = tmp_path / "WD"
    (wd / "Logs").mkdir(parents=True)
    found = metrics.discover_log_directories(r"C:\does\not\exist\x.py", str(wd))
    assert wd in found
    assert (wd / "Logs") in found


def test_discover_log_directories_dedupes_case_insensitively(tmp_path):
    """Windows paths are case-insensitive; a working directory that is the script's own folder
    under different casing must not double-count that folder."""
    script_dir = tmp_path / "Flow"
    (script_dir / "Logs").mkdir(parents=True)
    script = script_dir / "run.py"
    script.write_text("", encoding="utf-8")

    found = metrics.discover_log_directories(str(script), str(script_dir).upper())
    assert len(found) == 2  # script_dir and script_dir/Logs, each counted once


def test_build_metric_rows_emits_time_series(tmp_path):
    script_dir = tmp_path / "Flow"
    logs = script_dir / "Logs"
    logs.mkdir(parents=True)
    (script_dir / "run.py").write_text("", encoding="utf-8")
    for idx, count in enumerate((10, 20, 30)):
        path = logs / f"run_{idx}.log"
        path.write_text(f"Records updated: {count}\n", encoding="utf-8")

    inventory_rows = [{
        "task_name": "Flow", "task_full_name": "\\Flow",
        "command_target_path": str(script_dir / "run.py"),
    }]
    rows, summary = metrics.build_metric_rows(inventory_rows, host_name="H", days_back=30)

    updated = [r for r in rows if r["metric_key"] == "updated_record_count"]
    assert len(updated) == 3, "one observation per log file, not a single last/min/max row"
    assert {r["metric_value"] for r in updated} == {10.0, 20.0, 30.0}
    assert {r["log_file_name"] for r in updated} == {"run_0.log", "run_1.log", "run_2.log"}
    assert summary["\\Flow"]["log_files_scanned"] == 3


def test_metric_mining_skips_taskdash_own_logs():
    """TaskDash's own summary lines ("TaskRuns: added 49, updated 72") parse as metrics."""
    own_dir = Path(metrics.__file__).resolve().parent
    assert metrics._is_within(own_dir / "Logs", own_dir) is True
    assert metrics._is_within(Path(r"C:/Jobs/AddressDataScript/Logs"), own_dir) is False

    rows, summary = metrics.build_metric_rows(
        [{"task_name": "Task Dash", "task_full_name": "\\Task Dash",
          "command_target_path": str(own_dir / "taskdash_publish.py")}],
        host_name="H")
    assert rows == [] and summary == {}, "the monitor must not mine its own logs"


def test_build_metric_rows_skips_tasks_without_logs():
    inventory_rows = [{"task_name": "X", "task_full_name": "\\X",
                       "command_target_path": r"C:\nope\x.py"}]
    rows, summary = metrics.build_metric_rows(inventory_rows, host_name="H")
    assert rows == [] and summary == {}


def test_build_metric_rows_honours_log_directory_override(tmp_path):
    """A per-task override must win over discovery, even when discovery alone finds nothing
    (script path off-host)."""
    real_logs = tmp_path / "RealLogs"
    real_logs.mkdir()
    (real_logs / "run.log").write_text("Records updated: 5\n", encoding="utf-8")

    inventory_rows = [{
        "task_name": "Strays", "task_full_name": "\\Strays",
        "command_target_path": r"C:\does\not\exist\run.py",
    }]
    rows, summary = metrics.build_metric_rows(
        inventory_rows, host_name="H",
        log_directory_overrides={"\\Strays": str(real_logs)})
    assert rows and rows[0]["metric_value"] == 5.0
    assert summary["\\Strays"]["log_files_scanned"] == 1


def test_build_metric_rows_reports_zero_when_directories_resolve_but_are_empty(tmp_path):
    """A resolved-but-empty log folder is a real signal (wrong folder, rotation stopped, bad
    override) -- distinct from a task whose script path doesn't resolve at all (which stays
    silent, per test_build_metric_rows_skips_tasks_without_logs)."""
    script_dir = tmp_path / "Empty"
    (script_dir / "Logs").mkdir(parents=True)
    (script_dir / "run.py").write_text("", encoding="utf-8")

    inventory_rows = [{"task_name": "Empty", "task_full_name": "\\Empty",
                       "command_target_path": str(script_dir / "run.py")}]
    rows, summary = metrics.build_metric_rows(inventory_rows, host_name="H")
    assert rows == []
    assert summary["\\Empty"]["log_files_scanned"] == 0


# ---------------------------------------------------------------- row preparation

def test_prepare_rows_converts_dates_to_millis():
    rows = publish.prepare_rows(
        [{"host_name": "H", "task_name": "T", "task_full_name": "\\T",
          "last_run_time": iso(NOW), "next_run_time": None}],
        publish.TABLE_INVENTORY, iso(NOW))
    assert rows[0]["last_run_time"] == to_epoch_millis(iso(NOW))
    assert rows[0]["next_run_time"] is None
    assert rows[0]["collected_at_utc"] == to_epoch_millis(iso(NOW))


def test_prepare_rows_drops_unknown_fields():
    rows = publish.prepare_rows(
        [{"task_name": "T", "not_a_real_field": "x"}], publish.TABLE_INVENTORY, iso(NOW))
    assert "not_a_real_field" not in rows[0]
    assert rows[0]["task_name"] == "T"


def test_prepare_rows_never_emits_objectid():
    rows = publish.prepare_rows([{"task_name": "T", "OBJECTID": 5}], publish.TABLE_INVENTORY, iso(NOW))
    assert "OBJECTID" not in rows[0]


def test_new_inventory_fields_survive_prepare_rows():
    """command_target_working_directory and latest_log_warning_lines must be declared in
    inventory_fields(), or prepare_rows silently drops them like any other unknown key."""
    rows = publish.prepare_rows(
        [{"task_name": "T", "command_target_working_directory": r"C:\Jobs\Flow",
          "latest_log_warning_lines": 3}],
        publish.TABLE_INVENTORY, iso(NOW), {"hide_paths": False})
    assert rows[0]["command_target_working_directory"] == r"C:\Jobs\Flow"
    assert rows[0]["latest_log_warning_lines"] == 3


def test_every_schema_field_has_a_type_and_alias():
    for name in (publish.TABLE_INVENTORY, publish.TABLE_RUNS, publish.TABLE_METRICS):
        definition = publish.table_definition(name)
        assert definition["name"] == name
        for field in definition["fields"]:
            assert field["name"] and field["type"] and field["alias"]
        names = [f["name"] for f in definition["fields"]]
        assert len(names) == len(set(names)), f"{name} has duplicate field names"


def test_collector_output_fits_declared_schema():
    """Every key the collector produces must exist in the table it targets."""
    inventory_rows = collect.build_inventory_rows([_task()], host_name="H",
                                                  descriptions={}, audit_dt=NOW)
    run_rows = collect.build_run_rows(
        [_event(100, 30), _event(102, 10, result_code=0)], inventory_rows, host_name="H")

    inv_fields = {f["name"] for f in publish.inventory_fields()}
    run_fields = {f["name"] for f in publish.runs_fields()}
    assert set(inventory_rows[0]) - inv_fields == set()
    assert {k for k in run_rows[0] if not k.startswith("_")} - run_fields == set()


def test_metric_rows_fit_declared_schema(tmp_path):
    logs = tmp_path / "Logs"
    logs.mkdir()
    (tmp_path / "run.py").write_text("", encoding="utf-8")
    (logs / "a.log").write_text("Records updated: 3\n", encoding="utf-8")
    rows, _ = metrics.build_metric_rows(
        [{"task_name": "T", "task_full_name": "\\T", "command_target_path": str(tmp_path / "run.py")}],
        host_name="H")
    metric_fields = {f["name"] for f in publish.metrics_fields()}
    assert rows and set(rows[0]) - metric_fields == set()


# ---------------------------------------------------------------- enrichment

RUN_ROWS = [
    {"task_full_name": "\\Parcel Refresh", "is_error": 0,
     "duration_seconds": 42.0, "start_time_utc": iso(NOW - timedelta(hours=2))},
    {"task_full_name": "\\Parcel Refresh", "is_error": 1,
     "duration_seconds": 10.0, "start_time_utc": iso(NOW - timedelta(days=2))},
    {"task_full_name": "\\Parcel Refresh", "is_error": 0,
     "duration_seconds": 11.0, "start_time_utc": iso(NOW - timedelta(days=30))},
]


@pytest.fixture
def frozen_now(monkeypatch):
    """summarize_runs windows on the real clock; RUN_ROWS are pinned to NOW. Without this the
    7-day assertions silently aged out on 2026-08-18."""
    monkeypatch.setattr(publish, "utc_now", lambda: NOW)


def test_summarize_runs_picks_newest_and_windows_seven_days(frozen_now):
    summary = publish.summarize_runs(RUN_ROWS)["\\Parcel Refresh"]
    assert summary["last_duration_seconds"] == 42.0, "newest run wins, regardless of input order"
    assert summary["runs_last_7_days"] == 2, "the 30-day-old run must fall outside the window"
    assert summary["failures_last_7_days"] == 1


def test_summarize_runs_is_order_independent():
    assert publish.summarize_runs(RUN_ROWS) == publish.summarize_runs(list(reversed(RUN_ROWS)))


def test_enrich_inventory_folds_in_runs_and_logs(frozen_now):
    inventory_rows = collect.build_inventory_rows([_task()], host_name="H",
                                                  descriptions={}, audit_dt=NOW)
    summary = {"\\Parcel Refresh": {"latest_log_name": "x.log", "latest_log_error_lines": 2}}
    publish.enrich_inventory(inventory_rows, publish.summarize_runs(RUN_ROWS), summary)

    row = inventory_rows[0]
    assert row["last_duration_seconds"] == 42.0
    assert row["runs_last_7_days"] == 2
    assert row["failures_last_7_days"] == 1
    assert row["latest_log_name"] == "x.log"
    assert row["latest_log_error_lines"] == 2


def test_enrich_inventory_handles_task_with_no_runs():
    """Incremental collection means a pass can legitimately contain no runs for a task."""
    inventory_rows = collect.build_inventory_rows([_task()], host_name="H",
                                                  descriptions={}, audit_dt=NOW)
    publish.enrich_inventory(inventory_rows, {}, {})
    assert inventory_rows[0]["runs_last_7_days"] == 0
    assert inventory_rows[0]["failures_last_7_days"] == 0
    assert inventory_rows[0]["last_duration_seconds"] is None


def _enriched_row(run_stats):
    """Run enrich_inventory on one \\Parcel Refresh task with the given run-summary stats."""
    inventory_rows = collect.build_inventory_rows([_task()], host_name="H",
                                                  descriptions={}, audit_dt=NOW)
    publish.enrich_inventory(inventory_rows, {"\\Parcel Refresh": run_stats}, {})
    return inventory_rows[0]


def test_duration_anomaly_fires_above_2x_baseline():
    """3.5: baseline is now the 30-day median/p95/p5, not the plain mean -- see
    test_tables_hardening.py for summarize_runs' own math on the baseline fields."""
    row = _enriched_row({"last_duration_seconds": 100.0, "baseline_median_seconds": 40.0,
                         "baseline_p95_seconds": 50.0, "baseline_p5_seconds": 30.0,
                         "baseline_run_count": 5})
    assert row["is_duration_anomaly"] == 1
    assert row["duration_ratio"] == pytest.approx(2.5)
    assert row["health_status"] == "Success", "the anomaly flag is advisory and must not touch health_status"
    # wording-16: "Last run took X, about Nx the usual Y" -- matches Task Detail's anomalyText.
    assert "usual" in row["attention_reason"]


def test_duration_anomaly_fires_below_033x_baseline():
    row = _enriched_row({"last_duration_seconds": 5.0, "baseline_median_seconds": 40.0,
                         "baseline_p95_seconds": 50.0, "baseline_p5_seconds": 30.0,
                         "baseline_run_count": 5})
    assert row["is_duration_anomaly"] == 1
    assert row["duration_ratio"] == pytest.approx(0.125)


def test_duration_anomaly_does_not_fire_below_five_completed_runs():
    """Same extreme ratio as the above-2x case, but only 4 baseline runs -- too thin a
    baseline to trust, so the flag must stay off and duration_ratio must stay null."""
    row = _enriched_row({"last_duration_seconds": 100.0, "baseline_median_seconds": 40.0,
                         "baseline_p95_seconds": 50.0, "baseline_p5_seconds": 30.0,
                         "baseline_run_count": 4})
    assert row["is_duration_anomaly"] == 0
    assert row["duration_ratio"] is None


def test_duration_ratio_within_normal_range_is_not_an_anomaly():
    row = _enriched_row({"last_duration_seconds": 45.0, "baseline_median_seconds": 40.0,
                         "baseline_p95_seconds": 50.0, "baseline_p5_seconds": 30.0,
                         "baseline_run_count": 5})
    assert row["is_duration_anomaly"] == 0


def test_incremental_query_uses_record_id_watermark():
    """With a watermark the PowerShell must filter on EventRecordID, not a time window."""
    script = collect.POWERSHELL_HISTORY_QUERY_TEMPLATE.replace("__MIN_RECORD_ID__", "12345")
    assert "EventRecordID > $MinRecordId" in script
    assert "$MinRecordId -gt 0" in script
    assert "AddDays(-1 * $DaysBack)" in script, "the first-run time window must still exist"


def test_run_rows_carry_watermark_field():
    """max_event_record_id is what the next incremental fetch reads back."""
    inv = collect.build_inventory_rows([_task()], host_name="H", descriptions={}, audit_dt=NOW)
    rows = collect.build_run_rows([_event(100, 30), _event(102, 10, result_code=0)], inv, host_name="H")
    assert rows[0]["max_event_record_id"] is not None


# ---------------------------------------------------------------- misc helpers

def test_parse_iso_handles_dotnet_seven_digit_fraction():
    parsed = parse_iso_datetime("2026-08-10T12:01:08.0000000Z")
    assert parsed is not None
    assert parsed.year == 2026 and parsed.tzinfo is not None


def test_parse_iso_rejects_garbage():
    assert parse_iso_datetime("not a date") is None
    assert parse_iso_datetime("") is None
    assert parse_iso_datetime(None) is None


def test_normalize_text_strips_nulls():
    assert normalize_text("a\x00b ") == "ab"
    assert normalize_text(None) == ""


def test_cli_requires_full_apply_flag():
    """allow_abbrev=False keeps --app from silently meaning --apply."""
    assert publish.parse_args([]).apply is False
    assert publish.parse_args(["--apply"]).apply is True
    with pytest.raises(SystemExit):
        publish.parse_args(["--app"])


# ---------------------------------------------------------------- retirement-safety guards

def _inventory_table(existing_task_full_names, object_id_field="OBJECTID"):
    """A fake TaskInventory table for sync_inventory: query() returns this host's stored rows
    (keyed on task_full_name), edit_features() records every call and reports success."""
    import types

    class FakeTable:
        def __init__(self):
            self.properties = types.SimpleNamespace(objectIdField=object_id_field, maxRecordCount=2000)
            self.existing = [{"OBJECTID": i + 1, "task_full_name": n}
                             for i, n in enumerate(existing_task_full_names)]
            self.edit_calls = []

        def query(self, *, where="1=1", out_fields="*", return_geometry=False,
                  result_offset=0, result_record_count=None, **kw):
            class _Feature:
                def __init__(self, attrs): self.attributes = attrs
            class _Result:
                pass
            result = _Result()
            result.features = [_Feature(row) for row in self.existing] if result_offset == 0 else []
            return result

        def edit_features(self, **kw):
            self.edit_calls.append(kw)
            result = {}
            for kind, key in (("adds", "addResults"), ("updates", "updateResults")):
                items = kw.get(kind) or []
                result[key] = [{"success": True}] * len(items)
            deletes = kw.get("deletes")
            result["deleteResults"] = [{"success": True}] * (len(deletes.split(",")) if deletes else 0)
            return result

    return FakeTable()


def test_sync_inventory_refuses_a_large_shrink():
    """Collecting from the wrong host yields far fewer tasks; syncing would delete most of this
    host's stored inventory."""
    table = _inventory_table([f"t{i}" for i in range(25)])
    with pytest.raises(SystemExit) as exc:
        publish.sync_inventory(table, [{"task_full_name": "a"}] * 3, "H",
                               chunk_size=200, label="TaskInventory")
    assert "wrong host" in str(exc.value)
    assert table.edit_calls == [], "must refuse BEFORE writing anything"


def test_sync_inventory_allows_a_normal_run():
    """A mix of matched, new and dropped tasks, with the drop safely under the 50% guard."""
    table = _inventory_table([f"t{i}" for i in range(20)])
    rows = ([{"task_full_name": f"t{i}"} for i in range(15)]
           + [{"task_full_name": f"u{i}"} for i in range(5)])
    counts = publish.sync_inventory(table, rows, "H", chunk_size=200, label="TaskInventory")
    assert counts == {"added": 5, "updated": 15, "deleted": 5}
    assert len(table.edit_calls) == 1, "well under 250 rows must land in a single edit_features call"
    call = table.edit_calls[0]
    assert len(call["adds"]) == 5 and len(call["updates"]) == 15
    assert len(call["deletes"].split(",")) == 5


def test_sync_inventory_force_overrides_the_guard():
    table = _inventory_table([f"t{i}" for i in range(10)])
    counts = publish.sync_inventory(table, [{"task_full_name": "a"}], "H", chunk_size=200,
                                    label="TaskInventory", force=True)  # must not raise
    assert counts == {"added": 1, "updated": 0, "deleted": 10}


def test_sync_inventory_never_touches_another_hosts_rows(monkeypatch):
    """query_all_rows must be scoped to this host; another host's stored rows are invisible."""
    wheres = []
    table = _inventory_table(["t0"])

    def recording_query_all_rows(table, *, where="1=1", out_fields="*", page=None):
        wheres.append(where)
        return [{"OBJECTID": 1, "task_full_name": "t0"}]

    monkeypatch.setattr(publish, "query_all_rows", recording_query_all_rows)
    publish.sync_inventory(table, [{"task_full_name": "t0"}], "O'Host", chunk_size=200, label="TaskInventory")
    assert wheres == ["host_name = 'O''Host'"]


def test_top_failing_7day_chart_does_not_query_the_90day_run_table():
    """The chart is labelled 7 days; TaskRuns keeps 90 and rule() has no date constraint.
    (Phase 4 replaced the "Failures (7 days)" tile with this per-task chart.)"""
    import taskdash_dashboard as dash
    chart = _generated_dashboard_widgets()["Top Failing Tasks"]
    ds = chart["datasets"][0]
    assert ds["dataSource"]["layerId"] == dash.LAYER_INVENTORY
    assert ds["statisticDefinitions"][0]["onStatisticField"] == "failures_last_7_days"


def test_dashboard_templates_are_stored_locally():
    """The builder must not depend on the old dashboard item, which is being retired."""
    import taskdash_dashboard as dash
    local = Path(dash.__file__).parent / dash.TEMPLATE_FILE
    assert local.exists(), f"{dash.TEMPLATE_FILE} must be committed alongside the code"
    widgets = {w["name"] for w in json.loads(local.read_text(encoding="utf-8"))["desktopView"]["widgets"]}
    assert {"Task Inventory", "Recent Runs", "Schedule Mix"} <= widgets


def _generated_dashboard_widgets():
    """Build the definition IN the test.

    Reading dashboard_taskdash.json instead made these assertions vacuous: a reviewer deleted
    the trend chart from build() outright and every test still passed, because the stale
    artifact on disk still contained it. The artifact is also regenerated with fresh widget
    UUIDs on every run, so it is not a stable fixture.
    """
    import taskdash_dashboard as dash
    tpl = json.loads((Path(dash.__file__).parent / dash.TEMPLATE_FILE).read_text(encoding="utf-8"))
    by_name = {w["name"]: w for w in tpl["desktopView"]["widgets"]}
    templates = {
        "table": by_name["Task Inventory"],
        "list": by_name["Recent Runs"],
        "serial_config": by_name["Schedule Mix"]["chartConfig"],
    }
    return {w["name"]: w for w in dash.build("TESTITEM", templates)["desktopView"]["widgets"]}


def test_new_widgets_present_with_correct_layers():
    import taskdash_dashboard as dash
    widgets = _generated_dashboard_widgets()
    # Collector Heartbeat is not listed: it reads an Arcade data source (for refreshInterval)
    # and has no layerId. test_dashboard_phase4.py covers its binding.
    expected_layers = {
        "7-Day Success Rate": dash.LAYER_INVENTORY,
        "Metric Summary": dash.LAYER_METRICS,
    }
    for name, layer in expected_layers.items():
        assert name in widgets, f"{name!r} missing from the generated dashboard"
        assert widgets[name]["datasets"][0]["dataSource"]["layerId"] == layer



def test_no_unscoped_duration_trend_chart():
    """A duration trend across ALL tasks is unreadable: Server Restart Window (~3,400s) and
    Utility Buffer (~70s) share one y-axis with no series split, so it says nothing about
    any task. Scoping it needs a categorySelector on task_name -- a grouped Metric Summary
    emits no usable whereClause, and a cross-layer Task Health selection keys on OBJECTID and
    matches nothing (both verified by rendering). If a trend widget is reintroduced it must
    come with that control, so its bare reappearance is a regression. Phase 4 reintroduced it
    behind a selection: it must be a requiresSelection target of the Task selector or of the
    Task Health row (the row is the required source since the render test showed Dashboards
    waits for EVERY required source)."""
    import taskdash_dashboard as dash
    tpl = json.loads((Path(dash.__file__).parent / dash.TEMPLATE_FILE).read_text(encoding="utf-8"))
    by_name = {w["name"]: w for w in tpl["desktopView"]["widgets"]}
    definition = dash.build("TESTITEM", {"table": by_name["Task Inventory"], "list": by_name["Recent Runs"],
                                         "serial_config": by_name["Schedule Mix"]["chartConfig"]})
    trends = [w for w in definition["desktopView"]["widgets"] if w["name"].startswith("Duration Trend")]
    sources = [w for w in dash.desktop_items(definition) if w["name"] in ("Task", "Task Health")]
    scoped = {t["targetId"].split("#")[0] for s in sources for e in s.get("events", []) for a in e["actions"]
              for t in a["targets"] if t["requiresSelection"]}
    assert trends and all(w["id"] in scoped for w in trends)


@pytest.mark.parametrize("dropped", ["Observations by Metric", "Observations by Task"])
def test_observation_count_charts_are_not_rebuilt(dropped):
    """These charted how many rows the MONITOR collected -- meta-data about TaskDash, not a
    signal about any workflow -- so they were removed. Re-adding one is a regression."""
    assert dropped not in _generated_dashboard_widgets()


def test_heartbeat_expression_corrects_for_dashboard_timezone():
    """The dashboard's timeZone:'system' setting makes Date(epoch_millis) read the millis as a
    LOCAL timestamp, not UTC. Arcade can't be executed here, so assert directly on the source:
    the subtraction must stay in raw Number() millis space and never call Date() on the field."""
    import taskdash_dashboard as dash
    assert "Number(Now())" in dash.HEARTBEAT_EXPRESSION
    assert "Number(raw)" in dash.HEARTBEAT_EXPRESSION
    assert "Date(" not in dash.HEARTBEAT_EXPRESSION


# ---------------------------------------------------------------- alerting

def _inv(**kw):
    base = {"task_name": "Parcel Refresh", "last_run_failed": 0, "is_overdue": 0,
            "last_result_text": "", "next_run_in_hours": 5.0}
    base.update(kw)
    return base


def test_alert_signature_reports_failures_and_overdue_only():
    inv = [_inv(), _inv(task_name="A", last_run_failed=1, last_result_text="Incorrect function."),
           _inv(task_name="B", is_overdue=1, next_run_in_hours=-3.0)]
    sig = publish.alert_signature(inv)
    assert len(sig) == 2
    assert any(s.startswith("FAILED: A") for s in sig)
    assert any(s.startswith("OVERDUE: B") for s in sig)


def test_alert_signature_is_stable_regardless_of_order():
    a = [_inv(task_name="X", last_run_failed=1), _inv(task_name="Y", is_overdue=1, next_run_in_hours=-1)]
    assert publish.alert_signature(a) == publish.alert_signature(list(reversed(a)))


def test_no_alert_when_problem_set_is_unchanged():
    """An unchanged failure mailed hourly trains the reader to ignore the alert."""
    send, _ = publish.should_alert({"signature": ["FAILED: A"]}, ["FAILED: A"], NOW, None)
    assert send is False


def test_alerts_on_new_problem_and_on_recovery():
    send, why = publish.should_alert({"signature": []}, ["FAILED: A"], NOW, None)
    assert send and why == "problem"
    send, why = publish.should_alert({"signature": ["FAILED: A"]}, [], NOW, None)
    assert send and why == "recovered"


def test_alerts_when_problem_list_changes_shape():
    send, why = publish.should_alert({"signature": ["FAILED: A"]}, ["FAILED: A", "FAILED: B"], NOW, None)
    assert send and why == "changed"


def test_daily_heartbeat_makes_silence_meaningful():
    """Without a heartbeat, 'no e-mail' and 'the monitor is dead' look identical."""
    local_7am = datetime(2026, 8, 11, 7, 0, tzinfo=NOW.astimezone().tzinfo)
    send, why = publish.should_alert({"signature": []}, [], local_7am.astimezone(timezone.utc), 7)
    assert send and why == "heartbeat"


def test_heartbeat_does_not_repeat_within_the_day():
    local_7am = datetime(2026, 8, 11, 7, 0, tzinfo=NOW.astimezone().tzinfo)
    utc = local_7am.astimezone(timezone.utc)
    send, _ = publish.should_alert(
        {"signature": [], "last_heartbeat_utc": utc.isoformat()}, [], utc, 7)
    assert send is False


def test_heartbeat_off_when_not_configured():
    send, _ = publish.should_alert({"signature": []}, [], NOW, None)
    assert send is False


# ---------------------------------------------------------------- module executes as a script

def test_module_runs_as_a_script_not_just_as_an_import():
    """pytest imports the module, which binds every name; running it as a script does not.

    A function defined BELOW `if __name__ == "__main__"` is unbound at execution time, so a
    call to it from main() raises NameError only in production. That happened: the alerting
    helpers were appended after the entry point and every real --apply run died after writing.
    """
    import subprocess
    result = subprocess.run([sys.executable, str(Path(publish.__file__)), "--help"],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f"module fails to execute as a script: {result.stderr[:400]}"
    assert "--apply" in result.stdout


def test_entry_point_is_the_last_statement_in_the_module():
    """Anything defined after it is invisible to script execution."""
    source = Path(publish.__file__).read_text(encoding="utf-8")
    guard = source.index('if __name__ == "__main__":')
    after = source[guard:]
    offenders = [ln for ln in after.splitlines()
                 if ln.startswith(("def ", "class ")) or (ln and not ln[0].isspace()
                 and not ln.startswith(("if __name__", "#", '"""')))]
    assert not offenders, f"defined after the entry point and therefore unbound at runtime: {offenders}"


def test_every_name_main_calls_is_defined_before_the_entry_point():
    """Generalises the above: catches the next append-below-the-guard mistake."""
    import ast
    source = Path(publish.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    guard_line = next(n.lineno for n in tree.body
                      if isinstance(n, ast.If) and "__main__" in ast.dump(n.test))
    defined_before = {n.name for n in tree.body
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                      and n.lineno < guard_line}
    main_fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    called = {c.func.id for c in ast.walk(main_fn)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    module_level = {n.name for n in tree.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    missing = (called & module_level) - defined_before
    assert not missing, f"main() calls names defined after the entry point: {sorted(missing)}"


# ------------------------------------------------- end-to-end wiring (mutation-proof)

def test_working_directory_flows_from_collector_into_metric_mining(tmp_path):
    """The collector->metrics plumbing, not just discover_log_directories() in isolation.

    A reviewer blanked both ends of this chain and all tests still passed, because every other
    test hand-passes the working directory instead of letting the collector supply it.
    """
    script_dir = tmp_path / "proj"
    script_dir.mkdir()
    script = script_dir / "run.py"
    script.write_text("", encoding="utf-8")
    wd_logs = tmp_path / "sidecar" / "Logs"
    wd_logs.mkdir(parents=True)
    (wd_logs / "run_1.log").write_text("Records updated: 55\n", encoding="utf-8")

    raw = _task(primary_executable="python.exe", primary_arguments=str(script))
    raw["primary_working_directory"] = str(tmp_path / "sidecar")
    inventory = collect.build_inventory_rows([raw], host_name="H", descriptions={}, audit_dt=NOW)
    assert inventory[0]["command_target_working_directory"] == str(tmp_path / "sidecar")

    rows, summary = metrics.build_metric_rows(inventory, host_name="H")
    assert any(r["metric_key"] == "updated_record_count" and r["metric_value"] == 55.0
               for r in rows), "the working directory's logs were not mined"
    assert summary[inventory[0]["task_full_name"]]["log_files_scanned"] == 1


def test_working_directory_that_is_an_ancestor_is_ignored(tmp_path):
    """7 of 25 real TASKHOST-1 tasks declare the shared repo root as their working directory."""
    script_dir = tmp_path / "proj"
    script_dir.mkdir()
    (script_dir / "run.py").write_text("", encoding="utf-8")
    (tmp_path / "Logs").mkdir()
    (tmp_path / "Logs" / "shared.log").write_text("Records updated: 999\n", encoding="utf-8")

    found = metrics.discover_log_directories(str(script_dir / "run.py"), str(tmp_path))
    resolved = {str(p.resolve()).lower() for p in found}
    assert str(tmp_path.resolve()).lower() not in resolved, \
        "an ancestor working directory would attribute one shared log to every task under it"


def test_summarize_runs_produces_the_duration_baseline_inputs():
    """enrich_inventory's anomaly needs these; the anomaly tests hand-inject them."""
    summary = publish.summarize_runs(RUN_ROWS)["\\Parcel Refresh"]
    assert summary["completed_run_count"] == 3
    assert summary["mean_duration_seconds"] == pytest.approx((42.0 + 10.0 + 11.0) / 3)


def test_duration_anomaly_fires_end_to_end_from_real_run_rows(frozen_now):
    """Wires summarize_runs -> enrich_inventory so blanking either end fails a test. Needs
    frozen_now: the 30-day baseline window (3.5) is relative to utc_now(), and every row here
    is dated relative to the fixed NOW."""
    rows = [{"task_full_name": "\\Parcel Refresh", "is_error": 0, "duration_seconds": 10.0,
             "start_time_utc": iso(NOW - timedelta(days=d))} for d in range(1, 6)]
    rows.append({"task_full_name": "\\Parcel Refresh", "is_error": 0, "duration_seconds": 90.0,
                 "start_time_utc": iso(NOW)})
    inventory = collect.build_inventory_rows([_task()], host_name="H",
                                             descriptions={}, audit_dt=NOW)
    publish.enrich_inventory(inventory, publish.summarize_runs(rows), {})
    row = inventory[0]
    assert row["is_duration_anomaly"] == 1
    assert row["duration_ratio"] > 2.0
    assert "usual" in row["attention_reason"]
    assert row["health_status"] == "Success", "the anomaly is advisory and must not change health"


def test_attention_reason_is_idempotent_across_repeated_enrichment():
    """enrich_inventory runs twice per publish; appending duplicated the sentence."""
    inventory = collect.build_inventory_rows([_task()], host_name="H",
                                             descriptions={}, audit_dt=NOW)
    summary = {"\\Parcel Refresh": {"last_duration_seconds": 100.0, "baseline_median_seconds": 40.0,
                                     "baseline_p95_seconds": 50.0, "baseline_p5_seconds": 30.0,
                                     "baseline_run_count": 5, "runs_last_7_days": 5,
                                     "failures_last_7_days": 0}}
    publish.enrich_inventory(inventory, summary, {})
    once = inventory[0]["attention_reason"]
    publish.enrich_inventory(inventory, summary, {})
    publish.enrich_inventory(inventory, summary, {})
    assert inventory[0]["attention_reason"] == once
    assert once.count("usual") == 1


def test_indicator_arcade_uses_the_flat_indicator_contract():
    """indicatorWidget returns flat middleText; `attributes:` is the listWidget contract and
    renders blank. Verified against the three proven arcade indicators in the template file."""
    widgets = _generated_dashboard_widgets()
    for name in ("Collector Heartbeat", "7-Day Success Rate"):
        w = widgets[name]
        assert w["arcadeEnabled"] is True
        assert "middleText:" in w["expression"], f"{name} must return flat middleText"
        assert "attributes:" not in w["expression"], f"{name} uses the listWidget contract"
        assert w["defaultSettings"]["middleSection"]["textInfo"]["text"] == "{calculated/value}"


# ------------------------------------------------- incremental collection across passes

class _RecordingTable:
    """Captures edit_features calls; optionally fails on the Nth call."""

    def __init__(self, existing=(), fail_on_call=0):
        self.existing = [dict(r) for r in existing]
        self.calls = []
        self.fail_on_call = fail_on_call

    def query(self, **kw):
        class _Result:
            features = []
        result = _Result()
        if not kw.get("result_offset"):
            result.features = [type("F", (), {"attributes": r})() for r in self.existing]
        return result

    def edit_features(self, **kw):
        self.calls.append(kw)
        ok = len(self.calls) != self.fail_on_call
        entry = [{"success": ok, "error": {"description": "boom"}}]
        return {"addResults": entry * len(kw.get("adds") or []),
                "updateResults": entry * len(kw.get("updates") or [])}


def _two_pass_rows():
    """Pass 1 sees a run start (100/200); pass 2 sees only its end (201/102)."""
    inventory = collect.build_inventory_rows([_task()], host_name="H", descriptions={}, audit_dt=NOW)
    first = collect.build_run_rows(
        [_event(100, 30, event_record_id=10), _event(200, 30, event_record_id=11)],
        inventory, host_name="H")
    second = collect.build_run_rows(
        [_event(201, 5, result_code=0, event_record_id=20), _event(102, 5, event_record_id=21)],
        inventory, host_name="H")
    return first, second


def test_second_pass_alone_estimates_the_start():
    """Pass 2 has no event 100: the start is estimated and the duration is left unknown."""
    _, second = _two_pass_rows()
    assert second[0]["start_time_estimated"] == 1
    assert second[0]["duration_seconds"] is None


def test_keep_stored_start_restores_the_real_start_and_duration():
    first, second = _two_pass_rows()
    stored_row = publish.prepare_rows(first, "TaskRuns", iso(NOW))[0]
    stored = {first[0]["run_key"]: stored_row}

    assert publish.keep_stored_start(second, stored) == 1
    run = second[0]
    assert run["start_time_estimated"] == 0
    assert parse_iso_datetime(run["start_time_utc"]) == NOW - timedelta(minutes=30)
    assert run["duration_seconds"] == 25 * 60
    assert run["duration_minutes"] == 25.0
    assert run["event_count"] == 4


def test_keep_stored_start_leaves_runs_alone_without_a_real_stored_start():
    _, second = _two_pass_rows()
    key = second[0]["run_key"]
    assert publish.keep_stored_start(second, {}) == 0
    estimated_prior = {key: {"start_time_utc": to_epoch_millis(NOW),
                             "start_time_estimated": 1, "event_count": 2}}
    assert publish.keep_stored_start(second, estimated_prior) == 0
    assert second[0]["start_time_estimated"] == 1


def test_keep_stored_start_never_touches_a_run_with_its_own_start():
    first, _ = _two_pass_rows()
    before = dict(first[0])
    prior = {first[0]["run_key"]: {"start_time_utc": to_epoch_millis(NOW - timedelta(days=9)),
                                   "start_time_estimated": 0, "event_count": 9}}
    assert publish.keep_stored_start(first, prior) == 0
    assert first[0] == before


def test_keep_stored_start_does_not_double_count_re_read_events():
    """--full-history re-reads events already counted in the stored row."""
    _, second = _two_pass_rows()
    prior = {second[0]["run_key"]: {"start_time_utc": to_epoch_millis(NOW - timedelta(minutes=30)),
                                    "start_time_estimated": 0, "event_count": 4,
                                    "max_event_record_id": 21.0}}
    publish.keep_stored_start(second, prior)
    assert second[0]["event_count"] == 4


def test_keep_stored_start_keeps_the_earlier_of_two_estimates():
    _, second = _two_pass_rows()
    earlier = NOW - timedelta(minutes=20)
    prior = {second[0]["run_key"]: {"start_time_utc": to_epoch_millis(earlier),
                                    "start_time_estimated": 1, "event_count": 1,
                                    "max_event_record_id": 12.0}}
    assert publish.keep_stored_start(second, prior) == 1
    assert parse_iso_datetime(second[0]["start_time_utc"]) == earlier
    assert second[0]["start_time_estimated"] == 1
    assert second[0]["duration_seconds"] is None
    assert second[0]["event_count"] == 3


def test_upsert_writes_in_record_id_order_with_rollback_per_chunk():
    table = _RecordingTable(existing=[{"OBJECTID": 7, "run_key": "H|b"}])
    rows = [{"run_key": "H|c", "max_event_record_id": 30.0},
            {"run_key": "H|b", "max_event_record_id": 20.0},
            {"run_key": "H|a", "max_event_record_id": 10.0}]
    counts = publish.upsert_by_key(table, rows, ["run_key"], chunk_size=2, label="TaskRuns",
                                   order_field="max_event_record_id")

    assert counts == {"added": 2, "updated": 1, "deleted": 0}
    assert all(call["rollback_on_failure"] is True for call in table.calls)
    first, second = table.calls
    assert [a["attributes"]["run_key"] for a in first["adds"]] == ["H|a"]
    assert [u["attributes"]["OBJECTID"] for u in first["updates"]] == [7]
    assert [a["attributes"]["run_key"] for a in second["adds"]] == ["H|c"]
    assert second["updates"] is None


def test_upsert_failure_leaves_only_a_low_prefix_written():
    """The watermark is max(max_event_record_id) over what landed, so nothing above an unwritten
    row may be written first."""
    table = _RecordingTable(fail_on_call=2)
    rows = [{"run_key": f"H|{n}", "max_event_record_id": float(n)} for n in (5, 1, 4, 2, 3)]
    with pytest.raises(SystemExit):
        publish.upsert_by_key(table, rows, ["run_key"], chunk_size=2, label="TaskRuns",
                              order_field="max_event_record_id")
    assert len(table.calls) == 2, "must stop at the failing chunk"
    assert [a["attributes"]["max_event_record_id"] for a in table.calls[0]["adds"]] == [1.0, 2.0]


def test_incremental_event_query_reads_oldest_first():
    template = collect.POWERSHELL_HISTORY_QUERY_TEMPLATE
    incremental = template[template.index("if ($MinRecordId -gt 0)"):template.index("} else {")]
    assert "-Oldest" in incremental


def test_read_run_events_flags_a_full_incremental_read(monkeypatch):
    monkeypatch.setattr(collect, "collect_run_events",
                        lambda days, budget, timeout, min_record_id=0: [{}] * budget)
    events, stalled_risk = publish.read_run_events({"max_events_per_run": 3}, 60, 500, 0)
    assert len(events) == 3 and stalled_risk is True
    _, stalled_risk = publish.read_run_events({"max_events_per_run": 3}, 60, 0, 0)
    assert stalled_risk is False, "a time-window read cannot stall the watermark"


def test_a_stalled_incremental_read_fails_loudly(monkeypatch):
    monkeypatch.setattr(collect, "collect_tasks", lambda timeout: [])
    monkeypatch.setattr(publish, "read_run_events", lambda *a: ([{"event_id": 999}], True))
    with pytest.raises(SystemExit, match="stalled"):
        publish.collect_everything({"host_name_override": "H"}, None, min_record_id=500)


def _watermark_table(stored_max, bounded_max, seen):
    def fake_max(table, field, where="1=1"):
        seen.append(where)
        return bounded_max if "<=" in where else stored_max
    return fake_max


def test_event_watermark_is_per_host(monkeypatch):
    seen = []
    monkeypatch.setattr(publish, "max_field_value", _watermark_table(900, None, seen))
    assert publish.event_watermark(object(), "O'Host", newest=1000) == 900
    assert seen == ["host_name = 'O''Host'"]


def test_event_watermark_survives_a_log_reset(monkeypatch):
    """Record ids restart low: the old max would hide every new event until the 90-day purge."""
    seen = []
    monkeypatch.setattr(publish, "max_field_value", _watermark_table(9_000_000, None, seen))
    assert publish.event_watermark(object(), "H", newest=500) == 0, "first pass after a reset"
    monkeypatch.setattr(publish, "max_field_value", _watermark_table(9_000_000, 480, seen))
    assert publish.event_watermark(object(), "H", newest=520) == 480, "later passes resume"
    assert "max_event_record_id <= 520" in seen[-1]


def test_event_watermark_without_a_readable_log_keeps_the_stored_max(monkeypatch):
    monkeypatch.setattr(publish, "max_field_value", _watermark_table(900, None, []))
    assert publish.event_watermark(object(), "H", newest=None) == 900


def test_fetch_stored_runs_queries_by_escaped_key_in_chunks(monkeypatch):
    wheres = []
    monkeypatch.setattr(publish, "query_all_rows",
                        lambda table, where, out_fields: wheres.append(where) or [])
    keys = ["H|o'brien"] + [f"H|{n:03d}" for n in range(150)]
    publish.fetch_stored_runs(object(), keys + keys)
    assert len(wheres) == 2, "151 unique keys -> chunks of 100 and 51"
    assert "'H|o''brien'" in "".join(wheres)


def test_publish_merges_before_writing_runs(monkeypatch):
    """publish() must route estimated runs through the stored-start merge before the upsert."""
    first, second = _two_pass_rows()
    stored_row = dict(publish.prepare_rows(first, "TaskRuns", iso(NOW))[0], OBJECTID=1)
    writes = {}

    def fake_query_all_rows(table, where="1=1", out_fields="*", page=2000):
        return [stored_row] if "run_key" in where or out_fields.startswith("OBJECTID") else []

    def fake_upsert(table, rows, keys, **kw):
        writes.setdefault(kw["label"], rows)
        return {"added": 0, "updated": len(rows), "deleted": 0}

    monkeypatch.setattr(publish, "query_all_rows", fake_query_all_rows)
    monkeypatch.setattr(publish, "upsert_by_key", fake_upsert)
    monkeypatch.setattr(publish, "sync_inventory", lambda *a, **k: {"added": 0, "updated": 0, "deleted": 0})
    monkeypatch.setattr(publish, "purge_older_than", lambda *a, **k: 0)
    monkeypatch.setattr(publish, "get_tables", lambda item: {"TaskRuns": 1, "TaskInventory": 2,
                                                              "TaskMetrics": 3})
    collected = {"host_name": "H", "runs": second, "inventory": [], "metrics": [], "log_summary": {}}
    publish.publish(None, None, collected, {})

    written = writes["TaskRuns"][0]
    assert written["start_time_estimated"] == 0
    assert written["duration_seconds"] == 25 * 60


@pytest.mark.parametrize("target,expect_alert", [("prod", True), ("test", False)])
def test_only_a_prod_publish_sends_alerts(tmp_path, monkeypatch, target, expect_alert):
    import socket
    config = {"host_name_override": socket.gethostname(), "logging": {"log_dir": str(tmp_path)},
              "rules": {"profiles": list(REFERENCE_PROFILES)},
              "arcgis": {"item_id": "p", "test_item_id": "t"}}
    config_path = tmp_path / "c.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    alerts = []
    monkeypatch.setattr(publish, "connect_agol", lambda cfg: object())
    monkeypatch.setattr(publish, "ensure_service",
                        lambda gis, cfg, tgt: type("I", (), {"title": "x", "id": tgt})())
    monkeypatch.setattr(publish, "get_tables", lambda item: {})
    monkeypatch.setattr(publish, "collect_everything",
                        lambda *a, **k: {"host_name": "H", "inventory": [], "runs": [], "metrics": []})
    monkeypatch.setattr(publish, "publish", lambda *a, **k: None)
    monkeypatch.setattr(publish, "maybe_send_alert", lambda *a: alerts.append(a))
    monkeypatch.setattr(publish, "setup_logging", lambda log_dir, prefix: tmp_path / "x.log")
    monkeypatch.setattr(publish, "cleanup_old_logs", lambda *a: None)

    assert publish.main(["--apply", "--target", target, "--config", str(config_path)]) == 0
    assert bool(alerts) is expect_alert


def test_keep_stored_start_keeps_a_failure_reported_in_an_earlier_pass():
    """Multi-action run: action 1 failed in pass N, action 2 succeeded in pass N+1."""
    _, second = _two_pass_rows()
    prior = {second[0]["run_key"]: {"start_time_utc": to_epoch_millis(NOW - timedelta(minutes=30)),
                                    "start_time_estimated": 0, "event_count": 3,
                                    "max_event_record_id": 12.0, "result_code": 2147942402.0,
                                    "result_text": "File not found.", "run_status": "Error",
                                    "is_error": 1, "is_success": 0}}
    publish.keep_stored_start(second, prior)
    assert (second[0]["run_status"], second[0]["is_error"], second[0]["is_success"]) == ("Error", 1, 0)
    assert second[0]["result_text"] == "Script exited with code 2."


def test_starts_that_never_happened_are_not_counted_as_runs(frozen_now):
    rows = [{"task_full_name": "\\T", "is_error": 0, "duration_seconds": 5.0,
             "start_time_utc": iso(NOW - timedelta(hours=1)), "run_status": "Success"},
            {"task_full_name": "\\T", "is_error": 0, "duration_seconds": None,
             "start_time_utc": iso(NOW - timedelta(hours=2)), "run_status": "Skipped-AlreadyRunning"},
            {"task_full_name": "\\T", "is_error": 0, "duration_seconds": None,
             "start_time_utc": iso(NOW - timedelta(hours=3)), "run_status": "Missed"}]
    assert publish.summarize_runs(rows)["\\T"]["runs_last_7_days"] == 1


def test_collection_reads_the_audit_clock_before_the_slow_queries(monkeypatch):
    order = []
    monkeypatch.setattr(publish, "utc_now", lambda: order.append("clock") or NOW)
    monkeypatch.setattr(collect, "collect_tasks", lambda timeout: order.append("tasks") or [])
    monkeypatch.setattr(publish, "read_run_events", lambda *a: (order.append("events") or [], False))
    captured = {}
    real = collect.build_inventory_rows
    monkeypatch.setattr(collect, "build_inventory_rows",
                        lambda *a, **k: captured.update(k) or real(*a, **k))
    publish.collect_everything({"host_name_override": "H", "scheduler": {"overdue_grace_minutes": 40}}, None)
    assert order.index("clock") < order.index("tasks")
    assert captured["audit_dt"] == NOW and captured["overdue_grace_minutes"] == 40.0


def test_a_fixture_is_audited_at_its_capture_time(tmp_path, monkeypatch):
    fixture = tmp_path / "f.json"
    fixture.write_text(json.dumps({"host_name": "H", "captured_at_utc": iso(NOW), "tasks": [],
                                   "events": []}), encoding="utf-8")
    captured = {}
    real = collect.build_inventory_rows
    monkeypatch.setattr(collect, "build_inventory_rows",
                        lambda *a, **k: captured.update(k) or real(*a, **k))
    publish.collect_everything({"host_name_override": "H"}, fixture)
    assert captured["audit_dt"] == NOW



# ------------------------------------------------- Phase 1 cross-module review fixes

def test_a_101_paired_with_a_203_adds_no_second_failure_row(inventory):
    """Measured: every 101 has a 203 for the same task within 5 s (424/424 on TASKHOST-1)."""
    events = [_event(100, 10, instance="i-9", event_record_id=1),
              _event(203, 10, instance="i-9", event_record_id=2, result_code=2147942402),
              _event(101, 10, instance="", event_record_id=3, result_code=2147942402)]
    rows = collect.build_run_rows(events, inventory, host_name="H")
    assert len(rows) == 1 and rows[0]["run_key"] == "H|i-9"
    assert rows[0]["is_error"] == 1


def test_a_lone_101_is_kept_as_the_only_record_of_the_failed_launch(inventory):
    rows = collect.build_run_rows([_event(101, 10, instance="", event_record_id=3)],
                                  inventory, host_name="H")
    assert len(rows) == 1 and rows[0]["run_status"] == "StartFailed"


def test_synthetic_keys_survive_a_log_clear(inventory):
    """Record ids restart after a clear; the same rec id at another time is another event."""
    before = collect.build_run_rows([_event(332, 60 * 24, instance="", event_record_id=7)],
                                    inventory, host_name="H")
    after = collect.build_run_rows([_event(332, 5, instance="", event_record_id=7)],
                                   inventory, host_name="H")
    assert before[0]["run_key"] != after[0]["run_key"]


def test_a_failing_code_stored_while_running_survives_the_next_pass():
    """Pass N: 100 + 201(fail) of action 1, still running. Pass N+1: 201(ok) + 102."""
    inventory = collect.build_inventory_rows([_task()], host_name="H", descriptions={}, audit_dt=NOW)
    first = collect.build_run_rows(
        [_event(100, 30, event_record_id=10), _event(201, 20, result_code=2147942402, event_record_id=11)],
        inventory, host_name="H")
    assert first[0]["run_status"] == "Running" and first[0]["is_error"] == 0
    second = collect.build_run_rows(
        [_event(201, 5, result_code=0, event_record_id=20), _event(102, 5, event_record_id=21)],
        inventory, host_name="H")
    stored = {first[0]["run_key"]: publish.prepare_rows(first, "TaskRuns", iso(NOW))[0]}
    publish.keep_stored_start(second, stored)
    assert (second[0]["run_status"], second[0]["is_error"]) == ("Error", 1)
    assert second[0]["result_code"] == 2147942402


def _inventory_row(**overrides):
    row = collect.build_inventory_rows([_task()], host_name="H", descriptions={}, audit_dt=NOW)[0]
    row.update(overrides)
    return row


def test_a_newer_failed_launch_fails_the_task_even_with_a_clean_last_result(frozen_now):
    task = _inventory_row(last_run_time=iso(NOW - timedelta(days=1)))
    assert task["last_run_failed"] == 0
    runs = [{"task_full_name": task["task_full_name"], "is_error": 1, "run_status": "NotLoggedOn",
             "result_text": "The task did not start because the required user was not logged on.",
             "start_time_utc": iso(NOW - timedelta(hours=1)), "duration_seconds": None}]
    publish.enrich_inventory([task], publish.summarize_runs(runs), {})
    assert (task["health_status"], task["last_run_failed"], task["needs_attention"]) == ("Error", 1, 1)
    assert task["attention_reason"].startswith("Last run: Account not signed in. ")
    assert publish.alert_signature([task])[0].startswith("FAILED: Parcel Refresh")


def test_an_old_failed_run_does_not_override_a_newer_clean_last_run(frozen_now):
    task = _inventory_row(last_run_time=iso(NOW - timedelta(hours=1)))
    runs = [{"task_full_name": task["task_full_name"], "is_error": 1, "run_status": "Error",
             "result_text": "x", "start_time_utc": iso(NOW - timedelta(days=2)), "duration_seconds": 5.0}]
    publish.enrich_inventory([task], publish.summarize_runs(runs), {})
    assert task["last_run_failed"] == 0 and task["health_status"] != "Error"


def test_run_level_failure_is_idempotent_and_reversible(frozen_now):
    """enrich runs twice per publish; the second pass (from the stored table) must be able to undo
    what the first pass (this collection only) concluded."""
    task = _inventory_row(last_run_time=iso(NOW - timedelta(days=1)))
    failing = {task["task_full_name"]: {"last_run_is_error": 1, "last_run_status": "StartFailed",
                                        "last_run_result_text": "boom",
                                        "last_run_start": NOW - timedelta(hours=1)}}
    publish.enrich_inventory([task], failing, {})
    publish.enrich_inventory([task], failing, {})
    assert task["attention_reason"].count("boom") == 1
    publish.enrich_inventory([task], {}, {})
    assert task["last_run_failed"] == 0 and "boom" not in task["attention_reason"]


def test_a_disabled_task_is_not_failed_by_its_old_run_rows(frozen_now):
    """Replay of the 2026-08-11 capture: the disabled old Task Dash would have alerted."""
    task = _inventory_row(last_run_time=iso(NOW - timedelta(days=1)), enabled=0,
                          health_status="Disabled", last_run_failed=0)
    runs = [{"task_full_name": task["task_full_name"], "is_error": 1, "run_status": "Error",
             "result_text": "Incorrect function.", "start_time_utc": iso(NOW - timedelta(hours=1)),
             "duration_seconds": 5.0}]
    publish.enrich_inventory([task], publish.summarize_runs(runs), {})
    assert task["health_status"] == "Disabled" and task["last_run_failed"] == 0
    assert publish.alert_signature([task]) == []
