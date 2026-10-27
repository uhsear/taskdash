"""H2-2: `host_previous_names` -- a computer rename adopts the old name's TaskInventory rows.

Every service call is a fake and nothing signs in. History tables keep the old name on purpose.
"""

from __future__ import annotations

import re
import socket
from types import SimpleNamespace

import pytest

import taskdash_common as common
import taskdash_publish as publish

NEW, OLD, OLDER, OTHER = "NEW-HOST", "OLD-HOST", "OLDER-HOST", "OTHER-HOST"


class InventoryTable:
    """Stateful TaskInventory fake: host_name filter, add, update (merge) and delete by OID."""

    def __init__(self, rows):
        self.rows, self.edits = [dict(r) for r in rows], []
        self.properties = SimpleNamespace(objectIdField="OBJECTID", maxRecordCount=1000)

    def query(self, *, where="1=1", out_fields="*", result_offset=0, **kw):
        match = re.fullmatch(r"host_name = '(.*)'", where)
        rows = [r for r in self.rows if match and r["host_name"] == match.group(1).replace("''", "'")]
        names = out_fields.split(",")
        return SimpleNamespace(features=[SimpleNamespace(attributes={k: r.get(k) for k in names})
                                         for r in rows[result_offset:]])

    def edit_features(self, adds=None, updates=None, deletes=None, **kw):
        self.edits.append({"adds": adds, "updates": updates, "deletes": deletes})
        next_oid = max((r["OBJECTID"] for r in self.rows), default=0) + 1
        for item in adds or []:
            self.rows.append({**item["attributes"], "OBJECTID": next_oid})
            next_oid += 1
        for item in updates or []:
            target = next(r for r in self.rows if r["OBJECTID"] == item["attributes"]["OBJECTID"])
            target.update(item["attributes"])
        gone = {int(o) for o in (deletes or "").split(",") if o}
        self.rows = [r for r in self.rows if r["OBJECTID"] not in gone]
        ok = [{"success": True}]
        return {"addResults": ok * len(adds or []), "updateResults": ok * len(updates or []),
                "deleteResults": ok * len(gone)}


def _row(oid, host, task):
    return {"OBJECTID": oid, "host_name": host, "task_full_name": task}


def _pairs(table):
    return sorted((r["host_name"], r["task_full_name"]) for r in table.rows)


# ------------------------------------------------------------------ the adoption step

def test_old_name_rows_move_to_the_current_name_and_are_counted(capsys):
    table = InventoryTable([_row(1, OLD, "a"), _row(2, OLD, "b"), _row(3, OTHER, "a")])
    assert publish.adopt_previous_hosts(table, NEW, [OLD], {"a", "b"}) == 2
    assert _pairs(table) == [(NEW, "a"), (NEW, "b"), (OTHER, "a")]
    assert f"adopted 2 row(s) from {OLD}" in capsys.readouterr().out


def test_a_task_already_stored_under_the_current_name_is_not_duplicated():
    table = InventoryTable([_row(1, OLD, "a"), _row(2, OLD, "b"), _row(3, NEW, "a")])
    assert publish.adopt_previous_hosts(table, NEW, [OLD], {"a", "b"}) == 1
    assert _pairs(table) == [(NEW, "a"), (NEW, "b")]


def test_two_old_names_holding_one_task_leave_one_row():
    table = InventoryTable([_row(1, OLD, "a"), _row(2, OLDER, "a"), _row(3, OLDER, "c")])
    assert publish.adopt_previous_hosts(table, NEW, [OLD, OLDER], {"a", "c"}) == 2
    assert _pairs(table) == [(NEW, "a"), (NEW, "c")]


def test_old_name_tasks_this_run_did_not_collect_are_neither_adopted_nor_deleted():
    # Adopting them would make them stale rows of this host, which sync_inventory then deletes
    # or refuses over. A task that is also stored under the new name is left alone too.
    table = InventoryTable([_row(1, OLD, "a"), _row(2, OLD, "gone"), _row(3, OLD, "dup"), _row(4, NEW, "dup")])
    assert publish.adopt_previous_hosts(table, NEW, [OLD], {"a"}) == 1
    assert table.edits == [{"adds": None, "updates": [{"attributes": {"OBJECTID": 1, "host_name": NEW}}],
                            "deletes": None}]
    assert _pairs(table) == [(NEW, "a"), (NEW, "dup"), (OLD, "dup"), (OLD, "gone")]


def test_a_name_with_no_rows_adopts_nothing(capsys):
    table = InventoryTable([_row(1, NEW, "a")])
    assert publish.adopt_previous_hosts(table, NEW, [OLD], {"a"}) == 0
    assert table.edits == [] and "adopted 0 row(s)" in capsys.readouterr().out


