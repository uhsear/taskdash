"""H2-5: a per-task mute is an alerting.maintenance_windows entry with "end": null.

No network, no ArcGIS Online and no SMTP: every test calls the pure helpers or fakes emit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import json

import pytest

import taskdash_common as common
from conftest import REFERENCE_PROFILES
import taskdash_publish as publish

NOW = datetime(2026, 9, 25, 14, 0, 0, tzinfo=timezone.utc)
LONG_AGO = (NOW - timedelta(days=30)).isoformat()


def _inv(name="Sync Job", **kw):
    base = {"task_name": name, "last_run_failed": 0, "is_overdue": 0, "last_result_text": "",
            "next_run_in_hours": 5.0, "latest_log_path": "", "latest_log_name": "",
            "attention_reason": ""}
    base.update(kw)
    return base


def _lines(monkeypatch):
    lines = []
    monkeypatch.setattr(publish, "emit", lines.append)
    return lines


@pytest.mark.parametrize("window", [
    {"task": "^Sync", "start": LONG_AGO, "end": None},
    {"task": "^Sync", "end": None},
])
def test_open_ended_window_is_active_and_suppresses_alerts(window):
    active = publish._active_maintenance_windows([window], NOW)
    assert active == [window]
    inventory = [_inv("Sync Job", last_run_failed=1), _inv("Other", last_run_failed=1)]
    alerted, suppressed = publish.partition_maintenance(inventory, active)
    assert alerted == ["FAILED: Other"]
    assert suppressed == ["FAILED: Sync Job"]


def test_open_ended_window_is_not_active_before_its_start():
    window = {"task": "^Sync", "start": (NOW + timedelta(days=1)).isoformat(), "end": None}
    assert publish._active_maintenance_windows([window], NOW) == []


@pytest.mark.parametrize("window", [{"task": "^Sync", "start": LONG_AGO}, {"task": "^Sync"}])
def test_an_entry_without_an_end_key_stays_ignored_as_before_h2(monkeypatch, window):
    # An older config can hold such an entry by accident. Only an explicit "end": null mutes for ever.
    lines = _lines(monkeypatch)
    assert publish._active_maintenance_windows([window], NOW) == []
    assert any(line.startswith("WARNING") and '"end": null' in line for line in lines)


@pytest.mark.parametrize("window", [
    {"task": "^Sync", "end": (NOW + timedelta(days=1)).isoformat()},
    {"task": "^Sync", "start": None, "end": (NOW + timedelta(days=1)).isoformat()},
])
def test_a_bounded_window_without_a_start_stays_ignored_as_before_h2(monkeypatch, window):
    # Before H2 the run rejected it as unparseable. It must not start muting on upgrade.
    lines = _lines(monkeypatch)
    assert publish._active_maintenance_windows([window], NOW) == []
    assert any(line.startswith("WARNING") and "unparseable" in line for line in lines)


def test_bounded_window_still_expires(monkeypatch):
    lines = _lines(monkeypatch)
    window = {"task": "^Sync", "start": LONG_AGO, "end": (NOW - timedelta(days=1)).isoformat()}
    assert publish._active_maintenance_windows([window], NOW) == []
    assert any("expired" in line for line in lines)


@pytest.mark.parametrize("bad", [{"end": "not a date"}, {"end": ""}, {"start": "not a date"}])
def test_malformed_start_or_end_is_still_rejected(monkeypatch, bad):
    lines = _lines(monkeypatch)
    window = {"task": "^Sync", "start": LONG_AGO, **bad}
    assert publish._active_maintenance_windows([window], NOW) == []
    assert any(line.startswith("WARNING") and "unparseable" in line for line in lines)


def test_muted_task_text_and_one_warning_with_the_count(monkeypatch):
    lines = _lines(monkeypatch)
    config = {"alerting": {"maintenance_windows": [{"task": "^Sync", "end": None}]}}
    inventory = [_inv("Sync A", attention_reason="Last run: Failed."), _inv("Sync B"), _inv("Other")]
    assert publish.apply_mute_notes(inventory, config, NOW) == 2
    assert inventory[0]["attention_reason"] == "Last run: Failed.; Muted (alerts off)"
    assert inventory[1]["attention_reason"] == "Muted (alerts off)"
    assert inventory[2]["attention_reason"] == ""
    warnings = [line for line in lines if line.startswith("WARNING") and "muted" in line]
    assert len(warnings) == 1 and "2 task(s)" in warnings[0]
    assert "Sync" not in warnings[0]


def test_bounded_window_shows_its_end_and_logs_no_mute_warning(monkeypatch):
    lines = _lines(monkeypatch)
    config = {"alerting": {"maintenance_windows": [
        {"task": "^Sync", "start": LONG_AGO, "end": "2026-09-26T00:00:00Z"}]}}
    inventory = [_inv("Sync A", attention_reason="Last run: Failed.")]
    assert publish.apply_mute_notes(inventory, config, NOW) == 0
    # Local time, like the e-mail: the zone name is the machine's, so build the expected text the same way.
    until = datetime(2026, 9, 26, tzinfo=timezone.utc).astimezone()
    assert inventory[0]["attention_reason"] == f"Last run: Failed.; In maintenance until {until:%Y-%m-%d %H:%M %Z}"
    assert not any("muted" in line for line in lines)


def test_a_failing_task_in_a_window_keeps_its_failure_as_the_first_reason(monkeypatch):
    # The Why cell shows only the first reason, so the note must never lead it.
    _lines(monkeypatch)
    config = {"alerting": {"maintenance_windows": [
        {"task": "^Sync", "start": LONG_AGO, "end": "2026-09-26T00:00:00Z"}]}}
    live = f"{publish._LIVE_STATE_PREFIX}Last run: Failed."
    inventory = [_inv("Sync A", attention_reason="Last run: Failed."), _inv("Sync B", attention_reason=live)]
    publish.apply_mute_notes(inventory, config, NOW)
    assert inventory[0]["attention_reason"].split("; ")[0] == "Last run: Failed."
    assert inventory[1]["attention_reason"].startswith("Running now.")


def test_a_long_reason_is_cut_before_the_note_never_through_it(monkeypatch):
    _lines(monkeypatch)
    config = {"alerting": {"maintenance_windows": [{"task": "^Sync", "end": None}]}}
    inventory = [_inv("Sync A", attention_reason="x" * 1000)]
    publish.apply_mute_notes(inventory, config, NOW)
    assert len(inventory[0]["attention_reason"]) == 1000
    assert inventory[0]["attention_reason"].endswith("; Muted (alerts off)")


def test_the_dashboard_shows_the_mute_of_a_healthy_task_in_the_why_cell():
    import taskdash_dashboard as dash
    expr = dash.task_table_expression()
    assert publish.MUTED_NOTE in expr and "In maintenance until" in expr
    # It is read from the last reason, after the real reasons, and runs before the log-error fallback.
    assert expr.index("In maintenance until") < expr.index('plural(logErrLines, "log error")')


def test_no_windows_leaves_the_inventory_alone(monkeypatch):
    lines = _lines(monkeypatch)
    inventory = [_inv("Sync A", attention_reason="Last run: Failed.")]
    assert publish.apply_mute_notes(inventory, {}, NOW) == 0
    assert inventory[0]["attention_reason"] == "Last run: Failed."
    assert lines == []


def test_the_windows_are_read_once_per_run_so_expired_and_unparseable_log_once(monkeypatch):
    lines = _lines(monkeypatch)
    config = {"alerting": {"maintenance_windows": [
        {"task": "^Sync", "start": LONG_AGO, "end": (NOW - timedelta(days=1)).isoformat()},
        {"task": "^Sync", "start": "not a date"}, {"task": "^Sync", "end": None}]}}
    collected = {"host_name": "H", "inventory": [_inv("Sync A", last_run_failed=1)]}
    windows = publish.run_maintenance_windows(collected, config, NOW)
    publish.apply_mute_notes(collected["inventory"], config, NOW, windows)
    publish.compose_alert(collected, config, {}, NOW)
    assert publish.run_maintenance_windows(collected, config, NOW) is windows
    assert sum("expired" in line for line in lines) == 1
    assert sum("unparseable" in line for line in lines) == 1
    assert collected["inventory"][0]["attention_reason"] == "Muted (alerts off)"


def _load(tmp_path, window):
    path = tmp_path / "c.json"
    config = {"rules": {"profiles": list(REFERENCE_PROFILES)},
              "alerting": {"maintenance_windows": [window]}}
    path.write_text(json.dumps(config), encoding="utf-8")
    return common.load_config(path, defer_warnings=True)


@pytest.mark.parametrize("window", [
    {"task": "^Sync"}, {"task": "^Sync", "end": None},
    {"task": "^Sync", "start": LONG_AGO, "end": "2026-09-26T00:00:00Z", "reason": "r", "_note": "n"},
])
def test_config_accepts_a_window_with_known_keys_and_no_end(tmp_path, window):
    config, warnings = _load(tmp_path, window)
    assert config["alerting"]["maintenance_windows"] == [window]
    assert warnings == []


@pytest.mark.parametrize("window", [
    {"task": "^Sync", "End": "2026-09-26T00:00:00Z"},
    {"task": "^Sync", "stop": "2026-09-26T00:00:00Z"},
    {"task": "^Sync", "end": None, "comment": "x"},
])
def test_a_misspelled_end_warns_and_never_becomes_a_mute(tmp_path, window):
    # The config still loads (an existing install must keep running). The end-less entry is skipped,
    # as it was before open windows existed, so a stray key cannot mute a task for ever.
    config, warnings = _load(tmp_path, window)
    assert len(warnings) == 1 and "maintenance_windows[0]" in warnings[0] and "ignored" in warnings[0]
    assert publish._active_maintenance_windows(config["alerting"]["maintenance_windows"], NOW) == []


@pytest.mark.parametrize("window", [
    {"task": "^Sync", "Start": (NOW + timedelta(days=1)).isoformat(), "end": (NOW + timedelta(days=2)).isoformat()},
    {"task": "^Sync", "begin": (NOW + timedelta(days=1)).isoformat(), "end": (NOW + timedelta(days=2)).isoformat()},
])
def test_a_misspelled_start_beside_a_real_end_is_not_active_now(window):
    # Without a start the window would be active now, so a future window with a typo must be skipped.
    assert publish._active_maintenance_windows([window], NOW) == []


@pytest.mark.parametrize("task", [5, True, ["Sync"]])
def test_a_non_string_task_mutes_nothing(task):
    window = {"task": task}
    assert publish._task_in_maintenance("Sync 5 True", [window]) is None
    alerted, suppressed = publish.partition_maintenance([_inv("Sync 5 True", last_run_failed=1)], [window])
    assert alerted == ["FAILED: Sync 5 True"] and suppressed == []


def test_a_stray_key_beside_a_real_end_changes_nothing(tmp_path):
    window = {"task": "^Sync", "start": LONG_AGO, "end": (NOW + timedelta(days=1)).isoformat(), "comment": "x"}
    config, warnings = _load(tmp_path, window)
    assert len(warnings) == 1 and "maintenance_windows[0]" in warnings[0]
    assert publish._active_maintenance_windows(config["alerting"]["maintenance_windows"], NOW) == [window]


@pytest.mark.parametrize("window", [{"start": LONG_AGO}, {"task": ""}, {"task": 5}])
def test_a_window_without_a_task_warns_and_does_not_stop_the_load(tmp_path, window):
    _, warnings = _load(tmp_path, window)
    assert any("maintenance_windows[0]" in w and "task" in w for w in warnings)


@pytest.mark.parametrize("stored", ["boom; Muted (alerts off)", "boom; In maintenance until 2026-09-26 00:00 UTC"])
def test_history_reason_drops_the_mute_note(stored):
    assert publish._history_reason(stored) == "boom"
    assert publish._history_reason(stored.split("; ")[-1]) == ""
    live = f"{publish._LIVE_STATE_PREFIX}{stored}"
    assert publish._history_reason(live) == "boom"


def test_muted_failures_stay_out_of_the_shared_cause_group():
    config = {"alerting": {"shared_cause_min_tasks": 2,
                           "maintenance_windows": [{"task": "^Sync B", "end": None}]}}
    collected = {"host_name": "H", "inventory": [
        _inv("Sync A", last_run_failed=1), _inv("Sync B", last_run_failed=1)]}
    alert = publish.compose_alert(collected, config, {}, NOW)
    assert alert["current"] == ["FAILED: Sync A"]
    assert alert["suppressed"] == ["FAILED: Sync B"]
    assert alert["incident"] is None
