"""Final prod audit (2026-10-16), collector side: findings 0+9, 1, 4 and 7.

No ArcGIS calls: AGOL is replaced by an in-memory table, and task rows are built directly.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import taskdash_dashboard as dashboard
import taskdash_publish as publish
from taskdash_common import normalize_text

NOW = datetime(2026, 10, 16, 21, 0, 23, tzinfo=timezone.utc)


class _MemTable:
    """Just enough of an AGOL table for upsert_by_key: rows by OBJECTID."""

    def __init__(self):
        self.rows = {}

    def edit_features(self, adds=None, updates=None, rollback_on_failure=True):
        for feature in adds or []:
            self.rows[len(self.rows) + 1] = dict(feature["attributes"])
        for feature in updates or []:
            attrs = dict(feature["attributes"])
            self.rows[attrs.pop("OBJECTID")] = attrs
        return {"addResults": [{"success": True}] * len(adds or []),
                "updateResults": [{"success": True}] * len(updates or [])}


@pytest.fixture
def mem_agol(monkeypatch):
    table = _MemTable()
    monkeypatch.setattr(publish, "object_id_field", lambda t: "OBJECTID")
    monkeypatch.setattr(publish, "fetch_keys", lambda t, rows, keys, oid: {
        publish.folded_key(r, keys): o for o, r in t.rows.items()})
    monkeypatch.setattr(publish, "get_tables", lambda item: {publish.TABLE_COLLECTOR_RUNS: table})
    monkeypatch.setattr(publish, "purge_older_than", lambda *a, **k: 0)
    return table


def _write(item, pending_path, started, status="success", error=""):
    publish._write_collector_run_row(
        item, {}, {"host_name": "H", "inventory": [], "raw_events": []},
        run_started=started, run_ended=started + timedelta(minutes=1), exit_status=status,
        error_text=error, watermark_from=0.0, watermark_to=0.0, publish_counts={},
        alert_sent=False, pending_path=pending_path)


# ==================================================================== 0 + 9: failed runs get a row

def test_a_run_without_agol_saves_a_failed_row_and_the_next_run_writes_it(mem_agol, tmp_path):
    pending = tmp_path / "collector_runs_pending_prod.json"
    failed_at = datetime(2026, 10, 16, 5, 0, 1, tzinfo=timezone.utc)
    _write(None, pending, failed_at, "failed",
           "ConnectionError: https://user:hunter2@www.arcgis.com refused")
    saved = json.loads(pending.read_text(encoding="utf-8"))
    assert [r["exit_status"] for r in saved] == ["failed"]
    assert "hunter2" not in saved[0]["error_text"], "error_text must go through _scrub_secrets"
    assert "ConnectionError" in saved[0]["error_text"]
    assert mem_agol.rows == {}

    _write(object(), pending, failed_at + timedelta(hours=1))
    stored = sorted(mem_agol.rows.values(), key=lambda r: r["started_utc"])
    assert [r["exit_status"] for r in stored] == ["failed", "success"]
    assert json.loads(pending.read_text(encoding="utf-8")) == [], "written rows leave the file"
    assert pending.exists(), "the file is rewritten, never deleted"


def test_a_pending_row_sent_twice_updates_and_never_duplicates(mem_agol, tmp_path):
    """The write landed but the file was not cleared (a crash between the two): the next run
    sends the same run_id again, and the run_id key turns it into an update."""
    pending = tmp_path / "p.json"
    failed_at = datetime(2026, 10, 16, 5, 0, 1, tzinfo=timezone.utc)
    _write(None, pending, failed_at, "failed", "boom")
    kept = pending.read_text(encoding="utf-8")
    _write(object(), pending, failed_at + timedelta(hours=1))
    pending.write_text(kept, encoding="utf-8")  # simulate the lost rewrite
    _write(object(), pending, failed_at + timedelta(hours=2))

    run_ids = [r["run_id"] for r in mem_agol.rows.values()]
    assert len(run_ids) == len(set(run_ids)) == 3


def test_a_failed_agol_write_keeps_the_row_pending(monkeypatch, tmp_path):
    pending = tmp_path / "p.json"
    monkeypatch.setattr(publish, "get_tables", lambda item: {publish.TABLE_COLLECTOR_RUNS: object()})
    monkeypatch.setattr(publish, "upsert_by_key",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("AGOL down")))
    _write(object(), pending, NOW, "failed", "publish failed")
    assert [r["error_text"] for r in json.loads(pending.read_text(encoding="utf-8"))] == ["publish failed"]


def test_a_missing_table_saves_nothing(monkeypatch, tmp_path):
    pending = tmp_path / "p.json"
    monkeypatch.setattr(publish, "get_tables", lambda item: {})
    _write(object(), pending, NOW)
    assert not pending.exists()


def _publish_args(**overrides):
    args = publish.parse_args(["--apply"])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_run_publish_saves_a_row_when_connect_agol_raises(monkeypatch, tmp_path):
    """The 2026-10-16 01:00 path: www.arcgis.com refused the connection, the run alerted and
    exited 1, and left no CollectorRuns row."""
    monkeypatch.setattr(publish, "connect_agol",
                        lambda cfg: (_ for _ in ()).throw(ConnectionError("refused")))
    monkeypatch.setattr(publish, "collect_everything",
                        lambda *a, **k: {"host_name": "H", "inventory": [], "runs": [],
                                         "metrics": [], "log_summary": {}})
    monkeypatch.setattr(publish, "report", lambda collected: None)
    monkeypatch.setattr(publish, "maybe_send_alert", lambda *a: True)
    with pytest.raises(ConnectionError):
        publish.run_publish(_publish_args(target="prod"), {}, tmp_path)
    rows = json.loads((tmp_path / "collector_runs_pending_prod.json").read_text(encoding="utf-8"))
    assert len(rows) == 1, "one row, not a second one from the uncaught-error handler"
    assert rows[0]["exit_status"] == "failed" and rows[0]["error_text"] == "refused"
    assert rows[0]["alert_sent"] == 1


def test_run_publish_saves_a_row_for_an_uncaught_error(monkeypatch, tmp_path):
    monkeypatch.setattr(publish, "connect_agol",
                        lambda cfg: (_ for _ in ()).throw(ConnectionError("refused")))
    monkeypatch.setattr(publish, "collect_everything",
                        lambda *a, **k: (_ for _ in ()).throw(KeyError("tasks")))
    with pytest.raises(KeyError):
        publish.run_publish(_publish_args(target="test"), {}, tmp_path)
    rows = json.loads((tmp_path / "collector_runs_pending_test.json").read_text(encoding="utf-8"))
    assert [r["exit_status"] for r in rows] == ["failed"]
    assert not (tmp_path / "collector_runs_pending_prod.json").exists(), "a test run stays out of prod"


def test_a_refused_wrong_host_run_saves_no_row(tmp_path):
    with pytest.raises(SystemExit, match="Refusing to publish"):
        publish.run_publish(_publish_args(), {"host_name_override": "NOT-THIS-HOST-X"}, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_collector_runs_help_says_when_a_failed_row_appears():
    text = " ".join(dashboard.HELP["Collector Runs"])
    assert "appears here after the next run that can" in text
    assert "A gap in Started shows it." in text


# ==================================================================== 1: a finished run is not Running

def _run(start, status, duration=None, is_error=0, text="", code=None, task="\\CSC"):
    return {"task_full_name": task, "start_time_utc": start.isoformat(), "run_status": status,
            "duration_seconds": duration, "is_error": is_error, "result_text": text,
            "result_code": code, "instance_id": f"{task}-{start.isoformat()}"}


def _running_task(started, **overrides):
    task = {"task_full_name": "\\CSC", "task_name": "Service Desk Mailer", "health_status": "Running",
            "state": "Running", "last_run_time": started.isoformat(), "last_run_failed": 0,
            "needs_attention": 0, "is_overdue": 0, "last_result_code": 267009,
            "last_result_text": "The task is currently running.", "attention_reason": "",
            "command_target_path": r"C:\Elsewhere\csc.py"}
    task.update(overrides)
    return task


def test_a_run_that_ended_before_the_event_read_is_not_running():
    started = NOW - timedelta(seconds=22)
    stats = publish.summarize_runs(
        [_run(started, "Success", 16.55, text="The operation completed successfully.", code=0)], NOW)
    task = _running_task(started)
    for _ in range(2):  # enrich_inventory runs twice per publish
        publish.enrich_inventory([task], stats, {}, now=NOW)
    assert task["health_status"] == "Success"
    assert task["state"] == "Ready"
    assert task["last_result_code"] == 0
    assert task["last_result_text"] == "The operation completed successfully."
    assert task["attention_kind"] == ""


def test_an_older_finished_run_does_not_end_a_run_still_going():
    started = NOW - timedelta(seconds=22)
    stats = publish.summarize_runs([_run(started - timedelta(hours=1), "Success", 17.0)], NOW)
    task = _running_task(started)
    publish.enrich_inventory([task], stats, {}, now=NOW)
    assert (task["health_status"], task["state"]) == ("Running", "Running")


def test_a_real_long_run_stays_running_and_still_goes_stuck():
    started = NOW - timedelta(hours=2)
    stats = publish.summarize_runs([_run(started, "Running")], NOW)
    task = _running_task(started)
    publish.enrich_inventory([task], stats, {}, now=NOW)
    assert task["state"] == "Running"
    assert task["attention_kind"] == "STUCK"
    assert task["health_status"] == "Warning"


def test_a_failed_finish_clears_the_running_state_too():
    started = NOW - timedelta(seconds=22)
    stats = publish.summarize_runs([_run(started, "Error", 5.0, is_error=1, text="boom", code=1)], NOW)
    task = _running_task(started)
    publish.enrich_inventory([task], stats, {}, now=NOW)
    assert (task["health_status"], task["last_run_failed"], task["state"]) == ("Error", 1, "Ready")


# ==================================================================== 4: Running never flips history

def _stored(failed, health, state, reason=""):
    return {"task_full_name": "\\DM", "task_name": "DM", "definition_hash": "",
            "last_run_failed": failed, "is_overdue": 0, "health_status": health,
            "state": state, "attention_reason": reason}


def _changes(old, new, history=None):
    return publish.detect_task_changes({"\\DM": old}, [new], "H", NOW.isoformat(), history)


def test_a_failed_task_that_starts_again_is_not_recovered():
    assert _changes(_stored(1, "Error", "Ready", "boom"), _stored(0, "Running", "Running")) == []


def test_recovered_needs_the_finished_good_run():
    history = {"\\DM": (True, "boom")}
    rows = _changes(_stored(0, "Running", "Running"), _stored(0, "Success", "Ready"), history)
    assert [(r["change_kind"], r["old_summary"]) for r in rows] == [("recovered", "boom")]


def test_a_failure_after_a_failure_with_a_run_between_logs_nothing_new():
    history = {"\\DM": (True, "boom")}
    assert _changes(_stored(0, "Running", "Running"), _stored(1, "Error", "Ready", "boom"), history) == []


def test_a_stuck_run_records_no_transition():
    assert _changes(_stored(1, "Error", "Ready"), _stored(0, "Warning", "Running")) == []


def test_a_full_fail_run_recover_cycle_writes_one_row_each_under_the_same_key(monkeypatch):
    """Four hourly passes through a keyed upsert: one became_failing and one recovered, never a
    duplicate, and the TaskChanges key is still (host, task, kind, changed_utc)."""
    table = _MemTable()
    monkeypatch.setattr(publish, "object_id_field", lambda t: "OBJECTID")
    monkeypatch.setattr(publish, "fetch_keys", lambda t, rows, keys, oid: {
        publish.folded_key(r, keys): o for o, r in t.rows.items()})
    key =["host_name", "task_full_name", "change_kind", "changed_utc"]
    passes = [_stored(0, "Success", "Ready"), _stored(1, "Error", "Ready", "boom"),
              _stored(0, "Running", "Running"), _stored(0, "Success", "Ready")]
    history = {}
    for hour, (old, new) in enumerate(zip(passes, passes[1:]), start=1):
        when = (NOW + timedelta(hours=hour)).isoformat()
        changes = publish.detect_task_changes({"\\DM": old}, [new], "H", when, history)
        for _ in range(2):  # a retried write sends the same rows again
            publish.upsert_by_key(table, publish.prepare_rows(changes, publish.TABLE_TASK_CHANGES, when),
                                  key, chunk_size=100, label="TaskChanges")
        for change in changes:
            history["\\DM"] = (change["change_kind"] == "became_failing", change["new_summary"])
    kinds = sorted(r["change_kind"] for r in table.rows.values())
    assert kinds == ["became_failing", "recovered"]
    recovered = next(r for r in table.rows.values() if r["change_kind"] == "recovered")
    assert recovered["changed_utc"] == publish.to_epoch_millis((NOW + timedelta(hours=3)).isoformat())


# ==================================================================== 7: one duration format

@pytest.mark.parametrize("seconds, text", [
    (0.4, "< 1 s"), (39.0, "39 s"), (59.5, "1 min"), (60.0, "1 min"), (1453.95, "24 min 14 s"),
    (3600.0, "1 h"), (3725.0, "1 h 2 min"),
])
def test_run_duration_matches_the_dashboard_fmtdur(seconds, text):
    assert publish._run_duration(seconds) == text


def test_the_anomaly_sentence_names_the_run_duration_like_failed_runs():
    runs = [_run(NOW - timedelta(days=d), "Success", 39.0, task="\\TD") for d in range(1, 8)]
    runs.append(_run(NOW - timedelta(hours=1), "Error", 1453.95, is_error=1, task="\\TD"))
    stats = publish.summarize_runs(runs, NOW)
    task = _running_task(NOW - timedelta(hours=1), task_full_name="\\TD", task_name="Task Dash",
                         health_status="Error", state="Ready", last_run_failed=1, needs_attention=1)
    publish.enrich_inventory([task], stats, {}, now=NOW)
    assert "Last run took 24 min 14 s, about 37.3 times the usual 39 s" in task["attention_reason"]
