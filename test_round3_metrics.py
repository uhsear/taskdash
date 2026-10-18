"""Regression tests for the round-3 audit fixes to taskdash_metrics.py.

Each test targets one finding id from scratchpad/round3_digest.md. Matches the tmp_path /
_scan / _values style already used in test_round1_metrics.py.
"""

from __future__ import annotations

import taskdash_metrics as metrics


def _scan(tmp_path, text, name="run.log"):
    log = tmp_path / name
    log.write_text(text, encoding="utf-8")
    return metrics.scan_log_file(log, max_lines=0)


def _values(result):
    return {key: value for key, (value, _) in result["metrics"].items()}


# ------------------------------------------------- metrics_tab-03 / wording-04: join display names

def test_ap_join_display_names_say_updated_not_matched():
    assert metrics.metric_display_name("ap_x_geo_updated_record_count") == (
        "Address points updated by the direction join")
    assert metrics.metric_display_name("ap_x_zip_updated_record_count") == (
        "Address points updated by the jurisdiction join")


# ------------------------------------------------------------------------------- metrics_tab-05

def test_full_refresh_insert_line_is_reloaded_not_inserted(tmp_path):
    """"inserted N rows in ..." is a full reload's whole-table row count (Streets OpenData
    full refresh); it must not land in the same series as a differential's "inserts=N"."""
    values = _values(_scan(tmp_path, "2026-09-18 02:32:19,495 INFO inserted 47342 rows in 0:01:20.620008\n"))
    assert values == {"reloaded_record_count": 47342.0}


def test_differential_inserts_line_still_gives_inserted_record_count(tmp_path):
    values = _values(_scan(
        tmp_path, "2026-09-25 02:30:24,179 INFO diff: inserts=0 updates=0 deletes=0 unchanged=47342\n"))
    assert values["inserted_record_count"] == 0.0
    assert "reloaded_record_count" not in values


# ------------------------------------------------------------------------------- metrics_tab-08

def test_all_locators_rebuilt_successfully_records_a_zero_failure_count(tmp_path):
    """A clean Permit Locator Rebuild run must record failure_count=0, or a stale nonzero value
    from an earlier failing run stays the latest metric forever."""
    values = _values(_scan(
        tmp_path,
        "2026-09-25 01:04:41,713 [INFO] All locators rebuilt successfully. Total time: 0:04:35.837861\n"))
    assert values["failure_count"] == 0.0


def test_failing_rebuild_line_still_records_the_failure_count(tmp_path):
    values = _values(_scan(tmp_path, "2026-09-05 01:04:49,741 [WARNING] Completed WITH FAILURES (4/5):\n"))
    assert values["failure_count"] == 4.0


# --------------------------------------------------------- metrics_tab-r2regress-07 / wording-06

def test_selector_names_are_clear_one_noun_no_raw_tokens():
    # metrics_tab-live-replicas-stops-parsing: since the 08:10 fix the count is anchored
    # replicas only, so the name says so.
    assert metrics.metric_display_name("replica_over_warning_count") == (
        "Replicas flagged for a watermark gap over 3")
    assert metrics.metric_display_name("replica_family_over_critical_count") == (
        "Replica families with a sync chain over 20")
    assert metrics.metric_display_name("idle_replica_count") == "Idle replicas touched"
    assert metrics.metric_display_name("segments_pending_retirement_count") == (
        "Segments checked for retirement")
    assert metrics.metric_display_name("target_record_count_before") == "Target records before sync"
    assert metrics.metric_display_name("target_record_count_after") == "Target records after sync"
    # metric_key values are unchanged -- only the display name moved.
    assert metrics.canonicalize_metric_key("target_record_count_before_sync") == "target_record_count_before"


def test_script_reported_time_seconds_display_name():
    # metrics_tab-01: renamed from "Script time (seconds)" -- on AP/Street Updates duration_seconds
    # times only the field-update phase, not the whole run, so the bare "Script time" name read
    # like the whole run's duration.
    assert metrics.metric_display_name("duration_seconds") == "Script-reported time (seconds)"
