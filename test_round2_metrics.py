"""Regression tests for the round-2 audit fixes to taskdash_metrics.py.

Each test targets one finding id from scratchpad/round2_digest.md. Matches the tmp_path /
monkeypatch style already used in test_metrics_hardening.py and test_round1_metrics.py.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

import taskdash_common as common
import taskdash_metrics as metrics


# ------------------------------------------------------- own-log mining (metrics_tab-03/wording-03)

def test_own_task_command_target_skips_directories_regardless_of_working_dir(tmp_path, monkeypatch):
    """The Task Dash inventory row is skipped by its command target alone, even when its
    working folder is nowhere near own_dir and would otherwise be discovered and mined."""
    fake_root = tmp_path / "TaskDash"
    fake_root.mkdir()
    other_dir = tmp_path / "Elsewhere"
    (other_dir / "Logs").mkdir(parents=True)
    (other_dir / "Logs" / "run.log").write_text("Records updated: 9\n", encoding="utf-8")

    monkeypatch.setattr(metrics, "__file__", str(fake_root / "taskdash_metrics.py"))
    inventory_rows = [{
        "task_name": "Task Dash", "task_full_name": "\\Task Dash",
        "command_target_path": str(other_dir / "run_taskdash.cmd"),
        "command_target_name": "run_taskdash.cmd",
        "command_target_working_directory": str(other_dir),
    }]
    rows, summary = metrics.build_metric_rows(inventory_rows, host_name="H")

    assert rows == []
    # Cleared, not absent: an absent key would keep the latest log an older version stored.
    assert summary["\\Task Dash"]["latest_log_name"] is None


def test_own_task_matches_a_taskdash_py_script_target_too(tmp_path, monkeypatch):
    fake_root = tmp_path / "TaskDash"
    fake_root.mkdir()
    other_dir = tmp_path / "Elsewhere"
    (other_dir / "Logs").mkdir(parents=True)
    (other_dir / "Logs" / "run.log").write_text("Records updated: 9\n", encoding="utf-8")

    monkeypatch.setattr(metrics, "__file__", str(fake_root / "taskdash_metrics.py"))
    inventory_rows = [{
        "task_name": "Task Dash", "task_full_name": "\\Task Dash",
        "command_target_path": str(other_dir / "taskdash_publish.py"),
        "command_target_name": "taskdash_publish.py",
        "command_target_working_directory": str(other_dir),
    }]
    rows, summary = metrics.build_metric_rows(inventory_rows, host_name="H")

    assert rows == []
    # Cleared, not absent: an absent key would keep the latest log an older version stored.
    assert summary["\\Task Dash"]["latest_log_name"] is None


def test_own_log_dir_override_still_excludes_the_historical_default_folder(tmp_path, monkeypatch):
    """wording-03: own_dir/"Logs" must be excluded on top of an own_log_dir override, not
    replaced by it -- the collector can still write bootstrap.log into the default folder."""
    fake_root = tmp_path / "TaskDash"
    fake_root.mkdir()
    (fake_root / "Logs").mkdir()
    (fake_root / "Logs" / "bootstrap.log").write_text(
        "TaskMetrics: added 0, updated 867.\n", encoding="utf-8")
    (fake_root / "taskdash_publish.py").write_text("", encoding="utf-8")
    relocated_logs = tmp_path / "Runtime" / "Logs"
    relocated_logs.mkdir(parents=True)

    monkeypatch.setattr(metrics, "__file__", str(fake_root / "taskdash_metrics.py"))
    inventory_rows = [{"task_name": "Task Dash", "task_full_name": "\\Task Dash",
                       "command_target_path": str(fake_root / "taskdash_publish.py")}]
    rows, _ = metrics.build_metric_rows(
        inventory_rows, host_name="H", own_log_dir=str(relocated_logs))

    assert rows == []


# ---------------------------------------------- manual/dry-run log exclusion (task_detail-01)

@pytest.mark.parametrize("as_millis,duration,expected", [
    (False, None, ["scheduled.log"]),
    (True, None, ["scheduled.log"]),  # --refresh-metrics passes the stored epoch millis
    (False, 3 * 3600, ["manual.log", "scheduled.log"]),  # a 3 h run can open a log 2 h in
])
def test_scan_task_logs_excludes_log_created_after_last_run_slack(tmp_path, monkeypatch, as_millis,
                                                                  duration, expected):
    script_dir = tmp_path / "Proj"
    logs = script_dir / "Logs"
    logs.mkdir(parents=True)
    (script_dir / "run.py").write_text("", encoding="utf-8")
    scheduled = logs / "scheduled.log"
    scheduled.write_text("Records updated: 1\n", encoding="utf-8")
    manual = logs / "manual.log"
    manual.write_text("Records updated: 99\n", encoding="utf-8")

    last_run = common.utc_now() - timedelta(hours=1)
    created = {
        str(scheduled): last_run,                # created right at the scheduled run
        str(manual): last_run + timedelta(hours=2),  # created 2h later -- a manual run
    }
    monkeypatch.setattr(metrics, "_file_created_dt", lambda p: created[str(p)])

    inventory_rows = [{
        "task_name": "Proj", "task_full_name": "\\Proj",
        "command_target_path": str(script_dir / "run.py"),
        "last_run_time": last_run.timestamp() * 1000 if as_millis else last_run.isoformat(),
        "last_duration_seconds": duration,
    }]
    rows, summary = metrics.build_metric_rows(inventory_rows, host_name="H")

    assert sorted(r["log_file_name"] for r in rows) == expected
    if expected == ["scheduled.log"]:
        assert summary["\\Proj"]["latest_log_name"] == "scheduled.log"


def test_scan_task_logs_falls_back_to_all_files_when_none_qualify(tmp_path, monkeypatch):
    """If every candidate was created after the slack window, keep today's behaviour (use
    every file found) instead of going silent."""
    script_dir = tmp_path / "Proj"
    logs = script_dir / "Logs"
    logs.mkdir(parents=True)
    (script_dir / "run.py").write_text("", encoding="utf-8")
    only_log = logs / "only.log"
    only_log.write_text("Records updated: 1\n", encoding="utf-8")

    last_run = common.utc_now() - timedelta(hours=1)
    monkeypatch.setattr(
        metrics, "_file_created_dt", lambda p: last_run + timedelta(hours=5))

    inventory_rows = [{
        "task_name": "Proj", "task_full_name": "\\Proj",
        "command_target_path": str(script_dir / "run.py"),
        "last_run_time": last_run.isoformat(),
    }]
    rows, summary = metrics.build_metric_rows(inventory_rows, host_name="H")

    assert [r["log_file_name"] for r in rows] == ["only.log"]
    assert summary["\\Proj"]["latest_log_name"] == "only.log"


# -------------------------------------------------------- idle-replica dry run (metrics_tab-10)

def test_would_touch_idle_replica_gives_no_metric():
    assert metrics.extract_metrics_from_line("RESULT: would touch 12 idle replica(s)") == []


def test_touched_idle_replica_gives_a_metric():
    result = dict(metrics.extract_metrics_from_line("RESULT: touched 1 idle replica(s)"))
    assert result["idle_replica_count"] == 1.0


# ------------------------------------------------------------------------------ critic-02

def test_scan_log_file_no_updates_needed_records_zero(tmp_path):
    log = tmp_path / "street.log"
    log.write_text(
        "2026-09-10 23:45:12,159 - INFO - Total records in feature class: 47342\n"
        "2026-09-10 23:45:12,161 - INFO - Checking if updates are needed...\n"
        "2026-09-10 23:45:15,713 - INFO - No updates needed - all fields already have correct values\n",
        encoding="utf-8",
    )
    result = metrics.scan_log_file(log, max_lines=0)
    assert result["metrics"]["updated_record_count"][0] == 0.0
    assert result["metrics"]["null_values_replaced_count"][0] == 0.0


# ------------------------------------------------------------------- metrics_tab-08 / metrics_tab-09

def test_target_record_count_sync_aliases_collapse_to_the_non_sync_key():
    assert metrics.canonicalize_metric_key("target_record_count_before_sync") == "target_record_count_before"
    assert metrics.canonicalize_metric_key("target_record_count_after_sync") == "target_record_count_after"


def test_exceptions_written_is_noise_not_its_own_series():
    assert metrics.canonicalize_metric_key("exceptions_written") is None


# ---------------------------------------------------------------------------- DISPLAY_NAMES

def test_display_name_overrides_cover_the_jargon_keys():
    assert metrics.metric_display_name("cf_change_count") == "Confidentiality changes"
    assert metrics.metric_display_name("adds_pts_feature_count") == "Address points extracted"
    assert metrics.metric_display_name("duration_seconds") == "Script-reported time (seconds)"
    assert metrics.metric_display_name("failure_count") == "Items that failed"  # any task can log it
    assert metrics.metric_display_name("ap_x_geo_unmatched_point_count") == (
        "Address points outside a direction area")