def test_the_current_name_in_the_list_is_never_deleted_as_a_duplicate():
    table = InventoryTable([_row(1, NEW, "a")])
    assert publish.adopt_previous_hosts(table, NEW, [NEW.lower()], {"a"}) == 0
    assert _pairs(table) == [(NEW, "a")]


def test_a_quote_in_an_old_name_cannot_widen_the_filter():
    table = InventoryTable([_row(1, OLD, "a")])
    assert publish.adopt_previous_hosts(table, NEW, ["x' OR '1'='1"], {"a"}) == 0
    assert _pairs(table) == [(OLD, "a")]


class RunsTable:
    """CollectorRuns fake: answers the newest-run query with each host's latest started_utc."""

    def __init__(self, started_by_host):
        self.started = started_by_host

    def query(self, *, where, **kw):
        host = re.fullmatch(r"host_name = '(.*)'", where).group(1)
        started = self.started.get(host)
        features = [] if started is None else [SimpleNamespace(attributes={"started_utc": started})]
        return SimpleNamespace(features=features)


def _minutes_ago(minutes):
    return (publish.utc_now().timestamp() - minutes * 60) * 1000


def test_an_old_name_with_a_recent_collector_run_is_left_alone_with_a_warning(capsys):
    table = InventoryTable([_row(1, OLD, "a"), _row(2, OLD, "b"), _row(3, NEW, "a")])
    runs = RunsTable({OLD: _minutes_ago(30)})
    assert publish.adopt_previous_hosts(table, NEW, [OLD], {"a", "b"}, runs_table=runs) == 0
    assert table.edits == []
    assert _pairs(table) == [(NEW, "a"), (OLD, "a"), (OLD, "b")]
    out = capsys.readouterr().out
    assert f"WARNING: host_previous_names lists '{OLD}'" in out and "adopted 0 row(s)" in out


def test_an_old_name_silent_for_more_than_two_intervals_is_adopted():
    table = InventoryTable([_row(1, OLD, "a")])
    runs = RunsTable({OLD: _minutes_ago(121)})
    assert publish.adopt_previous_hosts(table, NEW, [OLD], {"a"}, runs_table=runs) == 1
    assert _pairs(table) == [(NEW, "a")]


def test_the_interval_sets_how_recent_counts_as_live():
    table = InventoryTable([_row(1, OLD, "a")])
    runs = RunsTable({OLD: _minutes_ago(45)})
    assert publish.adopt_previous_hosts(table, NEW, [OLD], {"a"}, runs_table=runs, interval_minutes=15) == 1


def test_only_the_live_name_is_skipped_when_two_are_listed():
    table = InventoryTable([_row(1, OLD, "a"), _row(2, OLDER, "b")])
    runs = RunsTable({OLD: _minutes_ago(5), OLDER: None})
    assert publish.adopt_previous_hosts(table, NEW, [OLD, OLDER], {"a", "b"}, runs_table=runs) == 1
    assert _pairs(table) == [(NEW, "b"), (OLD, "a")]


# ------------------------------------------------------------------ inside publish()

@pytest.fixture
def run_publish(monkeypatch):
    """publish() with the inventory fake, the real adopt step and the real sync_inventory."""
    history = []
    monkeypatch.setattr(publish, "upsert_by_key", lambda t, rows, keys, **k: history.append(t) or
                        {"added": 0, "updated": 0})
    monkeypatch.setattr(publish, "summarize_runs_from_agol", lambda *a, **k: {})
    monkeypatch.setattr(publish, "fetch_other_host_counts", lambda *a, **k: [])
    monkeypatch.setattr(publish, "purge_older_than", lambda *a, **k: 0)
    monkeypatch.setattr(publish, "enrich_inventory", lambda *a, **k: None)
    monkeypatch.setattr(publish, "stamp_success_rate", lambda *a, **k: None)

    def run(config, table, tasks=("a", "b"), collector_runs=None):
        tables = {publish.TABLE_INVENTORY: table, publish.TABLE_RUNS: "runs", publish.TABLE_METRICS: "m"}
        if collector_runs is not None:
            tables[publish.TABLE_COLLECTOR_RUNS] = collector_runs
        monkeypatch.setattr(publish, "get_tables", lambda item: tables)
        collected = {"runs": [], "metrics": [], "host_name": NEW, "log_summary": {},
                     "inventory": [{"task_full_name": t, "host_name": NEW} for t in tasks]}
        return publish.publish(None, None, collected, config), history
    return run


