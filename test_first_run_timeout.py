"""H1-3: a timed-out first-run event read must not repeat the identical failing read forever.

No network and no PowerShell: subprocess.run is faked, so TimeoutExpired is raised the same way
the real call raises it.
"""

from __future__ import annotations

import json
import subprocess

import pytest

import taskdash_collect as collect
import taskdash_common as common
import taskdash_publish as publish
from test_taskdash import _task

HISTORY = {"days_back": 30, "max_events_per_run": 5000}


def _fake_powershell(monkeypatch, slow_above):
    """Replace subprocess.run: a history read of more than slow_above events times out."""
    seen = []

    def run(cmd, timeout=None, **kwargs):
        script = cmd[-1]
        if "$MaxEvents" not in script:
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        budget = int(script.split("$MaxEvents = ")[1].split()[0])
        seen.append(budget)
        if budget > slow_above:
            raise subprocess.TimeoutExpired(cmd, timeout)
        return subprocess.CompletedProcess(cmd, 0, b"[]", b"")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(collect, "check_event_log_health", lambda **kwargs: {})
    return seen


def test_a_timed_out_powershell_call_exits_with_the_timeout_key_not_a_traceback(monkeypatch):
    def run(cmd, timeout=None, **kwargs):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SystemExit) as exc:
        common.run_powershell_json("Get-Date", 7)
    assert isinstance(exc.value, common.PowerShellTimeout)
    assert "powershell_timeout_seconds" in str(exc.value) and "7" in str(exc.value)


def test_a_timed_out_first_run_read_names_both_settings(monkeypatch, tmp_path):
    _fake_powershell(monkeypatch, slow_above=0)
    with pytest.raises(SystemExit) as exc:
        publish.read_run_events(HISTORY, 60, 0, 30000, tmp_path / "budget.json")
    message = str(exc.value)
    assert "powershell_timeout_seconds" in message and "history.first_run_max_events" in message


def test_a_timed_out_incremental_read_names_its_own_budget(monkeypatch, tmp_path):
    _fake_powershell(monkeypatch, slow_above=0)
    with pytest.raises(SystemExit) as exc:
        publish.read_run_events(HISTORY, 60, 500, 0, tmp_path / "budget.json")
    assert "history.max_events_per_run" in str(exc.value)
    saved = json.loads((tmp_path / "budget.json").read_text(encoding="utf-8"))
    assert saved == {"configured": 5000, "max_events": 2500}   # H3-0: the incremental read halves too


def test_repeated_first_runs_that_time_out_still_advance_within_a_bounded_number_of_runs(
        monkeypatch, tmp_path):
    seen = _fake_powershell(monkeypatch, slow_above=2000)
    path = tmp_path / "budget.json"
    outcomes = []
    for _ in range(10):
        try:
            publish.read_run_events(HISTORY, 60, 0, 30000, path)
            outcomes.append("ok")
            break
        except SystemExit:
            outcomes.append("timeout")

    assert outcomes[-1] == "ok", f"never got through: {seen}"
    assert len(outcomes) <= 6
    assert seen == sorted(seen, reverse=True) and len(set(seen)) == len(seen), \
        f"a run repeated a budget that already failed: {seen}"


def test_a_successful_read_keeps_the_saved_budget_until_the_watermark_moves(monkeypatch, tmp_path):
    seen = _fake_powershell(monkeypatch, slow_above=20000)
    path = tmp_path / "budget.json"
    with pytest.raises(SystemExit):
        publish.read_run_events(HISTORY, 60, 0, 30000, path)
    publish.read_run_events(HISTORY, 60, 0, 30000, path)   # 15000: fits, but nothing was published
    assert path.exists()
    publish.read_run_events(HISTORY, 60, 0, 30000, path)   # the next run must not go back to 30000
    assert seen == [30000, 15000, 15000]


