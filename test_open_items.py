"""Regression tests for the post-phase-4 open items: schedule-aware definition hash and the
IgnoreNew-vs-repetition check (1), the fleet-wide success rate (2), audit-time windows (3),
--preview-alert (4), and the deferred minors (5): per-run bootstrap logs, resent deletes,
healthcheck scope, and acknowledged config notes.

No network, no ArcGIS Online, no SMTP: fakes throughout. The subprocess tests run the real
run_taskdash.cmd against a stub script in a temp folder.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import taskdash_common as common
from conftest import REFERENCE_PROFILES

common.disable_arcpy_import()

import taskdash_collect as collect  # noqa: E402
import taskdash_publish as publish  # noqa: E402

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)
HERE = Path(__file__).resolve().parent


def _trigger(**kw):
    base = {"type": "Daily", "start": "2024-01-05T02:00:00", "end": "", "enabled": True,
            "days_interval": "1", "days_of_week": "", "weeks_interval": "",
            "repetition_interval": "", "repetition_duration": "", "random_delay": "", "delay": ""}
    base.update(kw)
    return base


def _task(name="T", triggers=None, **kw):
    base = {"task_name": name, "task_path": "\\", "task_full_name": "\\" + name, "state": "Ready",
            "enabled": True, "actions": "C:\\Jobs\\run.cmd", "primary_executable": "C:\\Jobs\\run.cmd",
            "primary_arguments": "", "primary_working_directory": "", "trigger_types": "Daily",
            "trigger_details": triggers if triggers is not None else [_trigger()],
            "run_as_user": "svc", "logon_type": "Interactive", "run_level": "Highest",
            "last_run_time": None, "next_run_time": None, "last_task_result": 0,
            "number_of_missed_runs": 0}
    base.update(kw)
    return base


def _fixture(tmp_path, tasks, events=(), captured="2026-01-10T12:00:00Z", name="f.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"host_name": "H", "captured_at_utc": captured,
                                "tasks": tasks, "events": list(events)}), encoding="utf-8")
    return path


class _Props(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


class _Feature:
    def __init__(self, attributes):
        self.attributes = attributes


class _Result:
    def __init__(self, rows):
        self.features = [_Feature(dict(r)) for r in rows]


# ==================================================================== 1. schedule in the definition

def test_the_task_query_reads_every_schedule_property_not_the_class_name():
    query = collect.POWERSHELL_TASK_QUERY
    for prop in ("StartBoundary", "EndBoundary", "DaysInterval", "DaysOfWeek", "WeeksInterval",
                 "Repetition", ".Interval", ".Duration", "RandomDelay", ".Delay", "trigger_details"):
        assert prop in query, prop
    assert "triggers = [string]$triggers" not in query


def test_describe_trigger_is_plain_text():
    weekly = _trigger(type="Weekly", days_interval="", days_of_week="18", weeks_interval="2",
                      repetition_interval="PT1H", repetition_duration="PT12H",
                      random_delay="PT10M", end="2027-01-01T00:00:00")
    assert collect.describe_trigger(weekly) == (
        "Weekly every 2 weeks on Mon, Thu at 2:00 AM (since Jan 5, 2024), repeats every 1 h for "
        "12 h, random delay 10 min, until Jan 1, 2027 12:00 AM")
    assert collect.describe_trigger(_trigger(days_interval="3")) == \
        "Daily every 3 days at 2:00 AM (since Jan 5, 2024)"
    assert collect.describe_trigger(_trigger(type="Time", repetition_interval="PT3H30M")) == \
        "One Time at Jan 5, 2024 2:00 AM, repeats every 3 h 30 min indefinitely"
    assert collect.describe_trigger(_trigger(type="Boot", start="", delay="PT30M", enabled=False)) == \
        "Startup, delay 30 min, trigger disabled"


def test_plain_duration_spells_out_days():
    """wording-13: "d" read oddly next to "h"/"min"; days are now spelled out and pluralized."""
    assert collect.plain_duration("P1D") == "1 day"
    assert collect.plain_duration("P3D") == "3 days"
    assert collect.plain_duration("PT72H") == "3 days"
    assert collect.plain_duration("P1DT2H") == "1 day 2 h"
    assert collect.plain_duration("PT3H30M") == "3 h 30 min"
    assert collect.plain_duration("") == ""


def test_trigger_recurrence_drops_every_day_and_every_week_at_interval_one():
    """collector_tab-04 / wording-06: "Daily every day" and "Weekly every week" repeated the
    kind word describe_trigger already put in front."""
    assert collect.describe_trigger(_trigger(type="Daily", days_interval="1")) == \
        "Daily at 2:00 AM (since Jan 5, 2024)"
    assert collect.describe_trigger(
        _trigger(type="Weekly", days_interval="", weeks_interval="1", days_of_week="64")) == \
        "Weekly on Sat at 2:00 AM (since Jan 5, 2024)"
    # weeks_interval > 1 still says "every N weeks" -- only the plain interval-of-1 case drops it.
    assert collect.describe_trigger(
        _trigger(type="Weekly", days_interval="", weeks_interval="2", days_of_week="64")) == \
        "Weekly every 2 weeks on Sat at 2:00 AM (since Jan 5, 2024)"
    # Every day-of-week bit set with no multi-week interval fires daily in practice (Task Dash's
    # real trigger); this now reads "Daily", not the self-contradicting "Weekly on every day"
    # (task_detail-r2regress-taskdash-schedule-wording / collector_tab-08).
    assert collect.describe_trigger(
        _trigger(type="Weekly", days_interval="", weeks_interval="1", days_of_week="127")) == \
        "Daily at 2:00 AM (since Jan 5, 2024)"
    # weeks_interval > 1 keeps the Weekly-on-every-day wording: skipping whole weeks is not daily.
    assert collect.describe_trigger(
        _trigger(type="Weekly", days_interval="", weeks_interval="2", days_of_week="127")) == \
        "Weekly every 2 weeks on every day at 2:00 AM (since Jan 5, 2024)"
    assert collect.describe_triggers({"type": "Logon"}) == "Logon"  # an unwrapped one-item array
    assert collect.describe_triggers(None) == ""


def test_a_monthly_trigger_read_from_the_task_xml_is_described():
    """The CIM layer returns monthly triggers as the base class ("Scheduled") with no days or
    months; the task query reads them from Export-ScheduledTask XML instead."""
    by_day = _trigger(type="Scheduled", days_interval="", days_of_month="1,15,Last",
                      months="January,July")
    assert collect.describe_trigger(by_day) == \
        "Monthly on day 1, 15, Last in Jan, Jul at 2:00 AM (since Jan 5, 2024)"
    by_weekday = _trigger(type="Scheduled", days_interval="", weeks_of_month="1,Last",
                          month_days_of_week="Monday,Friday",
                          months=",".join(calendar.month_name[1:]))
    assert collect.describe_trigger(by_weekday) == \
        "Monthly on Mon, Fri of week 1, Last at 2:00 AM (since Jan 5, 2024)"
    assert collect.describe_trigger(_trigger(type="Scheduled", days_interval="")) == \
        "Scheduled at Jan 5, 2024 2:00 AM"
    moved = dict(by_day, days_of_month="2,15,Last")
    assert publish.compute_definition_hash(_task(triggers=[by_day])) != \
        publish.compute_definition_hash(_task(triggers=[moved]))
    for prop in ("Export-ScheduledTask", "ScheduleByMonth", "ScheduleByMonthDayOfWeek"):
        assert prop in collect.POWERSHELL_TASK_QUERY, prop


@pytest.mark.parametrize("change", [
    {"start": "2024-01-05T03:00:00"}, {"days_interval": "2"}, {"repetition_interval": "PT1H"},
    {"repetition_duration": "PT4H", "repetition_interval": "PT1H"}, {"random_delay": "PT5M"},
    {"enabled": False}, {"type": "Weekly", "days_of_week": "2"},
])
def test_a_schedule_only_edit_changes_the_definition_hash(change):
    before = publish.compute_definition_hash(_task())
    after = publish.compute_definition_hash(_task(triggers=[_trigger(**change)]))
    assert before != after
    assert before.startswith(publish.DEFINITION_HASH_PREFIX) and len(before) == 64
    assert before == publish.compute_definition_hash(_task()), "the hash must be stable"


def test_the_first_v2_hash_after_upgrade_is_a_baseline_not_a_change():
    """Prod stores v1 hashes (bare hex). Without the scheme check every task would log
    DEFINITION CHANGED on the first run of this code."""
    old = {"task_full_name": "\\T", "task_name": "T", "definition_hash": "ab" * 32,
           "last_run_failed": 0, "is_overdue": 0, "config_warnings": ""}
    new = dict(old, definition_hash=publish.compute_definition_hash(_task()))
    changes = publish.detect_task_changes({"\\T": old}, [new], "H", "2026-09-25T00:00:00+00:00")
    assert changes == []


def test_a_stored_v2_hash_is_still_a_baseline_under_v4():
    """collector_tab-04 / wording-06 bumped v2 -> v3, and round 3 bumped v3 -> v4: a v2 hash
    still does not startswith the current prefix, so it reads as a baseline the same way a v1
    hash did under v2, not as 27 spurious DEFINITION CHANGED rows."""
    old = {"task_full_name": "\\T", "task_name": "T", "definition_hash": "v2" + "a" * 62,
          "last_run_failed": 0, "is_overdue": 0, "config_warnings": ""}
    new = dict(old, definition_hash=publish.compute_definition_hash(_task()))
    assert publish.detect_task_changes({"\\T": old}, [new], "H", "2026-09-25T00:00:00+00:00") == []


def test_a_stored_v3_hash_is_also_a_baseline_under_v4():
    """Round 3 bumped v3 -> v4 (the Weekly/Daily wording, the 12-hour time/plain-date change, and
    the newly-hashed Settings-tab values are all hash inputs): a stored v3 hash must not
    startswith the new prefix, so it is a baseline too, not a spurious DEFINITION CHANGED row."""
    assert publish.DEFINITION_HASH_PREFIX == "v9"
    old = {"task_full_name": "\\T", "task_name": "T", "definition_hash": "v3" + "a" * 62,
          "last_run_failed": 0, "is_overdue": 0, "config_warnings": ""}
    new = dict(old, definition_hash=publish.compute_definition_hash(_task()))
    assert publish.detect_task_changes({"\\T": old}, [new], "H", "2026-09-25T00:00:00+00:00") == []


def test_a_schedule_change_is_detected_end_to_end_and_shown_plainly(tmp_path):
    first = publish.collect_everything({}, _fixture(tmp_path, [_task()], name="a.json"))
    moved = [_task(triggers=[_trigger(start="2024-01-05T03:30:00")])]
    second = publish.collect_everything({}, _fixture(tmp_path, moved, name="b.json"))
    stored = {r["task_full_name"]: r for r in first["inventory"]}
    changes = publish.detect_task_changes(stored, second["inventory"], "H", "2026-09-25T00:00:00+00:00")

    assert [c["change_kind"] for c in changes] == ["definition_changed"]
    # collector_tab-02: only the differing part shows, not the full ~260-char definition twice.
    # collector_tab-04 / wording-06: "every day" is dropped for a plain daily (interval 1) trigger.
    assert changes[0]["old_summary"] == "Schedule: Daily at 2:00 AM (since Jan 5, 2024)"
    assert changes[0]["new_summary"] == "Schedule: Daily at 3:30 AM (since Jan 5, 2024)"
    assert "Actions:" not in changes[0]["old_summary"]
    assert second["inventory"][0]["schedule_detail"] == "Daily at 3:30 AM (since Jan 5, 2024)"
    assert "schedule_detail" in {f["name"] for f in publish.inventory_fields()}


def test_a_run_level_only_change_shows_different_summaries(tmp_path):
    """run_level is hashed; a change the summary cannot show logged identical old and new text."""
    first = publish.collect_everything({}, _fixture(tmp_path, [_task()], name="a.json"))
    second = publish.collect_everything({}, _fixture(tmp_path, [_task(run_level="Limited")], name="b.json"))
    stored = {r["task_full_name"]: r for r in first["inventory"]}
    changes = publish.detect_task_changes(stored, second["inventory"], "H", "2026-09-25T00:00:00+00:00")

    assert [c["change_kind"] for c in changes] == ["definition_changed"]
    assert changes[0]["old_summary"] == "Runs as: svc (Interactive, Highest)"
    assert changes[0]["new_summary"] == "Runs as: svc (Interactive, Limited)"
    assert "run_level" in {f["name"] for f in publish.inventory_fields()}
    assert "run_level" in publish._CHANGE_DETECTION_FIELDS


def test_ignorenew_with_a_limit_longer_than_the_repetition_interval_warns():
    warnings = collect.check_silent_skip_settings("IgnoreNew", "PT72H", True, "Weekly", 3600.0)
    assert len(warnings) == 1 and "(3 days)" in warnings[0] and "(1 h)" in warnings[0]
    assert collect.check_silent_skip_settings("IgnoreNew", "PT45M", True, "Weekly", 3600.0) == []
    assert collect.check_silent_skip_settings("Parallel", "PT72H", True, "Weekly", 3600.0) == []
    # A daily recurrence alone is not a repetition interval: PT72H is Windows' default limit.
    assert collect.check_silent_skip_settings("IgnoreNew", "PT72H", True, "Daily", None) == []
    # PT0S keeps its own, stronger warning and is not reported twice.
    assert len(collect.check_silent_skip_settings("IgnoreNew", "PT0S", True, "Weekly", 3600.0)) == 1


def test_the_repetition_check_reads_enabled_triggers_only():
    raw = _task(multiple_instances_policy="IgnoreNew", execution_time_limit="PT72H",
                start_when_available=True,
                triggers=[_trigger(repetition_interval="PT1H", enabled=False),
                          _trigger(repetition_interval="PT4H")])
    warnings = collect.build_config_warnings(raw, "Daily", exists=lambda p: True)
    assert "repetition interval (4 h)" in warnings


# ==================================================================== 5e. acknowledged config notes

def test_an_acknowledged_config_note_is_dropped_and_others_are_kept():
    raw = _task(multiple_instances_policy="IgnoreNew", execution_time_limit="PT0S",
                start_when_available=False)
    both = collect.build_config_warnings(raw, "Weekly", exists=lambda p: True)
    assert "StartWhenAvailable" in both and "PT0S" in both
    kept = collect.build_config_warnings(raw, "Weekly", exists=lambda p: True,
                                         acknowledged=["startwhenavailable"])
    assert "StartWhenAvailable" not in kept and "PT0S" in kept


def test_acknowledgements_load_from_workflow_descriptions(tmp_path):
    path = tmp_path / "wd.json"
    path.write_text(json.dumps({"tasks": {
        "Server Restart Window": {"acknowledge_config_warnings": ["StartWhenAvailable"]},
        "Plain": "A description"}}), encoding="utf-8")
    loaded = collect.load_descriptions(path)
    assert loaded["server restart window"]["acknowledge_config_warnings"] == ["StartWhenAvailable"]
    assert loaded["plain"]["acknowledge_config_warnings"] == []
    rows = collect.build_inventory_rows(
        [_task("Server Restart Window", start_when_available=False)], host_name="H", descriptions=loaded,
        audit_dt=NOW, exists=lambda p: True)
    assert "StartWhenAvailable" not in rows[0]["config_warnings"]


# ==================================================================== 2. fleet-wide success rate

def _counts(runs, fails, host="B", collected=NOW):
    return {"host_name": host, "runs_last_7_days": runs, "failures_last_7_days": fails,
            "collected_at_utc": common.to_epoch_millis(collected)}


def test_one_host_keeps_exactly_its_old_value():
    inventory = [{"runs_last_7_days": 10, "failures_last_7_days": 1},
                 {"runs_last_7_days": 0, "failures_last_7_days": 0}]
    assert publish.stamp_success_rate(inventory) == 90.0
    assert {t["success_rate_7d"] for t in inventory} == {90.0}


def test_every_host_stamps_the_same_fleet_value():
    """MAX(success_rate_7d) used to pick the healthiest host; now each host adds the others'."""
    a_rows = [{"runs_last_7_days": 100, "failures_last_7_days": 0}]
    b_rows = [{"runs_last_7_days": 10, "failures_last_7_days": 5}]
    from_a = publish.stamp_success_rate([dict(r) for r in a_rows], [_counts(10, 5)])
    from_b = publish.stamp_success_rate([dict(r) for r in b_rows], [_counts(100, 0, host="A")])
    # Mean of the two tasks' own ratios (1.0, 0.5), not run-weighted (indicators-01).
    assert from_a == from_b == 75.0


