"""Phase 2 read-only view: plan/apply, dashboard data source ids, migrate, the everyone guard.

Mocks only: nothing here reaches ArcGIS.
"""

from __future__ import annotations

import itertools
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

import taskdash_common

taskdash_common.disable_arcpy_import()

import taskdash_dashboard as dash  # noqa: E402
import taskdash_publish as publish  # noqa: E402

SVC, VIEW = "svcitem0001", "viewitem0002"
# Every switch off. A missing switch hides its fields, so "show everything" must be spelled out.
SHOWN = {"privacy": {"hide_command_lines": False, "hide_sample_lines": False,
                     "hide_accounts": False, "hide_paths": False}}


# ------------------------------------------------------------------ fakes

class _Manager:
    def __init__(self, log, label):
        self.log, self.label = log, label

    def update_definition(self, payload):
        self.log.append(("update", self.label, json.loads(json.dumps(payload))))
        return {"success": True}


def _field_names(spec_name, skip=()):
    return [SimpleNamespace(name=f["name"]) for f in publish.TABLE_SPECS[spec_name][0]()
            if f["name"] not in skip]


def _view_tables(log, drop=None):
    drop = drop or {}
    return {name: SimpleNamespace(manager=_Manager(log, name),
                                  properties=SimpleNamespace(fields=_field_names(name, drop.get(name, ()))))
            for name in publish.TABLE_SPECS}


class _Item:
    def __init__(self, item_id, title="TaskDash", shared=None):
        self.id, self.title, self.shares, self.access = item_id, title, [], "private"
        self.shared_with = shared if shared is not None else {"everyone": False, "org": True,
                                                              "groups": [SimpleNamespace(id="grp1")]}

    def share(self, **kw):
        self.shares.append(kw)


@pytest.fixture
def world(monkeypatch):
    log = []
    view = _Item(VIEW, "TaskDash_view")
    tables = _view_tables(log)

    class _Flc:
        def __init__(self, item):
            self.manager = SimpleNamespace(
                create_view=lambda **kw: log.append(("create_view", kw)) or view,
                update_definition=lambda p: log.append(("update", "service-level", p)) or {"success": True})

        @classmethod
        def fromitem(cls, item):
            return cls(item)

    fake = types.ModuleType("arcgis.features")
    fake.FeatureLayerCollection = _Flc
    monkeypatch.setitem(sys.modules, "arcgis.features", fake)
    monkeypatch.setattr(publish, "get_tables", lambda item: tables)
    gis = SimpleNamespace(content=SimpleNamespace(search=lambda *a, **k: [], get=lambda item_id: view),
                          users=SimpleNamespace(me=SimpleNamespace(username="u")))
    return SimpleNamespace(log=log, view=view, tables=tables, gis=gis, item=_Item(SVC))


# ------------------------------------------------------------------ create-view

def test_plan_without_apply_makes_no_calls(world):
    cfg = {"privacy": {"hide_accounts": True}}
    assert publish.create_view(world.gis, world.item, cfg, apply=False) is None
    assert world.log == [] and world.view.shares == []


def test_apply_creates_hides_resends_capabilities_and_never_shares_everyone(world):
    cfg = {"privacy": {"hide_command_lines": True, "hide_accounts": True}}
    view = publish.create_view(world.gis, world.item, cfg, apply=True)
    assert view is world.view
    assert world.log[0] == ("create_view", {"name": "TaskDash_view", "updateable": False})
    kinds = [(e[0], e[1]) for e in world.log]
    hide_at = max(i for i, e in enumerate(world.log) if e[0] == "update" and "fields" in e[2])
    caps = [i for i, e in enumerate(world.log) if e[2:] and e[2] == {"capabilities": "Query"}]
    assert caps and min(caps) > hide_at
    assert ("update", "service-level") in kinds
    hidden = {e[1]: [f["name"] for f in e[2]["fields"] if not f["visible"]]
              for e in world.log if e[0] == "update" and "fields" in e[2]}
    # hide_paths and hide_sample_lines are missing from cfg, so they hide too.
    assert hidden[publish.TABLE_INVENTORY] == ["actions", "command_target_arguments", "run_as_user",
                                               "command_target_path", "command_target_working_directory",
                                               "latest_log_path", "definition_hash"]
    assert hidden[publish.TABLE_RUNS] == ["user_context"]
    assert hidden[publish.TABLE_METRICS] == ["sample_line"]
    assert world.view.shares == [{"everyone": False, "org": True, "groups": ["grp1"]}]


