"""H1-6: a failed alert e-mail must fail the run, not pass as "nothing to send".

The run still publishes, saves the CollectorRuns row with the reason, pings the health check with
/fail and exits 1 at the end. A retry next hour needs the alert state left alone. No network:
AGOL, SMTP and the health check are all faked.
"""

from __future__ import annotations

import json

import pytest

import taskdash_cli as cli
import taskdash_common as common
import taskdash_publish as publish
from conftest import REFERENCE_PROFILES

REASON = "SMTPServerDisconnected: connection unexpectedly closed"


def _failed_task():
    return {"task_name": "Parcel Refresh", "last_run_failed": 1, "is_overdue": 0,
            "last_result_text": "", "next_run_in_hours": 5.0,
            "latest_log_path": "", "latest_log_name": ""}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A prod --apply run over one failed synthetic task, with AGOL, mail and pings faked."""
    seen = {"published": 0, "rows": [], "pings": [], "sent": []}
    config = {"logging": {"log_dir": str(tmp_path)}, "rules": {"profiles": list(REFERENCE_PROFILES)},
              "alerting": {"enabled": True, "to": ["ops@example.test"],
                           "healthcheck_url": "https://hc.example/x"}}
    config_path = tmp_path / "c.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    item = type("I", (), {"title": "x", "id": "svc", "access": "private"})()

    monkeypatch.setattr(publish, "connect_agol", lambda cfg: object())
    monkeypatch.setattr(publish, "ensure_service", lambda gis, cfg, target: item)
    monkeypatch.setattr(publish, "get_tables", lambda i: {})
    monkeypatch.setattr(publish, "check_schema_drift", lambda tables: None)
    monkeypatch.setattr(publish, "check_source_sharing", lambda *a: None)
    monkeypatch.setattr(publish, "collect_everything",
                        lambda *a, **k: {"host_name": "H", "inventory": [_failed_task()], "runs": [],
                                         "metrics": [], "log_summary": {}})
    monkeypatch.setattr(publish, "report", lambda collected: None)
    monkeypatch.setattr(publish, "publish",
                        lambda *a, **k: seen.__setitem__("published", seen["published"] + 1) or {})
    monkeypatch.setattr(publish, "_write_collector_run_row",
                        lambda i, cfg, collected, **kw: seen["rows"].append(kw))
    monkeypatch.setattr(publish, "ping_healthcheck", lambda cfg, suffix: seen["pings"].append(suffix))
    monkeypatch.setattr(common, "load_mail_settings",
                        lambda cfg: {"smtp server": "mail.example.test", "from address": "td@example.test"})
    seen["argv"] = ["--config", str(config_path), "--apply"]
    seen["dir"] = tmp_path
    return seen


def _fail_send(world, monkeypatch, reason=REASON):
    def send(mail, recipients, subject, body):
        world["sent"].append(subject)
        monkeypatch.setattr(common, "last_send_error", reason)
        return False

    monkeypatch.setattr(common, "send_email", send)


def test_a_failed_send_fails_the_run_after_everything_else_is_done(world, monkeypatch):
    _fail_send(world, monkeypatch, "SMTPAuthenticationError: password=hunter2 rejected")
    with pytest.raises(SystemExit) as exc:
        publish.main(world["argv"])

    message = str(exc.value)
    assert "Alert e-mail failed" in message and "smtp" in message and "taskdash test-alert" in message
    assert "hunter2" not in message
    assert world["published"] == 1, "publishing finishes before the run fails"
    (row,) = world["rows"]
    assert row["exit_status"] == "failed" and row["alert_sent"] is False
    assert row["error_text"].startswith("Alert e-mail failed: SMTPAuthenticationError")
    assert world["pings"] == ["/start", "/fail"]
    assert not (world["dir"] / "alert_state.json").exists(), "the next run must retry the send"


def test_alerting_on_with_no_usable_mail_settings_is_a_failed_send(world, monkeypatch):
    monkeypatch.setattr(common, "load_mail_settings", lambda cfg: None)
    monkeypatch.setattr(common, "send_email", lambda *a: world["sent"].append(a) or True)
    with pytest.raises(SystemExit, match="Alert e-mail failed"):
        publish.main(world["argv"])

    assert world["sent"] == []
    assert "no usable mail settings" in world["rows"][0]["error_text"]
    assert world["pings"] == ["/start", "/fail"]


def test_a_failed_send_does_not_hide_a_publish_failure(world, monkeypatch):
    _fail_send(world, monkeypatch)
    monkeypatch.setattr(publish, "publish",
                        lambda *a, **k: (_ for _ in ()).throw(SystemExit("AGOL write failed")))
    with pytest.raises(SystemExit, match="AGOL write failed"):
        publish.main(world["argv"])

    error_text = world["rows"][0]["error_text"]
    assert error_text.startswith("AGOL write failed") and "Alert e-mail failed" in error_text


def test_nothing_to_send_is_still_a_clean_exit(world, monkeypatch):
    monkeypatch.setattr(publish, "collect_everything",
                        lambda *a, **k: {"host_name": "H", "inventory": [], "runs": [],
                                         "metrics": [], "log_summary": {}})
    _fail_send(world, monkeypatch)

    assert publish.main(world["argv"]) == 0
    assert world["sent"] == [], "no problem means no send attempt"
    (row,) = world["rows"]
    assert row["exit_status"] == "success" and row["error_text"] == "" and row["alert_sent"] is False
    assert world["pings"] == ["/start", ""]


def test_a_good_send_saves_the_state_and_exits_0(world, monkeypatch):
    monkeypatch.setattr(common, "send_email", lambda *a: world["sent"].append(a[2]) or True)

    assert publish.main(world["argv"]) == 0
    assert len(world["sent"]) == 1
    assert world["rows"][0]["alert_sent"] is True and world["rows"][0]["error_text"] == ""
    assert (world["dir"] / "alert_state.json").exists()


# ---------------------------------------------------------------- taskdash test-alert

def _cli_config(tmp_path, **alerting):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"logging": {"log_dir": str(tmp_path)},
                                "rules": {"profiles": list(REFERENCE_PROFILES)},
                                "alerting": {"to": ["a@example.test", "b@example.test"], **alerting}}),
                    encoding="utf-8")
    return str(path)


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setattr(common, "load_mail_settings",
                        lambda cfg: {"smtp server": "mail.example.test", "smtp port": 25,
                                     "from address": "td@example.test", "smtp password": "s3cret"})
    monkeypatch.setattr(common, "send_email", lambda m, to, subject, body: sent.append((to, subject)) or True)
    return sent


def test_test_alert_without_apply_prints_the_plan_and_sends_nothing(tmp_path, mail, capsys):
    assert cli.main(["test-alert", "--config", _cli_config(tmp_path)]) == 0

    out = capsys.readouterr().out
    assert mail == []
    assert "a@example.test" in out and "b@example.test" in out and "mail.example.test" in out
    assert "--apply" in out and "s3cret" not in out


def test_test_alert_apply_sends_exactly_one_message(tmp_path, mail):
    assert cli.main(["test-alert", "--apply", "--config", _cli_config(tmp_path)]) == 0
    assert len(mail) == 1 and mail[0][0] == ["a@example.test", "b@example.test"]


def test_test_alert_apply_exits_1_with_the_reason_on_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(common, "load_mail_settings",
                        lambda cfg: {"smtp server": "mail.example.test", "from address": "td@example.test"})

    def refuse(*a):
        monkeypatch.setattr(common, "last_send_error", REASON)
        return False

    monkeypatch.setattr(common, "send_email", refuse)
    assert cli.main(["test-alert", "--apply", "--config", _cli_config(tmp_path)]) == 1
    assert REASON in capsys.readouterr().out


def test_test_alert_needs_recipients_and_mail_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "load_mail_settings", lambda cfg: None)
    with pytest.raises(SystemExit, match="no usable mail settings"):
        cli.main(["test-alert", "--config", _cli_config(tmp_path)])
    with pytest.raises(SystemExit, match="alerting.to is empty"):
        cli.main(["test-alert", "--config", _cli_config(tmp_path, to=[])])


def test_send_email_records_why_it_failed(monkeypatch):
    import smtplib

    def refuse(*a, **k):
        raise OSError("relay down")

    monkeypatch.setattr(smtplib, "SMTP", refuse)
    assert common.send_email({"smtp server": "x", "from address": "a@b.c"}, ["d@e.f"], "s", "b") is False
    assert "OSError: relay down" in common.last_send_error
