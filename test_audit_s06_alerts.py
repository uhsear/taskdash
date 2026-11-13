"""Audit step "Make alerts trustworthy": realert_hours 0, the preflight recipient check, the first
e-mail of a new install, the daily check, e-mail wording and times. No network, no SMTP, no ArcGIS."""

from __future__ import annotations

import json
import smtplib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import taskdash_cli as cli
import taskdash_common as common
import taskdash_preflight as pf
import taskdash_publish as publish
from conftest import REFERENCE_PROFILES

NOW = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)


def _task(name="Parcel Refresh", **kw):
    base = {"task_name": name, "last_run_failed": 0, "is_overdue": 0, "last_result_text": "",
            "next_run_in_hours": 5.0, "latest_log_path": "", "latest_log_name": "",
            "attention_reason": ""}
    base.update(kw)
    return base


def _failed(name, last_run=None):
    return _task(name, last_run_failed=1, last_result_text="boom", last_run_time=last_run)


def _compose(inventory, previous=None, now=NOW, **alerting):
    config = {"alerting": {"enabled": True, "to": ["a@b.c"], **alerting}, "arcgis": {"dashboard_id": "D"}}
    return publish.compose_alert({"host_name": "H", "inventory": inventory}, config, previous or {}, now)


# ------------------------------------------------------------------ TD-031 realert_hours 0 is off

@pytest.mark.parametrize("hours", [0, 0.0, -1])
def test_realert_hours_zero_or_less_turns_repeats_off(hours):
    previous = {"signature": ["FAILED: A"],
                "problem_last_alert_utc": {"FAILED: A": (NOW - timedelta(days=9)).isoformat()}}
    assert publish.should_alert(previous, ["FAILED: A"], NOW, None, realert_hours=hours) == (False, "")
    assert publish.should_alert(previous, ["FAILED: A"], NOW, None, realert_hours=24) == (True, "realert")


def test_preview_says_realert_is_off_for_zero(tmp_path):
    state = tmp_path / "alert_state.json"
    state.write_text(json.dumps({"signature": ["FAILED: A"], "problem_last_alert_utc": {
        "FAILED: A": (NOW - timedelta(hours=30)).isoformat()}}), encoding="utf-8")
    collected = {"host_name": "H", "inventory": [_failed("A")]}
    config = {"alerting": {"enabled": True, "to": ["a@b.c"], "realert_hours": 0}}
    text = publish.preview_alert(collected, config, tmp_path, now=NOW).read_text(encoding="utf-8")
    assert "(re-alert off)" in text and "Decision: would NOT send" in text


# ------------------------------------------------------------------ TD-130 daily check

def _local(hour, day=4):
    return datetime(2026, 11, day, hour, 5).astimezone().astimezone(timezone.utc)


def test_daily_check_goes_out_on_the_first_run_after_its_hour():
    assert publish.should_alert({"signature": []}, [], _local(9), 7) == (True, "heartbeat")
    assert publish.should_alert({"signature": []}, [], _local(6), 7) == (False, "")


def test_daily_check_is_sent_once_per_local_day():
    sent_today = {"signature": [], "last_heartbeat_utc": _local(9).isoformat()}
    assert publish.should_alert(sent_today, [], _local(20), 7) == (False, "")
    assert publish.should_alert(sent_today, [], _local(8, day=5), 7) == (True, "heartbeat")


# ------------------------------------------------------------------ TD-131 / TD-132 / TD-135

def test_daily_check_with_open_problems_has_its_own_subject_and_footer():
    previous = {"signature": ["FAILED: A"], "updated_utc": "x",
                "problem_last_alert_utc": {"FAILED: A": NOW.isoformat()},
                "problem_first_seen_utc": {"FAILED: A": (NOW - timedelta(days=2)).isoformat()}}
    alert = _compose([_failed("A")], previous, heartbeat_hour=NOW.astimezone().hour)
    assert alert["reason"] == "heartbeat"
    assert alert["subject"] == "[TaskDash] daily check on H: 1 problem(s) still open"
    assert "(open since " in alert["body"] and "alerting.maintenance_windows" in alert["body"]


def test_repeat_email_shows_age_and_how_to_stop_repeats():
    previous = {"signature": ["FAILED: A"], "updated_utc": "x",
                "problem_last_alert_utc": {"FAILED: A": (NOW - timedelta(hours=30)).isoformat()},
                "problem_first_seen_utc": {"FAILED: A": (NOW - timedelta(days=2)).isoformat()}}
    alert = _compose([_failed("A")], previous)
    assert alert["reason"] == "realert"
    since = (NOW - timedelta(days=2)).astimezone()
    assert f"FAILED: A -- boom (open since {since:%Y-%m-%d %H:%M %Z})" in alert["body"]
    assert "at most every 24 hours" in alert["body"] and "alerting.realert_hours to 0" in alert["body"]


def test_a_new_problem_shows_no_age_and_no_footer():
    alert = _compose([_failed("A")], {"signature": [], "updated_utc": "x"})
    assert alert["reason"] == "problem"
    assert "open since" not in alert["body"] and "alerting.maintenance_windows" not in alert["body"]