def test_apply_refetches_the_view_until_its_tables_appear(world, monkeypatch):
    # The item create_view returns lists no tables; a later get of the same id does.
    answers = iter([{}, {}, world.tables])
    monkeypatch.setattr(publish, "get_tables", lambda item: next(answers, world.tables))
    monkeypatch.setattr(publish.time, "sleep", lambda s: None)
    assert publish.create_view(world.gis, world.item, {}, apply=True) is world.view


def test_view_whose_tables_never_appear_stops_with_the_fix(world, monkeypatch):
    monkeypatch.setattr(publish, "get_tables", lambda item: {})
    monkeypatch.setattr(publish.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit, match="has no table"):
        publish.create_view(world.gis, world.item, {}, apply=True)


def test_private_source_makes_no_share_call(world):
    world.item.shared_with = {"everyone": False, "org": False, "groups": []}
    world.view.share = lambda **kw: (_ for _ in ()).throw(AssertionError("share() must not be called"))
    publish.create_view(world.gis, world.item, {}, apply=True)


def test_private_source_stops_when_the_view_is_not_private(world):
    world.item.shared_with = {"everyone": False, "org": False, "groups": []}
    world.view.access = "org"
    with pytest.raises(SystemExit, match="should be private but its access is 'org'"):
        publish.create_view(world.gis, world.item, {}, apply=True)


def test_existing_view_search_uses_the_source_owner(world):
    queries = []
    world.item.owner = "service_owner"
    world.gis.content.search = lambda q, **k: queries.append(q) or []
    publish.create_view(world.gis, world.item, {}, apply=True)
    assert queries and "owner:service_owner" in queries[0]


def test_failed_group_share_stops_the_run_and_names_the_group(world):
    world.view.share = lambda **kw: {"notSharedWith": ["grp1"]}
    with pytest.raises(SystemExit, match="grp1"):
        publish.create_view(world.gis, world.item, {}, apply=True)


def test_existing_view_message_names_the_target_key_and_says_delete(world):
    world.gis.content.search = lambda *a, **k: [SimpleNamespace(id="old", title="TaskDash_view")]
    with pytest.raises(SystemExit, match=r"test_view_item_id.*") as err:
        publish.create_view(world.gis, world.item, {}, apply=True, target="test")
    assert "Delete it" in str(err.value)


def test_switches_off_hide_only_definition_hash_and_still_lock_capabilities(world):
    publish.create_view(world.gis, world.item, SHOWN, apply=True)
    updates = [e for e in world.log if e[0] == "update" and "fields" in e[2]]
    assert updates == [("update", publish.TABLE_INVENTORY,
                        {"fields": [{"name": "definition_hash", "visible": False}]})]
    assert any(e[2] == {"capabilities": "Query"} for e in world.log if e[0] == "update")


def test_run_create_view_refuses_when_a_view_is_configured(world):
    args = SimpleNamespace(target="test", apply=True)
    with pytest.raises(SystemExit, match="test_view_item_id"):
        publish.run_create_view({"arcgis": {"test_view_item_id": VIEW}}, args)


def test_create_view_flag_runs_alone():
    with pytest.raises(SystemExit):
        publish.parse_args(["--create-view", "--migrate"])
    assert publish.parse_args(["--create-view"]).apply is False


# ------------------------------------------------------------------ everyone guard

def test_everyone_guard_refuses_and_names_the_fix():
    item = _Item("x")
    with pytest.raises(SystemExit, match="organization or named groups"):
        publish.share_item(item, everyone=True)
    assert item.shares == []
    publish.share_item(item, org=True)
    assert item.shares[0]["everyone"] is False


# ------------------------------------------------------------------ dashboard ids

def _templates():
    tpl = json.loads((Path(dash.__file__).parent / dash.TEMPLATE_FILE).read_text(encoding="utf-8"))
    by_name = {w["name"]: w for w in tpl["desktopView"]["widgets"]}
    return {"table": by_name["Task Inventory"], "list": by_name["Recent Runs"],
            "serial_config": by_name["Schedule Mix"]["chartConfig"]}


def _build(monkeypatch, item_id):
    counter = itertools.count()
    monkeypatch.setattr(dash, "new_id", lambda: f"id-{next(counter)}")
    return dash.build(item_id, _templates(), None, portal_url="https://example.invalid", config=SHOWN)


