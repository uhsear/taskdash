"""Round 6 audit (final prod audit) fixes to taskdash_metrics.py.

Each test targets one finding id from scratchpad/round6_digest.md. Matches the tmp_path /
monkeypatch style already used in test_round1_metrics.py through test_round4_metrics.py. The
replica log lines are copied verbatim from the real TASKHOST-1 logs in scratchpad/truth6/logs
(monitor_replica_lag_20260929_070501.log and _081034.log), and the CSC line shape is
copied from scratchpad/truth6/logs/deskmailer_debug.log.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import taskdash_metrics as metrics


def _scan(tmp_path, text, name="run.log"):
    log = tmp_path / name
    log.write_text(text, encoding="utf-8")
    return metrics.scan_log_file(log, max_lines=0)


def _values(result):
    return {key: value for key, (value, _) in result["metrics"].items()}


# ------------------------------- metrics_tab-live-replicas-stops-parsing / wording-live-replicas-metric-lost

def test_old_wording_live_replica_items_still_parses(tmp_path):
    """The pre-08:10 wording ("N live replica items parsed") must keep working: older logs with
    it are still inside the scan window."""
    values = _values(_scan(tmp_path,
        "2026-09-29 07:05:01,689 - INFO - CHECK 2  watermark gap: 312 live replica items "
        "parsed, 38 over the warning threshold (gap > 3)\n"))
    assert values == {"replica_item_count": 312.0, "replica_over_warning_count": 38.0}


def test_new_wording_replica_items_and_sync_anchor_and_stale_count_are_mined(tmp_path):
    """The 08:10 fix dropped "live" and added an anchored-replica breakdown; all four counts on
    the new CHECK 2 / "Not flagged" lines must be mined under their own keys."""
    values = _values(_scan(tmp_path,
        "2026-09-29 08:10:34,409 - INFO - CHECK 2  watermark gap: 312 replica items parsed, "
        "15 with a SYNC anchor; 12 over the warning threshold (gap > 3)\n"
        "2026-09-29 08:10:34,410 - INFO -   Not flagged: 26 registration(s) with a gap but no "
        "SYNC anchor (retired/test; cannot pin the floor): 56675 gap=443, 74318 gap=212\n"))
    assert values == {
        "replica_item_count": 312.0,
        "anchored_replica_count": 15.0,
        "replica_over_warning_count": 12.0,
        "stale_replica_registration_count": 26.0,
    }


def test_display_names_for_the_new_and_changed_replica_keys():
    assert metrics.metric_display_name("replica_item_count") == "Replica registrations"
    assert metrics.metric_display_name("anchored_replica_count") == "Replicas with a sync anchor"
    assert metrics.metric_display_name("stale_replica_registration_count") == (
        "Unanchored registrations with a watermark gap over 3 (not flagged)")
    assert metrics.metric_display_name("replica_over_warning_count") == (
        "Replicas flagged for a watermark gap over 3")


# --------------------------------------------------- repeat-operations-csc-email-failures-invisible

def _build(tmp_path, debug_text):
    logs = tmp_path / "Logs"
    logs.mkdir()
    (logs / "deskmailer_debug.log").write_text(debug_text, encoding="utf-8")
    (tmp_path / "csc.py").write_text("", encoding="utf-8")
    _, summary = metrics.build_metric_rows(
        [{"task_name": "Service Desk Mailer", "task_full_name": "\\Service Desk Mailer",
          "command_target_path": str(tmp_path / "csc.py")}], host_name="H")
    return summary["\\Service Desk Mailer"]


def test_debug_file_counts_only_error_and_warning_lines_in_the_last_7_days(tmp_path, monkeypatch):
    """One line older than 7 days and two within the window: only the two recent ones count,
    for both ERROR and WARNING levels, matching deskmailer_debug.log's own '-- LEVEL:'
    shape."""
    monkeypatch.setattr(metrics, "utc_now", lambda: datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc))
    entry = _build(tmp_path,
        "2026-09-16 12:45:50 -- ERROR: SMTP connection/setup failed, e-mail section aborted: old\n"
        "2026-09-23 12:00:31 -- ERROR: SMTP connection/setup failed, e-mail section aborted: recent\n"
        "2026-09-24 07:54:02 -- WARNING: retrying the query\n")
    assert entry["latest_log_name"] == "deskmailer_debug.log"
    assert entry["latest_log_error_lines"] == 1
    assert entry["latest_log_warning_lines"] == 1


def test_debug_file_with_no_parsable_timestamp_keeps_none(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "utc_now", lambda: datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc))
    entry = _build(tmp_path, "ERROR: no leading timestamp on this line at all\n")
    assert entry["latest_log_error_lines"] is None
    assert entry["latest_log_warning_lines"] is None


@pytest.mark.parametrize("tz", ["UTC0", "CET-1", "EST5"])  # UTC, UTC+1, UTC-5 (as on TASKHOST-1)
def test_debug_file_stamps_are_read_as_server_local_time(tmp_path, monkeypatch, local_zone, tz):
    """The debug file writes local time. Read as UTC, a line 1 h inside the window west of UTC
    fell outside it. The zone is pinned so the case runs the same on every machine."""
    local_zone(tz)
    now = datetime(2026, 9, 29, 9, 0, 0).astimezone(timezone.utc)  # local 09:00, 7 days later
    monkeypatch.setattr(metrics, "utc_now", lambda: now)
    entry = _build(tmp_path, "2026-09-22 09:30:00 -- ERROR: SMTP failed 30 min inside the window\n")
    assert entry["latest_log_error_lines"] == 1