def test_no_dashboard_means_no_dashboard_line():
    collected = {"host_name": "H", "inventory": [_failed("A")]}
    _, body = publish._alert_message(collected, {"alerting": {}, "arcgis": {}}, NOW, "problem",
                                     ["FAILED: A"], [])
    assert "Dashboard" not in body and body.endswith("\n")
    assert publish.alert_dashboard_link({"alerting": {"dashboard_url": "https://x.test/d"}}) == "https://x.test/d"
    assert publish.alert_dashboard_link({"arcgis": {"dashboard_id": "D"}}).endswith("/apps/dashboards/D")


# ------------------------------------------------------------------ TD-034 first e-mail

THREE = ["Old A", "Old B", "Old C"]


def test_first_email_lists_old_failures_instead_of_one_new_incident():
    old = "2026-10-01T02:00:00+00:00"
    alert = _compose([_failed(n, old) for n in THREE])
    assert alert["incident"] is None
    assert alert["subject"] == "[TaskDash] 3 problem(s) on H"
    assert "Already open when alerts started" in alert["body"]
    assert "Likely shared cause" not in alert["body"]
    assert alert["first_seen"]["FAILED: Old A"] == old, "seeded from the task's last run, not from now"
    since = datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc).astimezone()
    assert f"(open since {since:%Y-%m-%d %H:%M %Z})" in alert["body"]


def test_a_failure_with_no_usable_last_run_time_is_first_seen_now():
    alert = _compose([_failed("A", None), _failed("B", "not a time")])
    assert set(alert["first_seen"].values()) == {NOW.isoformat()}


def test_with_alert_state_the_incident_e_mail_still_groups():
    previous = {"signature": ["FAILED: Old A"], "updated_utc": "x"}
    alert = _compose([_failed(n, "2026-10-01T02:00:00+00:00") for n in THREE], previous)
    assert alert["incident"]["count"] == 3 and "already open" not in alert["body"].lower()


def test_a_healthy_run_with_no_heartbeat_still_writes_state_so_the_next_failure_groups(tmp_path, monkeypatch):
    import taskdash_common
    config = {"alerting": {"enabled": True, "to": ["a@b.c"]}, "arcgis": {"dashboard_id": "D"}}
    healthy = {"host_name": "H", "inventory": [_task("Old A"), _task("Old B"), _task("Old C")]}
    assert publish.maybe_send_alert(healthy, config, tmp_path) is False
    assert json.loads((tmp_path / "alert_state.json").read_text(encoding="utf-8"))["signature"] == []

    sent = []
    monkeypatch.setattr(taskdash_common, "load_mail_settings", lambda c: {"host": "x"})
    monkeypatch.setattr(taskdash_common, "send_email", lambda m, to, subject, body: sent.append(subject) or True)
    failing = {"host_name": "H", "inventory": [_failed(n, "2026-11-04T14:00:00+00:00") for n in THREE]}
    assert publish.maybe_send_alert(failing, config, tmp_path) is True
    assert "3 tasks failing" in sent[0]


# ------------------------------------------------------------------ TD-137

def test_unclear_cause_heading_does_not_claim_a_shared_cause():
    previous = {"signature": ["FAILED: Old A"], "updated_utc": "x"}
    alert = _compose([_failed(n) for n in THREE], previous)
    assert alert["incident"]["cause"] == ""
    assert "Likely shared cause" not in alert["body"] and "They may share one cause." in alert["body"]
    assert "Their logs show no single cause." in alert["body"]
    assert alert["subject"].endswith("check for a shared cause")


# ------------------------------------------------------------------ TD-129 local times

def test_the_preview_header_shows_local_time(tmp_path):
    stamp = f"{NOW.astimezone():%Y-%m-%d %H:%M %Z}"
    assert publish._local_stamp(NOW) == stamp
    text = publish.preview_alert({"host_name": "H", "inventory": []}, {"alerting": {}}, tmp_path,
                                 now=NOW).read_text(encoding="utf-8")
    assert text.startswith(f"TaskDash alert preview, {stamp}.")


# ------------------------------------------------------------------ TD-033 preflight

def _ctx(config):
    return pf.Context(SimpleNamespace(config="", offline=False),
                      config={"rules": {"profiles": []}, "arcgis": {"item_id": "svc1"}, **config})


class _Relay:
    refuse_with = None

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        if _Relay.refuse_with:
            raise _Relay.refuse_with


def _smtp(**block):
    return {"host": "relay.example.invalid", "from": "td@example.invalid", **block}


@pytest.mark.parametrize("recipients", [[], [""], ["  "], None])
def test_preflight_fails_when_alerts_are_on_but_nobody_receives_them(monkeypatch, recipients):
    monkeypatch.setattr(smtplib, "SMTP", _Relay)
    status, message = pf.check_smtp(_ctx({"alerting": {"enabled": True, "to": recipients}, "smtp": _smtp()}))
    assert status == pf.FAIL and "alerting.to has no address" in message


