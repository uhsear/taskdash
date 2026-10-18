"""Round-1 audit fixes on the publish side: C2, R2-R5, T1, X2, X6, X7, the frozen test clock
and the slow connect-failure test. Fakes only: no ArcGIS Online, no PowerShell, no SMTP.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import conftest
import taskdash_common as common
import taskdash_publish as publish

NOW = conftest.FROZEN_NOW
SUCCESS = "The operation completed successfully."


def _iso(value: datetime) -> str:
    return value.isoformat()


def _run(hours_ago, status="Success", duration=30.0, is_error=0, text=SUCCESS, task="\\T"):
    return {"task_full_name": task, "run_status": status, "duration_seconds": duration,
            "is_error": is_error, "result_text": text,
            "start_time_utc": _iso(NOW - timedelta(hours=hours_ago))}


def _task(**overrides):
    task = {"task_full_name": "\\T", "task_name": "T", "health_status": "Success",
            "needs_attention": 0, "last_run_failed": 0, "is_overdue": 0, "state": "Ready",
            "last_result_text": SUCCESS, "attention_reason": SUCCESS, "last_run_time": None}
    task.update(overrides)
    return task


# ==================================================================== frozen clock

def test_every_module_clock_is_frozen_by_default():
    assert publish.utc_now() == common.utc_now() == NOW


@pytest.mark.real_clock
def test_real_clock_marker_opts_out():
    assert abs((publish.utc_now() - datetime.now(timezone.utc)).total_seconds()) < 60


# ==================================================================== C2

def test_definition_hash_ignores_the_case_of_the_run_as_user():
    raw = {"actions": "run.py", "trigger_details": [], "run_as_user": "svc_gisjobs",
           "logon_type": "Password", "run_level": "Limited", "enabled": True}
    assert (publish.compute_definition_hash(raw)
            == publish.compute_definition_hash(dict(raw, run_as_user="svc_gisjobs")))
    assert publish.compute_definition_hash(raw) != publish.compute_definition_hash(
        dict(raw, run_as_user="OtherUser"))


# ==================================================================== R3

def test_last_duration_comes_from_the_newest_finished_run_not_the_running_one():
    rows = [_run(0.1, status="Running", duration=None), _run(1, duration=35.77)]
    stats = publish.summarize_runs(rows)["\\T"]
    assert stats["last_duration_seconds"] == 35.77
    assert stats["running_since"] is not None, "the running row still drives stuck detection"


def _own_task(**overrides):
    fields = {"task_name": "Task Dash", "state": "Running", "health_status": "Running",
              "command_target_path": str(common.runtime_dir() / "run_taskdash.cmd"),
              "last_result_text": "Task is currently running.", "attention_reason": ""}
    return _task(**{**fields, **overrides})


def test_task_dash_is_judged_by_its_previous_run_while_it_collects():
    task = _own_task()
    stats = publish.summarize_runs([_run(0.01, status="Running", duration=None),
                                    _run(1, duration=31.12)])
    for _ in range(2):  # enrich_inventory runs twice per publish
        publish.enrich_inventory([task], stats, {})
    assert task["health_status"] == "Success" and task["needs_attention"] == 0
    assert task["last_duration_seconds"] == 31.12
    assert task["attention_reason"] == f"Running now. Status is from the previous run: {SUCCESS}"
    # task_detail-r3regress-own-task-running-previous-duration: last_run_time must move to the
    # same previous finished run that last_duration_seconds (31.12) describes, not stay at the
    # collector's own still-running current start (the 0.01h-ago row).
    assert common.parse_iso_datetime(task["last_run_time"]) == NOW - timedelta(hours=1)


def test_task_dash_previous_failed_run_shows_as_failed():
    task = _own_task()
    stats = publish.summarize_runs([_run(0.01, status="Running", duration=None),
                                    _run(1, status="Error", is_error=1, text="boom")])
    publish.enrich_inventory([task], stats, {})
    assert (task["health_status"], task["last_run_failed"], task["attention_kind"]) == \
        ("Error", 1, "FAILED")


def test_another_running_task_still_reads_running():
    task = _own_task(command_target_path=r"D:\Elsewhere\job.py")
    publish.enrich_inventory([task], publish.summarize_runs([_run(1)]), {})
    assert task["health_status"] == "Running"


# ==================================================================== R5

def _estimated(seen, end, task="\\T"):
    return {"task_full_name": task, "run_status": "Success", "start_time_estimated": 1,
            "start_time_utc": _iso(seen), "end_time_utc": _iso(end), "duration_seconds": None}


def test_estimated_start_takes_last_run_time_when_it_fits():
    last_run = NOW - timedelta(minutes=10)
    run = _estimated(last_run + timedelta(seconds=172), last_run + timedelta(seconds=172))
    inventory = [{"task_full_name": "\\T", "last_run_time": _iso(last_run)}]
    assert publish.reconcile_estimated_starts([run], inventory, {"\\T": 0.0}) == 1
    assert (run["start_time_estimated"], run["duration_seconds"]) == (0, 172.0)
    assert run["start_time_utc"] == _iso(last_run)


def test_estimated_start_is_kept_when_last_run_time_belongs_to_a_newer_run():
    seen = NOW - timedelta(hours=2)
    run = _estimated(seen, seen)
    inventory = [{"task_full_name": "\\T", "last_run_time": _iso(NOW - timedelta(hours=1))}]
    assert publish.reconcile_estimated_starts([run], inventory, {}) == 0
    assert run["start_time_estimated"] == 1


def test_estimated_start_is_kept_past_the_execution_time_limit():
    last_run = NOW - timedelta(hours=3)
    run = _estimated(NOW, NOW)
    inventory = [{"task_full_name": "\\T", "last_run_time": _iso(last_run)}]
    assert publish.reconcile_estimated_starts([run], inventory, {"\\T": 3600.0}) == 0


# ==================================================================== T1, X7

def test_failed_reason_does_not_also_say_the_run_succeeded():
    task = _task(last_run_time=_iso(NOW - timedelta(days=1)))
    runs = [_run(1, status="StartFailed", is_error=1, duration=None, text="An action failed to start.")]
    publish.enrich_inventory([task], publish.summarize_runs(runs), {})
    assert task["attention_kind"] == "FAILED"
    assert "successfully" not in task["attention_reason"]
    assert "An action failed to start." in task["attention_reason"]


def test_failed_run_with_a_success_code_gets_a_failure_text():
    task = _task(last_run_time=_iso(NOW - timedelta(days=1)))
    runs = [_run(1, status="StartFailed", is_error=1, duration=None, text=SUCCESS)]
    publish.enrich_inventory([task], publish.summarize_runs(runs), {})
    assert "successfully" not in task["attention_reason"]
    assert "successfully" not in task["last_result_text"]


def test_stuck_reason_says_running_once_in_one_unit_style():
    task = _task(state="Running", health_status="Running", attention_reason="task is currently running")
    rows = [_run(d * 24, duration=600.0) for d in range(1, 6)] + [_run(2, status="Running", duration=None)]
    publish.enrich_inventory([task], publish.summarize_runs(rows), {})
    assert task["attention_reason"] == "Still running 2.0 h, usually about 10 min"


def test_anomaly_reason_uses_usual_not_percentiles():
    task = _task()
    stats = {"last_duration_seconds": 300.0, "baseline_median_seconds": 60.0,
             "baseline_p95_seconds": 90.0, "baseline_p5_seconds": 30.0, "baseline_run_count": 5}
    publish.enrich_inventory([task], {"\\T": stats}, {})
    # wording-16: matches the Task Detail card's anomalyText ("Duration: 5.0 times the usual").
    assert "Last run took 5 min, about 5.0 times the usual 60 s" in task["attention_reason"]
    assert "p95" not in task["attention_reason"]


# ==================================================================== R2

def test_a_rewritten_failed_run_gets_its_text_described_again():
    run = {"run_key": "k", "is_error": 0, "end_time_utc": _iso(NOW)}
    prior = {"is_error": 1, "result_code": 3221225786.0, "run_status": "Error", "is_success": 0,
             "result_text": "Unmapped task result code (3221225786, 0xC000013A)."}
    publish.carry_stored_failure(run, prior)
    assert run["is_error"] == 1
    assert run["result_text"].startswith("The run was stopped before it finished")


class _FakeRunsTable:
    properties = SimpleNamespace(objectIdField="OBJECTID", maxRecordCount=1000)

    def __init__(self, rows):
        self.rows, self.sent = rows, []

    def query(self, **kw):
        page = self.rows[kw.get("result_offset", 0):][:kw.get("result_record_count", 1000)]
        return SimpleNamespace(features=[SimpleNamespace(attributes=dict(r)) for r in page])

    def edit_features(self, **kw):
        self.sent.append(kw)
        return {"updateResults": [{"success": True} for _ in kw.get("updates") or []]}


def _stored_runs():
    return [{"OBJECTID": 1, "result_code": 3221225786.0, "run_status": "Error",
             "result_text": "Unmapped task result code (3221225786, 0xC000013A)."},
            {"OBJECTID": 2, "result_code": 0.0, "run_status": "Success", "result_text": SUCCESS}]


def test_refresh_result_text_is_check_by_default():
    table = _FakeRunsTable(_stored_runs())
    assert publish.refresh_result_text(table, apply=False) == 1
    assert table.sent == []


def test_refresh_result_text_apply_updates_only_the_outdated_text():
    table = _FakeRunsTable(_stored_runs())
    assert publish.refresh_result_text(table, apply=True) == 1
    [update] = table.sent[0]["updates"]
    assert set(update["attributes"]) == {"OBJECTID", "result_text"}
    assert update["attributes"]["OBJECTID"] == 1


def test_refresh_result_text_runs_on_its_own():
    with pytest.raises(SystemExit):
        publish.parse_args(["--refresh-result-text", "--preview-alert"])
    assert publish.parse_args(["--refresh-result-text", "--apply"]).refresh_result_text


# ==================================================================== X2, X6

def _alert(inventory, previous=None, heartbeat=False):
    config = {"alerting": {"enabled": True, "to": ["a@b.c"],
                           "heartbeat_hour": NOW.astimezone().hour if heartbeat else None}}
    collected = {"host_name": "H", "inventory": inventory}
    return publish.compose_alert(collected, config, previous or {}, NOW)


def test_daily_check_never_says_healthy_while_warnings_are_open():
    stuck = _task(task_name="S", needs_attention=1, attention_kind="STUCK")
    alert = _alert([stuck, _task()], heartbeat=True)
    assert alert["reason"] == "heartbeat"
    assert alert["subject"] == "[TaskDash] daily check on H: 1 task(s) need attention"
    assert "healthy" not in alert["subject"]
    assert "  STUCK: S" in alert["body"]


def test_daily_check_says_healthy_when_nothing_is_open():
    assert _alert([_task()], heartbeat=True)["subject"] == \
        "[TaskDash] daily check: all 1 task healthy on H"


def test_changed_alert_names_what_is_new_and_what_cleared():
    failed = _task(task_name="B", last_run_failed=1, needs_attention=1, last_result_text="boom")
    alert = _alert([failed], previous={"signature": ["FAILED: A"]})
    assert alert["reason"] == "changed"
    body = alert["body"]
    assert "New:\n  FAILED: B -- boom" in body
    assert "Cleared:\n  FAILED: A" in body
    assert not body.startswith("[TaskDash]"), "the body must not repeat the subject"


# ==================================================================== slow connect-failure test

def test_collector_run_row_is_skipped_without_an_agol_item(monkeypatch):
    def no_arcgis(item):
        raise AssertionError("get_tables must not run: it imports arcgis for nothing")

    monkeypatch.setattr(publish, "get_tables", no_arcgis)
    publish._write_collector_run_row(
        None, {}, {}, run_started=NOW, run_ended=NOW, exit_status="failed", error_text="x",
        watermark_from=0.0, watermark_to=0.0, publish_counts={}, alert_sent=False)
