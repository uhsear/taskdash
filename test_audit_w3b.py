"""Audit wave 3, lane b: several computers and first runs. Definitions are built in the test and
Arcade cannot run here, so Arcade checks read the generated source (same pattern as
test_phase0_dashboard.py and test_h2_retire_host.py).
"""

from __future__ import annotations

from test_phase0_dashboard import _build, _by_name, dash


def _source(name_part, config=None):
    definition = _build(config)
    return next(a for a in definition["arcadeDataSourceItems"] if name_part in a["name"])


# ------------------------------------------------------------------ TD-049, TD-053: heartbeat

def test_heartbeat_keeps_a_silent_computer_in_the_oldest_time_and_counts_it():
    script = _source("heartbeat")["script"]
    loop = script.split('for (var f in Filter(fs, "collected_at_utc IS NOT NULL"))')[1]
    # Every computer sets the oldest time; the 7-day window only decides who is silent.
    assert "if (IsEmpty(ms) || t < ms) { ms = t;" in loop
    assert "HasKey(live" not in loop.split("if (IsEmpty(ms)")[1]
    assert "if (!HasKey(live, key)) { silent[key] = true; }" in loop
    assert "silent_count: Count(Keys(silent))" in script


def test_heartbeat_source_returns_the_oldest_computer_name_and_declares_every_field():
    definition = _build()
    tile = _by_name(definition)["Collector Heartbeat"]
    declared = dash.arcade_source_fields(definition, tile)
    assert declared == [dash.HEARTBEAT_SOURCE_FIELD, dash.HEARTBEAT_HOST_FIELD, dash.HEARTBEAT_SILENT_FIELD]
    script = _source("heartbeat")["script"]
    assert "oldest = Text(f[\"host_name\"])" in script and "oldest_host: oldest" in script
    assert tile["datasets"][0]["outFields"] == declared


def test_heartbeat_tile_names_the_computer_and_the_silent_count():
    for name in ("Collector Heartbeat", "Heartbeat"):
        expression = _by_name(_build())[name]["expression"]
        assert 'var host = Text(DefaultValue($datapoint["oldest_host"], ""));' in expression
        assert '", 1 computer silent"' in expression and '" computers silent"' in expression
        assert "topText: host," in expression and "bottomText: state + silentText," in expression
        assert "middleText:" in expression and "attributes:" not in expression


def test_the_layer_statistic_fallback_tile_reads_no_field_the_layer_lacks():
    off = dash.heartbeat_expression("MIN_collected_at_utc")
    assert "oldest_host" not in off and "silent_count" not in off and "topText" not in off
    assert "bottomText: state," in off


def test_heartbeat_help_names_the_computer_and_how_to_retire_one():
    shows, _, todo = dash.HELP["Collector Heartbeat"]
    assert "names it" in shows and "how many are silent" in shows
    assert "`taskdash collect --retire-host`" in todo
    assert dash.HELP["Heartbeat"][0] == shows


# ------------------------------------------------------------------ TD-155: success tile

def test_success_source_leaves_out_a_silent_computer():
    source = _source("success")
    script = source["script"]
    assert source["refreshInterval"] == dash.HEARTBEAT_REFRESH_MINUTES
    assert '"lowest_success_rate_7d IS NOT NULL"' in script
    assert 'HasKey(live, Lower(Text(f["host_name"]))) && (IsEmpty(low) || v < low)' in script
    assert 'DateAdd(Now(), -10080, "minutes")' in script
    assert 'Filter(runs, "started_utc > @cutoff")' in script


def test_success_tile_reads_the_source_and_declares_its_field():
    definition = _build({"scheduler": {"heartbeat_ignore_after_days": 3}})
    tile = _by_name(definition)["7-Day Success Rate"]
    assert tile["datasets"][0]["dataSource"]["type"] == "arcadeDataSource"
    assert tile["valueType"] == "feature" and tile["valueField"] == dash.SUCCESS_SOURCE_FIELD
    assert dash.arcade_source_fields(definition, tile) == [dash.SUCCESS_SOURCE_FIELD]
    assert '-4320, "minutes")' in _source("success", {"scheduler": {"heartbeat_ignore_after_days": 3}})["script"]
    assert "silent computer" in dash.HELP["7-Day Success Rate"][0]