@pytest.mark.parametrize("config", [
    {"alerting": {"enabled": True, "to": ["alerts@example.com"]}, "smtp": _smtp()},
    {"alerting": {"enabled": True, "to": ["a@b.test"]}, "smtp": _smtp(**{"from": "td@example.com"})},
])
def test_preflight_warns_on_the_example_placeholder(monkeypatch, config):
    monkeypatch.setattr(smtplib, "SMTP", _Relay)
    status, message = pf.check_smtp(_ctx(config))
    assert status == pf.WARN and "example.com placeholders" in message


def test_preflight_fails_on_a_placeholder_smtp_host(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _Relay)
    config = {"alerting": {"enabled": True, "to": ["a@b.test"]}, "smtp": _smtp(host="smtp.example.com")}
    status, message = pf.check_smtp(_ctx(config))
    assert status == pf.FAIL and "example.com placeholders" in message


def test_preflight_passes_with_a_real_recipient(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _Relay)
    config = {"alerting": {"enabled": True, "to": ["ops@agency.test"]}, "smtp": _smtp()}
    assert pf.check_smtp(_ctx(config))[0] == pf.OK


# ------------------------------------------------------------------ TD-128 port 25 relay

def test_a_relay_without_starttls_gets_the_port_25_hint_not_the_firewall_one(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _Relay)
    _Relay.refuse_with = smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")
    try:
        status, message = pf.check_smtp(_ctx({"alerting": {"enabled": True, "to": ["a@b.test"]},
                                              "smtp": _smtp(port=25)}))
    finally:
        _Relay.refuse_with = None
    assert status == pf.FAIL and "port 25" in message and "smtp.starttls to false" in message
    assert "firewall" not in message


def test_hint_is_picked_by_cause():
    starttls = common.mail_failure_hint("SMTPNotSupportedError: STARTTLS extension not supported by server.")
    assert "smtp.starttls to false" in starttls
    # An AUTH refusal is also SMTPNotSupportedError, and the STARTTLS advice would mislead there.
    assert "starttls to false" not in common.mail_failure_hint(
        "SMTPNotSupportedError: SMTP AUTH extension not supported by server.")
    assert "preflight --as-task --apply" in common.mail_failure_hint("OSError: relay down")


# ------------------------------------------------------------------ TD-032 / TD-127 / TD-133

def _cli_config(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"logging": {"log_dir": str(tmp_path)},
                                "rules": {"profiles": list(REFERENCE_PROFILES)},
                                "alerting": {"to": ["a@example.test"]}}), encoding="utf-8")
    return str(path)


def test_test_alert_names_the_account_and_points_to_the_task_account_check(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(common, "load_mail_settings",
                        lambda cfg: {"smtp server": "mail.example.test", "from address": "td@example.test"})
    sent = []
    monkeypatch.setattr(common, "send_email", lambda m, to, subject, body: sent.append(subject) or True)
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "someadmin")
    assert cli.main(["test-alert", "--apply", "--config", _cli_config(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "read as someadmin" in out and "preflight --as-task --apply" in out
    assert sent and sent[0].startswith("[TaskDash] test alert from ")


def test_test_alert_failure_points_at_the_task_account_check(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(common, "load_mail_settings",
                        lambda cfg: {"smtp server": "mail.example.test", "from address": "td@example.test"})

    def refuse(*a):
        monkeypatch.setattr(common, "last_send_error", "SMTPAuthenticationError: bad password")
        return False

    monkeypatch.setattr(common, "send_email", refuse)
    assert cli.main(["test-alert", "--apply", "--config", _cli_config(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "SMTPAuthenticationError" in out and "preflight --as-task --apply" in out


def test_a_mail_settings_failure_points_at_the_task_account_check(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "load_mail_settings", lambda cfg: None)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    collected = {"host_name": "H", "inventory": [_failed("A")]}
    config = {"alerting": {"enabled": True, "to": ["a@b.c"]}}
    with pytest.raises(publish.AlertSendError) as exc:
        publish.maybe_send_alert(collected, config, log_dir)
    assert "smtp line" in exc.value.hint and "account that runs the task" in exc.value.hint


def test_an_empty_recipient_list_is_not_blamed_on_the_smtp_keys(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    collected = {"host_name": "H", "inventory": [_failed("A")]}
    config = {"alerting": {"enabled": True, "to": []}}
    with pytest.raises(publish.AlertSendError) as exc:
        publish.maybe_send_alert(collected, config, log_dir)
    assert "alerting.to" in exc.value.hint and "smtp.host" not in exc.value.hint


# ------------------------------------------------------------------ TD-126 wording

def test_mail_warnings_say_the_run_fails_not_that_alerting_is_disabled(monkeypatch):
    lines = []
    monkeypatch.setattr(common, "emit", lines.append)
    assert common.load_mail_settings({"smtp": {"host": "h"}}) is None
    assert common.load_mail_settings({}) is None
    assert lines and all("cannot send alerts" in m and "exits 1" in m for m in lines)
    assert not any("disabled" in m.lower() for m in lines)
