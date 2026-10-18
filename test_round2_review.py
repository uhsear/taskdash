"""Round-2 review fixes: the reconcile/merge order, T1 status words, X7 units, X10 help,
M3 debug-log counts, structural section keys, the Duration name, the --refresh-metrics
clean-up and the metrics clock. Fakes only: no ArcGIS Online, no PowerShell, no SMTP.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

import taskdash_common as common

common.disable_arcpy_import()

import conftest  # noqa: E402
import taskdash_collect as collect  # noqa: E402
import taskdash_dashboard as dash  # noqa: E402
import taskdash_metrics as metrics  # noqa: E402
import taskdash_publish as publish  # noqa: E402
from test_taskdash import NOW, _event, _task, iso  # noqa: E402


# ==================================================================== reconcile, then merge

class _Stop(Exception):
    pass


def _publish_runs(monkeypatch, runs, stored):
    """Run publish() up to its TaskRuns upsert and return the rows it would write."""
    sent = {}

    def capture(table, rows, keys, **kw):
        sent["rows"] = rows
        raise _Stop

    monkeypatch.setattr(publish, "get_tables", lambda item: dict.fromkeys(
        (publish.TABLE_INVENTORY, publish.TABLE_RUNS, publish.TABLE_METRICS), object()))
    monkeypatch.setattr(publish, "fetch_stored_runs", lambda table, keys: stored)
    monkeypatch.setattr(publish, "upsert_by_key", capture)
    with pytest.raises(_Stop):
        publish.publish(None, None, {"runs": runs, "inventory": [], "metrics": [], "host_name": "H"}, {})
    return sent["rows"]


def _in_flight_then_finished():
    """Pass N: 100 + a failing 201, still running. Pass N+1: a clean 201 + 102 only."""
    inventory = collect.build_inventory_rows([_task()], host_name="H", descriptions={}, audit_dt=NOW)
    first = collect.build_run_rows(
        [_event(100, 30, event_record_id=10), _event(201, 20, result_code=2147942402, event_record_id=11)],
        inventory, host_name="H")
    second = collect.build_run_rows(
        [_event(201, 5, result_code=0, event_record_id=20), _event(102, 5, event_record_id=21)],
        inventory, host_name="H")
    return first, second


def test_a_run_reconciled_at_collect_still_keeps_the_stored_failure(monkeypatch):
    first, second = _in_flight_then_finished()
    task = [{"task_full_name": second[0]["task_full_name"], "last_run_time": iso(NOW - timedelta(minutes=30))}]
    assert publish.reconcile_estimated_starts(second, task, {}) == 1  # collect side
    stored = {first[0]["run_key"]: publish.prepare_rows(first, "TaskRuns", iso(NOW))[0]}

    [row] = _publish_runs(monkeypatch, second, stored)
    assert (row["run_status"], row["is_error"], row["result_code"]) == ("Error", 1, 2147942402)
    assert row["event_count"] == 4, "the stored events are merged, not replaced"
    assert row["start_time_estimated"] == 0
    assert row["duration_seconds"] == 25 * 60
    assert "_start_from_last_run_time" not in row, "the marker never reaches the table"


def test_a_reconciled_start_beats_a_stored_estimate(monkeypatch):
    _, second = _in_flight_then_finished()
    task = [{"task_full_name": second[0]["task_full_name"], "last_run_time": iso(NOW - timedelta(minutes=30))}]
    publish.reconcile_estimated_starts(second, task, {})
    prior = {second[0]["run_key"]: {"start_time_utc": common.to_epoch_millis(NOW - timedelta(minutes=10)),
                                    "start_time_estimated": 1, "event_count": 1, "max_event_record_id": 12.0,
                                    "result_code": 2147942402.0, "run_status": "Error",
                                    "is_error": 1, "is_success": 0, "result_text": "x"}}
    assert publish.keep_stored_start(second, prior) == 1
    run = second[0]
    assert common.parse_iso_datetime(run["start_time_utc"]) == NOW - timedelta(minutes=30)
    assert (run["start_time_estimated"], run["is_error"], run["event_count"]) == (0, 1, 3)


# ==================================================================== T1

def test_failed_reason_uses_the_status_column_words():
    for status, word in publish.RUN_STATUS_WORDS.items():
        assert dash.PALETTE[status][0] == word, status


def test_failed_reason_starts_with_a_capital_and_a_plain_status():
    task = {"task_full_name": "\\T", "task_name": "T", "health_status": "Success", "last_run_failed": 0,
            "needs_attention": 0, "last_result_text": "ok", "last_run_time": None}
    stats = {"last_run_is_error": 1, "last_run_status": "StartFailed",
             "last_run_result_text": "An action failed to start.", "last_run_start": conftest.FROZEN_NOW}
    assert publish.apply_run_level_failure(task, stats) == "Last run: Did not start. An action failed to start."


# ==================================================================== X7

@pytest.mark.parametrize("seconds,text", [(45, "45 s"), (600, "10 min"), (11520, "3.2 h")])
def test_one_duration_format_everywhere(seconds, text):
    assert common.short_duration(seconds) == text
    assert publish.short_duration is common.short_duration is collect.short_duration


def test_overdue_reason_and_email_use_the_same_format():
    reason = collect.build_attention_reason("Warning", "", 0, -3.2, False)
    assert reason == "Next run is overdue by 3.2 h"
    task = {"task_name": "B", "is_overdue": 1, "needs_attention": 1, "next_run_in_hours": -3.2}
    assert publish.alert_details([task])["OVERDUE: B"] == "OVERDUE: B by 3.2 h"


# ==================================================================== X10

def test_recent_changes_help_points_at_the_filters_panel():
    # A removed task has no TaskInventory row, so the Task selector cannot find its "Task
    # removed" row (collector_tab-06); the search box is the only way to find it.
    assert dash.HELP["Recent Changes"][2].startswith("Pick a task in the Filters panel")
    assert "search box" in dash.HELP["Recent Changes"][2]


# ==================================================================== M3

# Round-6: a cumulative debug file's counts are no longer unconditionally null (M3's original
# rule). _count_recent_debug_lines now counts only the lines whose OWN leading timestamp falls
# in the last 7 days, and is (None, None) only when no line carries a parsable one
# (repeat-operations-csc-email-failures-invisible). Split in two: a real, in-window count, and
# the still-null case with no parsable timestamp.

def test_a_debug_file_counts_error_lines_from_the_last_7_days(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "utc_now", lambda: NOW)
    logs = tmp_path / "Logs"
    logs.mkdir()
    recent = (NOW - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    (logs / "deskmailer_debug.log").write_text(
        f"{recent} -- ERROR: SMTP connection/setup failed\n" * 3, encoding="utf-8")
    (tmp_path / "csc.py").write_text("", encoding="utf-8")
    _, summary = metrics.build_metric_rows(
        [{"task_name": "Service Desk Mailer", "task_full_name": "\\Service Desk Mailer",
          "command_target_path": str(tmp_path / "csc.py")}], host_name="H")
    entry = summary["\\Service Desk Mailer"]
    assert entry["latest_log_name"] == "deskmailer_debug.log"
    assert entry["latest_log_error_lines"] == 3
    assert entry["latest_log_warning_lines"] == 0
    # The dashboard renders the count, not the old unconditional "no per-run counts" line
    # (repeat-task_detail-log-name-shown-twice): checked on the Arcade source, since the value
    # itself is filled in by Dashboards at render time, not by this generator.
    expression = dash.detail_list_expression()
    assert "error-only file kept across runs; " in expression
    assert '+ plural($datapoint["latest_log_error_lines"], "error line") + " in the last 7 days"' in expression


def test_a_debug_file_with_no_parsable_timestamp_gives_no_error_count(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "utc_now", lambda: NOW)
    logs = tmp_path / "Logs"
    logs.mkdir()
    (logs / "deskmailer_debug.log").write_text(
        "ERROR: SMTP connection/setup failed\n" * 3, encoding="utf-8")
    (tmp_path / "csc.py").write_text("", encoding="utf-8")
    _, summary = metrics.build_metric_rows(
        [{"task_name": "Service Desk Mailer", "task_full_name": "\\Service Desk Mailer",
          "command_target_path": str(tmp_path / "csc.py")}], host_name="H")
    entry = summary["\\Service Desk Mailer"]
    assert entry["latest_log_error_lines"] is None and entry["latest_log_warning_lines"] is None
    assert "Error count not known" in dash.detail_list_expression()


# ==================================================================== structural section keys

def test_equal_counts_across_sections_keep_per_section_keys(tmp_path):
    log = tmp_path / "run.log"
    log.write_text(
        "2026-09-25 00:25:18,797 - INFO - Processing: Acme_StormBasins\n"
        "2026-09-25 00:26:54,188 - INFO -   Verification: 0 features in hosted layer\n"
        "2026-09-25 00:28:02,377 - INFO - Processing: Acme_StormPipe\n"
        "2026-09-25 00:31:20,582 - INFO -   Verification: 0 features in hosted layer\n", encoding="utf-8")
    keys = set(metrics.scan_log_file(log, max_lines=0)["metrics"])
    assert keys == {"acme_storm_basins_hosted_record_count", "acme_storm_pipe_hosted_record_count"}


# ==================================================================== Duration name

def test_duration_has_no_unit_but_other_seconds_keys_do():
    # metrics.py names this "Script time (seconds)" now (metrics_tab-04/wording-08): the old
    # unitless "Duration" read as raw seconds with no unit in the native Metric range table.
    assert metrics.metric_display_name("duration_seconds") == "Script-reported time (seconds)"
    assert metrics.metric_display_name("step_seconds") == "Step (seconds)"


# ==================================================================== --refresh-metrics

def _stored(oid, key, log, name=None, task="\\S"):
    return {"OBJECTID": oid, "host_name": "H", "task_full_name": task, "metric_key": key,
            "log_file_name": log, "display_name": name or metrics.metric_display_name(key),
            "series_key": metrics.metric_series_key("H", task, key)}


def test_refresh_backfills_a_missing_series_key():
    row = {**_stored(1, "exported_record_count", "new.log"), "series_key": None}
    _, updates = publish.plan_metric_refresh([row], [_fresh("exported_record_count", "new.log")], "OBJECTID")
    assert updates == [{"OBJECTID": 1, "series_key": "H|" + chr(92) + "S|exported_record_count"}]


def _fresh(key, log, task="\\S"):
    return {"host_name": "H", "task_full_name": task, "metric_key": key, "log_file_name": log}


def test_refresh_deletes_retired_keys_and_renames_old_names():
    stored = [_stored(1, "features_max_oid", "new.log"),      # retired: new.log re-mined without it
              _stored(2, "features_max_oid", "old.log"),      # same key, older than the window
              _stored(3, "exported_record_count", "new.log"),  # still produced
              _stored(4, "stopped_count", "old.log"),         # only old logs hold it: a real stop
              _stored(5, "duration_seconds", "new.log", name="Duration Seconds"),
              _stored(6, "features_max_oid", "x.log", task="\\Other")]  # another task: not re-mined
    fresh = [_fresh("exported_record_count", "new.log"), _fresh("duration_seconds", "new.log")]
    deletes, updates = publish.plan_metric_refresh(stored, fresh, "OBJECTID")
    assert sorted(deletes) == [1, 2]
    assert updates == [{"OBJECTID": 5, "display_name": "Script-reported time (seconds)"}]


def test_refresh_deletes_rows_an_examined_log_no_longer_gives_and_own_task_rows():
    stored = [_stored(1, "idle_replica_count", "dry.log"),        # examined, gives nothing now
              _stored(2, "idle_replica_count", "real.log"),       # examined, still given
              _stored(3, "idle_replica_count", "old.log"),        # not examined: kept
              _stored(4, "metric_rows", "taskdash_1.log", task="\\Task Dash")]  # own task
    fresh = [_fresh("idle_replica_count", "real.log")]
    examined = {("\\S", "dry.log"), ("\\S", "real.log")}
    deletes, _ = publish.plan_metric_refresh(stored, fresh, "OBJECTID", examined, ["\\Task Dash"])
    assert sorted(deletes) == [1, 4]


class _MetricsTable:
    properties = SimpleNamespace(objectIdField="OBJECTID", maxRecordCount=1000)

    def __init__(self, rows):
        self.rows, self.edits = rows, []

    def query(self, **kw):
        page = self.rows[kw.get("result_offset", 0):][:kw.get("result_record_count", 1000)]
        return SimpleNamespace(features=[SimpleNamespace(attributes=dict(r)) for r in page])

    def edit_features(self, **kw):
        self.edits.append(kw)
        return {key + "Results": [{"success": True}] * len(kw[key]) for key in kw}

    def delete_features(self, **kw):
        self.edits.append(kw)
        return {"deleteResults": [{"success": True}]}


@pytest.mark.parametrize("apply", [False, True])
def test_refresh_metrics_is_check_by_default(monkeypatch, apply):
    table = _MetricsTable([_stored(1, "features_max_oid", "new.log")])
    monkeypatch.setattr(publish, "connect_agol", lambda config: None)
    monkeypatch.setattr(publish, "ensure_service", lambda gis, config, target: None)
    monkeypatch.setattr(publish, "check_schema_drift", lambda tables: None)  # fakes have no schema
    monkeypatch.setattr(publish, "get_tables", lambda item: {publish.TABLE_METRICS: table,
                                                             publish.TABLE_INVENTORY: _MetricsTable([])})
    monkeypatch.setattr(publish, "mine_metrics",
                        lambda config, inventory, host, examined=None: ([_fresh("exported_record_count", "new.log")], {}))
    args = publish.parse_args(["--refresh-metrics"] + (["--apply"] if apply else []))
    assert publish.run_refresh_metrics({"host_name_override": "H", "privacy": {"hide_paths": False}}, args) == 0
    assert bool(table.edits) is apply


def test_refresh_metrics_runs_on_its_own():
    with pytest.raises(SystemExit):
        publish.parse_args(["--refresh-metrics", "--refresh-result-text"])
    with pytest.raises(SystemExit):
        publish.parse_args(["--refresh-metrics", "--migrate"])


# ==================================================================== clock

def test_metrics_keeps_the_real_clock():
    """Its tests age real files with os.utime relative to now (see conftest)."""
    assert metrics.utc_now() != conftest.FROZEN_NOW


def test_refresh_metrics_refuses_an_unmigrated_service_with_the_fix(monkeypatch):
    monkeypatch.setattr(publish, "connect_agol", lambda config: None)
    monkeypatch.setattr(publish, "ensure_service", lambda gis, config, target: None)
    monkeypatch.setattr(publish, "get_tables", lambda item: {publish.TABLE_METRICS: object(),
                                                             publish.TABLE_INVENTORY: object()})

    def drift(tables):
        raise SystemExit("Schema drift -- Run `--migrate --apply` to add them.")
    monkeypatch.setattr(publish, "check_schema_drift", drift)
    with pytest.raises(SystemExit, match="--migrate --apply"):
        publish.run_refresh_metrics({"host_name_override": "H"}, publish.parse_args(["--refresh-metrics"]))
