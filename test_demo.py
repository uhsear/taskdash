"""Phase 4: `taskdash demo` runs on synthetic data in its own folder and never touches the real install.

No test signs in or reaches a network: every sign-in is a fake that fails the test if called, and
the --apply tests replace setup and publish with recorders.
"""

from __future__ import annotations

import json

import pytest

import taskdash_cli as cli
import taskdash_common as common

common.disable_arcpy_import()

import taskdash_demo as demo  # noqa: E402
import taskdash_publish as publish  # noqa: E402
import taskdash_setup as setup  # noqa: E402

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


def test_check_mode_runs_offline_and_writes_only_the_demo_folder(real_home):
    before = snapshot(real_home)
    assert demo.main([]) == 0
    home = real_home / "demo"
    assert (home / "dashboard_taskdash.json").is_file()
    assert len(list((home / "fixtures").glob("*.json"))) == 2
    assert list((home / "Logs").glob("taskdash_*.log"))
    assert snapshot(real_home) == before


def test_faked_apply_leaves_the_real_install_untouched(real_home, faked_apply):
    before = snapshot(real_home)
    assert demo.main(["--apply"]) == 0
    kinds = [kind for kind, _ in faked_apply]
    assert kinds == ["setup", "publish", "publish"]
    setup_argv = faked_apply[0][1]
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
    assert [kind for kind, _ in faked_apply] == ["publish", "publish"]


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
