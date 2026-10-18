"""Regression tests for the round-4 audit fixes to taskdash_metrics.py.

Each test targets one finding id from scratchpad/round4_digest.md. Matches the tmp_path /
monkeypatch style already used in test_round1_metrics.py and test_round2_metrics.py.
"""

from __future__ import annotations

import os

import taskdash_metrics as metrics


def _scan(tmp_path, text, name="run.log"):
    log = tmp_path / name
    log.write_text(text, encoding="utf-8")
    return metrics.scan_log_file(log, max_lines=0)


def _values(result):
    return {key: value for key, (value, _) in result["metrics"].items()}


# ------------------------------------------------- task_detail-taskdash-no-log-found-false

def test_own_task_shows_its_newest_taskdash_log_not_no_log_found(tmp_path, monkeypatch):
    """Task Dash is never mined (no metric rows), but its Task Detail card must show its own
    newest per-run log with real error/warning counts instead of 'No log found.'"""
    fake_root = tmp_path / "TaskDash"
    fake_root.mkdir()
    logs = fake_root / "Logs"
    logs.mkdir()
    (logs / "bootstrap.log").write_text("TaskMetrics: added 0, updated 867.\n", encoding="utf-8")
    (logs / "taskdash_20260927_070000.log").write_text(
        "2026-09-27 07:00:01 INFO Starting collection\n"
        "2026-09-27 07:00:05 ERROR Could not reach host X\n"
        "2026-09-27 07:00:06 WARNING Retrying host X\n",
        encoding="utf-8",
    )
    newest = logs / "taskdash_20260928_070000.log"
    newest.write_text(
        "2026-09-28 07:00:01 INFO Starting collection\n"
        "2026-09-28 07:00:09 INFO TaskRuns: added 49, updated 72\n",
        encoding="utf-8",
    )
    # Force a real mtime gap: files written back-to-back in a test can land on the same
    # filesystem timestamp, which would make "newest" ambiguous.
    older = logs / "taskdash_20260927_070000.log"
    os.utime(older, (older.stat().st_mtime - 60,) * 2)

    monkeypatch.setattr(metrics, "__file__", str(fake_root / "taskdash_metrics.py"))
    inventory_rows = [{
        "task_name": "Task Dash", "task_full_name": "\\Task Dash",
        "command_target_path": str(fake_root / "run_taskdash.cmd"),
        "command_target_name": "run_taskdash.cmd",
    }]
    rows, summary = metrics.build_metric_rows(inventory_rows, host_name="H")

    assert rows == []  # still never mined for metric series
    own = summary["\\Task Dash"]
    assert own["latest_log_name"] == "taskdash_20260928_070000.log"
    assert own["latest_log_error_lines"] == 0
    assert own["latest_log_warning_lines"] == 0
    assert own["log_files_scanned"] == 1


def test_own_task_keeps_no_log_found_when_only_bootstrap_log_exists(tmp_path, monkeypatch):
    """A bootstrap.log with no taskdash_*.log sibling must not be picked up as the own log."""
    fake_root = tmp_path / "TaskDash"
    fake_root.mkdir()
    logs = fake_root / "Logs"
    logs.mkdir()
    (logs / "bootstrap.log").write_text("TaskMetrics: added 0, updated 867.\n", encoding="utf-8")

    monkeypatch.setattr(metrics, "__file__", str(fake_root / "taskdash_metrics.py"))
    inventory_rows = [{
        "task_name": "Task Dash", "task_full_name": "\\Task Dash",
        "command_target_path": str(fake_root / "run_taskdash.cmd"),
        "command_target_name": "run_taskdash.cmd",
    }]
    rows, summary = metrics.build_metric_rows(inventory_rows, host_name="H")

    assert rows == []
    assert summary["\\Task Dash"]["latest_log_name"] is None


# ---------------------------------------------- metrics_tab-02/wording-r3regress-noise-metrics-in-picker

def test_noise_metrics_in_picker_retired():
    """Buffer count (always 3) and Output feature count (always 1) are structural, not workload."""
    assert metrics.canonicalize_metric_key("created_individual_buffers") is None
    assert metrics.canonicalize_metric_key("features") is None


# --------------------------------------------------------------------------- metrics_tab-02

