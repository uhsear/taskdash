"""Audit wave 3, lane c: the one-command view replace, the host-name case rule, and leftovers.

No test signs in or reaches a network.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import taskdash_dashboard as dash
import taskdash_publish as publish
import taskdash_setup as setup


# ------------------------------------------------------------------ TD-020: --create-view --replace

class _OldView:
    def __init__(self, protected=True, everyone=False):
        self.id, self.title, self.protected, self.ownerFolder = "old1", "TaskDash_view", protected, "fold1"
        self.renames = []
        self.sharing = {"everyone": everyone, "org": True, "groups": ["grp1"]}

    def update(self, item_properties):
        self.renames.append(item_properties["title"])
        self.title = item_properties["title"]
        return True


def _replace_world(monkeypatch, tmp_path, old=None, manifest=True, taken=("TaskDash_view",)):
    old = old or _OldView()
    config_path = tmp_path / "TaskDash.config.json"
    config_path.write_text(json.dumps({"arcgis": {"item_id": "svc1", "view_item_id": "old1",
                                                  "dashboard_id": "dash1"}}), encoding="utf-8")
    if manifest:
        (tmp_path / setup.MANIFEST_FILE).write_text(json.dumps({"manifest_version": 1, "items": [
            {"order": 2, "id": "svc1", "type": "Feature Service", "title": "TaskDash"},
            {"order": 3, "id": "old1", "type": "Feature Service", "title": "TaskDash_view"},
            {"order": 5, "id": "dash1", "type": "Dashboard", "title": "TaskDash"}]}), encoding="utf-8")
    log = []
    new = SimpleNamespace(id="new1", title="TaskDash_view_v2")
    # A hosted service name is unique across the organization; a title change does not free it.
    gis = SimpleNamespace(content=SimpleNamespace(
        get=lambda item_id: old if item_id == "old1" else None,
        is_service_name_available=lambda name, kind: name not in taken,
        folders=SimpleNamespace(get=lambda folder: ("folder", folder))))

    def fake_create_view(gis_, item, config, apply, target="prod", folder=None, on_create=None, name=None):
        assert name not in taken, f"service name {name} is taken"
        log.append(("create_view", apply, folder, list(old.renames), name))
        if not apply:
            return None
        on_create(new)
        return new

    monkeypatch.setattr(publish, "connect_agol", lambda config: gis)
    monkeypatch.setattr(publish, "ensure_service",
                        lambda gis_, config, target: SimpleNamespace(id="svc1", title="TaskDash"))
    monkeypatch.setattr(publish, "item_sharing", lambda item: item.sharing)
    monkeypatch.setattr(publish, "create_view", fake_create_view)
    monkeypatch.setattr(publish, "share_item", lambda item, **kw: log.append(("share", item.id, kw)))
    monkeypatch.setattr(publish, "table_urls", lambda *a: [])
    monkeypatch.setattr(publish, "require_no_anonymous_access", lambda urls: log.append(("anonymous",)))
    monkeypatch.setattr(setup, "protect_items", lambda items, protected: log.append(("protect", items[0].id)))
    monkeypatch.setattr(dash, "main", lambda argv: log.append(("dashboard", argv)) or 0)
    args = SimpleNamespace(target="prod", apply=True, replace=True, config=str(config_path))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    return SimpleNamespace(old=old, new=new, log=log, args=args, config=config, path=config_path)


def test_replace_renames_creates_copies_sharing_and_protection_and_rebuilds(monkeypatch, tmp_path, capsys):
    world = _replace_world(monkeypatch, tmp_path)
    assert publish.run_create_view(world.config, world.args) == 0
    assert world.old.renames == ["TaskDash_view_old"]
    # The rename comes first: the new view needs the title.
    assert world.log[0] == ("create_view", True, ("folder", "fold1"), ["TaskDash_view_old"],
                            "TaskDash_view_v2")
    assert ("share", "new1", {"org": True, "groups": ["grp1"]}) in world.log
    assert ("protect", "new1") in world.log
    assert world.log[-1] == ("dashboard", ["--target", "prod", "--apply", "--config", str(world.path)])
    saved = json.loads(world.path.read_text(encoding="utf-8"))
    assert saved["arcgis"]["view_item_id"] == "new1"
    manifest = json.loads((world.path.with_name(setup.MANIFEST_FILE)).read_text(encoding="utf-8"))
    assert [(e["order"], e["id"]) for e in manifest["items"]] == [(2, "svc1"), (3, "new1"), (5, "dash1")]
    out = capsys.readouterr().out
    assert "TaskDash never deletes it" in out and "old1" in out


def test_replace_picks_a_service_name_that_no_item_uses(monkeypatch, tmp_path):
    world = _replace_world(monkeypatch, tmp_path, taken=("TaskDash_view", "TaskDash_view_v2"))
    publish.run_create_view(world.config, world.args)
    assert world.log[0][-1] == "TaskDash_view_v3"


def test_replace_refuses_before_any_write_when_no_service_name_is_free(monkeypatch, tmp_path):
    taken = ["TaskDash_view"] + [f"TaskDash_view_v{n}" for n in range(2, 20)]
    world = _replace_world(monkeypatch, tmp_path, taken=taken)
    with pytest.raises(SystemExit, match="No free service name"):
        publish.run_create_view(world.config, world.args)
    assert world.old.renames == [] and world.log == []


def test_replace_leaves_config_and_protection_alone_when_the_anonymous_check_stops(monkeypatch, tmp_path):
    world = _replace_world(monkeypatch, tmp_path)

    def stop(urls):
        raise SystemExit("inconclusive")

    monkeypatch.setattr(publish, "require_no_anonymous_access", stop)
    before = world.path.read_text(encoding="utf-8")
    with pytest.raises(SystemExit, match="inconclusive"):
        publish.run_create_view(world.config, world.args)
    assert world.path.read_text(encoding="utf-8") == before
    assert not [entry for entry in world.log if entry[0] in ("protect", "dashboard")]
    assert not list(tmp_path.glob("TaskDash.config.json.*"))


def test_replace_never_deletes_the_old_view(monkeypatch, tmp_path):
    world = _replace_world(monkeypatch, tmp_path)
    world.old.delete = lambda: pytest.fail("the old view must not be deleted")
    publish.run_create_view(world.config, world.args)


def test_replace_without_apply_writes_nothing(monkeypatch, tmp_path, capsys):
    world = _replace_world(monkeypatch, tmp_path)
    world.args.apply = False
    before = world.path.read_text(encoding="utf-8")
    assert publish.run_create_view(world.config, world.args) == 0
    assert world.old.renames == [] and world.path.read_text(encoding="utf-8") == before
    assert [entry[0] for entry in world.log] == ["create_view"]
    assert "DRY RUN" in capsys.readouterr().out


def test_replace_run_again_does_not_rename_a_second_time(monkeypatch, tmp_path):
    world = _replace_world(monkeypatch, tmp_path)
    world.old.title = "TaskDash_view_old"
    publish.run_create_view(world.config, world.args)
    assert world.old.renames == []


def test_replace_skips_protection_when_the_old_view_had_none(monkeypatch, tmp_path):
    world = _replace_world(monkeypatch, tmp_path, old=_OldView(protected=False))
    publish.run_create_view(world.config, world.args)
    assert not [entry for entry in world.log if entry[0] == "protect"]


def test_replace_leaves_a_manifest_that_does_not_list_the_old_view(monkeypatch, tmp_path, capsys):
    world = _replace_world(monkeypatch, tmp_path, manifest=False)
    assert publish.run_create_view(world.config, world.args) == 0
    assert not world.path.with_name(setup.MANIFEST_FILE).exists()
    assert "does not list the old view" in capsys.readouterr().out


def test_replace_refuses_an_old_view_shared_with_everyone(monkeypatch, tmp_path):
    world = _replace_world(monkeypatch, tmp_path, old=_OldView(everyone=True))
    with pytest.raises(SystemExit, match="never shares"):
        publish.run_create_view(world.config, world.args)
    assert world.old.renames == []


def test_replace_needs_a_configured_view(monkeypatch, tmp_path):
    world = _replace_world(monkeypatch, tmp_path)
    world.config["arcgis"]["view_item_id"] = ""
    with pytest.raises(SystemExit, match="no view to replace"):
        publish.run_create_view(world.config, world.args)


def test_replace_reports_a_failed_dashboard_rebuild_and_exits_1(monkeypatch, tmp_path, capsys):
    world = _replace_world(monkeypatch, tmp_path)

    def fail(argv):
        raise SystemExit("no layer")

    monkeypatch.setattr(dash, "main", fail)
    assert publish.run_create_view(world.config, world.args) == 1
    assert "taskdash dashboard --target prod --apply" in capsys.readouterr().out


def test_create_view_with_an_existing_view_names_the_replace_command(monkeypatch):
    args = SimpleNamespace(target="prod", apply=True, replace=False)
    with pytest.raises(SystemExit, match=r"--create-view --replace --apply"):
        publish.run_create_view({"arcgis": {"view_item_id": "old1"}}, args)


def test_replace_flag_needs_create_view():
    with pytest.raises(SystemExit):
        publish.parse_args(["--replace"])
    assert publish.parse_args(["--create-view", "--replace"]).replace is True


def test_replace_manifest_view_keeps_the_entry_order(tmp_path):
    config = tmp_path / "TaskDash.config.json"
    manifest = tmp_path / setup.MANIFEST_FILE
    manifest.write_text(json.dumps({"items": [{"order": 3, "id": "old1", "type": "Feature Service",
                                               "title": "V"}]}), encoding="utf-8")
    assert setup.replace_manifest_view(config, "old1", SimpleNamespace(id="n", title="V2")) is True
    assert json.loads(manifest.read_text(encoding="utf-8"))["items"][0] == {
        "order": 3, "id": "n", "type": "Feature Service", "title": "V2"}
    assert setup.replace_manifest_view(config, "other", SimpleNamespace(id="n", title="V2")) is False


# ------------------------------------------------------------------ TD-096: host names compare without case

def test_host_clause_keeps_the_column_bare_and_escapes_quotes():
    from taskdash_common import host_clause

    assert host_clause("Task-Pc") == "host_name = 'TASK-PC'"
    assert host_clause("o'Neil", exclude=True) == "host_name <> 'O''NEIL'"
    assert host_clause("a", "b") == "host_name IN ('A','B')"
    assert host_clause("a", "b", exclude=True) == "host_name NOT IN ('A','B')"

    # UPPER(host_name) would stop the index seek on a large table.
    for name in ("taskdash_common.py", "taskdash_publish.py", "taskdash_remove.py", "taskdash_cli.py"):
        assert "UPPER(host_name" not in (Path(__file__).resolve().parent / name).read_text(encoding="utf-8"), name


def test_no_service_where_clause_quotes_a_host_name_by_hand():
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent
    for name in ("taskdash_publish.py", "taskdash_remove.py", "taskdash_cli.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert not re.search(r"host_name\s*(=|<>|IN)\s*'", text), name


# ------------------------------------------------------------------ TD-024, TD-059, TD-168, TD-190: CHANGELOG

CHANGELOG = (Path(__file__).resolve().parent / "CHANGELOG.md").read_text(encoding="utf-8")


def test_each_release_with_upgrade_steps_opens_with_an_upgrade_notes_block():
    sections = re.split(r"(?m)^## \[", CHANGELOG)[1:]
    for section in sections:
        name = section.splitlines()[0]
        if "Upgrade" in section or "SCHEMA_VERSION" in section:
            first = re.search(r"(?m)^### (.+)$", section).group(1)
            assert first == "Upgrade notes", f"{name} must open with an Upgrade notes block"


def test_the_changelog_names_no_internal_id_or_stale_command():
    assert not re.search(r"H\d-\d|reference install|<wheel>\.sha256|five tables", CHANGELOG)
    assert not re.search(r"dashboard --apply", CHANGELOG)


def test_the_release_notes_carry_the_changelog_section():
    workflow = (Path(__file__).resolve().parent / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "$section.Value" in workflow and "--generate-notes" not in workflow.split("gh release create")[-1]


# ------------------------------------------------------------------ TD-208: the shipped descriptions file is an example

def test_the_repo_descriptions_file_is_a_generic_example_that_loads_clean():
    import taskdash_collect as collect

    root = Path(__file__).resolve().parent
    problems = []
    loaded = collect.load_descriptions(root / "workflow_descriptions.json", warnings=problems)
    assert problems == [] and loaded
    text = (root / "workflow_descriptions.json").read_text(encoding="utf-8-sig")
    assert "example" in json.loads(text)["_notes"].lower()


# ------------------------------------------------------------------ LEFTOVER: remove ignores this computer's earlier names

def test_remove_does_not_count_this_computers_earlier_names_as_other_computers(monkeypatch):
    import taskdash_remove as remove

    class Runs:
        def query(self, where, **kw):
            names = ["OLD-NAME", "second-pc", "OWNER-PC"]
            return SimpleNamespace(features=[SimpleNamespace(attributes={"host_name": n}) for n in names])

    monkeypatch.setattr(remove, "get_tables", lambda item: {"CollectorRuns": Runs()})
    gis = SimpleNamespace(content=SimpleNamespace(get=lambda item_id: object()))
    manifest = {"items": [{"order": 1, "id": "svc", "type": "Feature Service", "title": "T"}]}
    assert remove.other_active_hosts(gis, manifest, "OWNER-PC", 7) == ["OLD-NAME", "second-pc"]
    assert remove.other_active_hosts(gis, manifest, "OWNER-PC", 7, ["old-name"]) == ["second-pc"]


# ------------------------------------------------------------------ LEFTOVER: the placeholder smtp host warning shows

def test_a_placeholder_smtp_host_is_named_without_a_connection_attempt(monkeypatch):
    import smtplib

    import taskdash_preflight as pf

    def refuse(*a, **k):
        raise AssertionError("the placeholder host must not be probed")

    monkeypatch.setattr(smtplib, "SMTP", refuse)
    monkeypatch.setattr(smtplib, "SMTP_SSL", refuse)
    config = {"alerting": {"enabled": True, "to": ["ops@agency.test"]},
              "smtp": {"host": "smtp.example.com", "from": "td@agency.test"}}
    status, message = pf.check_smtp(pf.Context(SimpleNamespace(config="", offline=False),
                                               config={"rules": {"profiles": []}, **config}))
    assert status == pf.FAIL and "smtp.host" in message and "no connection was tried" in message


# ------------------------------------------------------------------ LEFTOVER: test-alert names a password only when one was read

@pytest.mark.parametrize("login, expected", [({"smtp password": "pw"}, "mail settings and password were read as"),
                                              ({}, "The mail settings were read as")])
def test_test_alert_mentions_a_password_only_when_a_login_is_set(tmp_path, monkeypatch, capsys, login, expected):
    import taskdash_cli as cli
    import taskdash_common as common

    config = tmp_path / "c.json"
    config.write_text(json.dumps({"logging": {"log_dir": str(tmp_path)}, "rules": {"profiles": []},
                                  "alerting": {"to": ["a@example.test"]}}), encoding="utf-8")
    monkeypatch.setattr(common, "load_mail_settings",
                        lambda cfg: {"smtp server": "mail.example.test", "from address": "td@example.test", **login})
    assert cli.main(["test-alert", "--config", str(config)]) == 0
    assert expected in capsys.readouterr().out


# ------------------------------------------------------------------ LEFTOVER: the rebuild warning prints once

def test_a_dashboard_update_warns_about_hand_edits_once(monkeypatch, tmp_path, capsys):
    posted = []
    gis = SimpleNamespace(
        url="https://org.example", _portal=SimpleNamespace(resturl="https://org.example/rest/"),
        users=SimpleNamespace(me=SimpleNamespace(username="svc")),
        _con=SimpleNamespace(post=lambda url, params: posted.append(url) or {"success": True}))
    existing = SimpleNamespace(id="D1", owner="svc", get_data=lambda: {"widgets": []})
    config = {"arcgis": {"item_id": "SVC", "dashboard_id": "D1"}, "logging": {"log_dir": str(tmp_path)}}
    monkeypatch.setattr(dash, "load_config", lambda path=None: config)
    monkeypatch.setattr(dash, "connect_agol", lambda c: gis)
    monkeypatch.setattr(dash, "load_templates", lambda gis_, source: {})
    monkeypatch.setattr(dash, "resolve_layer_ids", lambda gis_, item_id, target: dict(dash.DEFAULT_LAYER_IDS))
    monkeypatch.setattr(dash, "build", lambda *a, **k: {"widgets": []})
    monkeypatch.setattr(dash, "write_definition", lambda *a: None)
    monkeypatch.setattr(dash, "validate_definition", lambda *a, **k: None)
    monkeypatch.setattr(dash, "resolve_dashboard", lambda gis_, target, agol, title="": (existing, "T"))
    monkeypatch.setattr(dash, "backup_dashboard", lambda item, log_dir: None)
    monkeypatch.setattr(publish, "resolve_log_dir", lambda config: tmp_path)
    assert dash.main(["--target", "prod", "--apply"]) == 0
    out = capsys.readouterr().out
    assert posted and out.count("Dashboards editor") == 1


# ------------------------------------------------------------------ LEFTOVER: the demo About panel and its time claims

def test_the_demo_about_panel_names_no_collector_interval():
    terms = dash.help_terms({"scheduler": {"collector_interval_minutes": 43200}})
    text = dash.about_widget(terms, demo=True)["text"]
    assert "43200" not in text and "published once" in text
    assert "every 60 min" in dash.about_widget(dash.help_terms({}))["text"]


def test_help_names_a_month_long_interval_in_days_not_minutes():
    terms = dash.help_terms({"scheduler": {"collector_interval_minutes": 43200}})
    shown = dash.localize_help("up to 1 hour after it happened", terms)
    assert shown == "up to 30 days after it happened"


def test_demo_apply_states_the_real_heartbeat_limits(monkeypatch, capsys):
    import taskdash_demo as demo

    monkeypatch.setattr(demo, "preview_alerts", lambda *a: [])
    monkeypatch.setattr(demo.common, "load_config", lambda path: {})
    import taskdash_publish as publish

    monkeypatch.setattr(publish, "alert_dashboard_link", lambda config: "https://org.example/d")
    demo.finish_apply(Path("."), [])
    out = capsys.readouterr().out
    assert "OK for 45 days, Late until 90 days" in out and "months" not in out