@pytest.fixture
def publish_world(monkeypatch, tmp_path):
    """_run_publish with a faked read: 'runs' is what the event read found."""
    state = {"runs": [], "publish_error": None}
    item = type("I", (), {"title": "x", "id": "svc", "access": "private"})()
    monkeypatch.setattr(publish, "connect_agol", lambda cfg: object())
    monkeypatch.setattr(publish, "ensure_service", lambda gis, cfg, target: item)
    monkeypatch.setattr(publish, "get_tables", lambda i: {})
    monkeypatch.setattr(publish, "check_schema_drift", lambda tables, *a: None)
    monkeypatch.setattr(publish, "check_source_sharing", lambda *a: None)
    monkeypatch.setattr(publish, "collect_everything",
                        lambda *a, **k: {"runs": state["runs"], "inventory": []})
    monkeypatch.setattr(publish, "report", lambda collected: None)

    def fake_publish(*a, **k):
        if state["publish_error"]:
            raise state["publish_error"]
        return {}

    monkeypatch.setattr(publish, "publish", fake_publish)
    monkeypatch.setattr(publish, "maybe_send_alert", lambda *a: False)
    monkeypatch.setattr(publish, "_write_collector_run_row", lambda *a, **k: None)
    budget = tmp_path / publish.FIRST_RUN_BUDGET_FILE
    budget.write_text(json.dumps({"configured": 30000, "max_events": 500}), encoding="utf-8")
    state["budget"] = budget

    def run():
        args = publish.parse_args(["--apply"])
        args.target = "prod"
        config = {"logging": {"log_dir": str(tmp_path)}}
        return publish._run_publish(args, config, tmp_path, publish.utc_now(),
                                    {"armed": False, "recorded": False})

    state["run"] = run
    return state


def test_the_saved_budget_is_cleared_once_a_publish_moves_the_watermark(publish_world):
    publish_world["runs"] = [{"max_event_record_id": 77}]
    assert publish_world["run"]() == 0
    assert not publish_world["budget"].exists()


def test_the_saved_budget_stays_when_the_read_held_no_monitored_run(publish_world):
    assert publish_world["run"]() == 0
    assert publish_world["budget"].exists()


def test_the_saved_budget_stays_when_publish_fails(publish_world):
    publish_world["runs"] = [{"max_event_record_id": 77}]
    publish_world["publish_error"] = SystemExit("synthetic AGOL outage")
    with pytest.raises(SystemExit):
        publish_world["run"]()
    assert publish_world["budget"].exists()


def test_preview_alert_never_touches_the_saved_budget(monkeypatch, tmp_path):
    _fake_powershell(monkeypatch, slow_above=0)
    monkeypatch.setattr(collect, "collect_tasks", lambda timeout, *_: [_task()])
    monkeypatch.setattr(collect, "load_descriptions", lambda: {})
    config = {"logging": {"log_dir": str(tmp_path)}}
    publish.collect_everything(config, None, max_events=30000, first_run_budget=False)
    assert not (tmp_path / publish.FIRST_RUN_BUDGET_FILE).exists()


def test_a_changed_first_run_setting_ignores_the_saved_budget(monkeypatch, tmp_path):
    path = tmp_path / "budget.json"
    path.write_text(json.dumps({"configured": 30000, "max_events": 500}), encoding="utf-8")
    seen = _fake_powershell(monkeypatch, slow_above=10 ** 9)
    publish.read_run_events(HISTORY, 60, 0, 40000, path)
    assert seen == [40000]


def test_the_saved_budget_never_drops_below_the_floor(monkeypatch, tmp_path):
    _fake_powershell(monkeypatch, slow_above=0)
    path = tmp_path / "budget.json"
    for _ in range(20):
        with pytest.raises(SystemExit):
            publish.read_run_events(HISTORY, 60, 0, 30000, path)
    assert json.loads(path.read_text(encoding="utf-8"))["max_events"] == publish.FIRST_RUN_BUDGET_FLOOR


def test_a_corrupt_budget_file_is_ignored(monkeypatch, tmp_path):
    path = tmp_path / "budget.json"
    path.write_text("{not json", encoding="utf-8")
    seen = _fake_powershell(monkeypatch, slow_above=10 ** 9)
    publish.read_run_events(HISTORY, 60, 0, 30000, path)
    assert seen == [30000]


def test_collect_everything_defers_a_read_timeout_so_inventory_and_alerts_still_go_out(
        monkeypatch, tmp_path):
    _fake_powershell(monkeypatch, slow_above=0)
    monkeypatch.setattr(collect, "collect_tasks", lambda timeout, *_: [_task()])
    monkeypatch.setattr(collect, "load_descriptions", lambda: {})
    config = {"logging": {"log_dir": str(tmp_path)}}

    collected = publish.collect_everything(config, None, max_events=30000)

    assert [r["task_name"] for r in collected["inventory"]] == ["Parcel Refresh"]
    assert isinstance(collected["event_log_error"], common.PowerShellTimeout)
    assert "powershell_timeout_seconds" in str(collected["event_log_error"])
    saved = json.loads((tmp_path / publish.FIRST_RUN_BUDGET_FILE).read_text(encoding="utf-8"))
    assert saved == {"configured": 30000, "max_events": 15000}
