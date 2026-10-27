"""H4-3: the Metric Change data source reads a small MetricChange table, not all of TaskMetrics.

The collector computes each series' latest and usual value after it publishes metrics. The table
is optional: a service without it publishes as before, and the dashboard keeps the old source.
No network: AGOL tables are fakes, and Arcade cannot run here, so Arcade checks read the source.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from types import SimpleNamespace

import pytest

import taskdash_common as common

common.disable_arcpy_import()

import taskdash_dashboard as dash  # noqa: E402
import taskdash_metrics as metrics  # noqa: E402
import taskdash_preflight as pf  # noqa: E402
import taskdash_publish as publish  # noqa: E402

MC = publish.TABLE_METRIC_CHANGE
HOUR = 3600 * 1000
T0 = 1_790_000_000_000  # an arbitrary epoch-millisecond origin


def metric_rows(host, task, key, values, name=None):
    """TaskMetrics rows as a query returns them: oldest first, one hour apart, dates in epoch ms."""
    return [{"host_name": host, "task_name": task, "task_full_name": f"\\Jobs\\{task}", "metric_key": key,
             "display_name": name or key, "metric_value": v, "observed_at_utc": T0 + i * HOUR}
            for i, v in enumerate(values)]


def reference(rows):
    """Metric Change as the Arcade source defines it: the newest value of each host + task +
    metric against the median of up to 30 values before it, with no usual value under 3."""
    series = {}
    for r in rows:
        if r["metric_value"] is None or r["observed_at_utc"] is None:
            continue
        series.setdefault((r["host_name"], r["task_full_name"], r["metric_key"]), []).append(r)
    out = {}
    for key, found in series.items():
        found.sort(key=lambda r: r["observed_at_utc"], reverse=True)
        head, earlier = found[0], [r["metric_value"] for r in found[1:31]]
        out["|".join(key)] = (head["metric_value"], head["observed_at_utc"], len(earlier),
                              statistics.median(earlier) if len(earlier) >= 3 else None)
    return out


SYNTHETIC = (
    metric_rows("HOST-A", "Sync", "rows_added", [10, 12, 11, 13, 9, 40])            # 5 earlier: odd
    + metric_rows("HOST-A", "Sync", "rows_failed", [0, 0, 0, 1, 0, 0, 7])           # lower-is-better key
    + metric_rows("HOST-A", "Export", "records", [100, 120, 80, 90, 0])             # 4 earlier: even
    + metric_rows("HOST-A", "Export", "once", [5])                                  # no history
    + metric_rows("HOST-A", "Export", "twice", [5, 6, 7])                           # 2 earlier: no usual
    + metric_rows("HOST-A", "Long", "daily", list(range(1, 41)))                    # 39 earlier: cap 30
    + metric_rows("HOST-B", "Sync", "rows_added", [1, 2, 3, 4])                     # same task, other host
    + metric_rows("HOST-A", "Sync", "empty_value", [None, None, None, None])        # nothing to compute
)


def by_key(computed):
    return {r["series_key"]: r for r in computed}


# ---------------------------------------------------------------- the computed values

def test_computed_values_match_the_arcade_definition_on_a_synthetic_set():
    computed = by_key(metrics.compute_metric_change(SYNTHETIC))
    expected = reference(SYNTHETIC)
    assert set(computed) == set(expected)
    for key, (latest, seen, earlier, usual) in expected.items():
        row = computed[key]
        assert (row["latest_value"], row["latest_at_utc"], row["history_count"], row["usual_value"]) == \
            (latest, seen, earlier, usual), key


def test_a_few_series_are_checked_by_hand():
    computed = by_key(metrics.compute_metric_change(SYNTHETIC))
    added = computed["HOST-A|\\Jobs\\Sync|rows_added"]
    assert (added["latest_value"], added["usual_value"]) == (40, 11)          # median of 10 12 11 13 9
    assert (added["lowest_value"], added["highest_value"]) == (9, 13)
    records = computed["HOST-A|\\Jobs\\Export|records"]
    assert records["usual_value"] == 95                                       # (90 + 100) / 2
    long_series = computed["HOST-A|\\Jobs\\Long|daily"]
    assert long_series["history_count"] == 30 and long_series["usual_value"] == 24.5  # values 10..39
    assert computed["HOST-A|\\Jobs\\Export|once"]["usual_value"] is None
    assert computed["HOST-A|\\Jobs\\Export|twice"]["lowest_value"] is None
    assert computed["HOST-B|\\Jobs\\Sync|rows_added"]["usual_value"] == 2     # hosts stay apart


def test_a_row_with_the_task_name_and_display_name_of_the_newest_value():
    rows = metric_rows("HOST-A", "Sync", "rows_added", [1, 2, 3, 4], name="Old name")
    rows[-1]["display_name"] = "New name"
    (row,) = metrics.compute_metric_change(rows)
    assert row["display_name"] == "New name" and row["task_name"] == "Sync"
    assert row["series_key"] == metrics.metric_series_key("HOST-A", "\\Jobs\\Sync", "rows_added")


def test_the_order_of_the_input_rows_does_not_matter():
    shuffled = list(reversed(SYNTHETIC))
    assert by_key(metrics.compute_metric_change(shuffled)) == by_key(metrics.compute_metric_change(SYNTHETIC))


# ---------------------------------------------------------------- the schema

def test_the_table_is_in_the_specs_with_a_unique_series_key():
    assert MC in publish.TABLE_SPECS and MC in publish.OPTIONAL_TABLES
    assert {"series_key", "host_name", "latest_value", "usual_value", "lowest_value", "highest_value",
            "history_count", "latest_at_utc"} <= {f["name"] for f in publish.metric_change_fields()}
    assert publish.TABLE_INDEXES[MC] == [{"fields": ["series_key"], "unique": True}]
    assert publish.table_definition(MC)["name"] == MC


def test_every_dashboard_field_of_the_table_is_in_its_schema():
    # The Arcade source reads these names; a typo here shows only as an empty widget.
    script = dash.metric_change_from_table_script("https://example.invalid", "ITEM", 7)
    names = {f["name"] for f in publish.metric_change_fields()}
    for field in ("series_key", "latest_value", "latest_at_utc", "usual_value", "lowest_value",
                  "highest_value", "history_count"):
        assert field in names and f'"{field}"' in script


def test_the_schema_version_moved_for_the_new_table():
    assert common.SCHEMA_VERSION == 2


# ---------------------------------------------------------------- an older service

def one_message(monkeypatch):
    messages = []
    monkeypatch.setattr(publish, "emit", lambda msg: messages.append(msg))
    return messages


def complete_table(name):
    fields = publish.TABLE_SPECS[name][0]()
    indexes = [{"fields": ",".join(s["fields"]), "isUnique": s["unique"]} for s in publish.TABLE_INDEXES[name]]
    return SimpleNamespace(url=f"https://example.invalid/{name}", manager=SimpleNamespace(),
                           properties=SimpleNamespace(fields=fields, indexes=indexes, objectIdField="OBJECTID",
                                                      maxRecordCount=2000, capabilities="Query"))


def tables_without(*missing):
    return {name: complete_table(name) for name in publish.TABLE_SPECS if name not in missing}


def test_drift_check_does_not_refuse_or_warn_for_the_missing_optional_table(monkeypatch):
    messages = one_message(monkeypatch)
    publish.check_schema_drift(tables_without(MC))  # must not raise
    assert messages == []


def test_drift_check_still_warns_for_a_missing_required_table(monkeypatch):
    messages = one_message(monkeypatch)
    publish.check_schema_drift(tables_without(MC, publish.TABLE_COLLECTOR_RUNS))
    assert len(messages) == 1 and "CollectorRuns" in messages[0] and MC not in messages[0]


def test_preflight_passes_a_service_without_the_optional_table(monkeypatch):
    item = SimpleNamespace(id="svc1", access="private", properties={"taskdash_schema_version": 1})
    monkeypatch.setattr(pf, "ensure_service", lambda gis, config, target: item)
    monkeypatch.setattr(pf, "get_tables", lambda i: tables_without(MC))
    ctx = pf.Context(SimpleNamespace(config="", offline=False),
                     config={"rules": {"profiles": []}, "arcgis": {"item_id": "svc1"}}, gis=object())
    status, message = pf.check_service(ctx)
    assert status == pf.OK and "schema version 1" in message


def test_preflight_passes_a_view_without_the_optional_table(monkeypatch):
    config = {"rules": {"profiles": []}, "arcgis": {"item_id": "svc1", "view_item_id": "view1"}}
    hidden = publish.view_hidden_fields(config)
    view_tables = tables_without(MC)
    for name, table in view_tables.items():  # a view hides what the privacy switches hide
        table.properties.fields = [f for f in table.properties.fields if f["name"] not in hidden.get(name, ())]
    monkeypatch.setattr(pf, "get_tables", lambda i: view_tables)
    monkeypatch.setattr(pf, "_view_item", lambda ctx: SimpleNamespace(id="view1"))
    monkeypatch.setattr(pf, "_view_capabilities", lambda v: "Query")
    ctx = pf.Context(SimpleNamespace(config="", offline=False), config=config, gis=object())
    status, message = pf.check_view(ctx)
    assert status == pf.OK, message


# ---------------------------------------------------------------- migrate

def test_migrate_reports_and_creates_the_missing_table(monkeypatch):
    messages = one_message(monkeypatch)
    assert publish.describe_migration(tables_without(MC)) is True
    assert messages == [f"{MC}: table not found -- would create it."]

    import arcgis.features as af

    created = []

    class Manager:
        @staticmethod
        def add_to_definition(definition):
            created.append(definition)
            return {"success": True}

    monkeypatch.setattr(af.FeatureLayerCollection, "fromitem",
                        staticmethod(lambda item: SimpleNamespace(manager=Manager())))
    monkeypatch.setattr(publish, "get_tables", lambda item: tables_without())
    stamps = []
    monkeypatch.setattr(publish, "stamp_service_version", lambda item: stamps.append(item))
    publish.apply_migration(tables_without(MC), item="ITEM")
    assert [d["tables"][0]["name"] for d in created] == [MC]
    assert stamps == ["ITEM"]


# ---------------------------------------------------------------- the collector writes it

class FakeTable:
    properties = SimpleNamespace(objectIdField="OBJECTID", maxRecordCount=2000)

    def __init__(self, stored=()):
        self.stored = list(stored)


def run_sync(monkeypatch, tables, stored_rows, stored_series=(), retention=90, stored_change=None,
             changed=True, **extra):
    """Run sync_metric_change with fakes; returns (upserts, deletes, purges). stored_change
    replaces the MetricChange rows the fake service holds (full rows, with OBJECTID)."""
    upserts, deletes, purges = [], [], []

    def fake_query(table, *, where="1=1", out_fields="*", page=None):
        if table is tables[publish.TABLE_METRICS]:
            return list(stored_rows)
        if stored_change is not None:
            return list(stored_change)
        return [{"OBJECTID": i, "series_key": k} for i, k in enumerate(stored_series, start=100)]

    monkeypatch.setattr(publish, "query_all_rows", fake_query)
    monkeypatch.setattr(publish, "upsert_by_key", lambda table, rows, key_fields, **kw: upserts.append(
        (rows, list(key_fields))) or {"added": len(rows), "updated": 0, "deleted": 0})
    monkeypatch.setattr(publish, "apply_edits_chunked", lambda table, **kw: deletes.append(kw["deletes"]) or {})
    monkeypatch.setattr(publish, "purge_older_than", lambda table, field, days, label="": purges.append(
        (field, days)) or 0)
    publish.sync_metric_change(tables, "HOST-A", "2026-10-22T00:00:00+00:00", 200, retention, changed=changed)
    return upserts, deletes, purges


def tables_with_metric_change(host_rows=0):
    change = FakeTable()
    change.query = lambda where="1=1", return_count_only=False: host_rows
    return {publish.TABLE_METRICS: FakeTable(), MC: change}


def stored_as_service_returns(rows):
    return [{**r, "OBJECTID": i} for i, r in enumerate(
        publish.prepare_rows(metrics.compute_metric_change(rows), MC, "2026-10-21T00:00:00+00:00"), start=100)]


def test_a_pass_that_wrote_no_metric_rows_leaves_a_filled_table_alone(monkeypatch):
    seen = []
    tables = tables_with_metric_change(host_rows=1)
    monkeypatch.setattr(publish, "query_all_rows", lambda *a, **k: seen.append("read") or [])
    monkeypatch.setattr(publish, "upsert_by_key", lambda *a, **k: seen.append("upsert"))
    monkeypatch.setattr(publish, "purge_older_than", lambda *a, **k: seen.append("purge") or 0)
    publish.sync_metric_change(tables, "HOST-A", "2026-10-22T00:00:00+00:00", 200, 90, changed=False)
    assert seen == ["purge"]  # no TaskMetrics read, no upsert


def test_a_pass_that_wrote_no_metric_rows_still_fills_an_empty_table(monkeypatch):
    rows = metric_rows("HOST-A", "Sync", "rows_added", [10, 12, 11, 13, 9, 40])
    upserts, _, _ = run_sync(monkeypatch, tables_with_metric_change(host_rows=0), rows, changed=False)
    assert len(upserts) == 1 and len(upserts[0][0]) == 1


def test_a_series_whose_values_did_not_move_is_not_sent_again(monkeypatch):
    rows = metric_rows("HOST-A", "Sync", "rows_added", [10, 12, 11, 13, 9, 40])
    rows += metric_rows("HOST-A", "Sync", "rows_failed", [0, 0, 0, 1])
    stored = stored_as_service_returns(rows)
    upserts, deletes, _ = run_sync(monkeypatch, tables_with_metric_change(), rows, stored_change=stored)
    assert upserts == [] and deletes == []
    moved = rows + [{**rows[-1], "metric_value": 5, "observed_at_utc": T0 + 10 * HOUR}]
    upserts, _, _ = run_sync(monkeypatch, tables_with_metric_change(), moved, stored_change=stored)
    (sent, _), = upserts
    assert [r["metric_key"] for r in sent] == ["rows_failed"] and sent[0]["latest_value"] == 5


def test_the_collector_upserts_one_row_per_series_by_series_key(monkeypatch):
    rows = metric_rows("HOST-A", "Sync", "rows_added", [10, 12, 11, 13, 9, 40])
    upserts, deletes, purges = run_sync(monkeypatch, tables_with_metric_change(), rows)
    (sent, keys), = upserts
    assert keys == ["series_key"] and len(sent) == 1
    assert sent[0]["latest_value"] == 40 and sent[0]["usual_value"] == 11 and sent[0]["host_name"] == "HOST-A"
    assert sent[0]["latest_at_utc"] == rows[-1]["observed_at_utc"]  # epoch ms, not converted twice
    assert "collected_at_utc" in sent[0]
    assert deletes == [] and purges == [("latest_at_utc", 90)]


def test_a_series_that_left_the_metrics_table_is_deleted_from_this_hosts_rows(monkeypatch):
    rows = metric_rows("HOST-A", "Sync", "rows_added", [1, 2, 3, 4])
    gone = "HOST-A|\\Jobs\\Sync|retired_key"
    kept = "HOST-A|\\Jobs\\Sync|rows_added"
    _, deletes, _ = run_sync(monkeypatch, tables_with_metric_change(), rows, stored_series=[gone, kept])
    assert deletes == [[100]]


def test_the_collector_reads_only_this_hosts_metric_rows(monkeypatch):
    seen = []
    tables = tables_with_metric_change()
    monkeypatch.setattr(publish, "query_all_rows",
                        lambda table, *, where="1=1", out_fields="*", page=None: seen.append(where) or [])
    monkeypatch.setattr(publish, "apply_edits_chunked", lambda *a, **k: {})
    monkeypatch.setattr(publish, "purge_older_than", lambda *a, **k: 0)
    publish.sync_metric_change(tables, "O'Neil-PC", "2026-10-22T00:00:00+00:00", 200, 90)
    assert seen[0] == ("host_name = 'O''Neil-PC' AND metric_value IS NOT NULL "
                       "AND observed_at_utc IS NOT NULL")


def test_a_failed_write_is_a_warning_not_a_failed_publish(monkeypatch):
    messages = one_message(monkeypatch)
    tables = tables_with_metric_change()

    def boom(*a, **k):
        raise RuntimeError("service busy")

    monkeypatch.setattr(publish, "query_all_rows", boom)
    ok = publish.sync_metric_change(tables, "HOST-A", "2026-10-22T00:00:00+00:00", 200, 90)
    assert len(messages) == 1 and messages[0].startswith("WARNING") and "service busy" in messages[0]
    assert ok is False  # the caller must not mark this pass's metric files as published


def test_publish_reports_a_failed_sync_so_the_metric_state_is_not_saved(monkeypatch):
    tables = {publish.TABLE_INVENTORY: OidTable(), publish.TABLE_RUNS: OidTable(),
              publish.TABLE_METRICS: OidTable(), MC: OidTable()}
    _, _, counts = publish_with(monkeypatch, tables, [publish_row()])
    assert "metric_change_failed" not in counts
    publish_with(monkeypatch, tables, [publish_row()])
    monkeypatch.setattr(publish, "sync_metric_change", lambda *a, **k: False)
    counts = publish.publish(gis=object(), item=object(), config={}, collected={
        "runs": [], "inventory": [], "metrics": [publish_row()], "host_name": "HOST-A", "log_summary": {}})
    assert counts["metric_change_failed"] == 1


# ---------------------------------------------------------------- publish with and without it

class OidTable:
    properties = SimpleNamespace(objectIdField="OBJECTID")


def publish_with(monkeypatch, tables, metric_rows_to_send):
    messages = one_message(monkeypatch)
    calls = []

    def fake_upsert(table, rows, key_fields, *, chunk_size, label, order_field="", **kw):
        calls.append(label)
        return {"added": 0, "updated": 0, "deleted": 0}

    monkeypatch.setattr(publish, "upsert_by_key", fake_upsert)
    monkeypatch.setattr(publish, "sync_inventory", lambda *a, **k: {"added": 0, "updated": 0, "deleted": 0})
    monkeypatch.setattr(publish, "summarize_runs_from_agol", lambda *a, **k: {})
    monkeypatch.setattr(publish, "fetch_other_host_counts", lambda *a, **k: [])
    monkeypatch.setattr(publish, "purge_older_than", lambda *a, **k: 0)
    if MC in tables:  # the write itself is tested above; here only its place in the pass matters
        monkeypatch.setattr(publish, "sync_metric_change", lambda *a, **k: calls.append("sync") or True)
    monkeypatch.setattr(publish, "get_tables", lambda item: tables)
    collected = {"runs": [], "inventory": [], "metrics": metric_rows_to_send, "host_name": "HOST-A",
                 "log_summary": {}}
    counts = publish.publish(gis=object(), item=object(), collected=collected, config={})
    return messages, calls, counts


def publish_row():
    return {"host_name": "HOST-A", "task_name": "Sync", "task_full_name": "\\Jobs\\Sync",
            "metric_key": "rows_added", "display_name": "Rows added", "metric_value": 5.0,
            "observed_at_utc": "2026-10-22T00:00:00+00:00", "log_file_name": "a.log", "series_key": "k"}


def test_without_the_table_publish_warns_once_and_publishes_everything_else(monkeypatch):
    base = {publish.TABLE_INVENTORY: OidTable(), publish.TABLE_RUNS: OidTable(), publish.TABLE_METRICS: OidTable()}
    messages, calls, counts = publish_with(monkeypatch, base, [publish_row()])
    warnings = [m for m in messages if MC in m]
    assert len(warnings) == 1
    assert "taskdash collect --migrate --apply" in warnings[0] and "taskdash dashboard" in warnings[0]
    assert calls == [publish.TABLE_RUNS, publish.TABLE_METRICS]     # the same writes as before
    assert counts["metrics_added"] == 0 and "sync" not in calls


def test_with_the_table_publish_syncs_it_after_the_metrics_write(monkeypatch):
    tables = {publish.TABLE_INVENTORY: OidTable(), publish.TABLE_RUNS: OidTable(),
              publish.TABLE_METRICS: OidTable(), MC: OidTable()}
    messages, calls, _ = publish_with(monkeypatch, tables, [publish_row()])
    assert calls == [publish.TABLE_RUNS, publish.TABLE_METRICS, "sync"]
    assert not [m for m in messages if m.startswith("WARNING")]


def test_publish_tells_the_sync_whether_metric_rows_were_written(monkeypatch):
    tables = {publish.TABLE_INVENTORY: OidTable(), publish.TABLE_RUNS: OidTable(),
              publish.TABLE_METRICS: OidTable(), MC: OidTable()}
    flags = []
    for rows in ([publish_row()], []):
        publish_with(monkeypatch, tables, rows)
        monkeypatch.setattr(publish, "sync_metric_change", lambda *a, **k: flags.append(k["changed"]) or True)
        publish.publish(gis=object(), item=object(), config={}, collected={
            "runs": [], "inventory": [], "metrics": rows, "host_name": "HOST-A", "log_summary": {}})
    assert flags == [True, False]


# ---------------------------------------------------------------- the dashboard

def templates():
    tpl = json.loads((Path(dash.__file__).parent / dash.TEMPLATE_FILE).read_text(encoding="utf-8"))
    by_name = {w["name"]: w for w in tpl["desktopView"]["widgets"]}
    return {"table": by_name["Task Inventory"], "list": by_name["Recent Runs"],
            "serial_config": by_name["Schedule Mix"]["chartConfig"]}


def change_script(layer_ids=None):
    definition = dash.build("TESTITEM", templates(), layer_ids)
    widget = next(w for w in dash.all_items(definition) if w["name"] == "Metric Change")
    source_id = widget["datasets"][0]["dataSource"]["itemId"]
    return next(a["script"] for a in definition["arcadeDataSourceItems"] if a["itemId"] == source_id)


def test_the_dashboard_keeps_the_old_source_until_the_table_exists():
    script = change_script()  # the default layer ids have no MetricChange
    assert script == dash.metric_change_source_script(dash.DEFAULT_PORTAL_URL, "TESTITEM", dash.LAYER_METRICS)
    assert "Filter(fs" in script


def test_the_dashboard_reads_the_table_once_it_has_a_layer_id():
    script = change_script({MC: 5})
    assert script == dash.metric_change_from_table_script(dash.DEFAULT_PORTAL_URL, "TESTITEM", 5)
    assert "Filter(fs" not in script and '"latest_value"' in script


def test_both_sources_share_the_flag_rules():
    old = dash.metric_change_source_script("https://example.invalid", "ITEM", 2)
    new = dash.metric_change_from_table_script("https://example.invalid", "ITEM", 5)
    for flag in dash.METRIC_CHANGE_FLAGS:
        assert f'"{flag}"' in old and f'"{flag}"' in new
    rule = (f"change <= {dash.METRIC_CHANGE_DROP_PCT} && usual >= {dash.METRIC_CHANGE_MIN_USUAL}"
            " && latest < lowest && !lowerIsBetter")
    assert rule in old and rule in new
    assert f"gapHours > {dash.METRIC_CHANGE_MISSING_HOURS}" in new


def test_the_definition_with_the_new_source_still_validates():
    ids = {**dash.DEFAULT_LAYER_IDS, MC: 5}
    dash.validate_definition(dash.build("TESTITEM", templates(), ids), ids)


class Gis:
    def __init__(self, tables):
        self.content = SimpleNamespace(get=lambda item_id: "ITEM")
        self._tables = tables


def resolve(monkeypatch, tables):
    monkeypatch.setattr(dash, "get_tables", lambda item: tables)
    return dash.resolve_layer_ids(Gis(tables), "ITEM", "prod")


def layer(table_id):
    return SimpleNamespace(properties=SimpleNamespace(id=table_id))


def test_resolve_layer_ids_adds_the_table_only_when_the_service_has_it(monkeypatch):
    base = {name: layer(i) for i, name in enumerate(dash.DEFAULT_LAYER_IDS)}
    assert MC not in resolve(monkeypatch, base)
    assert resolve(monkeypatch, {**base, MC: layer(9)})[MC] == 9
