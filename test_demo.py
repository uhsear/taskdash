"""Phase 4: `taskdash demo` runs on synthetic data in its own folder and never touches the real install.

No test signs in or reaches a network: every sign-in is a fake that fails the test if called, and
the --apply tests replace setup and publish with recorders.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import taskdash_cli as cli
import taskdash_common as common

common.disable_arcpy_import()

import taskdash_dashboard as dashboard  # noqa: E402
import taskdash_demo as demo  # noqa: E402
import taskdash_publish as publish  # noqa: E402
import taskdash_remove as remover  # noqa: E402
import taskdash_setup as setup  # noqa: E402
from test_h2_remove import MANIFEST, _gis  # noqa: E402

REAL_IDS = {"item_id": "a" * 32, "view_item_id": "b" * 32, "dashboard_id": "c" * 32}
ALERT_STATE = '{"signature": ["FAILED: real task"]}'


@pytest.fixture
def real_home(tmp_path, monkeypatch):
    """A fake real install with a config and an alert_state.json, and no way to sign in."""
    home = tmp_path / "real"
    (home / "Logs").mkdir(parents=True)
    config = json.loads((common.data_dir() / "TaskDash.config.example.json").read_text(encoding="utf-8"))
    config["arcgis"].update(REAL_IDS, url="https://real.example.com", profile="realprofile",
                            password="hunter2", allow_plaintext_password=True)
    config["alerting"]["enabled"] = True
    (home / "TaskDash.config.json").write_text(json.dumps(config), encoding="utf-8")
    (home / "Logs" / "alert_state.json").write_text(ALERT_STATE, encoding="utf-8")
    monkeypatch.setattr(common, "home_dir", lambda: home)

    def no_signin(*_a, **_k):
        raise AssertionError("the demo signed in")

    for module in (common, publish, setup):
        monkeypatch.setattr(module, "connect_agol", no_signin)
    return home


def snapshot(home):
    files = [home / "TaskDash.config.json", home / "Logs" / "alert_state.json"]
    return [(f.read_bytes(), f.stat().st_mtime_ns) for f in files]


@pytest.fixture
def faked_apply(monkeypatch):
    calls = []

    def fake_setup(argv):
        calls.append(("setup", argv))
        path = argv[argv.index("--config") + 1]
        raw = json.loads(open(path, encoding="utf-8").read())
        raw["arcgis"].update(item_id="d" * 32, view_item_id="e" * 32, dashboard_id="f" * 32)
        open(path, "w", encoding="utf-8").write(json.dumps(raw))
        return 0

    monkeypatch.setattr(setup, "main", fake_setup)
    monkeypatch.setattr(publish, "main", lambda argv: calls.append(("publish", argv)) or 0)
    return calls


def published(calls):
    """The fixture file names each --apply publish got, in order."""
    return [Path(argv[argv.index("--fixture") + 1]).name for kind, argv in calls
            if kind == "publish" and "--apply" in argv]


def test_check_mode_runs_offline_and_writes_only_the_demo_folder(real_home):
    before = snapshot(real_home)
    assert demo.main([]) == 0
    home = real_home / "demo"
    assert (home / "dashboard_taskdash.json").is_file()
    assert len(list((home / "fixtures").glob("*.json"))) == 4
    assert list((home / "Logs").glob("taskdash_*.log"))
    assert snapshot(real_home) == before


@pytest.mark.real_clock
def test_check_mode_prints_the_tiles_rows_changes_and_alert_previews_as_text(real_home, capsys):
    assert demo.main([]) == 0
    out = capsys.readouterr().out
    assert "Tiles (invented data): Needs attention 9 (Failed 5, Overdue 1, Other warnings 3), Disabled 1, Tasks tracked 25." in out
    for word, task in (("Failed", "Report Mailer"), ("Blocked", "Status Poller"), ("Warning", "Session Cleaner"),
                       ("Not yet run", "Cert Checker"), ("Running", "Disk Reporter"), ("Disabled", "Legacy Sync")):
        assert any(line.split()[:len(word.split())] == word.split() and task in line for line in out.splitlines()), (word, task)
    assert "2 log errors" in out and "In maintenance until" in out and "Muted (alerts off)" in out
    for change in ("Task changed", "Started failing", "Recovered"):
        assert change in out
    logs = real_home / "demo" / "Logs"
    previews = sorted(logs.glob("alert_preview_app_host_0*.txt"))
    assert [p.name for p in previews] == ["alert_preview_app_host_01.txt", "alert_preview_app_host_02.txt"]
    assert all(str(p) in out for p in previews)
    assert "likely shared cause" in previews[1].read_text(encoding="utf-8")
    assert "in maintenance (not alerted): OVERDUE" in previews[0].read_text(encoding="utf-8")
    assert not list(logs.glob("alert_preview_2*.txt"))


def test_faked_apply_leaves_the_real_install_untouched(real_home, faked_apply):
    before = snapshot(real_home)
    assert demo.main(["--apply"]) == 0
    assert faked_apply[0][0] == "setup"
    # The capture before comes first on each host, so the second publish finds changes to record.
    assert published(faked_apply) == ["synthetic_app_host_01_before.json", "synthetic_app_host_01.json",
                                      "synthetic_app_host_02_before.json", "synthetic_app_host_02.json"]
    setup_argv = faked_apply[0][1]
    assert "--demo" in setup_argv
    assert setup_argv[setup_argv.index("--title-prefix") + 1] == "DEMO_"
    assert setup_argv[setup_argv.index("--folder") + 1] == "TaskDash_Demo"
    assert str(real_home / "demo") in setup_argv[setup_argv.index("--config") + 1]
    assert snapshot(real_home) == before


def test_demo_config_has_alerting_off_and_no_password(real_home):
    demo.main([])
    text = (real_home / "demo" / "TaskDash.config.json").read_text(encoding="utf-8")
    config = json.loads(text)
    assert config["alerting"]["enabled"] is False
    assert "password" not in text.lower() and "hunter2" not in text
    assert config["arcgis"]["url"] == "https://real.example.com"
    assert config["arcgis"]["profile"] == "realprofile"
    assert not set(REAL_IDS.values()) & set(config["arcgis"].values())


def test_apply_refuses_when_demo_ids_collide_with_the_real_ids(real_home, faked_apply):
    (real_home / "demo").mkdir()
    (real_home / "demo" / "TaskDash.config.json").write_text(
        json.dumps({"arcgis": {"item_id": REAL_IDS["item_id"]}}), encoding="utf-8")
    before = snapshot(real_home)
    with pytest.raises(SystemExit, match="also uses"):
        demo.main(["--apply"])
    assert faked_apply == []
    assert snapshot(real_home) == before


def test_apply_reuses_existing_demo_items(real_home, faked_apply):
    demo.main(["--apply"])
    faked_apply.clear()
    demo.main(["--apply"])
    assert "setup" not in [kind for kind, _ in faked_apply]
    assert published(faked_apply) == ["synthetic_app_host_01.json", "synthetic_app_host_02.json"]


def test_cli_maps_demo_to_its_module():
    assert cli.SUBCOMMAND_MODULES["demo"] == "taskdash_demo"
    assert "demo" in cli.USAGE


def test_apply_refuses_a_partial_demo_install(real_home, faked_apply):
    (real_home / "demo").mkdir()
    (real_home / "demo" / "TaskDash.config.json").write_text(
        json.dumps({"arcgis": {"item_id": "9" * 32}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="partial install") as refusal:
        demo.main(["--apply"])
    assert "view_item_id" in str(refusal.value) and "dashboard_id" in str(refusal.value)
    assert faked_apply == []


def test_a_failed_demo_collect_run_is_not_reported_as_published(real_home, faked_apply, monkeypatch):
    monkeypatch.setattr(publish, "main", lambda argv: 2)
    with pytest.raises(SystemExit, match="exited with code 2"):
        demo.main(["--apply"])
    with pytest.raises(SystemExit, match="exited with code 2"):
        demo.main([])


def test_real_publish_preflight_failure_stays_inside_the_demo_folder(real_home, monkeypatch):
    before = snapshot(real_home)
    stored = {"item_id": "d" * 32, "view_item_id": "e" * 32, "dashboard_id": "f" * 32}
    (real_home / "demo").mkdir()
    (real_home / "demo" / "TaskDash.config.json").write_text(json.dumps({"arcgis": stored}), encoding="utf-8")

    def unreachable(*_a, **_k):
        raise ConnectionError("fake outage")

    def no_mail(*_a, **_k):
        raise AssertionError("the demo sent mail")

    monkeypatch.setattr(publish, "connect_agol", unreachable)
    monkeypatch.setattr(common, "send_email", no_mail)
    monkeypatch.setattr(publish, "send_email", no_mail, raising=False)
    with pytest.raises(ConnectionError, match="fake outage"):
        demo.main(["--apply"])
    assert (real_home / "demo" / "Logs" / "collector_runs_pending_prod.json").is_file()
    assert snapshot(real_home) == before
    assert not (real_home / "Logs" / "collector_runs_pending_prod.json").exists()


def test_apply_refuses_ids_that_setup_wrote_from_the_real_install(real_home, monkeypatch):
    calls = []

    def setup_writing_real_id(argv):
        calls.append("setup")
        path = argv[argv.index("--config") + 1]
        raw = json.loads(open(path, encoding="utf-8").read())
        raw["arcgis"].update(REAL_IDS)
        open(path, "w", encoding="utf-8").write(json.dumps(raw))
        return 0

    monkeypatch.setattr(setup, "main", setup_writing_real_id)
    monkeypatch.setattr(publish, "main", lambda argv: calls.append("publish") or 0)
    with pytest.raises(SystemExit, match="also uses"):
        demo.main(["--apply"])
    assert calls == ["setup"]


def test_apply_ends_with_the_dashboard_link_the_next_step_and_how_to_refresh_or_remove(real_home, faked_apply, capsys):
    demo.main(["--apply"])
    first = capsys.readouterr().out
    demo.main(["--apply"])
    again = capsys.readouterr().out
    link = "Open: https://real.example.com/apps/dashboards/" + "f" * 32
    for out in (first, again):
        assert out.rstrip().splitlines()[-1] == link
        assert "step 4 of docs/INSTALL.md" in out
        assert "Run `taskdash demo --apply` again to refresh it" in out
        assert "`taskdash demo --remove --apply`" in out


@pytest.mark.real_clock
def test_the_demo_config_keeps_the_heartbeat_ok_for_months_and_holds_two_maintenance_windows(real_home):
    demo.main([])
    config = json.loads((real_home / "demo" / "TaskDash.config.json").read_text(encoding="utf-8"))
    terms = dashboard.help_terms(config)
    # Late and Down are in minutes; the real defaults are 90 and 180.
    assert terms["late"] >= 30 * 24 * 60 and terms["down"] >= 60 * 24 * 60
    assert terms["ignore_days"] >= 180
    windows = config["alerting"]["maintenance_windows"]
    assert len(windows) == 2 and any(w["end"] is None for w in windows) and any(w["end"] for w in windows)
    now = publish.utc_now()
    active = publish._active_maintenance_windows(windows, now)
    assert len(active) == 2
    names = [row["task_name"] for row in publish.collect_everything(config, real_home / "demo" / "fixtures"
                                                                      / "synthetic_app_host_01.json")["inventory"]]
    assert all(any(publish._task_in_maintenance(name, [w]) for name in names) for w in windows)


def test_the_real_config_is_not_needed_to_read_the_offline_demo(real_home, capsys):
    (real_home / "TaskDash.config.json").write_text("{not json", encoding="utf-8")
    assert demo.main([]) == 0
    out = capsys.readouterr().out
    assert "WARNING: the real config was not read" in out and "Tiles (invented data)" in out


def test_a_bad_real_config_still_stops_a_run_that_signs_in(real_home, faked_apply):
    (real_home / "TaskDash.config.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit, match="not valid JSON"):
        demo.main(["--apply"])
    assert faked_apply == []


def test_config_names_the_real_config_to_read(real_home, tmp_path, faked_apply):
    other = tmp_path / "other.json"
    config = json.loads((real_home / "TaskDash.config.json").read_text(encoding="utf-8"))
    config["arcgis"].update(url="https://other.example.com", profile="otherprofile")
    other.write_text(json.dumps(config), encoding="utf-8")
    demo.main(["--config", str(other)])
    stored = json.loads((real_home / "demo" / "TaskDash.config.json").read_text(encoding="utf-8"))["arcgis"]
    assert (stored["url"], stored["profile"]) == ("https://other.example.com", "otherprofile")
    with pytest.raises(SystemExit, match="Config not found"):
        demo.main(["--config", str(tmp_path / "missing.json"), "--apply"])


def test_remove_deletes_only_the_demo_items_and_forgets_their_ids(real_home, faked_apply, monkeypatch):
    demo.main(["--apply"])
    seen = []
    monkeypatch.setattr(remover, "connect_agol", lambda config: seen.append(config["arcgis"]["profile"]) or "gis")
    monkeypatch.setattr(remover, "remove", lambda gis, manifest, apply, task, demo=False:
                        seen.append((gis, apply, demo, [e["id"] for e in manifest["items"]])) or 0)
    (real_home / "demo" / setup.MANIFEST_FILE).write_text(json.dumps(MANIFEST), encoding="utf-8")
    before = snapshot(real_home)
    assert demo.main(["--remove"]) == 0
    assert seen[-1] == ("gis", False, True, ["f1", "svc1", "view1", "g1", "dash1"])
    stored = json.loads((real_home / "demo" / "TaskDash.config.json").read_text(encoding="utf-8"))["arcgis"]
    assert stored["item_id"] == "d" * 32, "a check run must not forget the items"
    assert demo.main(["--remove", "--apply"]) == 0
    assert seen[-1][1] is True and seen[0] == "realprofile"
    stored = json.loads((real_home / "demo" / "TaskDash.config.json").read_text(encoding="utf-8"))["arcgis"]
    assert not any(stored[key] for key in demo.ID_KEYS)
    assert snapshot(real_home) == before
    faked_apply.clear()
    demo.main(["--apply"])
    assert faked_apply[0][0] == "setup"


def test_remove_keeps_the_ids_when_an_item_was_skipped(real_home, faked_apply, monkeypatch):
    demo.main(["--apply"])
    (real_home / "demo" / setup.MANIFEST_FILE).write_text(json.dumps(MANIFEST), encoding="utf-8")
    monkeypatch.setattr(remover, "connect_agol", lambda config: "gis")
    monkeypatch.setattr(remover, "remove", lambda *a, **k: 1)
    assert demo.main(["--remove", "--apply"]) == 1
    stored = json.loads((real_home / "demo" / "TaskDash.config.json").read_text(encoding="utf-8"))["arcgis"]
    assert stored["item_id"] == "d" * 32


def test_remove_without_a_demo_install_says_there_is_nothing_to_remove(real_home):
    with pytest.raises(SystemExit, match="nothing to remove"):
        demo.main(["--remove"])


def test_remove_refuses_demo_ids_that_are_the_real_ones(real_home):
    (real_home / "demo").mkdir()
    (real_home / "demo" / setup.MANIFEST_FILE).write_text(json.dumps(MANIFEST), encoding="utf-8")
    (real_home / "demo" / "TaskDash.config.json").write_text(
        json.dumps({"arcgis": {"item_id": REAL_IDS["item_id"]}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="also uses"):
        demo.main(["--remove", "--apply"])


def test_the_demo_form_of_remove_leaves_out_the_real_task_steps(capsys):
    log = []
    assert remover.remove(_gis(log), MANIFEST, apply=False, task_name="", demo=True) == 0
    out = capsys.readouterr().out
    assert "Nothing deleted." in out
    assert "Unregister-ScheduledTask" not in out and "pip uninstall" not in out


def test_the_demo_form_of_remove_names_the_demo_command_when_it_skips_an_item(capsys):
    log = []
    gis = _gis(log)
    stranger = gis.content.get("dash1")
    stranger.owner = "someone-else"
    assert remover.remove(gis, MANIFEST, apply=True, task_name="", demo=True) == 1
    out = capsys.readouterr().out
    assert "`taskdash demo --remove --apply`" in out and "Unregister-ScheduledTask" not in out