def test_success_rate_is_the_mean_of_per_task_ratios_not_run_weighted():
    """A 15-min task with hundreds of runs must not swamp a daily task that fails every night."""
    inventory = [{"runs_last_7_days": 343, "failures_last_7_days": 0},
                 {"runs_last_7_days": 7, "failures_last_7_days": 7}]
    assert publish.stamp_success_rate(inventory) == 50.0


def test_lowest_success_rate_is_the_worst_task_not_the_mean():
    """indicators-success-rate-ok-hides-chronic-failure: the mean can move by only a few points
    per task, so a task that fails every run still needs its own rate visible."""
    inventory = [{"runs_last_7_days": 343, "failures_last_7_days": 3},
                 {"runs_last_7_days": 7, "failures_last_7_days": 7}]
    publish.stamp_success_rate(inventory)
    assert {t["lowest_success_rate_7d"] for t in inventory} == {0.0}


def test_lowest_success_rate_is_none_when_nothing_ran():
    inventory = [{"runs_last_7_days": 0, "failures_last_7_days": 0}]
    publish.stamp_success_rate(inventory)
    assert inventory[0]["lowest_success_rate_7d"] is None


def test_other_hosts_are_read_without_this_host_and_without_a_host_silent_for_7_days():
    class _Table:
        properties = _Props(objectIdField="OBJECTID", maxRecordCount=1000)
        rows = [_counts(5, 0, host="A"), _counts(10, 1, host="B"),
                _counts(50, 50, host="C", collected=NOW - timedelta(days=8))]

        def query(self, where, **kw):
            assert where == "host_name <> 'A'"
            return _Result([r for r in self.rows if r["host_name"] != "A"]
                           if not kw.get("result_offset") else [])

    others = publish.fetch_other_host_counts(_Table(), "A", NOW)
    assert [r["host_name"] for r in others] == ["B"]