def test_data_item_id_defaults_to_the_service():
    agol = {"item_id": SVC, "test_item_id": "svctest", "view_item_id": VIEW}
    assert dash.data_item_id({"item_id": SVC}, "prod") == SVC
    assert dash.data_item_id(agol, "prod") == VIEW
    assert dash.data_item_id(agol, "test") == "svctest"
    assert dash.data_item_id({**agol, "test_view_item_id": "vtest"}, "test") == "vtest"


def test_unconfigured_build_is_identical_and_view_build_reads_only_the_view(monkeypatch):
    agol = {"item_id": SVC}
    today = _build(monkeypatch, SVC)
    same = _build(monkeypatch, dash.data_item_id(agol, "prod"))
    assert json.dumps(today, sort_keys=True) == json.dumps(same, sort_keys=True)

    on_view = _build(monkeypatch, dash.data_item_id({**agol, "view_item_id": VIEW}, "prod"))
    blob = json.dumps(on_view)
    assert SVC not in blob and VIEW in blob
    assert json.dumps(today).replace(SVC, VIEW) == blob
    dash.validate_definition(on_view, config=SHOWN, data_item=VIEW)


def test_hidden_fields_are_not_read_when_a_switch_is_on(monkeypatch):
    # hide_paths off: the Task detail list still reads the path fields (dashboard owner to fix).
    cfg = {"privacy": {"hide_command_lines": True, "hide_accounts": True, "hide_sample_lines": True,
                       "hide_paths": False}}
    counter = itertools.count()
    monkeypatch.setattr(dash, "new_id", lambda: f"id-{next(counter)}")
    definition = dash.build(VIEW, _templates(), None, portal_url="https://example.invalid", config=cfg)
    blob = json.dumps(definition)
    for field in ("run_as_user", "command_target_arguments", "sample_line"):
        assert field not in blob
    dash.validate_definition(definition, config=cfg, data_item=VIEW)
    plain = _build(monkeypatch, VIEW)
    with pytest.raises(SystemExit, match="sample_line"):
        dash.validate_definition(plain, config={"privacy": dict(SHOWN["privacy"], hide_sample_lines=True)},
                                 data_item=VIEW)


def test_validate_flags_a_stray_service_id(monkeypatch):
    definition = _build(monkeypatch, VIEW)
    dash.validate_definition(definition, config=SHOWN, data_item=VIEW)
    with pytest.raises(SystemExit, match="not the configured"):
        dash.validate_definition(definition, config=SHOWN, data_item=SVC)
    stray = json.loads(json.dumps(definition))
    stray["arcadeDataSourceItems"][0]["script"] = stray["arcadeDataSourceItems"][0]["script"].replace(
        f'"{VIEW}"', f'"{SVC}"')
    with pytest.raises(SystemExit, match="Arcade source"):
        dash.check_data_item(stray, VIEW)


# ------------------------------------------------------------------ migrate

# The view as --create-view leaves it with every switch off: definition_hash hidden.
NO_HASH = {publish.TABLE_INVENTORY: ("definition_hash",)}


def test_migrate_reports_without_writing(world, monkeypatch):
    world.tables.update(_view_tables(world.log, {**NO_HASH, publish.TABLE_RUNS: ("user_context",)}))
    publish.migrate_view(world.view, SHOWN, apply=False)
    assert world.log == []
    assert publish.view_missing_fields(world.tables, SHOWN) == {publish.TABLE_RUNS: ["user_context"]}


def test_migrate_exposes_missing_fields_and_resends_capabilities(world):
    drop = {**NO_HASH, publish.TABLE_RUNS: ("user_context",)}
    world.tables.update(_view_tables(world.log, drop))
    publish.migrate_view(world.view, SHOWN, apply=True)
    assert world.log[0] == ("update", publish.TABLE_RUNS,
                            {"fields": [{"name": "user_context", "visible": True}]})
    assert any(e[2] == {"capabilities": "Query"} for e in world.log[1:])


def test_migrate_does_not_expose_fields_the_switches_hide(world):
    world.tables.update(_view_tables(world.log, {publish.TABLE_INVENTORY: ("definition_hash", "run_as_user"),
                                                 publish.TABLE_RUNS: ("user_context",)}))
    cfg = {"privacy": dict(SHOWN["privacy"], hide_accounts=True)}
    publish.migrate_view(world.view, cfg, apply=True)
    assert world.log == []


def test_migrate_with_nothing_missing_writes_nothing(world):
    world.tables.update(_view_tables(world.log, NO_HASH))
    publish.migrate_view(world.view, SHOWN, apply=True)
    assert world.log == []
