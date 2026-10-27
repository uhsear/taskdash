"""H2-3: `taskdash collect --retire-host NAME` and the heartbeat's N-day host filter.

Every service call is a fake and nothing signs in. Arcade cannot run here, so the heartbeat checks
read the generated source (same pattern as test_phase0_dashboard.py).
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

import taskdash_common as common
import taskdash_publish as publish
from test_phase0_dashboard import _build, dash

OLD, KEPT = "OLD-HOST", "KEPT-HOST"


class HostTable:
    """Rows filtered by `host_name = '...'`; records every delete and refuses to write anything else."""

    def __init__(self, rows):
        self.rows, self.edits = rows, []
        self.properties = SimpleNamespace(objectIdField="OBJECTID", maxRecordCount=1000)

    def _matching(self, where):
        match = re.fullmatch(r"host_name = '(.*)'", where)
        return [r for r in self.rows if match and r["host_name"] == match.group(1)]

    def query(self, *, where="1=1", out_fields="*", result_offset=0, return_count_only=False, **kw):
        rows = self._matching(where)
        if return_count_only:
            return len(rows)
        names = out_fields.split(",")
        return SimpleNamespace(features=[SimpleNamespace(attributes={k: r.get(k) for k in names})
                                         for r in rows[result_offset:]])

    def edit_features(self, **kw):
        self.edits.append(kw)
        gone = {int(o) for o in kw["deletes"].split(",")}
        self.rows = [r for r in self.rows if r["OBJECTID"] not in gone]
        return {"deleteResults": [{"objectId": o, "success": True} for o in gone]}


def _rows(*hosts):
    return [{"OBJECTID": i, "host_name": h} for i, h in enumerate(hosts, start=1)]


@pytest.fixture
def tables():
    return {
        publish.TABLE_INVENTORY: HostTable(_rows(OLD, KEPT, OLD, KEPT, OLD)),
        publish.TABLE_RUNS: HostTable(_rows(OLD, OLD, KEPT)),
        publish.TABLE_METRICS: HostTable(_rows(OLD)),
        publish.TABLE_TASK_CHANGES: HostTable(_rows(KEPT)),
        publish.TABLE_COLLECTOR_RUNS: HostTable(_rows(OLD, KEPT, KEPT)),
    }


def _inventory_hosts(tables):
    return sorted(r["host_name"] for r in tables[publish.TABLE_INVENTORY].rows)


# ------------------------------------------------------------------ check mode and --apply

def test_check_mode_counts_every_table_and_deletes_nothing(tables, capsys):
    assert publish.retire_host(tables, OLD, apply=False) == 3
    out = capsys.readouterr().out
    assert f"{publish.TABLE_INVENTORY}: 3 row(s)" in out and f"{publish.TABLE_RUNS}: 2 row(s)" in out
    assert f"{publish.TABLE_METRICS}: 1 row(s)" in out and f"{publish.TABLE_TASK_CHANGES}: 0 row(s)" in out
    assert f"{publish.TABLE_COLLECTOR_RUNS}: 1 row(s)" in out and "DRY RUN - nothing written" in out
    assert all(t.edits == [] for t in tables.values())


def test_apply_deletes_only_that_hosts_inventory_rows(tables):
    assert publish.retire_host(tables, OLD, apply=True, chunk_size=2) == 3
    assert _inventory_hosts(tables) == [KEPT, KEPT]
    assert len(tables[publish.TABLE_INVENTORY].edits) == 2  # chunk_size 2 over 3 rows


def test_apply_leaves_history_rows_in_place(tables):
    history = [t for n, t in tables.items() if n != publish.TABLE_INVENTORY]
    before = [len(t.rows) for t in history]
    publish.retire_host(tables, OLD, apply=True)
    assert [len(t.rows) for t in history] == before and all(t.edits == [] for t in history)


def test_a_host_with_no_inventory_rows_deletes_nothing_and_says_how_to_check(tables, capsys):
    assert publish.retire_host(tables, "NO-SUCH-HOST", apply=True) == 0
    assert all(t.edits == [] for t in tables.values())
    assert "Check the spelling" in capsys.readouterr().out


def test_a_quote_in_the_name_cannot_widen_the_filter(tables):
    assert publish.retire_host(tables, "x' OR '1'='1", apply=True) == 0
    assert len(tables[publish.TABLE_INVENTORY].rows) == 5


def test_a_service_without_the_history_tables_still_retires(tables):
    del tables[publish.TABLE_COLLECTOR_RUNS], tables[publish.TABLE_TASK_CHANGES]
    assert publish.retire_host(tables, OLD, apply=True) == 3


# ------------------------------------------------------------------ refusals

def _config(host=KEPT):
    return {"host_name_override": host, "arcgis": {"item_id": "ITEM"}}


def _args(name, apply=False):
    return SimpleNamespace(retire_host=name, apply=apply, target="prod")


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **kw):
        raise AssertionError("a refused name must not reach the portal")
    monkeypatch.setattr(publish, "connect_agol", refuse)


@pytest.mark.parametrize("name", [KEPT, KEPT.lower(), f"  {KEPT} "])
def test_own_host_is_refused_before_any_portal_call(no_network, name):
    with pytest.raises(SystemExit, match="Stop the collector on that computer first"):
        publish.run_retire_host(_config(), _args(name, apply=True))


@pytest.mark.parametrize("name", ["", "   ", "*", "OLD-*", "%", "OLD%", "?LD-HOST"])
def test_empty_and_wildcard_names_are_refused_before_any_portal_call(no_network, name):
    with pytest.raises(SystemExit, match="exact computer name"):
        publish.run_retire_host(_config(), _args(name, apply=True))


def test_run_retire_host_counts_through_the_service(monkeypatch, tables, capsys):
    monkeypatch.setattr(publish, "connect_agol", lambda config: object())
    monkeypatch.setattr(publish, "ensure_service", lambda gis, config, target: object())
    monkeypatch.setattr(publish, "get_tables", lambda item: tables)
    assert publish.run_retire_host(_config(), _args(OLD)) == 0
    assert tables[publish.TABLE_INVENTORY].edits == []
    assert publish.run_retire_host(_config(), _args(OLD, apply=True)) == 0
    assert _inventory_hosts(tables) == [KEPT, KEPT]


def test_a_service_without_an_inventory_table_names_the_fix(monkeypatch):
    monkeypatch.setattr(publish, "connect_agol", lambda config: object())
    monkeypatch.setattr(publish, "ensure_service", lambda gis, config, target: object())
    monkeypatch.setattr(publish, "get_tables", lambda item: {})
    with pytest.raises(SystemExit, match="has no TaskInventory table"):
        publish.run_retire_host(_config(), _args(OLD))


# ------------------------------------------------------------------ command line

def test_retire_host_runs_alone_and_its_name_may_be_empty():
    assert publish.parse_args(["--retire-host", OLD]).retire_host == OLD
    assert publish.parse_args(["--retire-host", ""]).retire_host == ""
    assert publish.parse_args([]).retire_host is None
    for other in ("--migrate", "--scrub-history", "--refresh-metrics", "--preview-alert"):
        with pytest.raises(SystemExit):
            publish.parse_args(["--retire-host", OLD, other])


def test_retire_host_locks_only_with_apply(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(publish, "run_retire_host", lambda config, args: seen.append(args.apply) or 0)
    monkeypatch.setattr(publish, "_run_under_lock", lambda log_dir, action: seen.append("lock") or action())
    for argv in (["--retire-host", OLD], ["--retire-host", OLD, "--apply"]):
        assert publish._main_dispatch(publish.parse_args(argv), {}, tmp_path) == 0
    assert seen == [False, "lock", True]


def test_retire_host_apply_never_pings_the_hourly_dead_mans_switch(monkeypatch, tmp_path):
    pings = []
    monkeypatch.setattr(publish, "load_config", lambda path=None, defer_warnings=False: ({}, []))
    monkeypatch.setattr(publish, "resolve_log_dir", lambda config: tmp_path)
    monkeypatch.setattr(publish, "setup_logging", lambda log_dir, name: tmp_path / "x.log")
    monkeypatch.setattr(publish, "cleanup_old_logs", lambda log_dir, days: None)
    monkeypatch.setattr(publish, "ping_healthcheck", lambda cfg, suffix: pings.append(suffix))
    monkeypatch.setattr(publish, "_main_dispatch", lambda args, config, log_dir: 0)
    assert publish.main(["--retire-host", OLD, "--apply"]) == 0
    assert pings == []


# ------------------------------------------------------------------ heartbeat N-day filter

def _heartbeat_script(config=None):
    definition = _build(config)
    return next(a["script"] for a in definition["arcadeDataSourceItems"] if "heartbeat" in a["name"])


def test_heartbeat_script_leaves_out_hosts_silent_for_7_days_by_default():
    script = _heartbeat_script()
    runs_layer = dash.DEFAULT_LAYER_IDS[common.TABLE_COLLECTOR_RUNS]
    assert f"{runs_layer}," in script and '"started_utc"' in script
    assert 'DateAdd(Now(), -10080, "minutes")' in script


def test_heartbeat_script_filters_the_runs_on_the_server_by_the_cutoff():
    # The whole table was paged and sorted in Arcade before; now the server returns only recent rows.
    script = _heartbeat_script()
    assert 'Filter(runs, "started_utc > @cutoff")' in script
    assert "OrderBy(" not in script.split("var live")[1].split("var ms")[0]
    assert "break" not in script


def test_heartbeat_script_uses_the_configured_day_count():
    assert '-4320, "minutes")' in _heartbeat_script({"scheduler": {"heartbeat_ignore_after_days": 3}})
    assert '-720, "minutes")' in _heartbeat_script({"scheduler": {"heartbeat_ignore_after_days": 0.5}})
    assert '-1, "minutes")' in _heartbeat_script({"scheduler": {"heartbeat_ignore_after_days": 0.0001}})


@pytest.mark.parametrize("value", [0, -1, "7", True, float("nan")])
def test_a_bad_ignore_after_days_fails_with_the_fix_in_the_message(value):
    with pytest.raises(SystemExit, match="heartbeat_ignore_after_days must be a number above 0"):
        _build({"scheduler": {"heartbeat_ignore_after_days": value}})


def test_the_loader_knows_the_new_key_as_a_number():
    assert "scheduler.heartbeat_ignore_after_days" in common._CONFIG_NUMBER_KEYS
    assert common._validate_config({"scheduler": {"heartbeat_ignore_after_days": "7"}})
    assert common._validate_config({"scheduler": {"heartbeat_ignore_after_days": 7}}) == []


def test_apply_refuses_a_host_whose_collector_still_runs(tables, capsys):
    # A run 5 minutes ago: the old computer still publishes, so its next run would add the rows back.
    started = (publish.utc_now().timestamp() - 300) * 1000
    tables[publish.TABLE_COLLECTOR_RUNS].rows[0]["started_utc"] = started
    assert publish.retire_host(tables, OLD, apply=False) == 3
    assert "WARNING: a collector under 'OLD-HOST' ran within the last 120 min" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="Cannot retire 'OLD-HOST'"):
        publish.retire_host(tables, OLD, apply=True)
    assert _inventory_hosts(tables).count(OLD) == 3

