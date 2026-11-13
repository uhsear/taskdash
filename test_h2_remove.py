"""H2-8: setup saves a manifest of what it created, and `taskdash remove` undoes it from that list.

Every portal call is a fake. Nothing signs in and nothing leaves tmp_path.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import taskdash_cli as cli
import taskdash_remove as remove
import taskdash_setup as setup
from test_phase3_setup import FakeGis, _args, _patch_publish_and_dashboard

ME = "owner1"


class RemoveItem:
    """An item or group that logs protect/delete in call order."""

    def __init__(self, log, item_id, owner=ME, protect_ok=True, delete_error=None, reprotect_ok=True,
                 kind=None, title=None, views=()):
        self.log, self.id, self.owner = log, item_id, owner
        self.views = [SimpleNamespace(id=v) for v in views]
        self.protect_ok, self.delete_error, self.reprotect_ok = protect_ok, delete_error, reprotect_ok
        # By default the item still matches its manifest entry; a test overrides one to drift.
        listed = next((e for e in MANIFEST["items"] if e["id"] == item_id), {})
        self.type, self.title = kind or listed.get("type"), title or listed.get("title")

    def protect(self, enable=True):
        self.log.append(("protect", self.id, enable))
        return {"success": self.protect_ok if not enable else self.reprotect_ok}

    def related_items(self, rel_type, direction="forward"):
        assert (rel_type, direction) == ("Service2Service", "forward")
        return self.views

    def delete(self):
        if self.delete_error:
            raise RuntimeError(self.delete_error)
        self.log.append(("delete", self.id))
        return True


class RemoveFolder:
    def __init__(self, log, content=(), folder_id="f1"):
        self.log, self.content = log, list(content)
        self.properties = {"id": folder_id}

    def list(self):
        return iter(self.content)

    def delete(self):
        self.log.append(("delete", "folder:TaskDash"))
        return True


def _entry(order, item_id, kind, title):
    return {"order": order, "id": item_id, "type": kind, "title": title}


MANIFEST = {
    "manifest_version": 1, "taskdash_version": "1.2.0", "folder": "TaskDash", "group_id": "g1",
    "items": [_entry(1, "f1", "Folder", "TaskDash"), _entry(2, "svc1", "Feature Service", "TaskDash"),
              _entry(3, "view1", "Feature Service", "TaskDash_view"), _entry(4, "g1", "Group", "TaskDash viewers"),
              _entry(5, "dash1", "Dashboard", "TaskDash")],
}


def _gis(log, **objects):
    """A portal that serves the manifest's objects; override one with objects={'view1': RemoveItem(...)}."""
    by_id = {i: RemoveItem(log, i) for i in ("svc1", "view1", "dash1")}
    by_id.update({k: v for k, v in objects.items() if k != "group" and k != "folder"})
    group = objects.get("group", RemoveItem(log, "g1"))
    made_folder = objects.get("folder", RemoveFolder(log))
    return SimpleNamespace(
        users=SimpleNamespace(me=SimpleNamespace(username=ME)),
        content=SimpleNamespace(get=lambda item_id: by_id.get(item_id),
                                folders=SimpleNamespace(get=lambda folder=None: made_folder if folder in (
                                    "TaskDash", made_folder.properties["id"]) else None)),
        groups=SimpleNamespace(get=lambda group_id: group if group_id == "g1" else None))


def _deletes(log):
    return [c[1] for c in log if c[0] == "delete"]


# ------------------------------------------------------------------ setup writes the manifest

def test_setup_apply_saves_a_manifest_next_to_the_config(tmp_path, monkeypatch):
    log = []
    path, args = _args(tmp_path, apply=True)
    gis = FakeGis(log)
    _patch_publish_and_dashboard(monkeypatch, gis, path, log)
    setup.setup(gis, json.loads(path.read_text()), path, args)
    manifest = json.loads((tmp_path / setup.MANIFEST_FILE).read_text(encoding="utf-8"))
    assert manifest["folder"] == "TaskDash" and manifest["group_id"] == "g1"
    assert [(e["order"], e["id"], e["type"], e["title"]) for e in manifest["items"]] == [
        (1, "f1", "Folder", "TaskDash"), (2, "svc1", "Feature Service", "TaskDash"),
        (3, "view1", "Feature Service", "TaskDash_view"), (4, "g1", "Group", "TaskDash viewers"),
        (5, "dash1", "Dashboard", "TaskDash")]
    assert "password" not in json.dumps(manifest).lower()