def test_acme_storm_deleted_and_hosted_keys_get_plain_names(tmp_path):
    # Two "Processing:" sections in one file, like the real Stormwater Dataset Upload
    # log -- only then does _resolve_metrics prefix the key by section (see M1 in
    # test_round1_metrics.py's STORMWATER fixture).
    values = _values(_scan(
        tmp_path,
        "2026-09-28 00:25:09,906 - INFO - Processing: Acme_StormBasins\n"
        "2026-09-28 00:25:46,807 - INFO -   Total deleted: 3,404\n"
        "2026-09-28 00:25:53,521 - INFO -   Verification: 3,404 features in hosted layer\n"
        "2026-09-28 00:26:51,686 - INFO - Processing: Acme_StormPipe\n"
        "2026-09-28 00:28:07,130 - INFO -   Total deleted: 15,562\n"
        "2026-09-28 00:28:19,454 - INFO -   Verification: 15,562 features in hosted layer\n",
    ))
    assert values["acme_storm_pipe_deleted_record_count"] == 15562.0
    assert values["acme_storm_pipe_hosted_record_count"] == 15562.0
    assert metrics.metric_display_name("acme_storm_pipe_deleted_record_count") == (
        "Acme storm pipe rows removed before reload")
    assert metrics.metric_display_name("acme_storm_pipe_hosted_record_count") == (
        "Acme storm pipe rows in hosted layer")


def test_acme_storm_exported_key_gets_a_plain_name(tmp_path):
    """metrics_tab-acme-exported-name-inconsistent: the exported-record count kept the
    automatic 'exported record count' name next to its renamed deleted/hosted siblings.
    "Exported N features to temp FGDB" is the real stormwater_agol_sync.py wording; two
    "Processing:" sections, as in the real log, so _resolve_metrics prefixes the key by section
    (see the deleted/hosted test above)."""
    values = _values(_scan(
        tmp_path,
        "2026-08-27 00:25:18,253 - INFO - Processing: Acme_StormBasins\n"
        "2026-08-27 00:25:25,262 - INFO -   Exported 3,404 features to temp FGDB\n"
        "2026-08-27 00:26:35,708 - INFO - Processing: Acme_StormPipe\n"
        "2026-08-27 00:26:41,000 - INFO -   Exported 15,562 features to temp FGDB\n",
    ))
    assert values["acme_storm_pipe_exported_record_count"] == 15562.0
    assert metrics.metric_display_name("acme_storm_pipe_exported_record_count") == (
        "Acme storm pipe rows exported for upload")


# --------------------------------------------------------------------------- metrics_tab-03 / wording-metric-names-mixed-patterns

def test_metric_names_mixed_patterns_get_plain_names():
    assert metrics.metric_display_name("feature_record_count") == "Records in the layer"
    assert metrics.metric_display_name("sde_parcels_before_truncate") == (
        "SDE parcels before reload")
    assert metrics.metric_display_name("today_parcel_count") == "Parcels today"
    assert metrics.metric_display_name("yesterday_parcel_count") == "Parcels yesterday"
    # repeat-wording-metric-names-jargon: "in both days" read badly; now says whose two copies.
    assert metrics.metric_display_name("common_parcel_count") == (
        "Parcels in both today's and yesterday's copy")


# --------------------------------------------------------- wording-r3regress-metric-name-failed-items

def test_failure_count_name_is_generic_not_task_specific():
    assert metrics.metric_display_name("failure_count") == "Items that failed"
    # metric_key itself is unchanged -- only the display name moved.
    assert metrics.canonicalize_metric_key("failed") is None  # bare "failed" stays noise


def test_own_task_log_skips_the_log_this_pass_is_still_writing(tmp_path):
    import logging
    import os
    import time
    older = tmp_path / "taskdash_20260928_130000.log"
    older.write_text("TaskDash started.\nERROR - previous run failed\n", encoding="utf-8")
    current = tmp_path / "taskdash_20260928_140000.log"
    current.write_text("TaskDash started.\n", encoding="utf-8")
    past = time.time() - 3600
    os.utime(older, (past, past))
    handler = logging.FileHandler(current, encoding="utf-8")
    logging.getLogger().addHandler(handler)
    try:
        summary = metrics._scan_own_task_logs(tmp_path, 30, 5000, metrics.utc_now())
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    assert summary["latest_log_name"] == older.name
    assert summary["latest_log_error_lines"] == 1
