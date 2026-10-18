"""Phase 3: taskdash_setup takes a new organization to a private install.

Every test runs on fakes. A fake portal records each call; the check-mode test fails on any write.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import taskdash_common

taskdash_common.disable_arcpy_import()

import taskdash_dashboard as dashboard  # noqa: E402
import taskdash_publish as publish  # noqa: E402
import taskdash_setup as setup  # noqa: E402

CONFIG = {
    "host_name_override": "",
    "arcgis": {"url": "https://org.example.com", "profile": "p", "item_id": "", "view_item_id": "",
               "dashboard_id": "", "dashboard_tags": "a,b"},
    "alerting": {"enabled": False},
}


class FakeItem:
    """Records every method call, so a test can prove an item was or was not touched."""

    def __init__(self, item_id, title="x", item_type="Feature Service", log=None):
        self.id, self.title, self.type, self.log = item_id, title, item_type, log if log is not None else []

    def move(self, folder):
        self.log.append(("move", self.id))
        return {"success": True}

    @property
    def sharing(self):
        self.log.append(("item.sharing", self.id))

    def update(self, **kw):
        self.log.append(("item.update", self.id))

    def delete(self):
        self.log.append(("item.delete", self.id))


class FakeGis:
    def __init__(self, log, writes_allowed=True, folders=(), items=(), groups=()):
        self.log, self.writes_allowed = log, writes_allowed
        self.url = "https://org.example.com"
        self.users = SimpleNamespace(me=SimpleNamespace(username="owner1"))
        self._folders, self._items, self._groups = set(folders), list(items), list(groups)
        self.by_id = {}
        self.content = SimpleNamespace(folders=SimpleNamespace(get=self._get_folder, create=self._create_folder),
                                       search=lambda query, **kw: list(self._items),
                                       get=lambda item_id: self.by_id.get(item_id))
        self.groups = SimpleNamespace(search=lambda query, **kw: list(self._groups), create=self._create_group)

    def _write(self, name):
        if not self.writes_allowed:
            pytest.fail(f"check mode wrote: {name}")
        self.log.append((name,))

    def _get_folder(self, folder=None):
        return SimpleNamespace(properties={"id": "f1"}) if folder in self._folders else None

    def _create_folder(self, folder):
        self._write("folder.create")
        return SimpleNamespace(properties={"id": "f1"})

    def _create_group(self, **kw):
        self._write("group.create")
        self.log.append(("group.kw", kw["access"], kw["is_invitation_only"]))
        return SimpleNamespace(id="g1", title=kw["title"])


def _args(tmp_path, **over):
    path = tmp_path / "TaskDash.config.json"
    if not path.exists():
        path.write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")
    argv = ["--config", str(path)] + [a for k, v in over.items() for a in ((f"--{k}",) if v is True
                                                                          else (f"--{k}", v))]
    return path, setup.parse_args(argv)


def _patch_publish_and_dashboard(monkeypatch, gis, path, log, fail_view=False):
    svc = FakeItem("svc1", "TaskDash", log=log)
    view = FakeItem("view1", "TaskDash_view", log=log)
    dash = FakeItem("dash1", "TaskDash", "Dashboard", log=log)
    gis.by_id["dash1"] = dash

    def create_service(g, title, config=None, folder=None):
        log.append(("create_service", title, folder))
        return svc

    def create_view(g, item, config, apply, target="prod"):
        log.append(("create_view", item.id, json.loads(path.read_text())["arcgis"]["item_id"]))
        if fail_view:
            raise SystemExit("view failed. fix it.")
        return view

    def share_item(item, **kw):
        log.append(("share", item.id, tuple(getattr(g, "id", g) for g in kw.get("groups", ())),
                    kw.get("org", False)))

    def post_dashboard(g, definition, title, existing, config=None):
        log.append(("post_dashboard", title, existing, definition))
        return "dash1"

    monkeypatch.setattr(publish, "create_service", create_service)
    monkeypatch.setattr(publish, "get_tables", lambda item: {"t": 1})
    monkeypatch.setattr(setup, "get_tables", lambda item: {"t": 1})
    monkeypatch.setattr(publish, "apply_migration",
                        lambda tables, item=None: log.append(("apply_migration", item.id)))
    monkeypatch.setattr(publish, "create_view", create_view)
    monkeypatch.setattr(publish, "share_item", share_item)
    monkeypatch.setattr(publish, "table_urls", lambda *items: ["https://h/0"])
    monkeypatch.setattr(publish, "require_no_anonymous_access", lambda urls: log.append(("anonymous", tuple(urls))))
    monkeypatch.setattr(dashboard, "load_templates", lambda g, src: log.append(("load_templates", src)) or {})
    monkeypatch.setattr(dashboard, "resolve_layer_ids",
                        lambda g, item_id, target: log.append(("layer_ids", item_id, target)) or {})
    monkeypatch.setattr(dashboard, "build",
                        lambda item_id, templates, layer_ids, portal_url, config: {"read": item_id})
    monkeypatch.setattr(dashboard, "post_dashboard", post_dashboard)
    monkeypatch.setattr(dashboard, "validate_definition",
                        lambda definition, layer_ids, config, data_item="": log.append(("validate", data_item)))
    return svc, view, dash


# ------------------------------------------------------------------ check mode

def test_check_mode_makes_zero_writes(tmp_path, monkeypatch, capsys):
    log = []
    path, args = _args(tmp_path)
    before = path.read_text(encoding="utf-8")
    gis = FakeGis(log, writes_allowed=False)
    for module, name in ((publish, "create_service"), (publish, "apply_migration"), (publish, "create_view"),
                         (publish, "share_item"), (dashboard, "post_dashboard")):
        monkeypatch.setattr(module, name, lambda *a, _n=name, **k: pytest.fail(f"check mode called {_n}"))
    assert setup.setup(gis, json.loads(before), path, args) == 0
    out = capsys.readouterr().out
    assert log == []
    assert path.read_text(encoding="utf-8") == before
    assert list(tmp_path.iterdir()) == [path]
    assert "DRY RUN - nothing written. Re-run with --apply." in out
    for text in ("folder 'TaskDash'", "TaskDash_view", "TaskDash viewers", "capabilities Query",
                 taskdash_common.TABLE_COLLECTOR_RUNS, "arcgis.dashboard_id", "definition_hash"):
        assert text in out


# ------------------------------------------------------------------ refusals

@pytest.mark.parametrize("key", ["item_id", "view_item_id", "dashboard_id"])
def test_refuses_a_config_that_already_has_an_id(tmp_path, key):
    log = []
    path, args = _args(tmp_path)
    config = json.loads(path.read_text())
    config["arcgis"][key] = "abc"
    with pytest.raises(SystemExit, match=f"arcgis.{key}"):
        setup.setup(FakeGis(log, writes_allowed=False), config, path, args)
    assert log == []


def test_refuses_an_existing_folder(tmp_path):
    path, args = _args(tmp_path, apply=True)
    with pytest.raises(SystemExit, match="folder 'TaskDash' already exists"):
        setup.setup(FakeGis([], folders={"TaskDash"}), CONFIG, path, args)


@pytest.mark.parametrize("title,kind,prefix", [
    ("TaskDash", "Feature Service", ""), ("TaskDash_view", "Feature Service", ""),
    ("TaskDash", "Dashboard", ""), ("Acme TaskDash", "Feature Service", "Acme "),
])
def test_refuses_an_existing_item_with_a_planned_title(tmp_path, title, kind, prefix):
    over = {"title-prefix": prefix} if prefix else {}
    path, args = _args(tmp_path, apply=True, **over)
    gis = FakeGis([], items=[FakeItem("old1", title, kind)])
    with pytest.raises(SystemExit, match="already exists \\(old1\\)"):
        setup.setup(gis, CONFIG, path, args)


def test_an_item_with_the_title_but_another_type_is_not_a_clash(tmp_path):
    path, args = _args(tmp_path)
    assert setup.setup(FakeGis([], items=[FakeItem("m1", "TaskDash", "Web Map")]), CONFIG, path, args) == 0


def test_refuses_an_existing_group(tmp_path):
    path, args = _args(tmp_path, apply=True)
    with pytest.raises(SystemExit, match="group titled 'TaskDash viewers' already exists"):
        setup.setup(FakeGis([], groups=[SimpleNamespace(id="g0", title="TaskDash viewers")]),
                    CONFIG, path, args)


def test_group_title_defaults_to_the_prefix():
    assert setup.parse_args(["--title-prefix", "Acme "]).group_title == "Acme TaskDash viewers"
    assert setup.parse_args(["--group-title", "Mine"]).group_title == "Mine"


def test_flags_are_not_abbreviated():
    with pytest.raises(SystemExit):
        setup.parse_args(["--app"])


# ------------------------------------------------------------------ apply

def test_apply_runs_in_order_and_shares_with_the_group_only(tmp_path, monkeypatch, capsys):
    log = []
    path, args = _args(tmp_path, apply=True)
    gis = FakeGis(log)
    _patch_publish_and_dashboard(monkeypatch, gis, path, log)
    assert setup.setup(gis, json.loads(path.read_text()), path, args) == 0
    names = [c[0] for c in log]
    assert names == ["folder.create", "create_service", "apply_migration", "create_view", "group.create",
                     "group.kw", "share", "load_templates", "layer_ids", "validate", "post_dashboard",
                     "move", "share", "anonymous"]
    assert ("create_service", "TaskDash", "TaskDash") in log
    # item_id was already in the config when the view was created.
    assert ("create_view", "svc1", "svc1") in log
    assert ("group.kw", "private", True) in log
    assert ("layer_ids", "view1", "prod") in log and ("validate", "view1") in log
    assert [c for c in log if c[0] == "post_dashboard"][0][2:] == (None, {"read": "view1"})
    shares = [c for c in log if c[0] == "share"]
    assert shares == [("share", "view1", ("g1",), False), ("share", "dash1", ("g1",), False)]
    assert not any(c[1] == "svc1" for c in shares)
    assert json.loads(path.read_text())["arcgis"]["dashboard_id"] == "dash1"
    out = capsys.readouterr().out
    assert "Created by this run" in out
    for item_id in ("f1", "svc1", "view1", "g1", "dash1"):
        assert item_id in out


def test_config_write_back_keeps_other_keys_and_order_and_backs_up(tmp_path, monkeypatch):
    log = []
    path, args = _args(tmp_path, apply=True)
    original = path.read_text(encoding="utf-8")
    # A BOM, as PowerShell writes it, must be read and not break the rewrite.
    path.write_text(original, encoding="utf-8-sig")
    gis = FakeGis(log)
    _patch_publish_and_dashboard(monkeypatch, gis, path, log)
    setup.setup(gis, json.loads(original), path, args)
    after = json.loads(path.read_text(encoding="utf-8-sig"))
    assert list(after) == list(CONFIG)
    assert list(after["arcgis"]) == list(CONFIG["arcgis"])
    assert after["arcgis"]["dashboard_tags"] == "a,b" and after["alerting"] == CONFIG["alerting"]
    assert (after["arcgis"]["item_id"], after["arcgis"]["view_item_id"],
            after["arcgis"]["dashboard_id"]) == ("svc1", "view1", "dash1")
    backups = list(tmp_path.glob("TaskDash.config.json.bak_setup_*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8-sig")) == json.loads(original)


def test_failure_mid_run_prints_what_was_created_and_keeps_the_ids(tmp_path, monkeypatch, capsys):
    log = []
    path, args = _args(tmp_path, apply=True)
    gis = FakeGis(log)
    _patch_publish_and_dashboard(monkeypatch, gis, path, log, fail_view=True)
    with pytest.raises(SystemExit, match="view failed"):
        setup.setup(gis, json.loads(path.read_text()), path, args)
    out = capsys.readouterr().out
    assert "Created by this run" in out and "svc1" in out and "Folder" in out
    assert "view1" not in out
    assert json.loads(path.read_text())["arcgis"]["item_id"] == "svc1"
    assert not any(c[0] in ("group.create", "post_dashboard") for c in log)


def test_never_touches_an_item_it_did_not_create(tmp_path, monkeypatch):
    log = []
    path, args = _args(tmp_path, apply=True)
    foreign = FakeItem("foreign1", "Someone else's service", "Feature Service", log)
    gis = FakeGis(log, items=[foreign], groups=[SimpleNamespace(id="g9", title="Other group")])
    _patch_publish_and_dashboard(monkeypatch, gis, path, log)
    setup.setup(gis, json.loads(path.read_text()), path, args)
    assert not any("foreign1" in map(str, c) for c in log)


def test_group_not_created_stops_the_run(tmp_path, monkeypatch, capsys):
    log = []
    path, args = _args(tmp_path, apply=True)
    gis = FakeGis(log)
    gis.groups.create = lambda **kw: None
    _patch_publish_and_dashboard(monkeypatch, gis, path, log)
    with pytest.raises(SystemExit, match="did not create the group"):
        setup.setup(gis, json.loads(path.read_text()), path, args)
    assert "view1" in capsys.readouterr().out


# ------------------------------------------------------------------ publish --create-service

def test_create_service_apply_also_migrates(monkeypatch, tmp_path, capsys):
    log = []
    item = SimpleNamespace(id="svcX")
    monkeypatch.setattr(publish, "connect_agol", lambda config: "gis")
    monkeypatch.setattr(publish, "create_service", lambda g, name, config: item)
    monkeypatch.setattr(publish, "get_tables", lambda i: {"t": 1})
    monkeypatch.setattr(publish, "apply_migration", lambda tables, item=None: log.append(item.id))
    args = publish.parse_args(["--create-service", "--apply"])
    assert publish._main_dispatch(args, {}, tmp_path) == 0
    assert log == ["svcX"]
    assert "no --migrate needed" in capsys.readouterr().out