def test_every_source_ends_in_one_feature_with_balanced_braces():
    for source in _build()["arcadeDataSourceItems"]:
        script = source["script"]
        assert script.count("{") == script.count("}") and script.count("(") == script.count(")")


def test_the_dashboard_validates_with_the_new_sources():
    dash.validate_definition(_build())


# ------------------------------------------------------------------ TD-156: daily check names silent peers

from datetime import datetime, timedelta, timezone  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import taskdash_publish as publish  # noqa: E402

NOW = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)


class _RunsTable:
    def __init__(self, newest_by_host):
        self.rows, self.queries = newest_by_host, []

    def query(self, **kw):
        self.queries.append(kw)
        return SimpleNamespace(features=[
            SimpleNamespace(attributes={"host_name": name, "newest_start": started.timestamp() * 1000})
            for name, started in self.rows.items()])


class _InventoryTable:
    def __init__(self, *names):
        self.names, self.queries = names, []

    def query(self, **kw):
        self.queries.append(kw)
        return SimpleNamespace(features=[SimpleNamespace(attributes={"host_name": n, "inventory_rows": 4})
                                         for n in self.names])


def test_fetch_silent_peers_makes_one_grouped_query_and_keeps_only_stale_peers():
    table = _RunsTable({"B-OLD": NOW - timedelta(days=3), "C-LIVE": NOW - timedelta(minutes=20),
                        "D-OLDER": NOW - timedelta(days=9)})
    inventory = _InventoryTable("B-OLD", "C-LIVE", "D-OLDER")
    peers = publish.fetch_silent_peers(table, inventory, "A-HOME", NOW, 180)
    assert [name for name, _ in peers] == ["D-OLDER", "B-OLD"]
    assert len(table.queries) == 1 and len(inventory.queries) == 1
    query = table.queries[0]
    assert query["where"] == "host_name <> 'A-HOME'" and query["group_by_fields_for_statistics"] == "host_name"
    assert query["out_statistics"][0]["statisticType"] == "max"
    assert inventory.queries[0]["group_by_fields_for_statistics"] == "host_name"


def test_a_retired_peer_with_old_collector_runs_is_not_listed():
    # --retire-host keeps CollectorRuns rows until retention but deletes the inventory.
    table = _RunsTable({"B-RETIRED": NOW - timedelta(days=30), "D-GONE": NOW - timedelta(days=9)})
    assert [n for n, _ in publish.fetch_silent_peers(table, _InventoryTable("D-GONE"), "A-HOME", NOW, 180)] == ["D-GONE"]
    assert publish.fetch_silent_peers(table, _InventoryTable(), "A-HOME", NOW, 180) == []


def _daily(peers, config=None):
    collected = {"host_name": "A-HOME", "inventory": [], "silent_peers": peers}
    config = config or {"alerting": {"enabled": True, "to": ["a@b.c"], "heartbeat_hour": 9}}
    return publish._alert_message(collected, config, NOW, "heartbeat", [], [])[1]


def test_the_daily_check_lists_a_silent_peer_and_how_to_retire_it():
    moment = NOW - timedelta(days=3)
    body = _daily(lambda: ([("B-OLD", moment)], 180))
    assert "1 other computer(s) with no collector run in the last 180 min:" in body
    assert f"  B-OLD: last run {publish._local_stamp(moment)}" in body
    assert "`taskdash collect --retire-host <name>`" in body


def test_the_daily_check_is_unchanged_when_no_peer_is_silent_or_the_loader_is_missing():
    assert "other computer" not in _daily(lambda: ([], 180))
    assert "other computer" not in _daily(None)