def test_setup_check_mode_writes_no_manifest(tmp_path):
    log = []
    path, args = _args(tmp_path)
    setup.setup(FakeGis(log, writes_allowed=False), json.loads(path.read_text()), path, args)
    assert not (tmp_path / setup.MANIFEST_FILE).exists()


def test_a_failed_setup_still_saves_what_it_created(tmp_path, monkeypatch):
    log = []
    path, args = _args(tmp_path, apply=True)
    gis = FakeGis(log)
    _patch_publish_and_dashboard(monkeypatch, gis, path, log, fail_view=True)
    with pytest.raises(SystemExit):
        setup.setup(gis, json.loads(path.read_text()), path, args)
    manifest = json.loads((tmp_path / setup.MANIFEST_FILE).read_text(encoding="utf-8"))
    assert [e["id"] for e in manifest["items"]] == ["f1", "svc1"]


# ------------------------------------------------------------------ remove: check mode

def test_check_mode_lists_in_reverse_order_and_deletes_nothing(capsys):
    log = []
    assert remove.remove(_gis(log), MANIFEST, apply=False, task_name="TaskDash Collector") == 0
    out = capsys.readouterr().out
    assert log == []
    positions = [out.index(i) for i in ("dash1", "g1", "view1", "svc1", "Folder           f1")]
    assert positions == sorted(positions)
    assert "Nothing deleted. Re-run with --apply" in out
    assert 'Unregister-ScheduledTask -TaskName "TaskDash Collector"' in out
    assert "pip uninstall taskdash" in out


def test_a_missing_manifest_says_how_to_continue(tmp_path, monkeypatch):
    path, _ = _args(tmp_path)
    monkeypatch.setattr(remove, "connect_agol", lambda config: pytest.fail("signed in without a manifest"))
    with pytest.raises(SystemExit, match="no setup manifest") as exc:
        remove.main(["--config", str(path)])
    assert "Undo the install" in str(exc.value) and setup.MANIFEST_FILE in str(exc.value)


def test_a_manifest_that_is_not_valid_json_says_how_to_continue(tmp_path):
    path, _ = _args(tmp_path)
    (tmp_path / setup.MANIFEST_FILE).write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit, match="cannot be read"):
        remove.main(["--config", str(path)])


# ------------------------------------------------------------------ remove: --apply

def test_apply_deletes_in_reverse_order_and_unprotects_each_item_first():
    log = []
    assert remove.remove(_gis(log), MANIFEST, apply=True, task_name="t") == 0
    assert log == [("protect", "dash1", False), ("delete", "dash1"),
                   ("delete", "g1"),
                   ("protect", "view1", False), ("delete", "view1"),
                   ("protect", "svc1", False), ("delete", "svc1"),
                   ("delete", "folder:TaskDash")]


def test_an_item_owned_by_someone_else_is_skipped_and_reported(capsys):
    log = []
    gis = _gis(log, view1=RemoveItem(log, "view1", owner="other"))
    assert remove.remove(gis, MANIFEST, apply=True, task_name="t") == 1
    out = capsys.readouterr().out
    assert "view1" not in _deletes(log) and "view1" not in [c[1] for c in log if c[0] == "protect"]
    assert "Skipped" in out and "owned by other" in out


def test_the_service_is_kept_while_its_view_is_kept(capsys):
    # A deleted service would leave the surviving view pointing at nothing.
    log = []
    held = RemoveFolder(log, content=[SimpleNamespace(id="svc1"), SimpleNamespace(id="view1")])
    gis = _gis(log, view1=RemoveItem(log, "view1", owner="other"), folder=held)
    assert remove.remove(gis, MANIFEST, apply=True, task_name="t") == 1
    out = capsys.readouterr().out
    assert "svc1" not in _deletes(log) and "svc1" not in [c[1] for c in log if c[0] == "protect"]
    assert "its view still exists" in out
    assert "folder:TaskDash" not in _deletes(log)


def test_the_service_is_kept_when_the_view_delete_fails(capsys):
    log = []
    gis = _gis(log, view1=RemoveItem(log, "view1", delete_error="item is shared"))
    assert remove.remove(gis, MANIFEST, apply=True, task_name="t") == 1
    assert "svc1" not in _deletes(log) and "its view still exists" in capsys.readouterr().out