def test_publish_stamps_the_fleet_value_on_the_rows_it_writes(monkeypatch):
    written = {}
    monkeypatch.setattr(publish, "get_tables", lambda item: {
        publish.TABLE_INVENTORY: "inv", publish.TABLE_RUNS: "runs", publish.TABLE_METRICS: "m"})
    monkeypatch.setattr(publish, "upsert_by_key", lambda *a, **k: {"added": 0, "updated": 0})
    monkeypatch.setattr(publish, "purge_older_than", lambda *a, **k: 0)
    monkeypatch.setattr(publish, "summarize_runs_from_agol", lambda *a, **k: {
        "\\T": {"runs_last_7_days": 10, "failures_last_7_days": 0}})
    monkeypatch.setattr(publish, "fetch_other_host_counts", lambda *a, **k: [_counts(10, 10)])
    monkeypatch.setattr(publish, "sync_inventory",
                        lambda table, rows, host, **k: written.update(rows=rows) or
                        {"added": 0, "updated": 0, "deleted": 0})
    collected = {"runs": [], "metrics": [], "host_name": "A", "log_summary": {}, "audit_dt": NOW,
                 "inventory": [{"task_full_name": "\\T", "task_name": "T", "health_status": "Success",
                                "last_run_failed": 0, "needs_attention": 0}]}
    publish.publish(object(), object(), collected, {})
    assert written["rows"][0]["success_rate_7d"] == 50.0