def test_a_failed_peer_read_is_a_warning_line_and_the_daily_check_still_builds(capsys):
    def broken():
        raise RuntimeError("service unreachable")

    body = _daily(broken)
    assert "Checked 0 scheduled task(s)" in body and "other computer" not in body
    assert "WARNING: could not read the other computers' collector runs for the daily check" in capsys.readouterr().out


def test_only_the_daily_check_reads_peers():
    def never():
        raise AssertionError("an hourly e-mail must not query")

    collected = {"host_name": "A-HOME", "inventory": [], "silent_peers": never}
    config = {"alerting": {"enabled": True, "to": ["a@b.c"]}}
    publish._alert_message(collected, config, NOW, "changed", [], [])


# ------------------------------------------------------------------ TD-037: Not in last run share

import taskdash_metrics as metrics  # noqa: E402

BACKSLASH = chr(92)
DAY = 24 * 3600 * 1000
T0 = 1_790_000_000_000


def _series(task, key, every_days, count, host="H"):
    """TaskMetrics rows of one series, the newest at T0 + (count - 1) * every_days days."""
    return [{"host_name": host, "task_name": task, "task_full_name": f"{BACKSLASH}{task}", "metric_key": key,
             "display_name": key, "metric_value": float(i), "observed_at_utc": T0 + i * every_days * DAY}
            for i in range(count)]


def _usually_logged(rows):
    """The Arcade's share rule on compute_metric_change output: each series against its task's most-logged."""
    computed = metrics.compute_metric_change(rows)
    most = max(r["history_count"] + 1 for r in computed)
    return {r["metric_key"]: (r["history_count"] + 1) >= dash.METRIC_CHANGE_USUALLY_LOGGED * most for r in computed}


def test_a_metric_logged_every_second_run_is_not_usually_logged_when_history_is_capped():
    # 90 days of retention: 60 nightly runs, the second metric only on the even nights. Both series
    # hold more than 30 values, so the old per-series count read 31 for both (a false flag).
    nightly = _series("Sync", "records", 1, 60)
    busy_only = [r for r in _series("Sync", "busy_rows", 1, 60) if int(r["metric_value"]) % 2 == 1]
    computed = {r["metric_key"]: r for r in metrics.compute_metric_change(nightly + busy_only)}
    assert computed["records"]["history_count"] == 30
    assert computed["busy_rows"]["history_count"] == 15
    assert _usually_logged(nightly + busy_only) == {"records": True, "busy_rows": False}


def test_a_metric_logged_on_every_run_stays_usually_logged():
    rows = _series("Sync", "records", 1, 60) + _series("Sync", "rows_added", 1, 60)
    assert _usually_logged(rows) == {"records": True, "rows_added": True}


def test_a_metric_that_stopped_is_still_usually_logged_and_flagged():
    # Hourly task, newest run at hour 59. The second metric stopped 13 runs before it (gap > 12 h).
    hourly = _series("Sync", "records", 1 / 24, 60)
    stopped = [r for r in _series("Sync", "gone", 1 / 24, 60) if int(r["metric_value"]) <= 46]
    computed = {r["metric_key"]: r for r in metrics.compute_metric_change(hourly + stopped)}
    assert computed["gone"]["history_count"] == 30
    assert _usually_logged(hourly + stopped) == {"records": True, "gone": True}
    assert round((computed["records"]["latest_at_utc"] - computed["gone"]["latest_at_utc"]) / 3600000) == 13


def test_a_young_task_keeps_the_plain_earlier_count():
    rows = _series("Sync", "records", 1, 8) + [r for r in _series("Sync", "busy", 1, 8) if int(r["metric_value"]) % 2]
    computed = {r["metric_key"]: r["history_count"] for r in metrics.compute_metric_change(rows)}
    assert computed == {"records": 7, "busy": 3}