def test_check_mode_says_the_service_would_be_kept(capsys):
    log = []
    gis = _gis(log, view1=RemoveItem(log, "view1", owner="other"))
    assert remove.remove(gis, MANIFEST, apply=False, task_name="t") == 0
    out = capsys.readouterr().out
    assert "Would skip" in out and "its view still exists" in out and log == []


def test_an_item_that_is_already_gone_is_reported_not_an_error(capsys):
    log = []
    gis = _gis(log)
    gis.content.get = lambda item_id: None if item_id == "dash1" else RemoveItem(log, item_id)
    assert remove.remove(gis, MANIFEST, apply=True, task_name="t") == 0
    assert "dash1" in capsys.readouterr().out and "dash1" not in _deletes(log)


def test_a_failed_delete_is_skipped_and_the_rest_continue(capsys):
    log = []
    gis = _gis(log, dash1=RemoveItem(log, "dash1", delete_error="item is shared"))
    assert remove.remove(gis, MANIFEST, apply=True, task_name="t") == 1
    assert _deletes(log) == ["g1", "view1", "svc1", "folder:TaskDash"]
    assert "item is shared" in capsys.readouterr().out


def test_a_failed_delete_turns_delete_protection_back_on(capsys):
    log = []
    gis = _gis(log, dash1=RemoveItem(log, "dash1", delete_error="item is shared"))
    remove.remove(gis, MANIFEST, apply=True, task_name="t")
    assert [c for c in log if c[1] == "dash1"] == [("protect", "dash1", False), ("protect", "dash1", True)]
    assert "delete protection is off" not in capsys.readouterr().out


def test_a_failed_delete_says_so_when_protection_cannot_be_restored(capsys):
    log = []
    gis = _gis(log, dash1=RemoveItem(log, "dash1", delete_error="item is shared", reprotect_ok=False))
    assert remove.remove(gis, MANIFEST, apply=True, task_name="t") == 1
    out = capsys.readouterr().out
    assert "item is shared" in out and "delete protection is off" in out


def test_an_item_whose_protection_cannot_be_turned_off_is_not_deleted(capsys):
    log = []
    gis = _gis(log, svc1=RemoveItem(log, "svc1", protect_ok=False))
    assert remove.remove(gis, MANIFEST, apply=True, task_name="t") == 1
    assert "svc1" not in _deletes(log)
    assert ("protect", "svc1", True) not in log  # it was never unprotected, so there is nothing to restore
    assert "delete protection" in capsys.readouterr().out


def test_a_folder_that_still_holds_items_is_kept(capsys):
    log = []
    gis = _gis(log, folder=RemoveFolder(log, content=[SimpleNamespace(id="stranger")]))
    assert remove.remove(gis, MANIFEST, apply=True, task_name="t") == 1
    assert "folder:TaskDash" not in _deletes(log)
    assert "not empty" in capsys.readouterr().out


def test_a_folder_with_the_same_title_but_another_id_is_kept(capsys):
    # Another account's folder, or a re-created one, shares the title but is not the manifest's folder.
    log = []
    gis = _gis(log, folder=RemoveFolder(log, folder_id="other-folder"))
    assert remove.remove(gis, MANIFEST, apply=True, task_name="t") == 1
    assert "folder:TaskDash" not in _deletes(log)
    assert "different folder" in capsys.readouterr().out


def test_a_manifest_folder_without_an_id_is_still_matched_by_title():
    log = []
    manifest = {**MANIFEST, "items": [{**e, "id": ""} if e["type"] == "Folder" else e for e in MANIFEST["items"]]}
    assert remove.remove(_gis(log), manifest, apply=True, task_name="t") == 0
    assert "folder:TaskDash" in _deletes(log)


def test_a_group_owned_by_someone_else_is_skipped():
    log = []
    gis = _gis(log, group=RemoveItem(log, "g1", owner="other"))
    assert remove.remove(gis, MANIFEST, apply=True, task_name="t") == 1
    assert "g1" not in _deletes(log)


@pytest.mark.parametrize("drift", [{"kind": "Web Map"}, {"title": "Someone else's layer"}])
def test_an_item_that_no_longer_matches_its_manifest_entry_is_skipped(capsys, drift):
    # The id now names a different item (or one retitled by hand): setup did not create what is there.
    log = []
    gis = _gis(log, dash1=RemoveItem(log, "dash1", **drift))
    assert remove.remove(gis, MANIFEST, apply=True, task_name="t") == 1
    assert [c for c in log if c[1] == "dash1"] == []
    assert "that setup created" in capsys.readouterr().out


