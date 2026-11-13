"""Audit step s09: several computers, viewers and outages.

Every service call is a fake and every file lives under tmp_path. Nothing signs in.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import taskdash_cli as cli
import taskdash_common as common


# ------------------------------------------------------------------ TD-162 / TD-163: status

@pytest.fixture
def log_dir(monkeypatch, tmp_path):
    folder = tmp_path / "Logs"
    folder.mkdir()
    (tmp_path / "TaskDash.config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(common, "load_config", lambda path=None: {"logging": {"log_dir": str(folder)}})
    return folder


def test_status_offline_shows_the_error_above_the_see_troubleshooting_line(log_dir, capsys):
    (log_dir / "taskdash_20261101_010000.log").write_text(
        "2026-11-01 01:00:00 - INFO - TaskDash started.\n"
        "2026-11-01 01:00:05 - ERROR - TaskDash exiting: Refusing to publish: wrong host.\n"
        "See TROUBLESHOOTING.md, section A collection from the wrong host is refused.\n", encoding="utf-8")
    assert cli.main(["status", "--offline"]) == 1
    out = capsys.readouterr().out
    assert "TaskDash exiting: Refusing to publish: wrong host." in out
    assert "See TROUBLESHOOTING.md, section A collection from the wrong host is refused." in out


def test_status_offline_keeps_the_plain_last_line(log_dir, capsys):
    (log_dir / "taskdash_20261101_010000.log").write_text(
        "2026-11-01 01:00:00 - INFO - TaskDash started.\n2026-11-01 01:01:00 - INFO - TaskDash publish complete.\n",
        encoding="utf-8")
    cli.main(["status", "--offline"])
    out = capsys.readouterr().out
    assert "TaskDash publish complete." in out and "TaskDash started." not in out


def test_status_offline_reports_unsent_runs_with_the_newest_error(log_dir, capsys):
    (log_dir / "taskdash_20261101_010000.log").write_text("2026-11-01 01:00:00 - INFO - x\n", encoding="utf-8")
    rows = [{"run_id": "a", "exit_status": "failed", "error_text": "old error"},
            {"run_id": "b", "exit_status": "failed", "error_text": "token expired"}]
    (log_dir / "collector_runs_pending_prod.json").write_text(json.dumps(rows), encoding="utf-8")
    cli.main(["status", "--offline"])
    out = capsys.readouterr().out
    assert "Unsent: 2 collector run(s)" in out and "token expired" in out and "old error" not in out


def test_status_offline_says_nothing_about_unsent_runs_when_none_wait(log_dir, capsys):
    (log_dir / "taskdash_20261101_010000.log").write_text("2026-11-01 01:00:00 - INFO - x\n", encoding="utf-8")
    cli.main(["status", "--offline"])
    assert "Unsent" not in capsys.readouterr().out


# ------------------------------------------------------------------ TD-164: outage run budget

def test_an_outage_run_reads_the_normal_budget_and_saves_no_first_run_budget(monkeypatch, tmp_path, capsys):
    import taskdash_publish as publish

    seen = {}

    def boom_connect(cfg):
        raise RuntimeError("network down")

    def fake_collect(*args, **kwargs):
        seen.update(kwargs)
        return {"host_name": "H", "inventory": [], "runs": [], "metrics": [], "log_summary": {}}

    monkeypatch.setattr(publish, "connect_agol", boom_connect)
    monkeypatch.setattr(publish, "collect_everything", fake_collect)
    monkeypatch.setattr(publish, "report", lambda collected: None)
    monkeypatch.setattr(publish, "maybe_send_alert", lambda *a: False)
    args = publish.parse_args(["--apply"])
    with pytest.raises(RuntimeError, match="network down"):
        publish.run_publish(args, {}, tmp_path)
    out = capsys.readouterr().out
    assert seen["max_events"] == 0 and seen["first_run_budget"] is False
    assert "ArcGIS is unreachable" in out and "No usable watermark" not in out


# ------------------------------------------------------------------ TD-159: why the run failed, in the alert

def test_the_publish_note_names_the_error_and_the_oldest_unsent_failure(tmp_path):
    import taskdash_publish as publish

    started = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)
    pending = tmp_path / "collector_runs_pending_prod.json"
    pending.write_text(json.dumps([
        {"run_id": "b", "exit_status": "failed", "started_utc": (started + timedelta(hours=1)).isoformat()},
        {"run_id": "a", "exit_status": "failed", "started_utc": started.isoformat()}]), encoding="utf-8")
    note = publish._publish_failure_note(SystemExit("token expired"), pending, started + timedelta(hours=5))
    assert "token expired" in note and publish._local_stamp(started) in note
    assert publish._local_stamp(started + timedelta(hours=5)) not in note


def test_the_publish_note_falls_back_to_this_run_with_no_pending_file(tmp_path):
    import taskdash_publish as publish

    now = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)
    note = publish._publish_failure_note(RuntimeError("network down"), tmp_path / "none.json", now)
    assert publish._local_stamp(now) in note and "network down" in note


def test_the_alert_body_carries_the_publish_note_above_the_dashboard_link():
    import taskdash_publish as publish

    now = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)
    inventory = [{"task_name": "A", "task_full_name": "A", "last_run_failed": 1, "is_overdue": 0,
                  "last_result_text": "Script exited with code 1."}]
    collected = {"host_name": "H", "inventory": inventory, "publish_note": "TaskDash could not publish this run."}
    config = {"alerting": {"dashboard_url": "https://example.test/dash"}, "arcgis": {}}
    _, body = publish._alert_message(collected, config, now, "changed", ["FAILED: A"], [])
    assert body.index("TaskDash could not publish this run.") < body.index("Dashboard: https://example.test/dash")
    collected.pop("publish_note")
    assert "could not publish" not in publish._alert_message(collected, config, now, "changed", ["FAILED: A"], [])[1]


# ------------------------------------------------------------------ TD-160: no false "Cleared" without run history

def _compose_with_history(event_log_error, previous, last_run_time=None):
    import taskdash_publish as publish

    now = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)
    task = {"task_name": "A", "task_full_name": "A", "last_run_failed": 0, "is_overdue": 0,
            "last_result_text": "", "next_run_in_hours": 5.0, "latest_log_path": "", "latest_log_name": "",
            "attention_reason": "", "last_run_time": last_run_time}
    collected = {"host_name": "H", "inventory": [task], "event_log_error": event_log_error}
    config = {"alerting": {"enabled": True, "to": ["a@b.c"]}, "arcgis": {}}
    return publish.compose_alert(collected, config, previous, now)


def test_an_unread_run_history_keeps_the_earlier_failure_open():
    previous = {"signature": ["FAILED: A"],
                "problem_last_alert_utc": {"FAILED: A": datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc).isoformat()}}
    import taskdash_collect as collect

    held = _compose_with_history(collect.EventLogUnreadable("cannot read"), previous)
    assert held["current"] == ["FAILED: A"] and held["send"] is False
    cleared = _compose_with_history(None, previous)
    assert cleared["current"] == [] and cleared["reason"] == "recovered"


def test_an_unread_run_history_does_not_keep_a_failure_of_a_task_that_is_gone():
    import taskdash_collect as collect

    held = _compose_with_history(collect.EventLogUnreadable("cannot read"), {"signature": ["FAILED: Gone"]})
    assert held["current"] == []


def test_a_disabled_log_does_not_hold_a_failure_open():
    import taskdash_collect as collect

    previous = {"signature": ["FAILED: A"],
                "problem_last_alert_utc": {"FAILED: A": datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc).isoformat()}}
    result = _compose_with_history(collect.EventLogDisabled("log is off"), previous)
    assert result["current"] == [] and result["reason"] == "recovered"


def test_a_task_the_scheduler_ran_since_the_failure_is_cleared_while_the_log_is_unread():
    import taskdash_collect as collect

    previous = {"signature": ["FAILED: A"],
                "problem_last_alert_utc": {"FAILED: A": datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc).isoformat()}}
    later = datetime(2026, 11, 2, 0, 30, tzinfo=timezone.utc).isoformat()
    earlier = datetime(2026, 11, 1, 23, 0, tzinfo=timezone.utc).isoformat()
    assert _compose_with_history(collect.EventLogUnreadable("x"), previous, later)["current"] == []
    assert _compose_with_history(collect.EventLogUnreadable("x"), previous, earlier)["current"] == ["FAILED: A"]


# ------------------------------------------------------------------ TD-157: no second prod dashboard

def test_prod_with_an_empty_dashboard_id_refuses_when_one_already_exists():
    import taskdash_dashboard as dash
    from test_dashboard_phase4 import FakeGIS, _item

    gis = FakeGIS([_item("OLD1", "TaskDash")])
    with pytest.raises(SystemExit, match="OLD1") as exc:
        dash.resolve_dashboard(gis, "prod", {"dashboard_id": ""})
    assert "arcgis.dashboard_id" in str(exc.value) and "never creates a second prod dashboard" in str(exc.value)


def test_prod_with_an_empty_dashboard_id_creates_when_none_exists():
    import taskdash_dashboard as dash
    from test_dashboard_phase4 import FakeGIS, _item

    assert dash.resolve_dashboard(FakeGIS([_item("T1", "TaskDash_TEST")]), "prod", {}) == (None, "TaskDash")


# ------------------------------------------------------------------ TD-052: scrub-history scope

def _scrub_run(monkeypatch, argv):
    import taskdash_publish as publish

    wheres = []

    class Table:
        properties = SimpleNamespace(objectIdField="OBJECTID", maxRecordCount=1000)

        def query(self, **kw):
            wheres.append(kw.get("where"))
            return SimpleNamespace(features=[])

    monkeypatch.setattr(publish, "connect_agol", lambda config: object())
    monkeypatch.setattr(publish, "ensure_service", lambda gis, config, target: object())
    monkeypatch.setattr(publish, "get_tables", lambda item: {name: Table() for name in publish.SCRUB_HISTORY_TABLES})
    monkeypatch.setattr(publish, "check_schema_drift", lambda tables, target: None)
    config = {"host_name_override": "HOST-A", "privacy": {"hide_accounts": True}}
    assert publish.run_scrub_history(config, publish.parse_args(argv)) == 0
    return wheres


def test_scrub_history_reads_only_this_computers_rows_by_default(monkeypatch):
    wheres = _scrub_run(monkeypatch, ["--scrub-history"])
    assert wheres and set(wheres) == {"host_name = 'HOST-A'"}


def test_scrub_history_all_hosts_reads_every_row(monkeypatch):
    assert set(_scrub_run(monkeypatch, ["--scrub-history", "--all-hosts"])) == {"1=1"}


def test_all_hosts_works_only_with_scrub_history():
    import taskdash_publish as publish

    with pytest.raises(SystemExit):
        publish.parse_args(["--all-hosts"])


# ------------------------------------------------------------------ TD-055: preflight dashboard audience

def _dash_check(dash_access, view_access, dash_groups=None, view_groups=None):
    import taskdash_preflight as pf
    from test_phase3_preflight import FakeGis, FakeItem, fake_dashboard, make_ctx

    dash = fake_dashboard(access=dash_access)
    view = FakeItem("view1", view_access)
    if dash_groups is not None:
        dash.shared_with = {"groups": [SimpleNamespace(id=g) for g in dash_groups]}
        view.shared_with = {"groups": [SimpleNamespace(id=g) for g in view_groups]}
    config = {"arcgis": {"dashboard_id": "dash1", "view_item_id": "view1"}}
    return pf.check_dashboard(make_ctx(config, gis=FakeGis(items={"dash1": dash, "view1": view})))


def test_a_dashboard_shared_with_nobody_warns():
    import taskdash_preflight as pf

    status, message = _dash_check("private", "private")
    assert status == pf.WARN and "shared with nobody" in message


def test_an_org_dashboard_over_a_group_only_view_warns():
    import taskdash_preflight as pf

    status, message = _dash_check("org", "shared")
    assert status == pf.WARN and "shared more widely ('org') than its view ('shared')" in message


def test_a_dashboard_and_view_with_the_same_sharing_pass():
    import taskdash_preflight as pf

    assert _dash_check("shared", "shared")[0] == pf.OK
    assert _dash_check("shared", "org")[0] == pf.OK


def test_a_dashboard_group_missing_from_the_view_warns():
    import taskdash_preflight as pf

    status, message = _dash_check("shared", "shared", ["g1", "g2"], ["g1"])
    assert status == pf.WARN and "not shared with" in message
    assert _dash_check("shared", "shared", ["g1"], ["g1", "g2"])[0] == pf.OK


# ------------------------------------------------------------------ TD-050: setup refusals point to "Add another computer"

def test_the_install_refusal_points_a_second_computer_to_the_install_guide():
    import taskdash_setup as setup

    with pytest.raises(SystemExit) as exc:
        setup.refuse_existing_install({"arcgis": {"item_id": "abc"}})
    assert "Add another computer" in str(exc.value)


def test_the_docs_describe_the_second_computer_steps():
    from pathlib import Path

    root = Path(__file__).resolve().parent
    install = (root / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    assert "## Add another computer" in install and "Do not run `taskdash setup` on the second computer" in install
    configure = (root / "docs" / "CONFIGURE.md").read_text(encoding="utf-8")
    for heading in ("## Run TaskDash on several computers", "### Give people access"):
        assert heading in configure