def test_the_window_is_per_task_and_per_host():
    other_task = _series("Other", "records", 3, 40)  # 30 earlier values over 90 days, a different task
    rows = _series("Sync", "records", 1, 60) + other_task + _series("Sync", "records", 1, 5, host="H2")
    counts = {(r["host_name"], r["task_name"]): r["history_count"] for r in metrics.compute_metric_change(rows)}
    assert counts == {("H", "Sync"): 30, ("H", "Other"): 30, ("H2", "Sync"): 4}


def test_the_dashboard_source_that_reads_taskmetrics_counts_the_same_window():
    script = dash.metric_change_source_script("https://example.invalid", "ITEM", 2)
    assert "Push(prevAt, Number(f[\"observed_at_utc\"]))" in script
    assert 'Push(allAt, Number(f["observed_at_utc"]))' in script
    assert f"if (ra[q] < xs && older < {dash.METRIC_CHANGE_HISTORY})" in script
    assert 'if (IsEmpty(ws) || xa[y] >= ws)' in script
    assert 'inWin[series[i]["head"]["series_key"]] + 1 >= 0.8 * most[tki]' in script
    assert script.count("{") == script.count("}") and script.count("[") == script.count("]")
    table = dash.metric_change_from_table_script("https://example.invalid", "ITEM", 5)
    assert 'series[i]["n"] + 1 >= 0.8 * most[tki]' in table


# ------------------------------------------------------------------ TD-145: About on Overview

def _tab_ids(definition, tab_name):
    def ids(element):
        if element["type"] == "itemLayoutElement":
            yield element["id"]
        for child in element.get("elements", []):
            yield from ids(child)

    tabs = definition["desktopView"]["layout"]["rootElement"]["elements"]
    return set(ids(next(t for t in tabs if t["tabName"] == tab_name)))


def test_a_short_about_text_sits_on_the_overview_tab_and_not_on_the_phone_view():
    definition = _build()
    about = _by_name(definition)[dash.OVERVIEW_ABOUT_NAME]
    assert about["type"] == "richTextWidget" and about["id"] in _tab_ids(definition, "Overview")
    assert about["id"] not in _tab_ids(definition, "Collector & Changes")
    assert all(w["name"] != dash.OVERVIEW_ABOUT_NAME for w in definition["mobileView"]["widgets"])
    assert f"TaskDash {dash.TASKDASH_VERSION}" in about["text"]
    assert "The tiles count all tasks and ignore every filter." in about["text"]
    assert about["description"]  # the definition check refuses a widget with no help


def test_the_full_about_stays_on_the_collector_tab_and_the_demo_note_reaches_both():
    definition = _build()
    assert _by_name(definition)[dash.ABOUT_NAME]["id"] in _tab_ids(definition, "Collector & Changes")
    demo = dash.overview_about_widget(demo=True)
    assert dash.DEMO_NOTE in demo["text"] and dash.DEMO_NOTE not in dash.overview_about_widget()["text"]


# ------------------------------------------------------------------ TD-192, TD-193: one name for each thing

def test_the_setting_notes_widget_is_named_and_titled_setting_notes_everywhere():
    definition = _build()
    widgets = _by_name(definition)
    assert "Config Notes" not in widgets and "Config Notes" not in dash.HELP
    assert "Setting notes" in widgets["Setting Notes"]["title"]
    blob = str(definition)
    assert "Config Notes" not in blob and "Config notes" not in blob


def test_dashboard_prose_says_computer_not_host_or_server():
    definition = _build()
    for source in definition["arcadeDataSourceItems"]:
        assert "host data" not in source["name"] and "server" not in source["name"].lower()
    for name in ("Collector Heartbeat", "Heartbeat"):
        text = (dash.HELP[name][0] + (dash.HELP[name][2] or "")).replace("--retire-host", "")
        assert "host" not in text.lower() and "server" not in text.lower()


# ------------------------------------------------------------------ TD-161: kept as documented

def test_recent_changes_help_says_an_outage_only_change_has_no_row():
    shows = dash.HELP["Recent Changes"][0]
    assert "up to 1 hour after it happened" in shows
    assert "a change that began and ended in the outage has no row" in shows
