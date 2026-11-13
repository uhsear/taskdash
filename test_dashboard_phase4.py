"""Phase 4 dashboard layout: every assertion runs against build() output made in the test.

Never read dashboard_taskdash.json here: it is regenerated with fresh ids on every run, and a
stale copy once kept a deleted widget "passing".
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import taskdash_common

taskdash_common.disable_arcpy_import()

import taskdash_dashboard as dash  # noqa: E402
import taskdash_publish as publish  # noqa: E402

STAT_ALIAS = re.compile(r"^(COUNT|SUM|AVG|MAX|MIN)_")
ID_TO_TABLE = {v: k for k, v in dash.DEFAULT_LAYER_IDS.items()}


def _templates():
    tpl = json.loads((Path(dash.__file__).parent / dash.TEMPLATE_FILE).read_text(encoding="utf-8"))
    by_name = {w["name"]: w for w in tpl["desktopView"]["widgets"]}
    return {"table": by_name["Task Inventory"], "list": by_name["Recent Runs"],
            "serial_config": by_name["Schedule Mix"]["chartConfig"]}


@pytest.fixture
def build(monkeypatch):
    def make(layer_ids=None, **flags):
        for name, value in flags.items():
            assert name in dash.FEATURE_FLAGS, name
            monkeypatch.setitem(dash.FEATURE_FLAGS, name, value)
        return dash.build("TESTITEM", _templates(), layer_ids)
    return make


def _by_name(definition):
    return {w["name"]: w for w in dash.all_items(definition)}


def _targets(widget):
    return [t for e in widget.get("events", []) for a in e["actions"] for t in a["targets"]]


def _schema(table):
    return {f["name"] for f in publish.TABLE_SPECS[table][0]()}


def _layout_ids(element):
    if element.get("type") == "itemLayoutElement":
        return [element["id"]]
    return [i for child in element.get("elements", []) for i in _layout_ids(child)]


# ------------------------------------------------------------------ structure

def test_every_event_target_exists_in_its_own_view(build):
    definition = build()
    wired = 0
    for view in (dash.desktop_items(definition), definition["mobileView"]["widgets"]):
        ids = {w["id"] for w in view}
        for widget in view:
            for t in _targets(widget):
                assert t["targetId"].split("#")[0] in ids, f"{widget['name']} -> {t['targetId']}"
                wired += 1
    assert wired > 30, "the selector and row-selection wiring is missing"


def test_every_referenced_field_exists_in_the_schema(build):
    referenced = set()
    for widget in dash.all_items(build()):
        table = dash.widget_table(widget, ID_TO_TABLE)
        if table is None:
            continue  # the Arcade heartbeat source: covered by its own test
        fields = set()
        for ds in dash.layer_datasets(widget):
            fields |= set(ds["groupByFields"]) | {o.split()[0] for o in ds["orderByFields"]}
            fields |= {s["onStatisticField"] for s in ds.get("statisticDefinitions", [])}
            fields |= {r["field"]["name"] for r in dash._walk_rules(ds.get("filter"))}
        fields |= set((widget.get("dataSettings") or {}).get("valueFields", []))
        fields |= set(re.findall(r'\$datapoint\["(\w+)"\]', widget.get("expression") or ""))
        fields |= set(re.findall(r"\{(\w+)\}", widget.get("text") or ""))
        fields |= {fm["sourceName"] for t in _targets(widget) for fm in t.get("fieldMap", [])}
        fields = {f for f in fields if not STAT_ALIAS.match(f)}
        assert not fields - _schema(table), f"{widget['name']} references {fields - _schema(table)} not on {table}"
        referenced |= fields
    assert "attention_kind" in referenced


def test_default_and_all_flags_off_builds_pass_the_publish_validators(build):
    dash.validate_definition(build())
    dash.validate_definition(build(**{k: False for k in dash.FEATURE_FLAGS}))
    dash.validate_definition(build(**{k: True for k in dash.FEATURE_FLAGS}))


@pytest.mark.parametrize("flags", [{}, {k: False for k in dash.FEATURE_FLAGS}])
def test_layout_places_every_built_widget_exactly_once(build, flags):
    definition = build(**flags)
    placed = _layout_ids(definition["desktopView"]["layout"]["rootElement"])
    assert sorted(placed) == sorted(w["id"] for w in definition["desktopView"]["widgets"])
    mobile = _layout_ids(definition["mobileView"]["layout"]["rootElement"])
    assert sorted(mobile) == sorted(w["id"] for w in definition["mobileView"]["widgets"])


def test_three_tabs_in_operator_order(build):
    tabs = build()["desktopView"]["layout"]["rootElement"]["elements"]
    assert [t["tabName"] for t in tabs] == ["Overview", "Workflow Metrics", "Collector & Changes"]


def test_dropped_charts_are_gone(build):
    definition = build()
    assert not set(_by_name(definition)) & {"Health Mix", "Schedule Mix", "Average Duration by Task"}
    assert not [w for w in dash.all_items(definition) if w["type"] == "pieChartWidget"]


def test_layer_ids_come_from_the_service_not_constants(build):
    ids = {**dash.DEFAULT_LAYER_IDS, "CollectorRuns": 7, "TaskChanges": 9}
    definition = build(layer_ids=ids)
    widgets = _by_name(definition)
    assert widgets["Collector Runs"]["datasets"][0]["dataSource"]["layerId"] == 7
    assert widgets["Recent Changes"]["datasets"][0]["dataSource"]["layerId"] == 9
    dash.validate_definition(definition, ids)


# ------------------------------------------------------------------ palette and status words

@pytest.mark.parametrize("name", ["Task Health", "Task Detail", "Recent Failures", "Collector Runs",
                                  "Recent Changes"])
def test_every_palette_status_has_word_and_symbol_in_the_status_arcade(build, name):
    expression = _by_name(build())[name]["expression"]
    for status, (word, symbol, _) in dash.PALETTE.items():
        assert f'"{status}", "{word}"' in expression, f"{name}: no word for {status}"
        assert f'"{status}", "{symbol}"' in expression, f"{name}: no symbol for {status}"
    assert 'var statusText = IIf(sym == "", label, sym + " " + label);' in expression


def test_every_attention_kind_enrich_inventory_emits_has_a_tier():
    emitted = set(re.findall(r'kind = "([A-Z_]+)"', inspect.getsource(publish.enrich_inventory)))
    assert {"FAILED", "OVERDUE"} <= emitted
    assert emitted <= set(dash.ATTENTION_TIER), f"new attention_kind(s) without a tier: {emitted - set(dash.ATTENTION_TIER)}"


def test_status_tiles_carry_a_word_not_just_a_colour(build):
    widgets = _by_name(build())
    for name in ("Collector Heartbeat", "Needs Attention", "Failed (last run)", "Overdue",
                 "Other Warnings", "7-Day Success Rate", "Disabled"):
        assert "bottomText" in widgets[name]["expression"], name


# ------------------------------------------------------------------ indicator row

def _matches(row, group):
    """Evaluate a filterGroup with SQL NULL semantics: any comparison against NULL is false,
    so NULL NOT IN (...) excludes the row, exactly as the feature service does."""
    ops = {
        "equal": lambda v, c: v is not None and v == c["value"],
        "is_not_in": lambda v, c: v is not None and v not in c["values"],
        "greater": lambda v, c: v is not None and v > c["value"],
        "is_not_empty": lambda v, c: v not in (None, ""),
        "is_null": lambda v, c: v is None,
    }
    results = (_matches(row, r) if r["type"] == "filterGroup"
               else ops[r["operator"]](row.get(r["field"]["name"]), r["constraint"])
               for r in (group or {}).get("rules", []))
    return any(results) if (group or {}).get("condition") == "OR" else all(results)


def test_attention_tiles_partition_needs_attention_without_double_counting(build):
    widgets = _by_name(build())
    rows = [{"needs_attention": 1, "attention_kind": k}
            for k in ("FAILED", "STUCK", "BLOCKED", "OVERDUE", "WARNING", "A_FUTURE_KIND", None)]
    rows += [{"needs_attention": 0, "attention_kind": ""}] * 3 + [{"needs_attention": 0, "attention_kind": None}]

    def count(name):
        return sum(_matches(r, widgets[name]["datasets"][0].get("filter")) for r in rows)

    # None: a row --migrate added the column to, before its host (or a retired host) collects again.
    assert count("Failed (last run)") + count("Overdue") + count("Other Warnings") == count("Needs Attention") == 7


def test_heartbeat_reads_the_stalest_host_and_refreshes(build):
    definition = build()
    heartbeat = _by_name(definition)["Collector Heartbeat"]
    source = definition["arcadeDataSourceItems"][0]
    assert heartbeat["datasets"][0]["dataSource"] == {"type": "arcadeDataSource", "itemId": source["itemId"]}
    assert heartbeat["valueType"] == "feature" and heartbeat["valueField"] == dash.HEARTBEAT_SOURCE_FIELD
    assert source["refreshInterval"] == dash.HEARTBEAT_REFRESH_MINUTES
    # The oldest collected_at_utc over the hosts that ran lately (H2-3), not over every stored row.
    assert "t < ms" in source["script"] and '"TESTITEM"' in source["script"]
    assert "collected_at_utc" in _schema("TaskInventory")
    fallback = _by_name(build(heartbeat_arcade_refresh=False))["Collector Heartbeat"]["datasets"][0]
    assert fallback["statisticDefinitions"][0]["statisticType"] == "min"


def test_success_rate_reads_the_precomputed_fleet_value(build):
    """The lowest per-task value is stamped by the pipeline (stamp_success_rate); the unproven
    $reference two-dataset shape is gone."""
    default = _by_name(build())["7-Day Success Rate"]
    assert [d["name"] for d in default["datasets"]] == ["main"]
    assert default["valueField"] == dash.SUCCESS_SOURCE_FIELD
    assert '$datapoint["lowest_success_rate_7d"]' in default["expression"]
    assert "$reference" not in default["expression"]
    fallback = _by_name(build(heartbeat_arcade_refresh=False))["7-Day Success Rate"]
    assert "MIN_lowest_success_rate_7d" in fallback["expression"]


# ------------------------------------------------------------------ selectors and selection

def test_selectors_never_filter_tiles_or_mobile_and_health_schedule_stay_on_inventory(build):
    definition = build()
    tiles = {w["id"] for w in definition["desktopView"]["widgets"] if w["type"] == "indicatorWidget"}
    mobile = {w["id"] for w in definition["mobileView"]["widgets"]}
    by_id = {w["id"]: w for w in dash.desktop_items(definition)}
    selectors = definition["desktopView"]["sidebar"]["selectors"]
    assert [s["name"] for s in selectors] == ["Host", "Task", "Health", "Schedule", "Metric"]
    for source in dash.desktop_items(definition):
        hit = {t["targetId"].split("#")[0] for t in _targets(source)}
        assert not hit & (tiles | mobile), f"{source['name']} filters a tile or a mobile widget"
        if source["name"] in ("Health", "Schedule"):
            assert {dash.widget_table(by_id[i], ID_TO_TABLE) for i in hit} == {"TaskInventory"}


def test_task_health_row_selection_is_restored(build):
    widgets = _by_name(build())
    targets = {t["targetId"].split("#")[0]: t for t in _targets(widgets["Task Health"])}
    assert targets[widgets["Task Detail"]["id"]]["requiresSelection"] is True
    # A row joins on task_full_name (task_name repeats across folders). The bar is grouped by
    # task_name, so it can only join on that.
    by_full_name = [{"sourceName": "task_full_name", "targetName": "task_full_name"}]
    assert targets[widgets["Recent Failures"]["id"]]["fieldMap"] == by_full_name
    assert targets[widgets["Duration Trend"]["id"]]["fieldMap"] == by_full_name
    bar = {t["targetId"].split("#")[0]: t for t in _targets(widgets["Top Failing Tasks"])}
    assert bar[widgets["Recent Failures"]["id"]]["fieldMap"][0]["sourceName"] == "task_name"
    assert widgets["Task Health"]["tableSettings"]["selectionMode"] == "single"


def test_row_caps_removed_where_they_hid_rows(build):
    widgets = _by_name(build())
    for name in ("Metric Observations", "Metric Summary", "Task Health", "Task Detail", "Setting Notes",
                 "Host", "Task", "Health", "Schedule", "Metric"):
        assert "maxFeatures" not in widgets[name]["datasets"][0], name


def test_every_widget_carries_help_text(build):
    for widget in dash.all_items(build()):
        assert widget.get("showDescription") is True and "<strong>Shows:</strong>" in widget["description"], widget["name"]


def test_help_never_points_at_a_control_the_view_lacks(build):
    definition = build()
    for widget in definition["mobileView"]["widgets"]:  # no selectors, no Task Health table
        assert "What to do" not in widget["description"], widget["name"]
    assert "Pick a task in the Filters panel" in _by_name(definition)["Recent Changes"]["description"]
    no_selectors = _by_name(build(sidebar_selectors=False))
    for name in dash.SELECTOR_TODO:
        assert "Filters panel" not in no_selectors[name]["description"], name
    assert "What to do" in no_selectors["Task Health"]["description"]


# ------------------------------------------------------------------ feature flags

FLAG_PROBES = {
    "sidebar_selectors": lambda d: "sidebar" in d["desktopView"],
    "cross_table_row_filter": lambda d: any(t.get("fieldMap") for w in d["desktopView"]["widgets"] for t in _targets(w)),
    "detail_requires_selection": lambda d: any(
        t["requiresSelection"] for t in _targets(_by_name(d)["Task Health"])
        if t["targetId"].startswith(_by_name(d)["Task Detail"]["id"])),
    "no_filter_prompt_text": lambda d: any("text" in w.get("noFilterState", {}) for w in dash.all_items(d)),
    "no_data_text": lambda d: any("text" in w.get("noDataState", {}) for w in dash.all_items(d)),
    "widget_description": lambda d: any("description" in w for w in dash.all_items(d)),
    "heartbeat_arcade_refresh": lambda d: "arcadeDataSource" in json.dumps(d),
    "trend_charts": lambda d: any(w.get("categoryType") == "features" for w in dash.all_items(d)),
    "column_number_format": lambda d: any(dash.COLUMN_FORMAT_KEY in c for w in dash.all_items(d)
                                          for c in w.get("columnSettings", [])),
    "status_symbols": lambda d: any(p[1] in json.dumps(d, ensure_ascii=False) for p in dash.PALETTE.values()),
    "mobile_element_mappings": lambda d: bool(d["elementMappings"]),
    "config_notes_filter": lambda d: "filter" in _by_name(d)["Setting Notes"]["datasets"][0],
}


def test_every_flag_has_a_probe():
    assert set(FLAG_PROBES) == set(dash.FEATURE_FLAGS)


@pytest.mark.parametrize("flag", sorted(FLAG_PROBES))
def test_flag_off_really_removes_the_feature(build, flag):
    assert FLAG_PROBES[flag](build(**{flag: True})), f"{flag} on: feature missing"
    off = build(**{flag: False})
    assert not FLAG_PROBES[flag](off), f"{flag} off: feature still present"
    dash.validate_definition(off)


# ------------------------------------------------------------------ validators refuse

def test_validator_refuses_a_dangling_target(build):
    definition = build()
    _by_name(definition)["Task Health"]["events"][0]["actions"][0]["targets"][0]["targetId"] = "gone#main"
    with pytest.raises(SystemExit, match="does not exist"):
        dash.validate_definition(definition)


def test_validator_refuses_a_cross_table_target_without_fieldmap(build):
    definition = build()
    for t in _targets(_by_name(definition)["Task Health"]):
        t.pop("fieldMap", None)
    with pytest.raises(SystemExit, match="without a fieldMap"):
        dash.validate_definition(definition)


def test_validator_refuses_a_select_caption_with_no_selection(build):
    definition = build()
    _by_name(definition)["Task Health"].pop("events")
    with pytest.raises(SystemExit, match="selecting does nothing"):
        dash.validate_definition(definition)


def test_validator_refuses_an_unstated_row_cap(build):
    definition = build()
    _by_name(definition)["Metric Observations"]["datasets"][0]["maxFeatures"] = 2000
    with pytest.raises(SystemExit, match="no silent caps"):
        dash.validate_definition(definition)


# ------------------------------------------------------------------ item resolution and --apply

def _item(item_id, title):
    return SimpleNamespace(id=item_id, title=title, type="Dashboard")


class FakeGIS:
    def __init__(self, items):
        self.items = {i.id: i for i in items}
        self.content = self
        self.users = SimpleNamespace(me=SimpleNamespace(username="alex"))
        self.searches = []

    def get(self, item_id):
        return self.items.get(item_id)

    def search(self, query, item_type=None):
        self.searches.append(query)
        return [i for i in self.items.values() if f'title:"{i.title}"' in query]


def test_test_and_prod_targets_resolve_different_items():
    prod, test = _item("PROD1", "TaskDash"), _item("TEST1", "TaskDash_TEST")
    gis = FakeGIS([prod, test])
    agol = {"dashboard_id": "PROD1"}
    prod_item, _ = dash.resolve_dashboard(gis, "prod", agol)
    assert gis.searches == [], "prod must resolve by arcgis.dashboard_id, never by title"
    test_item, title = dash.resolve_dashboard(gis, "test", agol)
    assert prod_item is prod and test_item is test and title == "TaskDash_TEST"


def test_prod_with_a_stale_dashboard_id_refuses_instead_of_title_matching():
    gis = FakeGIS([_item("OTHER", "TaskDash")])
    with pytest.raises(SystemExit, match="never creates a second prod dashboard"):
        dash.resolve_dashboard(gis, "prod", {"dashboard_id": "PROD1"})


def test_test_target_refuses_to_land_on_the_prod_item():
    gis = FakeGIS([_item("PROD1", "TaskDash")])
    with pytest.raises(SystemExit, match="resolves to the prod dashboard"):
        dash.resolve_dashboard(gis, "test", {"dashboard_id": "PROD1"}, title_override="TaskDash")


def test_backup_saves_the_live_definition(tmp_path):
    item = SimpleNamespace(id="TEST1", get_data=lambda: {"version": "5.0.0"})
    path = dash.backup_dashboard(item, tmp_path / "Logs")
    assert path.name.startswith("dashboard_backup_TEST1_")
    assert json.loads(path.read_text(encoding="utf-8")) == {"version": "5.0.0"}
    with pytest.raises(SystemExit, match="not overwriting"):
        dash.backup_dashboard(SimpleNamespace(id="X", get_data=lambda: {}), tmp_path)


def test_main_is_check_by_default_and_backs_up_before_it_overwrites(monkeypatch, tmp_path):
    calls = []
    existing = _item("TEST1", "TaskDash_TEST")
    monkeypatch.setattr(dash, "load_config", lambda path=None: {"arcgis": {"test_item_id": "SVC"}})
    monkeypatch.setattr(dash, "connect_agol", lambda config: SimpleNamespace(url="https://example.maps.arcgis.com"))
    monkeypatch.setattr(dash, "load_templates", lambda gis, source: _templates())
    monkeypatch.setattr(dash, "resolve_layer_ids", lambda gis, item_id, target: dict(dash.DEFAULT_LAYER_IDS))
    monkeypatch.setattr(dash, "resolve_dashboard", lambda gis, target, agol, title="": (existing, "TaskDash_TEST"))
    monkeypatch.setattr(dash, "backup_dashboard", lambda item, log_dir: calls.append(("backup", item.id)))
    monkeypatch.setattr(dash, "post_dashboard",
                        lambda gis, definition, title, ex, config=None: calls.append(("post", ex.id)) or ex.id)
    monkeypatch.setattr(dash, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(publish, "resolve_log_dir", lambda config: tmp_path / "Logs")

    assert dash.main(["--target", "test"]) == 0
    assert calls == [] and (tmp_path / dash.OUTPUT_JSON).exists()
    assert dash.main(["--target", "test", "--apply"]) == 0
    assert calls == [("backup", "TEST1"), ("post", "TEST1")]


def _run_main(monkeypatch, tmp_path, arcgis, existing, argv):
    seen = []
    monkeypatch.setattr(dash, "load_config", lambda path=None: {"arcgis": arcgis})
    monkeypatch.setattr(dash, "connect_agol", lambda config: SimpleNamespace(url="https://example.maps.arcgis.com"))
    monkeypatch.setattr(dash, "load_templates", lambda gis, source: _templates())
    monkeypatch.setattr(dash, "resolve_layer_ids", lambda gis, item_id, target: dict(dash.DEFAULT_LAYER_IDS))
    monkeypatch.setattr(dash, "resolve_dashboard", lambda gis, target, agol, title="": (existing, "T"))
    monkeypatch.setattr(dash, "backup_dashboard", lambda item, log_dir: None)
    monkeypatch.setattr(dash, "post_dashboard", lambda *a, **k: "ID")
    monkeypatch.setattr(dash, "write_definition", lambda definition, item_id, layer_ids: seen.append(item_id))
    monkeypatch.setattr(dash, "validate_definition", lambda *a, **k: None)
    monkeypatch.setattr(publish, "resolve_log_dir", lambda config: tmp_path / "Logs")
    monkeypatch.setattr(dash, "runtime_dir", lambda: tmp_path)  # a new prod dashboard writes its id to this config
    assert dash.main(argv) == 0
    return seen[0]


def test_no_target_uses_test_only_when_the_config_has_a_test_service(monkeypatch, tmp_path, capsys):
    both = {"item_id": "PRODSVC", "test_item_id": "TESTSVC"}
    assert _run_main(monkeypatch, tmp_path, both, None, []) == "TESTSVC"
    assert "No --target given: using test" in capsys.readouterr().out
    assert _run_main(monkeypatch, tmp_path, {"item_id": "PRODSVC"}, None, []) == "PRODSVC"
    assert "No --target given: using prod" in capsys.readouterr().out
    assert _run_main(monkeypatch, tmp_path, both, None, ["--target", "prod"]) == "PRODSVC"
    assert "No --target given" not in capsys.readouterr().out


def test_hand_edit_warning_appears_only_on_apply_over_an_existing_item(monkeypatch, tmp_path, capsys):
    arcgis = {"item_id": "PRODSVC"}
    existing = _item("D1", "TaskDash")
    _run_main(monkeypatch, tmp_path, arcgis, existing, ["--target", "prod", "--apply"])
    assert "replaces any hand edits" in capsys.readouterr().out
    _run_main(monkeypatch, tmp_path, arcgis, existing, [])
    assert "hand edits" not in capsys.readouterr().out
    _run_main(monkeypatch, tmp_path, arcgis, None, ["--target", "prod", "--apply"])
    assert "hand edits" not in capsys.readouterr().out


def test_apply_without_target_never_posts_to_prod_when_there_is_no_test_service(monkeypatch, tmp_path):
    posted = []
    monkeypatch.setattr(dash, "load_config", lambda path=None: {"arcgis": {"item_id": "PRODSVC"}})
    monkeypatch.setattr(dash, "connect_agol", lambda config: posted.append("signed in"))
    monkeypatch.setattr(dash, "post_dashboard", lambda *a, **k: posted.append("post"))
    with pytest.raises(SystemExit, match="--target prod --apply"):
        dash.main(["--apply"])
    assert posted == []


# ------------------------------------------------------------------ render fixes (TaskDash_TEST, 1920 px)

def _required_sources(definition):
    by_target = {}
    for src in dash.desktop_items(definition):
        for t in _targets(src):
            if t["requiresSelection"]:
                by_target.setdefault(t["targetId"].split("#")[0], []).append(src["name"])
    return by_target


def test_a_row_required_widget_requires_nothing_else(build):
    definition = build()
    widgets = _by_name(definition)
    required = _required_sources(definition)
    assert required[widgets["Duration Trend"]["id"]] == ["Task Health"]
    assert required[widgets["Task Detail"]["id"]] == ["Task Health"]
    # One line needs a task AND a metric: a Metric change row gives both in series_key.
    assert required[widgets["Metric Trend"]["id"]] == ["Metric Change"]
    assert "Metric change" in widgets["Metric Trend"]["noFilterState"]["text"]
    # Without the row source, the Task selector must still scope the trend on its own.
    fallback = build(cross_table_row_filter=False)
    assert _required_sources(fallback)[_by_name(fallback)["Duration Trend"]["id"]] == ["Task"]
    # ...and the Task AND Metric pickers draw the metric trend, with a prompt that says so.
    fallback_trend = _by_name(fallback)["Metric Trend"]
    assert sorted(_required_sources(fallback)[fallback_trend["id"]]) == ["Metric", "Task"]
    assert "a Task and a Metric" in fallback_trend["noFilterState"]["text"]


def test_validator_refuses_a_row_source_plus_another_required_source(build):
    definition = build()
    widgets = _by_name(definition)
    for t in _targets(widgets["Task"]):
        if t["targetId"].startswith(widgets["Duration Trend"]["id"]):
            t["requiresSelection"] = True
    with pytest.raises(SystemExit, match="one row click can never satisfy"):
        dash.validate_definition(definition)


def test_task_health_gets_half_the_overview_width(build):
    definition = build()
    overview = definition["desktopView"]["layout"]["rootElement"]["elements"][0]["elements"][0]
    assert overview["orientation"] == "col"
    health_id = _by_name(definition)["Task Health"]["id"]
    column = next(c for c in overview["elements"] if health_id in _layout_ids(c))
    assert column["orientation"] == "row" and column["width"] / sum(c["width"] for c in overview["elements"]) == 0.5


def test_success_rate_says_why_it_is_empty(build):
    tile = _by_name(build())["7-Day Success Rate"]
    for state in ("noDataState", "noValueState"):
        assert "No runs in the last 7 days" in tile[state]["text"], state


def test_mobile_tiles_are_tall_enough_and_keep_their_order(build):
    definition = build()
    mobile = definition["mobileView"]
    expected = [n + dash.MOBILE_SUFFIX for n in dash.MOBILE_TILES + ("Attention Tasks", "Recent Failures")]
    assert [w["name"] for w in mobile["widgets"]] == expected
    elements = mobile["layout"]["rootElement"]["elements"]
    heights = [e["height"] for e in elements]
    pairs = len(dash.MOBILE_TILE_PAIRS)
    assert heights == [dash.MOBILE_TILE_HEIGHT] * pairs + [dash.MOBILE_ATTENTION_HEIGHT, dash.MOBILE_FAILURES_HEIGHT]
    assert all(e["orientation"] == "col" and len(e["elements"]) == 2 for e in elements[:pairs])
    # Dashboards scales mobile heights to one screen: a total above 1 shrank tiles until text overlapped.
    assert abs(sum(heights) - 1.0) < 1e-9



def test_no_list_text_uses_a_bare_field_placeholder(build):
    """Render finding 2026-09-25: an Arcade list drops a bare {task_name} without a word, so no
    list said which task a row was about. Every field must travel through the expression."""
    import re
    definition = build()
    lists = [w for w in dash.desktop_items(definition) + definition["mobileView"]["widgets"]
             if w.get("type") == "listWidget"]
    assert lists
    for w in lists:
        assert not re.search(r"\{[A-Za-z_][A-Za-z0-9_]*\}", w["text"]), w["name"]
        for ref in re.findall(r"\{expression/(f_[A-Za-z0-9_]+)\}", w["text"]):
            assert f"{ref}:" in w["expression"], (w["name"], ref)


def test_host_guard_matches_only_a_quoted_value(build):
    definition = build()
    dash.validate_definition(definition, config={"host_name_override": "ta"})
    _by_name(definition)["Task Health"]["filter_probe"] = "HOST-X"
    with pytest.raises(SystemExit, match="hardcoded host filter"):
        dash.validate_definition(definition, config={"host_name_override": "HOST-X"})
