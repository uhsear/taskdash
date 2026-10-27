"""H4-8: stored keys fold case, and alert state is keyed on task_full_name.

A task renamed only in case must update its stored row, not add a second one. An alert_state.json
written before the upgrade (keyed on the task's leaf name) must be re-keyed on read, so the upgrade
neither re-sends nor drops an alert. No network: AGOL tables are fakes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import taskdash_publish as publish

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
FULL = "\\Jobs\\Nightly Sync"
LEAF = "Nightly Sync"


# ---------------------------------------------------------------- stored keys

class _Table:
    properties = SimpleNamespace(objectIdField="OBJECTID")

    def __init__(self):
        self.calls = []

    def edit_features(self, **kw):
        self.calls.append(kw)
        return {"addResults": [{"success": True}] * len(kw.get("adds") or []),
                "updateResults": [{"success": True}] * len(kw.get("updates") or [])}


def test_a_case_only_rename_updates_the_stored_row(monkeypatch):
    stored = [{"OBJECTID": 7, "run_key": "HOST|\\Jobs\\Nightly Sync|rec1"}]
    monkeypatch.setattr(publish, "query_all_rows", lambda table, *, where="1=1", out_fields="*", page=None: stored)
    table = _Table()
    counts = publish.upsert_by_key(table, [{"run_key": "HOST|\\Jobs\\NIGHTLY SYNC|rec1"}], ["run_key"],
                                   chunk_size=200, label="TaskRuns")
    assert counts == {"added": 0, "updated": 1, "deleted": 0}
    assert table.calls[0]["updates"][0]["attributes"]["OBJECTID"] == 7
    assert table.calls[0]["adds"] is None


def test_a_case_only_rename_updates_a_multi_field_row(monkeypatch):
    stored = [{"OBJECTID": 3, "host_name": "HOST-A", "task_full_name": FULL}]
    monkeypatch.setattr(publish, "query_all_rows", lambda table, *, where="1=1", out_fields="*", page=None: stored)
    table = _Table()
    row = {"host_name": "HOST-A", "task_full_name": FULL.upper()}
    counts = publish.upsert_by_key(table, [row], ["host_name", "task_full_name"], chunk_size=200, label="X")
    assert counts["updated"] == 1 and counts["added"] == 0


def test_sync_inventory_updates_a_case_only_rename(monkeypatch):
    stored = [{"OBJECTID": 5, "task_full_name": FULL}, {"OBJECTID": 6, "task_full_name": "\\Jobs\\Other"}]
    monkeypatch.setattr(publish, "query_all_rows", lambda table, *, where="1=1", out_fields="*", page=None: stored)
    table = _Table()
    table.edit_features = lambda **kw: table.calls.append(kw) or {
        "addResults": [], "updateResults": [{"success": True}] * len(kw.get("updates") or [])}
    rows = [{"task_full_name": FULL.upper()}, {"task_full_name": "\\Jobs\\Other"}]
    counts = publish.sync_inventory(table, rows, "H", chunk_size=200, label="TaskInventory")
    assert counts == {"added": 0, "updated": 2, "deleted": 0}
    assert table.calls[0]["adds"] is None and table.calls[0]["deletes"] is None


def test_fetch_keys_folds_the_keys_it_returns(monkeypatch):
    monkeypatch.setattr(publish, "query_all_rows", lambda table, *, where="1=1", out_fields="*", page=None:
                        [{"OBJECTID": 1, "run_key": "AbC"}])
    assert publish.fetch_keys(object(), [{"run_key": "abc"}], ["run_key"], "OBJECTID") == {("abc",): 1}


def _stored(full):
    return {"task_full_name": full, "task_name": full.rsplit("\\", 1)[-1], "definition_hash": "", "last_run_failed": 0}


def test_a_case_only_rename_is_no_added_or_removed_task():
    other = "\\Jobs\\Other"
    old = {FULL: _stored(FULL), other: _stored(other)}
    new = [_stored(FULL.upper()), _stored(other)]
    assert publish.detect_task_changes(old, new, "H", "2026-10-22T12:00:00+00:00") == []


def test_a_case_only_rename_still_reports_a_real_change_under_the_new_name():
    old = {FULL: _stored(FULL)}
    new = dict(_stored(FULL.upper()), last_run_failed=1)
    changes = publish.detect_task_changes(old, [new], "H", "2026-10-22T12:00:00+00:00")
    assert [(c["change_kind"], c["task_full_name"]) for c in changes] == [("became_failing", FULL.upper())]


def test_a_task_that_is_really_gone_is_still_removed_under_its_stored_name():
    changes = publish.detect_task_changes({FULL: _stored(FULL)}, [_stored("\\Jobs\\Other")], "H",
                                          "2026-10-22T12:00:00+00:00")
    assert sorted((c["change_kind"], c["task_full_name"]) for c in changes) == [
        ("task_added", "\\Jobs\\Other"), ("task_removed", FULL)]


# ---------------------------------------------------------------- alert state

def _task(name=LEAF, full=FULL, failed=True):
    return {"task_name": name, "task_full_name": full, "last_run_failed": failed,
            "last_result_text": "boom", "latest_log_path": ""}


def test_alert_keys_use_the_full_task_name():
    inventory = [_task(), _task(full="\\Other\\Nightly Sync")]
    assert publish.alert_signature(inventory) == [f"FAILED: {FULL}", "FAILED: \\Other\\Nightly Sync"]


def test_the_email_names_the_task_by_its_short_name():
    config = {"alerting": {}, "arcgis": {"dashboard_id": "D"}}
    state = {"signature": [f"FAILED: {FULL}", "FAILED: \\Jobs\\Gone Task"]}
    alert = publish.compose_alert({"host_name": "H", "inventory": [_task(), _task("Other", "\\Jobs\\Other")]},
                                  config, state, NOW)
    assert f"FAILED: {LEAF} -- boom" in alert["body"]
    assert FULL not in alert["body"]
    assert "FAILED: \\Jobs\\Gone Task" in alert["body"]


def test_a_task_without_a_full_name_falls_back_to_its_name():
    assert publish.alert_signature([{"task_name": LEAF, "last_run_failed": True}]) == [f"FAILED: {LEAF}"]


def test_old_state_is_rekeyed_on_the_full_name():
    old = {"signature": [f"FAILED: {LEAF}"],
           "problem_last_alert_utc": {f"FAILED: {LEAF}": "2026-10-22T10:00:00+00:00"},
           "problem_first_seen_utc": {f"FAILED: {LEAF}": "2026-10-21T10:00:00+00:00"},
           "digest_pending": [f"FAILED: {LEAF}"], "last_heartbeat_utc": "2026-10-22T01:00:00+00:00"}
    new = publish.rekey_alert_state(old, [_task()])
    key = f"FAILED: {FULL}"
    assert new["signature"] == [key]
    assert new["problem_last_alert_utc"] == {key: "2026-10-22T10:00:00+00:00"}
    assert new["problem_first_seen_utc"] == {key: "2026-10-21T10:00:00+00:00"}
    assert new["digest_pending"] == [key]
    assert new["last_heartbeat_utc"] == old["last_heartbeat_utc"]
    assert old["signature"] == [f"FAILED: {LEAF}"]  # the input is not changed


def test_old_state_for_a_task_no_longer_failing_keeps_its_key():
    old = {"signature": [f"FAILED: {LEAF}"]}
    assert publish.rekey_alert_state(old, [_task(failed=False)])["signature"] == [f"FAILED: {LEAF}"]


def test_a_leaf_name_shared_by_two_tasks_is_not_rekeyed():
    # The old key belonged to the task that recovered; the same-named task elsewhere newly fails.
    other = "\\Other\\Nightly Sync"
    old = {"signature": [f"FAILED: {LEAF}"], "problem_last_alert_utc": {f"FAILED: {LEAF}": "x"}}
    inventory = [_task(failed=False), _task(full=other)]
    new = publish.rekey_alert_state(old, inventory)
    assert new["signature"] == [f"FAILED: {LEAF}"]
    assert new["problem_last_alert_utc"] == {f"FAILED: {LEAF}": "x"}
    assert set(publish.alert_signature(inventory)) != set(new["signature"])


def test_a_shared_leaf_name_still_mails_the_new_failure(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr("taskdash_common.load_mail_settings", lambda cfg: {"smtp server": "x", "from address": "a@b.c"})
    monkeypatch.setattr("taskdash_common.send_email", lambda mail, to, subject, body: sent.append(subject) or True)
    monkeypatch.setattr(publish, "utc_now", lambda: NOW)
    config = {"alerting": {"enabled": True, "to": ["a@b.c"]}, "arcgis": {"dashboard_id": "D"}}
    logs = tmp_path / "logs"
    logs.mkdir()
    old = {"signature": [f"FAILED: {LEAF}"], "problem_last_alert_utc": {f"FAILED: {LEAF}": "2026-10-22T11:00:00+00:00"}}
    (logs / "alert_state.json").write_text(json.dumps(old), encoding="utf-8")
    collected = {"host_name": "H", "inventory": [_task(failed=False), _task(full="\\Other\\Nightly Sync")]}
    assert publish.maybe_send_alert(collected, config, logs) is True
    assert len(sent) == 1


def test_already_rekeyed_state_is_not_changed():
    state = {"signature": [f"FAILED: {FULL}"], "problem_last_alert_utc": {f"FAILED: {FULL}": "x"}}
    assert publish.rekey_alert_state(state, [_task()]) == state


def test_no_duplicate_alert_after_the_upgrade(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr("taskdash_common.load_mail_settings", lambda cfg: {"smtp server": "x", "from address": "a@b.c"})
    monkeypatch.setattr("taskdash_common.send_email", lambda mail, to, subject, body: sent.append(subject) or True)
    monkeypatch.setattr(publish, "utc_now", lambda: NOW)
    config = {"alerting": {"enabled": True, "to": ["a@b.c"], "realert_hours": 24}, "arcgis": {"dashboard_id": "D"}}
    logs = tmp_path / "logs"
    logs.mkdir()
    old = {"signature": [f"FAILED: {LEAF}"],
           "problem_last_alert_utc": {f"FAILED: {LEAF}": "2026-10-22T10:00:00+00:00"}}
    (logs / "alert_state.json").write_text(json.dumps(old), encoding="utf-8")
    collected = {"host_name": "H", "inventory": [_task()]}
    assert publish.maybe_send_alert(collected, config, logs) is False
    assert sent == []


def test_recovery_after_the_upgrade_still_alerts_once(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr("taskdash_common.load_mail_settings", lambda cfg: {"smtp server": "x", "from address": "a@b.c"})
    monkeypatch.setattr("taskdash_common.send_email", lambda mail, to, subject, body: sent.append(subject) or True)
    monkeypatch.setattr(publish, "utc_now", lambda: NOW)
    config = {"alerting": {"enabled": True, "to": ["a@b.c"]}, "arcgis": {"dashboard_id": "D"}}
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "alert_state.json").write_text(json.dumps({"signature": [f"FAILED: {LEAF}"]}), encoding="utf-8")
    collected = {"host_name": "H", "inventory": [_task(failed=False)]}
    assert publish.maybe_send_alert(collected, config, logs) is True
    assert len(sent) == 1
    state = json.loads((logs / "alert_state.json").read_text(encoding="utf-8"))
    assert state["signature"] == []
