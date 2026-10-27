"""Regression tests for the 3.5-3.7 hardening pass on taskdash_publish.py / taskdash_common.py:
the median/p95/p5 duration baseline plus stuck/blocked warnings (3.5), the CollectorRuns audit
table (3.6), and the TaskChanges state-transition table (3.7), plus the schema/--migrate/
check_schema_drift plumbing and config keys all three need.

Each test targets one behaviour and fails against the pre-hardening implementation. No network
writes, no ArcGIS Online calls -- fakes throughout. Matches test_taskdash.py /
test_writer_hardening.py style.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import taskdash_common as common
import taskdash_publish as publish
from conftest import REFERENCE_PROFILES

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def frozen_now(monkeypatch):
    monkeypatch.setattr(publish, "utc_now", lambda: NOW)


def _run(task="\\T", days_ago=0, hours_ago=None, duration=10.0, status=None,
        estimated=False, is_error=0):
    delta = timedelta(hours=hours_ago) if hours_ago is not None else timedelta(days=days_ago)
    row = {"task_full_name": task, "duration_seconds": duration, "is_error": is_error,
          "start_time_utc": (NOW - delta).isoformat()}
    if status:
        row["run_status"] = status
    if estimated:
        row["start_time_estimated"] = 1
    return row


# ==================================================================== 3.5 duration baseline

def test_percentile_of_identical_values_is_that_value():
    assert publish._percentile([10.0, 10.0, 10.0], 95) == 10.0


def test_percentile_interpolates_between_two_points():
    assert publish._percentile([0.0, 100.0], 50) == pytest.approx(50.0)


def test_summarize_runs_computes_30_day_baseline_stats(frozen_now):
    rows = [_run(days_ago=d, duration=v) for d, v in
           [(1, 10.0), (2, 10.0), (3, 10.0), (4, 10.0), (5, 100.0)]]
    summary = publish.summarize_runs(rows)["\\T"]
    assert summary["baseline_run_count"] == 5
    assert summary["baseline_median_seconds"] == pytest.approx(10.0)
    assert summary["baseline_p95_seconds"] > 10.0


def test_summarize_runs_excludes_estimated_and_out_of_window_runs_from_the_baseline(frozen_now):
    rows = [_run(days_ago=1, duration=10.0), _run(days_ago=2, duration=10.0),
           _run(days_ago=3, duration=10.0), _run(days_ago=4, duration=10.0, estimated=True),
           _run(days_ago=40, duration=10.0)]
    summary = publish.summarize_runs(rows)["\\T"]
    assert summary["baseline_run_count"] == 3, \
        "the estimated-start run and the 40-day-old run must not count toward the baseline"


def test_summarize_runs_counts_skipped_already_running_in_the_last_24h():
    rows = [_run(hours_ago=1, duration=None, status="Skipped-AlreadyRunning"),
           _run(hours_ago=5, duration=None, status="Skipped-AlreadyRunning"),
           _run(days_ago=2, duration=None, status="Skipped-AlreadyRunning")]
    summary = publish.summarize_runs(rows)["\\T"]
    assert summary["skipped_already_running_24h"] == 2


def test_summarize_runs_counts_skipped_and_missed_in_the_last_7_days(frozen_now):
    """skipped_last_7_days covers every NON_RUN_STATUSES row (Missed and Skipped-AlreadyRunning),
    not just the 24h Skipped-AlreadyRunning count used for blocked detection."""
    rows = [_run(hours_ago=1, duration=None, status="Skipped-AlreadyRunning"),
           _run(days_ago=3, duration=None, status="Missed"),
           _run(days_ago=10, duration=None, status="Skipped-AlreadyRunning"),
           _run(days_ago=1, duration=5.0, status="Success")]
    summary = publish.summarize_runs(rows)["\\T"]
    assert summary["skipped_last_7_days"] == 2
    assert summary["runs_last_7_days"] == 1  # the Success row only: Missed/Skipped are not runs

    task = {"task_full_name": "\\T", "task_name": "T", "health_status": "Success",
           "needs_attention": 0, "last_run_failed": 0}
    publish.enrich_inventory([task], {"\\T": summary}, {})
    assert task["skipped_last_7_days"] == 2
    assert "skipped_last_7_days" in {f["name"] for f in publish.inventory_fields()}


def test_summarize_runs_tracks_the_newest_still_running_start():
    """Not the earliest: a killed run from before collect-07 (2026-09-25) can sit "Running" in
    TaskRuns history for up to 90 days, and the earliest such row would make an otherwise-healthy
    task look like it has been running ever since."""
    rows = [_run(hours_ago=1, duration=None, status="Running"),
           _run(hours_ago=3, duration=None, status="Running")]
    summary = publish.summarize_runs(rows)["\\T"]
    assert summary["running_since"] == (NOW - timedelta(hours=1)).isoformat()


def test_duration_anomaly_gates_on_baseline_percentiles_not_just_the_ratio():
    """Above 2x the median is not enough on its own -- it must also clear p95 (or, for the low
    side, clear under p5). A ratio inside the percentile band must not fire."""
    row = {"task_full_name": "\\T", "task_name": "T", "health_status": "Success",
          "needs_attention": 0, "attention_reason": "", "last_run_time": None, "last_run_failed": 0}
    stats = {"last_duration_seconds": 90.0, "baseline_median_seconds": 40.0,
             "baseline_p95_seconds": 95.0, "baseline_p5_seconds": 10.0, "baseline_run_count": 5}
    publish.enrich_inventory([row], {"\\T": stats}, {})
    assert row["is_duration_anomaly"] == 0, "90 > 2x40 but 90 is still under p95 (95)"


# ==================================================================== 3.5 stuck detection

def test_stuck_run_sets_warning_with_the_expected_reason(frozen_now):
    baseline = [_run(days_ago=d, duration=600.0) for d in range(1, 6)]  # usual run: 10 minutes
    running = _run(hours_ago=2, duration=None, status="Running")
    stats = publish.summarize_runs(baseline + [running])["\\T"]
    task = {"task_full_name": "\\T", "health_status": "Success", "needs_attention": 0,
           "state": "Running"}
    warning = publish.apply_stuck_or_blocked_warning(task, stats, None)
    assert task["health_status"] == "Warning"
    assert task["needs_attention"] == 1
    assert "running 2.0 h" in warning
    assert "usually about 10 min" in warning


def test_stuck_run_does_not_fire_under_the_max_3x_p95_or_30min_threshold(frozen_now):
    baseline = [_run(days_ago=d, duration=600.0) for d in range(1, 6)]
    running = _run(hours_ago=0.1, duration=None, status="Running")  # 6 minutes in
    stats = publish.summarize_runs(baseline + [running])["\\T"]
    task = {"task_full_name": "\\T", "health_status": "Success", "needs_attention": 0,
           "state": "Running"}
    assert publish.apply_stuck_or_blocked_warning(task, stats, None) == ""
    assert task["health_status"] == "Success"


def test_stuck_run_can_fire_from_execution_time_limit_alone(frozen_now):
    """No baseline yet (a brand-new task) and under the 30-minute default floor, but past its
    own (short) ExecutionTimeLimit."""
    running = _run(hours_ago=0.2, duration=None, status="Running")
    stats = publish.summarize_runs([running])["\\T"]
    task = {"task_full_name": "\\T", "health_status": "Success", "needs_attention": 0,
           "state": "Running"}
    warning = publish.apply_stuck_or_blocked_warning(task, stats, 600.0)  # 10-minute limit
    assert "running 12 min" in warning


def test_stuck_run_never_fires_when_the_live_task_is_not_running(frozen_now):
    """A TaskRuns row can say "Running" for up to 90 days after a pre-collect-07 kill left it
    stuck (taskdash_collect.py's collect-07 note); if Get-ScheduledTask no longer agrees the task
    is live-Running, that stale history must never flag an otherwise-healthy task."""
    baseline = [_run(days_ago=d, duration=600.0) for d in range(1, 6)]
    orphaned_running = _run(hours_ago=800, duration=None, status="Running")  # ~33 days stuck
    later_completed = [_run(hours_ago=h, duration=600.0) for h in (1, 2, 3)]
    stats = publish.summarize_runs(baseline + [orphaned_running] + later_completed)["\\T"]
    task = {"task_full_name": "\\T", "health_status": "Success", "needs_attention": 0,
           "state": "Ready"}
    assert publish.apply_stuck_or_blocked_warning(task, stats, None) == ""
    assert task["health_status"] == "Success"


def test_stuck_run_ignores_a_pt0s_execution_time_limit(frozen_now):
    """ExecutionTimeLimit PT0S means "never times out" (see check_silent_skip_settings), not
    "always past its limit" -- parse_iso8601_duration_seconds returns 0.0 for it, and 0.0 must
    not be treated as a real limit here."""
    running = _run(hours_ago=0.01, duration=None, status="Running")  # 36 seconds in
    stats = publish.summarize_runs([running])["\\T"]
    task = {"task_full_name": "\\T", "health_status": "Success", "needs_attention": 0,
           "state": "Running"}
    assert publish.apply_stuck_or_blocked_warning(task, stats, 0.0) == ""


def test_stuck_never_escalates_over_an_existing_error(frozen_now):
    baseline = [_run(days_ago=d, duration=600.0) for d in range(1, 6)]
    running = _run(hours_ago=5, duration=None, status="Running")
    stats = publish.summarize_runs(baseline + [running])["\\T"]
    task = {"task_full_name": "\\T", "health_status": "Error", "needs_attention": 1,
           "state": "Running"}
    assert publish.apply_stuck_or_blocked_warning(task, stats, None) == ""
    assert task["health_status"] == "Error"


def test_enrich_inventory_wires_execution_time_limits_into_stuck_detection(frozen_now):
    """End-to-end through enrich_inventory, not just the helper directly -- proves the
    execution_time_limits parameter is actually threaded and keyed by task_full_name."""
    row = {"task_full_name": "\\T", "task_name": "T", "health_status": "Success",
          "needs_attention": 0, "attention_reason": "", "last_run_time": None, "last_run_failed": 0,
          "state": "Running"}
    running = _run(hours_ago=0.2, duration=None, status="Running")
    stats = publish.summarize_runs([running])
    publish.enrich_inventory([row], stats, {}, {"\\T": 600.0})
    assert row["health_status"] == "Warning"
    assert row["needs_attention"] == 1
    assert "running 12 min" in row["attention_reason"]


# ==================================================================== 3.5 blocked detection

def test_blocked_task_sets_warning(frozen_now):
    rows = [_run(hours_ago=1, duration=None, status="Skipped-AlreadyRunning"),
           _run(hours_ago=2, duration=None, status="Skipped-AlreadyRunning")]
    stats = publish.summarize_runs(rows)["\\T"]
    task = {"task_full_name": "\\T", "health_status": "Success", "needs_attention": 0}
    warning = publish.apply_stuck_or_blocked_warning(task, stats, None)
    assert task["health_status"] == "Warning"
    assert warning == "Blocked: new runs skipped because an earlier run is still going"


def test_a_single_skipped_run_does_not_yet_count_as_blocked(frozen_now):
    rows = [_run(hours_ago=1, duration=None, status="Skipped-AlreadyRunning")]
    stats = publish.summarize_runs(rows)["\\T"]
    task = {"task_full_name": "\\T", "health_status": "Success", "needs_attention": 0}
    assert publish.apply_stuck_or_blocked_warning(task, stats, None) == ""


# ==================================================================== schema: new fields/tables

def test_inventory_fields_include_the_new_columns():
    names = {f["name"] for f in publish.inventory_fields()}
    assert {"config_warnings", "logon_type", "definition_hash"} <= names


def test_collector_runs_fields_match_the_spec():
    names = {f["name"] for f in publish.collector_runs_fields()}
    assert names == {"OBJECTID", "run_id", "host_name", "started_utc", "ended_utc",
                     "duration_seconds", "exit_status", "error_text", "tasks_seen",
                     "events_read", "watermark_from", "watermark_to", "runs_written",
                     "inventory_written", "metrics_written", "alert_sent",
                     "taskdash_version", "schema_version"}


def test_task_changes_fields_match_the_spec():
    names = {f["name"] for f in publish.task_changes_fields()}
    assert names == {"OBJECTID", "host_name", "task_full_name", "task_name", "change_kind",
                     "changed_utc", "old_summary", "new_summary"}


def test_table_indexes_declare_the_new_unique_keys():
    collector_idx = next(s for s in publish.TABLE_INDEXES[publish.TABLE_COLLECTOR_RUNS] if s["unique"])
    assert collector_idx["fields"] == ["run_id"]
    changes_idx = next(s for s in publish.TABLE_INDEXES[publish.TABLE_TASK_CHANGES] if s["unique"])
    assert changes_idx["fields"] == ["host_name", "task_full_name", "change_kind", "changed_utc"]


# ==================================================================== --migrate can create a table

class _FieldProp:
    def __init__(self, name, type_):
        self.name = name
        self.type = type_


class _Props(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


class _FakeTableManager:
    def add_to_definition(self, definition):
        return {"success": True}


class _SchemaTable:
    def __init__(self, field_names, indexes=()):
        self.properties = _Props(
            fields=[_FieldProp(n, "esriFieldTypeString") for n in field_names],
            indexes=[{"fields": ",".join(f), "isUnique": True} for f in indexes],
            objectIdField="OBJECTID",
        )
        self.manager = _FakeTableManager()


def _complete(fields_fn, indexes):
    return _SchemaTable([f["name"] for f in fields_fn()], indexes=indexes)


def test_describe_migration_reports_a_missing_table_without_writing(monkeypatch):
    messages = []
    monkeypatch.setattr(publish, "emit", lambda msg: messages.append(msg))
    changed = publish.describe_migration({})
    assert changed is True
    assert any("CollectorRuns" in m and "create" in m for m in messages)


def test_check_schema_drift_warns_on_a_missing_table_but_does_not_raise(monkeypatch):
    messages = []
    monkeypatch.setattr(publish, "emit", lambda msg: messages.append(msg))
    publish.check_schema_drift({})  # nothing present at all -- must not raise
    assert any("CollectorRuns" in m for m in messages)


def test_apply_migration_without_item_still_skips_a_missing_table():
    """Old call sites that hand a partial tables dict, scoped to one table on purpose, must keep
    working unchanged: a table absent from `tables` is silently skipped when no item is given."""
    table = _complete(publish.runs_fields, [("run_key",)])
    publish.apply_migration({publish.TABLE_RUNS: table})  # must not raise, must not need item


def test_apply_migration_creates_a_missing_table_when_item_is_given(monkeypatch):
    # Same order as production (connect_agol): without the guard, importing arcgis.features
    # probes arcpy, which fails with "The Product License has not been initialized" in any
    # session that is not the licensed interactive one (a WinRM run on TASKHOST-1).
    common.disable_arcpy_import()
    import arcgis.features as af

    created = []

    class _FakeManager:
        @staticmethod
        def add_to_definition(defn):
            created.append(defn)
            return {"success": True}

    class _FakeFLC:
        manager = _FakeManager()

    monkeypatch.setattr(af.FeatureLayerCollection, "fromitem", staticmethod(lambda item: _FakeFLC()))

    complete = {
        publish.TABLE_INVENTORY: _complete(publish.inventory_fields, [("host_name", "task_full_name")]),
        publish.TABLE_RUNS: _complete(publish.runs_fields, [("run_key",)]),
        publish.TABLE_METRICS: _complete(publish.metrics_fields,
                                         [("host_name", "task_full_name", "metric_key", "log_file_name")]),
        publish.TABLE_TASK_CHANGES: _complete(publish.task_changes_fields,
                                              [("host_name", "task_full_name", "change_kind", "changed_utc")]),
    }
    collector_table = _complete(publish.collector_runs_fields, [("run_id",)])
    metric_change_table = _complete(publish.metric_change_fields, [("series_key",)])
    monkeypatch.setattr(publish, "get_tables",
                        lambda item: {**complete, publish.TABLE_COLLECTOR_RUNS: collector_table,
                                      publish.TABLE_METRIC_CHANGE: metric_change_table})

    item = _StampItem()
    publish.apply_migration(dict(complete), item=item)

    assert created and created[0]["tables"][0]["name"] == publish.TABLE_COLLECTOR_RUNS
    assert json.loads(item.updates[0]["properties"])["taskdash_schema_version"] == publish.SCHEMA_VERSION


class _StampItem:
    id = "SVC"

    def __init__(self, properties=None):
        self.properties = properties
        self.updates = []

    def update(self, item_properties=None):
        self.updates.append(item_properties)
        return True


def test_stamp_keeps_other_properties_and_records_both_versions():
    item = _StampItem({"other": 1})
    publish.stamp_service_version(item)
    stamped = json.loads(item.updates[0]["properties"])
    assert stamped == {"other": 1, "taskdash_version": publish.TASKDASH_VERSION,
                       "taskdash_schema_version": publish.SCHEMA_VERSION}


def test_stamp_refuses_a_service_migrated_by_a_newer_schema():
    item = _StampItem({"taskdash_schema_version": publish.SCHEMA_VERSION + 1})
    with pytest.raises(SystemExit, match="Upgrade TaskDash"):
        publish.stamp_service_version(item)
    assert item.updates == []


def test_collector_run_row_carries_both_versions():
    now = publish.utc_now()
    row = publish._collector_run_row({"host_name": "H"}, run_started=now, run_ended=now,
                                     exit_status="success", error_text="", watermark_from=0,
                                     watermark_to=0, publish_counts={}, alert_sent=False)
    assert (row["taskdash_version"], row["schema_version"]) == (publish.TASKDASH_VERSION,
                                                                 publish.SCHEMA_VERSION)


# ==================================================================== 3.7 TaskChanges detection

def test_definition_hash_is_stable_and_changes_with_the_definition():
    raw = {"task_full_name": "\\T", "actions": "run.py", "trigger_details": [{"type": "Daily"}],
          "run_as_user": "svc", "logon_type": "Interactive", "run_level": "Highest", "enabled": True}
    h1 = publish.compute_definition_hash(raw)
    assert h1 == publish.compute_definition_hash(dict(raw))
    assert publish.compute_definition_hash(dict(raw, actions="run2.py")) != h1
    assert publish.compute_definition_hash(dict(raw, enabled=False)) != h1


def test_detect_task_changes_covers_every_change_kind():
    prefix = publish.DEFINITION_HASH_PREFIX
    old_by_key = {
        "\\A": {"task_full_name": "\\A", "task_name": "A", "definition_hash": prefix + "h1",
               "last_run_failed": 0, "is_overdue": 0, "config_warnings": ""},
        "\\B": {"task_full_name": "\\B", "task_name": "B", "definition_hash": prefix + "h2",
               "last_run_failed": 1, "is_overdue": 0, "config_warnings": "stale path"},
        "\\C": {"task_full_name": "\\C", "task_name": "C", "definition_hash": "h3",
               "last_run_failed": 0, "is_overdue": 0, "config_warnings": ""},
    }
    new_rows = [
        {"task_full_name": "\\A", "task_name": "A", "definition_hash": prefix + "h1-changed",
        "last_run_failed": 1, "is_overdue": 1, "config_warnings": "new warning"},
        {"task_full_name": "\\B", "task_name": "B", "definition_hash": prefix + "h2",
        "last_run_failed": 0, "is_overdue": 0, "config_warnings": ""},
        {"task_full_name": "\\D", "task_name": "D", "definition_hash": "h4",
        "last_run_failed": 0, "is_overdue": 0, "config_warnings": ""},
    ]
    changed_utc = "2026-09-25T12:00:00+00:00"
    changes = publish.detect_task_changes(old_by_key, new_rows, "H", changed_utc)
    kinds = {(c["task_full_name"], c["change_kind"]) for c in changes}

    assert ("\\A", "definition_changed") in kinds
    assert ("\\A", "became_failing") in kinds
    assert ("\\A", "became_overdue") in kinds
    assert ("\\A", "config_warning_added") in kinds
    assert ("\\B", "recovered") in kinds
    assert ("\\B", "config_warning_cleared") in kinds
    assert ("\\C", "task_removed") in kinds
    assert ("\\D", "task_added") in kinds
    assert all(c["host_name"] == "H" and c["changed_utc"] == changed_utc for c in changes)


def test_detect_task_changes_is_quiet_when_nothing_changed():
    # A v2-prefixed hash, so the equality comparison really runs: an unprefixed one is a baseline.
    row = {"task_full_name": "\\A", "task_name": "A",
          "definition_hash": publish.DEFINITION_HASH_PREFIX + "a" * 62,
          "last_run_failed": 0, "is_overdue": 0, "config_warnings": ""}
    assert publish.detect_task_changes({"\\A": row}, [dict(row)], "H", "2026-09-25T12:00:00+00:00") == []


def test_definition_summary_uses_runs_as_and_expands_wd_for_display_only():
    # wording-12: "Runs as:" matches the Task Detail card's label, and "(wd=...)" (the raw
    # PowerShell action token) reads as "(Working folder: ...)" -- display text only, the hashed
    # actions string itself is untouched.
    row = {"actions": r'C:\pythonw.exe script.py (wd=C:\Jobs\)', "run_as_user": "svc",
          "logon_type": "Interactive", "enabled": 1}
    summary = publish._definition_summary(row)
    assert "Runs as: svc (Interactive)" in summary
    assert "(Working folder: C:\\Jobs\\)" in summary
    assert "(wd=" not in summary
    assert "Run as:" not in summary


def test_definition_change_summaries_falls_back_to_full_text_when_no_tracked_part_differs():
    # A synthetic case where the tracked (label, value) parts are identical even though the
    # caller believes the definition changed (e.g. a hash input outside the 4 shown parts):
    # the diff must not silently produce two empty summaries (collector_tab-02).
    old = {"actions": "run.py", "run_as_user": "svc", "logon_type": "Interactive",
          "run_level": "Highest", "enabled": 1, "schedule_detail": "Daily at 02:00"}
    new = dict(old)
    old_summary, new_summary = publish._definition_change_summaries(old, new)
    assert old_summary == new_summary == publish._definition_summary(old)
    assert "Actions:" in old_summary and "Schedule:" in old_summary


def test_detect_task_changes_is_quiet_on_a_hosts_first_ever_sync():
    # No prior inventory at all (a brand-new host) is a baseline, not 27 task_added rows for
    # tasks that have run since 2021 (collector_tab-01 / wording-15).
    new_rows = [{"task_full_name": "\\A", "task_name": "A", "definition_hash": "v2h1",
                "last_run_failed": 0, "is_overdue": 0, "config_warnings": ""}]
    assert publish.detect_task_changes({}, new_rows, "H", "2026-09-25T12:00:00+00:00") == []


def test_task_changes_write_failure_does_not_abort_metrics_or_later_purges(monkeypatch):
    """The TaskChanges write is best-effort (see the comment above it in publish()) -- a failure
    there must not skip the metrics upsert or the TaskRuns/TaskMetrics purges after it."""
    tables = {publish.TABLE_INVENTORY: "inv", publish.TABLE_RUNS: "runs",
             publish.TABLE_METRICS: "metrics", publish.TABLE_TASK_CHANGES: "changes"}
    monkeypatch.setattr(publish, "get_tables", lambda item: tables)
    monkeypatch.setattr(publish, "summarize_runs_from_agol", lambda *a, **k: {})
    monkeypatch.setattr(publish, "fetch_other_host_counts", lambda *a, **k: [])
    monkeypatch.setattr(publish, "enrich_inventory", lambda *a, **k: None)
    monkeypatch.setattr(publish, "sync_inventory",
                        lambda *a, **k: {"added": 0, "updated": 0, "deleted": 0})
    monkeypatch.setattr(publish, "_fetch_prior_inventory_for_changes", lambda *a, **k: {})
    monkeypatch.setattr(publish, "detect_task_changes", lambda *a, **k: [
        {"host_name": "H", "task_full_name": "\\T", "task_name": "T", "change_kind": "task_added",
        "changed_utc": "2026-09-25T00:00:00Z", "old_summary": "", "new_summary": ""}])

    calls, purges = [], []

    def fake_upsert(table, rows, keys, **kwargs):
        calls.append(kwargs.get("label"))
        if kwargs.get("label") == publish.TABLE_TASK_CHANGES:
            raise RuntimeError("AGOL down")
        return {"added": 0, "updated": 0}

    monkeypatch.setattr(publish, "upsert_by_key", fake_upsert)
    monkeypatch.setattr(publish, "purge_older_than",
                        lambda table, field, days, label: purges.append(label) or 0)

    collected = {
        "runs": [], "host_name": "H", "log_summary": {},
        "inventory": [{"task_full_name": "\\T"}],
        "metrics": [{"host_name": "H", "task_full_name": "\\T", "metric_key": "k",
                     "log_file_name": "f"}],
    }
    publish.publish(object(), object(), collected, {})  # must not raise

    assert publish.TABLE_METRICS in calls, "the metrics upsert must still run"
    assert purges == [publish.TABLE_RUNS, publish.TABLE_METRICS], \
        "the TaskRuns/TaskMetrics purges after the TaskChanges block must still run"


# ==================================================================== 3.6 CollectorRuns

def test_write_collector_run_row_builds_the_expected_row(monkeypatch):
    written = {}
    monkeypatch.setattr(publish, "get_tables", lambda item: {publish.TABLE_COLLECTOR_RUNS: object()})

    def fake_upsert_by_key(table, rows, key_fields, *, chunk_size, label):
        written["rows"], written["key_fields"] = rows, key_fields
        return {"added": 1, "updated": 0}

    monkeypatch.setattr(publish, "upsert_by_key", fake_upsert_by_key)
    monkeypatch.setattr(publish, "purge_older_than", lambda *a, **k: 0)

    started = NOW
    ended = NOW + timedelta(seconds=30)
    collected = {"host_name": "H", "inventory": [1, 2, 3], "raw_events": [1, 2]}
    publish._write_collector_run_row(
        object(), {}, collected, run_started=started, run_ended=ended,
        exit_status="success", error_text="", watermark_from=10.0, watermark_to=20.0,
        publish_counts={"runs_added": 2, "runs_updated": 1, "inventory_added": 1,
                        "inventory_updated": 0, "metrics_added": 0, "metrics_updated": 0},
        alert_sent=True,
    )
    row = written["rows"][0]
    assert written["key_fields"] == ["run_id"]
    assert row["host_name"] == "H"
    assert row["tasks_seen"] == 3
    assert row["events_read"] == 2
    assert row["runs_written"] == 3
    assert row["inventory_written"] == 1
    assert row["alert_sent"] == 1
    assert row["exit_status"] == "success"


def test_write_collector_run_row_skips_silently_when_the_table_is_missing(monkeypatch):
    monkeypatch.setattr(publish, "get_tables", lambda item: {})
    calls = []
    monkeypatch.setattr(publish, "upsert_by_key", lambda *a, **k: calls.append(1))
    publish._write_collector_run_row(
        object(), {}, {"host_name": "H", "inventory": [], "raw_events": []},
        run_started=NOW, run_ended=NOW, exit_status="success", error_text="",
        watermark_from=0.0, watermark_to=0.0, publish_counts={}, alert_sent=False,
    )
    assert calls == []


def test_write_collector_run_row_swallows_write_failures(monkeypatch):
    """Best-effort: a failure here must never raise into run_publish -- it would then mask the
    real publish outcome it is only supposed to record."""
    monkeypatch.setattr(publish, "get_tables", lambda item: {publish.TABLE_COLLECTOR_RUNS: object()})
    monkeypatch.setattr(publish, "upsert_by_key",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("AGOL down")))
    publish._write_collector_run_row(  # must not raise
        object(), {}, {"host_name": "H", "inventory": [], "raw_events": []},
        run_started=NOW, run_ended=NOW, exit_status="success", error_text="",
        watermark_from=0.0, watermark_to=0.0, publish_counts={}, alert_sent=False,
    )


# ==================================================================== config keys

def test_config_accepts_all_the_new_keys(tmp_path):
    config = {
        "rules": {"profiles": list(REFERENCE_PROFILES)},
        "scheduler": {"interactive_required_pattern": r"\ArcGIS\Pro"},
        "retention": {"changes_days": 180},
        "alerting": {"realert_hours": 12, "healthcheck_url": "https://hc.example/x",
                    "maintenance_windows": [{"task": "Foo", "start": "2026-01-01T00:00:00Z",
                                             "end": "2026-01-02T00:00:00Z"}]},
    }
    path = tmp_path / "c.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    loaded = common.load_config(path)  # must not raise
    assert loaded["retention"]["changes_days"] == 180


def test_config_rejects_a_non_list_maintenance_windows(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"alerting": {"maintenance_windows": "not-a-list"}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="maintenance_windows"):
        common.load_config(path)


def test_config_rejects_a_non_number_changes_days(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"retention": {"changes_days": "many"}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="changes_days"):
        common.load_config(path)


# ==================================================================== wiring: scheduler.interactive_required_pattern

def test_collect_everything_wires_interactive_required_pattern_from_config(tmp_path):
    task = {
        "task_name": "T", "task_path": "\\", "task_full_name": "\\T", "state": "Ready",
        "enabled": True, "actions": "", "primary_executable": r"C:\Tools\myrunner.exe",
        "primary_arguments": "", "trigger_types": "Daily", "run_as_user": "svc",
        "logon_type": "Password", "last_run_time": None, "next_run_time": None,
        "last_task_result": None, "number_of_missed_runs": 0,
    }
    fixture = tmp_path / "f.json"
    fixture.write_text(json.dumps({"host_name": "H", "captured_at_utc": "2026-09-25T00:00:00Z",
                                   "tasks": [task], "events": []}), encoding="utf-8")
    config = {"scheduler": {"interactive_required_pattern": r"\Tools\myrunner"}}
    collected = publish.collect_everything(config, fixture)
    assert "LogonType" in collected["inventory"][0]["config_warnings"]



def test_a_missing_stored_hash_is_a_baseline_not_a_definition_change():
    """After --migrate adds definition_hash, every stored row has an empty one."""
    old = {"host_name": "H", "task_full_name": "\\A", "task_name": "A", "definition_hash": "",
           "last_run_failed": 0, "is_overdue": 0, "config_warnings": ""}
    new = dict(old, definition_hash="abc123")
    changes = publish.detect_task_changes({old["task_full_name"]: old}, [new], "H",
                                          "2026-09-25T00:00:00+00:00")
    assert "definition_changed" not in {c["change_kind"] for c in changes}