# ==================================================================== 3. windows end at the audit time

def test_summarize_runs_windows_on_the_given_audit_time():
    capture = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)
    run = {"task_full_name": "\\T", "start_time_utc": (capture - timedelta(days=1)).isoformat(),
           "duration_seconds": 5.0, "is_error": 0, "run_status": "Success"}
    assert publish.summarize_runs([run], capture)["\\T"]["runs_last_7_days"] == 1
    assert publish.summarize_runs([run])["\\T"]["runs_last_7_days"] == 0  # real clock: long gone


def test_a_fixture_replay_counts_its_runs_at_capture_time(tmp_path):
    def event(record, event_id, when, code=None):
        return {"event_id": event_id, "event_record_id": record, "event_time_utc": when,
                "task_full_name": "\\T", "instance_id": "{1}", "result_code": code}
    events = [event(1, 100, "2026-01-09T02:00:00Z"), event(2, 201, "2026-01-09T02:05:00Z", 0),
              event(3, 102, "2026-01-09T02:05:01Z")]
    collected = publish.collect_everything({}, _fixture(tmp_path, [_task()], events))
    row = collected["inventory"][0]
    assert row["runs_last_7_days"] == 1 and row["success_rate_7d"] == 100.0
    assert collected["audit_dt"] == datetime(2026, 1, 10, 12, tzinfo=timezone.utc)


