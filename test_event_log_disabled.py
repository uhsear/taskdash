"""H1-5: a disabled Task Scheduler Operational log must not discard the rest of the run.

The task list is still published and alerted on; the run records the failure and exits 1 at the
end. No network: AGOL, the collector's PowerShell and e-mail are all faked.
"""

from __future__ import annotations

import pytest

import taskdash_collect as collect
import taskdash_publish as publish
from test_taskdash import _task

DISABLED_MESSAGE = "Event log 'Synthetic/Operational' is disabled, so run history cannot be collected."


def _disabled_log(*args, **kwargs):
    raise collect.EventLogDisabled(DISABLED_MESSAGE)


def _args(**overrides):
    args = publish.parse_args(["--apply"])
    args.target = "prod"
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


@pytest.fixture
def fake_world(monkeypatch):
    """Real collect_everything over one synthetic task and a disabled log; AGOL is faked."""
    seen = {"published": [], "alerts": [], "rows": [], "events_read_from": []}
    item = type("I", (), {"title": "x", "id": "svc", "access": "private"})()

    def read_events(*args, **kwargs):
        seen["events_read_from"].append(args)
        return _disabled_log()

    monkeypatch.setattr(collect, "collect_tasks", lambda timeout: [_task()])
    monkeypatch.setattr(collect, "collect_run_events", read_events)
    monkeypatch.setattr(collect, "load_descriptions", lambda: {})
    monkeypatch.setattr(publish, "connect_agol", lambda cfg: object())
    monkeypatch.setattr(publish, "ensure_service", lambda gis, cfg, target: item)
    monkeypatch.setattr(publish, "get_tables", lambda i: {})
    monkeypatch.setattr(publish, "check_schema_drift", lambda tables: None)
    monkeypatch.setattr(publish, "check_source_sharing", lambda *a: None)
    monkeypatch.setattr(publish, "publish",
                        lambda gis, i, collected, *a, **k: seen["published"].append(collected) or {})
    monkeypatch.setattr(publish, "maybe_send_alert",
                        lambda collected, *a: seen["alerts"].append(collected) or True)
    monkeypatch.setattr(publish, "_write_collector_run_row",
                        lambda i, cfg, collected, **kw: seen["rows"].append(kw))
    return seen


def test_collect_everything_continues_with_zero_events_when_the_log_is_disabled(fake_world):
    collected = publish.collect_everything({}, None)
    assert [r["task_name"] for r in collected["inventory"]] == ["Parcel Refresh"]
    assert collected["runs"] == [] and collected["raw_events"] == []
    assert DISABLED_MESSAGE in str(collected["event_log_error"])


def test_a_disabled_log_still_publishes_alerts_and_records_the_run_then_exits_1(
        fake_world, tmp_path):
    progress = {"armed": False, "recorded": False}
    with pytest.raises(SystemExit) as exc:
        publish._run_publish(_args(), {}, tmp_path, publish.utc_now(), progress)

    assert DISABLED_MESSAGE in str(exc.value)
    assert [len(c["inventory"]) for c in fake_world["published"]] == [1], "inventory is published"
    assert len(fake_world["alerts"]) == 1, "alerts are still evaluated"
    (row,) = fake_world["rows"]
    assert row["exit_status"] == "failed" and DISABLED_MESSAGE in row["error_text"]
    assert row["watermark_to"] == row["watermark_from"], "a disabled log never advances the watermark"
    assert progress["recorded"] is True


def test_a_publish_failure_wins_over_the_disabled_log_message(fake_world, monkeypatch, tmp_path):
    monkeypatch.setattr(publish, "publish",
                        lambda *a, **k: (_ for _ in ()).throw(SystemExit("AGOL write failed")))
    with pytest.raises(SystemExit, match="AGOL write failed"):
        publish._run_publish(_args(), {}, tmp_path, publish.utc_now(),
                             {"armed": False, "recorded": False})
    assert fake_world["rows"][0]["error_text"] == "AGOL write failed"


def test_a_dry_run_with_a_disabled_log_still_exits_nonzero(fake_world, tmp_path):
    with pytest.raises(SystemExit, match="is disabled"):
        publish._run_publish(_args(apply=False), {}, tmp_path, publish.utc_now(),
                             {"armed": False, "recorded": False})
    assert fake_world["published"] == []


def test_the_disabled_log_exception_is_a_system_exit_so_other_callers_still_stop(monkeypatch):
    monkeypatch.setattr(collect, "run_powershell_json", lambda script, timeout: [
        {"exists": True, "is_enabled": False, "record_count": 0, "error_message": ""}])
    with pytest.raises(SystemExit) as exc:
        collect.check_event_log_health()
    assert isinstance(exc.value, collect.EventLogDisabled)