def test_a_group_retitled_since_setup_is_skipped(capsys):
    log = []
    gis = _gis(log, group=RemoveItem(log, "g1", title="Another group"))
    assert remove.remove(gis, MANIFEST, apply=True, task_name="t") == 1
    assert "g1" not in _deletes(log) and "that setup created" in capsys.readouterr().out


def test_the_folder_is_found_by_its_manifest_id_when_the_title_changed():
    log = []
    folder = RemoveFolder(log)
    gis = _gis(log, folder=folder)
    seen = []
    real_get = gis.content.folders.get
    gis.content.folders.get = lambda folder=None: (seen.append(folder), real_get(folder=folder))[1]
    assert remove.remove(gis, MANIFEST, apply=True, task_name="t") == 0
    assert seen == ["f1"] and "folder:TaskDash" in _deletes(log)


# ------------------------------------------------------------------ command line

def test_main_signs_in_with_the_config_and_uses_the_collector_task_name(tmp_path, monkeypatch, capsys):
    path, _ = _args(tmp_path)
    path.write_text(json.dumps({**json.loads(path.read_text()), "rules": {"profiles": []},
                                "scheduler": {"collector_task_name": "Synthetic Collector"}}), encoding="utf-8")
    (tmp_path / setup.MANIFEST_FILE).write_text(json.dumps(MANIFEST), encoding="utf-8")
    log = []
    monkeypatch.setattr(remove, "connect_agol", lambda config: _gis(log))
    assert remove.main(["--config", str(path)]) == 0
    assert log == []
    assert '-TaskName "Synthetic Collector"' in capsys.readouterr().out


def test_the_cli_routes_remove_to_its_module():
    assert cli.SUBCOMMAND_MODULES["remove"] == "taskdash_remove"
    assert "remove" in cli.USAGE


def test_flags_are_not_abbreviated():
    with pytest.raises(SystemExit):
        remove.parse_args(["--app"])


def test_a_service_with_a_view_outside_the_manifest_is_kept(capsys):
    log = []
    # collect --create-view made view9 later; ArcGIS would delete it with the service.
    gis = _gis(log, svc1=RemoveItem(log, "svc1", views=("view1", "view9")))
    assert remove.remove(gis, MANIFEST, apply=True, task_name="T") == 1
    assert "svc1" not in _deletes(log) and "view1" in _deletes(log)
    assert "it has a view that is not in the manifest: view9" in capsys.readouterr().out


def test_a_protected_group_is_skipped_not_cleared(capsys):
    log = []
    group = RemoveItem(log, "g1")
    group.protected = True
    assert remove.remove(_gis(log, group=group), MANIFEST, apply=True, task_name="T") == 1
    assert "g1" not in _deletes(log) and not any(c[1] == "g1" for c in log if c[0] == "protect")
    assert "delete protection is on" in capsys.readouterr().out


# ------------------------------------------------------------------ remove: other computers still publish

class RunsTable:
    def __init__(self, hosts=(), error=None):
        self.hosts, self.error, self.wheres = hosts, error, []

    def query(self, where, **kwargs):
        self.wheres.append(where)
        if self.error:
            raise RuntimeError(self.error)
        return SimpleNamespace(features=[SimpleNamespace(attributes={"host_name": h}) for h in self.hosts])


def _shared(monkeypatch, runs, tmp_path):
    path, _ = _args(tmp_path)
    (tmp_path / setup.MANIFEST_FILE).write_text(json.dumps(MANIFEST), encoding="utf-8")
    config = {**json.loads(path.read_text()), "rules": {"profiles": []}, "host_name_override": "OWNER-PC"}
    path.write_text(json.dumps(config), encoding="utf-8")
    log = []
    monkeypatch.setattr(remove, "connect_agol", lambda config: _gis(log))
    monkeypatch.setattr(remove, "get_tables", lambda item: {"CollectorRuns": runs} if runs else {})
    monkeypatch.setattr(remove, "get_host_name", lambda config: "OWNER-PC")
    return path, log


def test_apply_refuses_while_another_computer_has_a_recent_run(tmp_path, monkeypatch):
    runs = RunsTable(hosts=["SECOND-PC"])
    path, log = _shared(monkeypatch, runs, tmp_path)
    with pytest.raises(SystemExit, match="SECOND-PC") as exc:
        remove.main(["--config", str(path), "--apply"])
    assert "Retire a computer" in str(exc.value) and log == []
    assert "host_name <> 'OWNER-PC'" in runs.wheres[0] and "started_utc >= TIMESTAMP" in runs.wheres[0]