def test_publish_with_the_key_updates_adopted_rows_instead_of_adding(run_publish, capsys):
    table = InventoryTable([_row(1, OLD, "a"), _row(2, OLD, "b"), _row(3, OTHER, "a")])
    counts, _ = run_publish({"host_previous_names": [OLD]}, table)
    assert counts["inventory_added"] == 0 and counts["inventory_updated"] == 2
    assert _pairs(table) == [(NEW, "a"), (NEW, "b"), (OTHER, "a")]
    assert "adopted 2 row(s)" in capsys.readouterr().out


def test_publish_leaves_old_name_tasks_it_did_not_collect_and_the_shrink_guard_stays_quiet(run_publish):
    # Six old-name tasks, two re-published: adopting all six would leave four stale rows, which is
    # more than half of this host's index, so sync_inventory would refuse every later run.
    table = InventoryTable([_row(1, OLD, "a"), _row(2, OLD, "b")] + [_row(10 + n, OLD, f"x{n}") for n in range(4)])
    counts, _ = run_publish({"host_previous_names": [OLD]}, table)
    assert counts["inventory_updated"] == 2 and counts["inventory_added"] == 0
    assert _pairs(table) == [(NEW, "a"), (NEW, "b")] + [(OLD, f"x{n}") for n in range(4)]


def test_publish_leaves_a_live_old_name_host_alone_and_honours_the_configured_interval(run_publish, capsys):
    table = InventoryTable([_row(1, OLD, "a"), _row(2, OLD, "b")])
    runs = RunsTable({OLD: _minutes_ago(20)})
    counts, _ = run_publish({"host_previous_names": [OLD]}, table, collector_runs=runs)
    assert counts["inventory_added"] == 2 and counts["inventory_updated"] == 0
    assert _pairs(table) == [(NEW, "a"), (NEW, "b"), (OLD, "a"), (OLD, "b")]
    assert "WARNING: host_previous_names lists" in capsys.readouterr().out
    # The same 20 min is past 2 x 5 min, so a 5 minute collector counts the old name as retired.
    table = InventoryTable([_row(1, OLD, "a"), _row(2, OLD, "b")])
    config = {"host_previous_names": [OLD], "scheduler": {"collector_interval_minutes": 5}}
    counts, _ = run_publish(config, table, collector_runs=runs)
    assert counts["inventory_updated"] == 2 and _pairs(table) == [(NEW, "a"), (NEW, "b")]


@pytest.mark.parametrize("config", [{}, {"host_previous_names": []}])
def test_without_the_key_the_old_rows_stay_and_the_new_ones_are_added(run_publish, config, capsys):
    table = InventoryTable([_row(1, OLD, "a"), _row(2, OLD, "b")])
    counts, _ = run_publish(config, table)
    assert counts["inventory_added"] == 2 and counts["inventory_updated"] == 0
    assert _pairs(table) == [(NEW, "a"), (NEW, "b"), (OLD, "a"), (OLD, "b")]
    assert "adopted" not in capsys.readouterr().out


def test_history_tables_are_not_touched_by_adoption(run_publish):
    table = InventoryTable([_row(1, OLD, "a")])
    _, history = run_publish({"host_previous_names": [OLD]}, table)
    assert history == ["runs"]  # only this pass's own TaskRuns upsert; no table is rewritten for OLD


# ------------------------------------------------------------------ config validation

def _problems(value, **extra):
    return common._validate_config({"host_previous_names": value, **extra})


def test_the_key_is_known_to_the_loader_and_the_example():
    assert "host_previous_names" in common._CONFIG_STRING_LIST_KEYS
    assert "host_previous_names" in common._CONFIG_TOP_LEVEL_KEYS


def test_a_list_of_old_names_is_valid():
    assert _problems([OLD, OLDER]) == []
    assert _problems([]) == []


@pytest.mark.parametrize("value", ["OLD-HOST", [1], [OLD, None], [{"a": 1}], {"a": 1}])
def test_non_string_values_are_rejected(value):
    assert any("host_previous_names must be a list of strings" in p for p in _problems(value))


@pytest.mark.parametrize("value", [[""], ["   "], [OLD, ""]])
def test_an_empty_name_is_rejected_with_the_fix(value):
    assert any("must not hold an empty name" in p for p in _problems(value))


def test_the_override_name_is_rejected_in_any_case():
    problems = _problems(["Task-Host"], host_name_override="TASK-HOST")
    assert any("must not list the current host name 'TASK-HOST'" in p for p in problems)


def test_the_computer_name_is_rejected_when_there_is_no_override():
    problems = _problems([f" {socket.gethostname().lower()} "])
    assert any("must not list the current host name" in p for p in problems)


def test_the_old_name_is_fine_next_to_a_different_override():
    assert _problems([OLD], host_name_override=NEW) == []
