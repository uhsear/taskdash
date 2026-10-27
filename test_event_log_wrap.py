"""H1-8: a wrapped or cleared event log is reported, not silent.

The history query returns the oldest surviving record id as one extra row. When it is more than one
past the watermark, records were lost while TaskDash was down: warn, and record the warning in
CollectorRuns error_text. The run still exits 0. No network: PowerShell, AGOL and e-mail are faked.
"""

from __future__ import annotations

import pytest

import taskdash_collect as collect
import taskdash_publish as publish


def test_oldest_next_to_the_watermark_gives_no_warning():
    assert collect.event_gap_warning(100, 101) == ""
    assert collect.event_gap_warning(100, 50) == ""


def test_oldest_far_above_the_watermark_names_the_gap():
    text = collect.event_gap_warning(100, 600)
    assert text.startswith("499 event record id(s) were skipped")
    assert "wrapped or was cleared" in text and "preflight event-log fix" in text


def test_first_run_or_unknown_oldest_gives_no_warning():
    assert collect.event_gap_warning(0, 600) == ""
    assert collect.event_gap_warning(100, None) == ""


def test_the_marker_row_is_split_from_the_events(monkeypatch):
    monkeypatch.setattr(collect, "check_event_log_health", lambda **k: {})
    rows = [{"event_id": 100, "event_record_id": 7}, {"oldest_record_id": 5}]
    monkeypatch.setattr(collect, "run_powershell_json", lambda script, timeout: rows)
    events = collect.collect_run_events(30, 100, 60, min_record_id=4)
    assert events == [{"event_id": 100, "event_record_id": 7}]
    assert collect.oldest_record_id == 5


def test_the_script_asks_for_the_oldest_record_only_on_incremental_reads():
    assert "-MaxEvents 1 -Oldest" in collect.POWERSHELL_HISTORY_QUERY_TEMPLATE


def _event(record_id, task="\\Synthetic\\Unmonitored"):
    return {"event_id": 102, "event_record_id": record_id, "event_time_utc": "2026-10-01T00:00:00Z",
            "task_full_name": task, "instance_id": "i", "user_context": "", "action_name": "",
            "result_code": None, "computer_name": "SYNTH-1"}


@pytest.fixture
def fake_world(monkeypatch, tmp_path):
    seen = {"rows": [], "oldest": 900, "events": []}
    (tmp_path / publish.EVENT_READ_MARK_FILE).write_text('{"record_id": 400}', encoding="utf-8")
    item = type("I", (), {"title": "x", "id": "svc", "access": "private"})()

    def read(*a, **k):
        collect.oldest_record_id = seen["oldest"]
        return list(seen["events"])

    monkeypatch.setattr(collect, "collect_tasks", lambda timeout=180, *_: [])
    monkeypatch.setattr(collect, "collect_run_events", read)
    monkeypatch.setattr(collect, "load_descriptions", lambda: {})
    monkeypatch.setattr(collect, "newest_record_id", lambda timeout=180: 2000)
    monkeypatch.setattr(publish, "connect_agol", lambda cfg: object())
    monkeypatch.setattr(publish, "ensure_service", lambda gis, cfg, target: item)
    monkeypatch.setattr(publish, "get_tables", lambda i: {publish.TABLE_RUNS: object()})
    monkeypatch.setattr(publish, "event_watermark", lambda *a: 100)
    monkeypatch.setattr(publish, "check_schema_drift", lambda tables: None)
    monkeypatch.setattr(publish, "check_source_sharing", lambda *a: None)
    monkeypatch.setattr(publish, "publish", lambda *a, **k: {})
    monkeypatch.setattr(publish, "maybe_send_alert", lambda *a: False)
    monkeypatch.setattr(publish, "_write_collector_run_row",
                        lambda i, cfg, collected, **kw: seen["rows"].append(kw))
    return seen


def _run(tmp_path):
    args = publish.parse_args(["--apply"])
    args.target = "prod"
    return publish._run_publish(args, {"logging": {"log_dir": str(tmp_path)}}, tmp_path,
                                publish.utc_now(), {"armed": False, "recorded": False})


def test_a_gap_is_recorded_and_the_run_still_succeeds(fake_world, tmp_path, capsys):
    assert _run(tmp_path) == 0
    (row,) = fake_world["rows"]
    assert row["exit_status"] == "success"
    assert "499 event record id(s) were skipped" in row["error_text"]
    assert capsys.readouterr().out.count("WARNING: 499 event record id(s)") == 1


def test_a_gap_is_reported_once_per_oldest_id(fake_world, tmp_path):
    assert _run(tmp_path) == 0
    assert _run(tmp_path) == 0
    assert "skipped" in fake_world["rows"][0]["error_text"]
    assert fake_world["rows"][1]["error_text"] == ""


def test_no_gap_leaves_error_text_empty(fake_world, tmp_path):
    fake_world["oldest"] = 401
    assert _run(tmp_path) == 0
    assert fake_world["rows"][0]["error_text"] == ""


def test_an_old_watermark_with_idle_monitored_tasks_is_not_a_gap(fake_world, tmp_path):
    # The watermark (100) lags: only monitored runs move it. The previous read got to 899, so
    # a log whose oldest record is 900 lost nothing.
    (tmp_path / publish.EVENT_READ_MARK_FILE).write_text('{"record_id": 899}', encoding="utf-8")
    assert _run(tmp_path) == 0
    assert fake_world["rows"][0]["error_text"] == ""


def test_the_first_run_after_the_upgrade_has_no_mark_so_no_warning(fake_world, tmp_path):
    (tmp_path / publish.EVENT_READ_MARK_FILE).unlink()
    fake_world["events"] = [_event(950), _event(960)]
    assert _run(tmp_path) == 0
    assert fake_world["rows"][0]["error_text"] == ""
    assert publish._saved_event_read_mark(tmp_path / publish.EVENT_READ_MARK_FILE) == 960


def test_the_mark_is_the_newest_record_read_even_when_no_monitored_task_owns_it(fake_world, tmp_path):
    fake_world["oldest"] = 401
    fake_world["events"] = [_event(950), _event(960)]
    assert _run(tmp_path) == 0
    assert publish._saved_event_read_mark(tmp_path / publish.EVENT_READ_MARK_FILE) == 960


def test_a_failed_publish_leaves_the_mark_where_it_was(fake_world, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise SystemExit("publish failed")

    monkeypatch.setattr(publish, "publish", boom)
    fake_world["oldest"] = 401
    fake_world["events"] = [_event(950)]
    with pytest.raises(SystemExit):
        _run(tmp_path)
    assert publish._saved_event_read_mark(tmp_path / publish.EVENT_READ_MARK_FILE) == 400


def test_a_damaged_mark_file_means_no_check(tmp_path):
    path = tmp_path / publish.EVENT_READ_MARK_FILE
    path.write_text("not json", encoding="utf-8")
    assert publish._saved_event_read_mark(path) == 0
    assert publish._saved_event_read_mark(tmp_path / "missing.json") == 0