def test_check_mode_warns_about_another_computer_and_still_lists(tmp_path, monkeypatch, capsys):
    path, log = _shared(monkeypatch, RunsTable(hosts=["SECOND-PC"]), tmp_path)
    assert remove.main(["--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert "WARNING" in out and "SECOND-PC" in out and "Would delete" in out and log == []


def test_apply_goes_on_when_only_this_computer_published(tmp_path, monkeypatch):
    path, log = _shared(monkeypatch, RunsTable(hosts=["owner-pc"]), tmp_path)
    assert remove.main(["--config", str(path), "--apply"]) == 0
    assert "svc1" in _deletes(log)


def test_apply_refuses_when_the_runs_table_cannot_be_read(tmp_path, monkeypatch):
    path, log = _shared(monkeypatch, RunsTable(error="timed out"), tmp_path)
    with pytest.raises(SystemExit, match="Cannot check"):
        remove.main(["--config", str(path), "--apply"])
    assert log == []


def test_a_service_without_a_runs_table_is_not_blocked(tmp_path, monkeypatch):
    path, log = _shared(monkeypatch, None, tmp_path)
    assert remove.main(["--config", str(path), "--apply"]) == 0
    assert "svc1" in _deletes(log)


# ------------------------------------------------------------------ remove: config ids and next steps

def _with_ids(monkeypatch, tmp_path, **gis_objects):
    path, log = _shared(monkeypatch, None, tmp_path)
    config = json.loads(path.read_text())
    config["arcgis"].update({"item_id": "svc1", "view_item_id": "view1", "dashboard_id": "dash1",
                             "test_item_id": "other-test-service", "profile": "taskdash"})
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(remove, "connect_agol", lambda c: _gis(log, **gis_objects))
    return path, log


def test_apply_backs_up_the_config_and_empties_the_ids_of_deleted_items(tmp_path, monkeypatch, capsys):
    path, _ = _with_ids(monkeypatch, tmp_path)
    assert remove.main(["--config", str(path), "--apply"]) == 0
    arcgis = json.loads(path.read_text())["arcgis"]
    assert (arcgis["item_id"], arcgis["view_item_id"], arcgis["dashboard_id"]) == ("", "", "")
    # An id that the manifest does not list is not ours to empty; other keys stay.
    assert arcgis["test_item_id"] == "other-test-service" and arcgis["profile"] == "taskdash"
    assert len(list(tmp_path.glob("TaskDash.config.json.bak_setup_*"))) == 1
    assert "Emptied the ids of the deleted items" in capsys.readouterr().out


def test_check_mode_leaves_the_config_alone(tmp_path, monkeypatch):
    path, _ = _with_ids(monkeypatch, tmp_path)
    before = path.read_text()
    assert remove.main(["--config", str(path)]) == 0
    assert path.read_text() == before and list(tmp_path.glob("*.bak_setup_*")) == []


def test_an_id_stays_when_its_item_was_skipped(tmp_path, monkeypatch):
    log = []
    path, _ = _with_ids(monkeypatch, tmp_path, view1=RemoveItem(log, "view1", delete_error="refused"))
    assert remove.main(["--config", str(path), "--apply"]) == 1
    arcgis = json.loads(path.read_text())["arcgis"]
    # The view could not be deleted, so the service stays too; only the dashboard went.
    assert arcgis["dashboard_id"] == "" and arcgis["view_item_id"] == "view1" and arcgis["item_id"] == "svc1"


def test_next_steps_forget_the_sign_ins_before_the_task_goes_and_quote_pip_for_powershell(capsys):
    remove.print_next_steps("My Task")
    lines = [line.strip() for line in capsys.readouterr().out.splitlines()]
    order = [next(i for i, line in enumerate(lines) if needle in line)
             for needle in ("signin --forget --apply", "signin --forget --for-task-account --apply",
                            "Unregister-ScheduledTask", "pip uninstall taskdash")]
    assert order == sorted(order)
    pip = lines[order[-1]]
    # A quoted path followed by arguments is a parse error in PowerShell unless the call operator leads.
    assert pip.startswith('& "') and pip.endswith('" -m pip uninstall taskdash')
    assert 'Unregister-ScheduledTask -TaskName "My Task"' in " ".join(lines)