# ==================================================================== 4. --preview-alert

def _inv(**kw):
    base = {"task_name": "A", "last_run_failed": 0, "is_overdue": 0, "last_result_text": "",
            "next_run_in_hours": 5.0, "latest_log_path": "", "latest_log_name": ""}
    base.update(kw)
    return base


@pytest.fixture
def no_mail(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the preview must never read SMTP settings or send")
    monkeypatch.setattr(common, "send_email", refuse)
    monkeypatch.setattr(common, "load_mail_settings", refuse)


def _alert_setup(tmp_path):
    log = tmp_path / "a.log"
    log.write_text("step 1 ok\nconnecting with password=hunter2\nTraceback: boom\n", encoding="utf-8")
    collected = {"host_name": "H", "inventory": [
        _inv(task_name="A", last_run_failed=1, last_result_text="boom",
             latest_log_path=str(log), latest_log_name="a.log"),
        _inv(task_name="B", is_overdue=1, next_run_in_hours=-2.0)]}
    config = {"alerting": {"enabled": True, "to": ["ops@example.org"], "realert_hours": 24,
                           "maintenance_windows": [{"task": "^B$",
                                                    "start": (NOW - timedelta(hours=1)).isoformat(),
                                                    "end": (NOW + timedelta(hours=1)).isoformat()}]}}
    return collected, config


def test_preview_shows_the_new_problem_mail_with_maintenance_and_a_scrubbed_tail(tmp_path, no_mail):
    collected, config = _alert_setup(tmp_path)
    text = publish.preview_alert(collected, config, tmp_path, now=NOW).read_text(encoding="utf-8")
    assert "Decision: WOULD SEND to ops@example.org (reason 'problem')" in text
    assert "Subject: [TaskDash] 1 problem(s) on H" in text
    assert "FAILED: A -- boom" in text and "OVERDUE: B by 2.0 h" in text
    assert "in maintenance (not alerted): OVERDUE: B" in text
    assert "hunter2" not in text and "password=<redacted>" in text
    assert "FAILED: A: never alerted" in text
    assert not (tmp_path / "alert_state.json").exists(), "the preview must not create alert state"


def test_preview_respects_the_cooldown_and_leaves_alert_state_untouched(tmp_path, no_mail):
    collected, config = _alert_setup(tmp_path)
    state = tmp_path / "alert_state.json"
    state.write_text(json.dumps({"signature": ["FAILED: A"], "problem_last_alert_utc": {
        "FAILED: A": (NOW - timedelta(hours=1)).isoformat()}}), encoding="utf-8")
    before = state.read_bytes()
    text = publish.preview_alert(collected, config, tmp_path, now=NOW).read_text(encoding="utf-8")
    assert "Decision: would NOT send" in text
    assert "re-alert due 2026-09-26 11:00 UTC" in text
    assert state.read_bytes() == before


def test_preview_shows_the_heartbeat_decision(tmp_path, no_mail):
    config = {"alerting": {"enabled": True, "to": ["ops@example.org"],
                           "heartbeat_hour": NOW.astimezone().hour}}
    collected = {"host_name": "H", "inventory": [_inv()]}
    text = publish.preview_alert(collected, config, tmp_path, now=NOW).read_text(encoding="utf-8")
    assert "(reason 'heartbeat')" in text and "daily check: all 1 task healthy on H" in text


def test_preview_says_why_a_disabled_alerting_config_sends_nothing(tmp_path, no_mail):
    collected, config = _alert_setup(tmp_path)
    config["alerting"]["enabled"] = False
    text = publish.preview_alert(collected, config, tmp_path, now=NOW).read_text(encoding="utf-8")
    assert "Decision: would NOT send: alerting.enabled is false" in text


def test_preview_body_is_exactly_the_body_the_real_send_uses(tmp_path, monkeypatch):
    collected, config = _alert_setup(tmp_path)
    sent = {}
    monkeypatch.setattr(publish, "utc_now", lambda: NOW)
    monkeypatch.setattr(common, "load_mail_settings", lambda cfg: {"smtp server": "x", "from address": "a@b.c"})
    monkeypatch.setattr(common, "send_email",
                        lambda mail, to, subject, body: sent.update(subject=subject, body=body) or True)
    preview_dir = tmp_path / "p"
    preview_dir.mkdir()
    preview = publish.preview_alert(collected, config, preview_dir, now=NOW)
    assert publish.maybe_send_alert(collected, config, tmp_path)
    text = preview.read_text(encoding="utf-8")
    assert f"Subject: {sent['subject']}\n\n{sent['body']}" in text


def test_preview_refuses_write_flags():
    for extra in (["--apply"], ["--migrate"], ["--target", "test"], ["--capture-fixture", "x.json"]):
        with pytest.raises(SystemExit):
            publish.parse_args(["--preview-alert", *extra])


def test_preview_runs_end_to_end_from_a_fixture_without_sending_or_pinging(tmp_path, monkeypatch, no_mail):
    pings = []
    monkeypatch.setattr(publish, "ping_healthcheck", lambda cfg, suffix: pings.append(suffix))
    config = {"logging": {"log_dir": str(tmp_path / "Logs")}, "rules": {"profiles": list(REFERENCE_PROFILES)},
              "alerting": {"enabled": True, "to": ["ops@example.org"],
                           "healthcheck_url": "https://hc.example/x"}}
    config_path = tmp_path / "c.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    fixture = _fixture(tmp_path, [_task(last_task_result=1, next_run_time="2026-01-11T02:00:00Z")])
    assert publish.main(["--config", str(config_path), "--preview-alert", "--fixture", str(fixture)]) == 0
    previews = list((tmp_path / "Logs").glob("alert_preview_*.txt"))
    assert len(previews) == 1 and "FAILED: T" in previews[0].read_text(encoding="utf-8")
    assert pings == []


# ==================================================================== 5b. resent deletes

class _DeleteTable:
    properties = _Props(objectIdField="OBJECTID", maxRecordCount=1000)

    def __init__(self, oids, commit_first):
        self.rows, self.commit_first, self.calls = set(oids), commit_first, []

    def edit_features(self, deletes=None, **kw):
        oids = [int(o) for o in deletes.split(",")]
        self.calls.append(oids)
        if len(self.calls) == 1:
            if self.commit_first:
                self.rows -= set(oids)  # the delete landed, then the response was lost
            raise Exception("(Error Code: 504)")
        missing = [o for o in oids if o not in self.rows]
        self.rows -= set(oids)
        return {"deleteResults": [
            {"objectId": o, "success": o not in missing,
             **({"error": {"description": "Delete for the object was not attempted. "
                                          "Object may not exist."}} if o in missing else {})}
            for o in oids]}

    def query(self, where, **kw):
        wanted = {int(x) for x in re.findall(r"\d+", where.split("IN", 1)[1])}
        return _Result([{"OBJECTID": o} for o in sorted(self.rows & wanted)]
                       if not kw.get("result_offset") else [])


def test_a_delete_that_landed_before_an_ambiguous_failure_is_not_resent(monkeypatch):
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    table = _DeleteTable([1, 2, 3], commit_first=True)
    counts = common.apply_edits_chunked(table, deletes=[1, 2, 3], label="T")
    assert counts["deleted"] == 3 and len(table.calls) == 1 and table.rows == set()


def test_a_delete_that_did_not_land_is_resent_in_full(monkeypatch):
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    table = _DeleteTable([1, 2, 3], commit_first=False)
    counts = common.apply_edits_chunked(table, deletes=[1, 2, 3], label="T")
    assert counts["deleted"] == 3 and table.calls == [[1, 2, 3], [1, 2, 3]] and table.rows == set()


def test_a_delete_resent_by_the_transport_layer_counts_rows_already_gone():
    """urllib3 resends a real HTTP 504 inside requests, so edit_features raises nothing and
    returns a 200 whose rows deleted by the lost attempt report 'Object may not exist'."""
    table = _DeleteTable([1, 2, 3], commit_first=True)
    table.calls.append([1, 2, 3])  # the lost attempt, invisible to TaskDash
    table.rows -= {1, 2}
    counts = common.apply_edits_chunked(table, deletes=[1, 2, 3], label="T")
    assert counts["deleted"] == 3 and len(table.calls) == 2 and table.rows == set()


def test_a_failed_delete_of_a_row_still_present_still_fails_the_run():
    class _Locked(_DeleteTable):
        def edit_features(self, deletes=None, **kw):
            self.calls.append(deletes)
            return {"deleteResults": [{"objectId": 2, "success": False,
                                       "error": {"description": "Row is locked."}}]}

    with pytest.raises(SystemExit, match="Row is locked"):
        common.apply_edits_chunked(_Locked([2], commit_first=False), deletes=[2], label="T")


# ==================================================================== 5c. healthcheck scope

@pytest.mark.parametrize("argv,expected", [
    ([], []), (["--apply", "--target", "test"], []), (["--migrate"], []),
    (["--migrate", "--apply"], []), (["--capture-fixture", "x.json"], []),
    (["--fixture", "f.json", "--apply"], []), (["--preview-alert"], []),
    (["--apply"], ["/start", ""]),
])
def test_only_the_prod_apply_publish_pings_the_healthcheck(tmp_path, monkeypatch, argv, expected):
    config_path = tmp_path / "c.json"
    config_path.write_text(json.dumps({"logging": {"log_dir": str(tmp_path)},
                                       "rules": {"profiles": list(REFERENCE_PROFILES)},
                                       "alerting": {"healthcheck_url": "https://hc.example/x"}}),
                           encoding="utf-8")
    monkeypatch.setattr(publish, "_main_dispatch", lambda args, cfg, log_dir: 0)
    pings = []
    monkeypatch.setattr(publish, "ping_healthcheck", lambda cfg, suffix: pings.append(suffix))
    publish.main(["--config", str(config_path), *argv])
    assert pings == expected


# ==================================================================== 5a. per-run bootstrap log

PRO_PYTHON = Path(r"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe")


@pytest.mark.skipif(sys.platform != "win32" or not PRO_PYTHON.exists(),
                    reason="run_taskdash.cmd needs cmd.exe and ArcGIS Pro's python")
def test_overlapping_wrappers_both_run_and_a_failure_keeps_its_output(tmp_path):
    """cmd opens a redirect target exclusively: with one shared bootstrap.log the second
    instance printed 'being used by another process', never ran python, and exited 0."""
    shutil.copy(HERE / "run_taskdash.cmd", tmp_path)
    (tmp_path / "taskdash_publish.py").write_text(
        "import sys, time\nprint('stub', sys.argv[1:], flush=True)\n"
        "time.sleep(float(sys.argv[1]))\nsys.exit(int(sys.argv[2]))\n", encoding="utf-8")
    wrapper = str(tmp_path / "run_taskdash.cmd")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    first = subprocess.Popen(["cmd", "/c", wrapper, "4", "0"], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, env=env)
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", "Start-Sleep -Seconds 1.5"], check=True)
        second = subprocess.run(["cmd", "/c", wrapper, "0", "0"], capture_output=True, env=env)
    finally:
        first_out = first.communicate(timeout=60)[0]
    assert first.returncode == 0 and second.returncode == 0
    assert b"another process" not in first_out + second.stdout + second.stderr
    logs = tmp_path / "Logs"
    assert "stub ['4', '0']" in (logs / "bootstrap.log").read_text(encoding="utf-8")

    failed = subprocess.run(["cmd", "/c", wrapper, "0", "3"], capture_output=True, env=env)
    assert failed.returncode == 3
    kept = list(logs.glob("bootstrap_FAIL_*.log"))
    assert len(kept) == 1 and "stub ['0', '3']" in kept[0].read_text(encoding="utf-8")
    assert not [p for p in logs.glob("bootstrap_*.log") if "FAIL" not in p.name]


@pytest.mark.skipif(sys.platform != "win32" or not PRO_PYTHON.exists(),
                    reason="run_taskdash.cmd needs cmd.exe and ArcGIS Pro's python")
def test_wrappers_started_in_the_same_second_get_distinct_bootstrap_logs(tmp_path):
    """cmd seeds %RANDOM% from the clock in whole seconds, so a stamp+%RANDOM% name collided
    for two wrappers launched together and the second never ran python."""
    shutil.copy(HERE / "run_taskdash.cmd", tmp_path)
    (tmp_path / "taskdash_publish.py").write_text(
        "import sys, time\nprint('stub', sys.argv[1:], flush=True)\ntime.sleep(3)\nsys.exit(5)\n",
        encoding="utf-8")
    wrapper = str(tmp_path / "run_taskdash.cmd")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    procs = [subprocess.Popen(["cmd", "/c", wrapper, str(n)], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, env=env) for n in range(3)]
    outs = [p.communicate(timeout=60)[0] for p in procs]
    assert [p.returncode for p in procs] == [5, 5, 5]
    assert not any(b"another process" in out for out in outs)
    kept = sorted((tmp_path / "Logs").glob("bootstrap_FAIL_*.log"))
    assert len(kept) == 3
    assert sorted(p.read_text(encoding="utf-8").split("'")[1] for p in kept) == ["0", "1", "2"]


# ==================================================================== 6. round 3: settings hash/summary

def test_describe_task_settings_reads_the_three_settings_tab_values():
    raw = {"multiple_instances_policy": "IgnoreNew", "execution_time_limit": "PT3H",
          "start_when_available": True}
    assert collect.describe_task_settings(raw) == \
        "If already running: Do not start a new instance | Time limit: 3 h | Catch-up: on"
    assert collect.describe_task_settings({}) == \
        "If already running: unknown | Time limit: none | Catch-up: off"


def test_a_settings_only_edit_changes_the_definition_hash_and_shows_in_the_summary(tmp_path):
    """collector_tab-03: MultipleInstancesPolicy/ExecutionTimeLimit/StartWhenAvailable used to
    move neither the hash nor the change summary unless they also flipped an existing
    config-warning note."""
    before = publish.compute_definition_hash(_task())
    after = publish.compute_definition_hash(_task(multiple_instances_policy="IgnoreNew"))
    assert before != after

    first = publish.collect_everything({}, _fixture(tmp_path, [_task()], name="a.json"))
    moved = [_task(execution_time_limit="PT3H")]
    second = publish.collect_everything({}, _fixture(tmp_path, moved, name="b.json"))
    stored = {r["task_full_name"]: r for r in first["inventory"]}
    changes = publish.detect_task_changes(stored, second["inventory"], "H", "2026-09-25T00:00:00+00:00")
    assert [c["change_kind"] for c in changes] == ["definition_changed"]
    # Each Settings-tab value is its own part now, so a one-setting edit shows only that setting,
    # not the other two unchanged ones alongside it (collector_tab-change-summary-separator-and
    # -jargon).
    assert changes[0]["old_summary"] == "Time limit: none"
    assert changes[0]["new_summary"] == "Time limit: 3 h"


def test_became_failing_and_recovered_rows_carry_the_reason():
    """collector_tab-05: these rows used to log an empty detail line."""
    base = {"task_full_name": "\\T", "task_name": "T",
           "definition_hash": publish.DEFINITION_HASH_PREFIX + "x" * 62,
           "last_run_failed": 0, "is_overdue": 0, "config_warnings": ""}
    failing_new = dict(base, last_run_failed=1, attention_reason="Script exited with code 1.")
    changes = publish.detect_task_changes({"\\T": base}, [failing_new], "H", "2026-09-25T00:00:00+00:00")
    assert next(c for c in changes if c["change_kind"] == "became_failing")["new_summary"] == \
        "Script exited with code 1."

    recovered_old = dict(base, last_run_failed=1, attention_reason="Script exited with code 1.")
    recovered_new = dict(base, last_run_failed=0, attention_reason="")
    changes = publish.detect_task_changes({"\\T": recovered_old}, [recovered_new], "H",
                                          "2026-09-25T00:00:00+00:00")
    # Stored bare; the dashboard adds "Was: " (a stored prefix rendered "Was: Was:").
    assert next(c for c in changes if c["change_kind"] == "recovered")["old_summary"] == \
        "Script exited with code 1."


def test_the_ignorenew_notes_name_the_dropdown_not_a_checkbox():
    """wording-07: "turn off ..." implied a checkbox; MultipleInstancesPolicy is a dropdown."""
    warnings = collect.check_silent_skip_settings("IgnoreNew", "PT0S", True, "Daily")
    assert any('"If the task is already running"' in w and "IgnoreNew" in w for w in warnings)
    assert not any("turn off" in w for w in warnings)
