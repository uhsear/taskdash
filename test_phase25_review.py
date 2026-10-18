"""Phase 2.5 review fixes owned by the integrator: host literal check, metric caption days, legacy SSL switch."""

from __future__ import annotations

import smtplib

import taskdash_common as common

common.disable_arcpy_import()

import taskdash_dashboard as dash  # noqa: E402
from test_phase25_dashboard import _templates  # noqa: E402

SHOWN = {"privacy": {"hide_command_lines": False, "hide_sample_lines": False, "hide_accounts": False,
                     "hide_paths": False}, "alerting": {"heartbeat_hour": 7}}


def _validate(definition, host):
    dash.validate_definition(definition, config={**SHOWN, "host_name_override": host})


def test_a_host_that_is_an_ordinary_word_is_not_a_false_hit(monkeypatch):
    monkeypatch.setattr(dash.socket, "gethostname", lambda: "nohost-xyz")
    _validate(dash.build("TESTITEM", _templates(), config=SHOWN), "TASKS")


def test_a_quoted_host_literal_is_still_refused(monkeypatch):
    monkeypatch.setattr(dash.socket, "gethostname", lambda: "nohost-xyz")
    definition = dash.build("TESTITEM", _templates(), config=SHOWN)
    definition["filters"] = [{"where": "host_name = 'TASKHOSTZ'"}, {"arcade": 'var h = "taskhostz";'}]
    try:
        _validate(definition, "TASKHOSTZ")
    except SystemExit as exc:
        assert "hardcoded host" in str(exc)
    else:
        raise AssertionError("a quoted host literal must be refused")


def test_metric_caption_uses_metrics_days_and_runs_text_uses_runs_days():
    config = {**SHOWN, "retention": {"runs_days": 30, "metrics_days": 120}}
    blob = str(dash.build("TESTITEM", _templates(), config=config))
    assert "of each metric in the saved history (up to 120 days)" in blob
    assert "(up to 30 days)" in blob and "(up to 90 days)" not in blob


def test_a_stray_use_ssl_in_the_legacy_mail_file_is_ignored(monkeypatch):
    used = []

    class _Conn:
        def __init__(self, *a, **k):
            used.append(type(self).__name__)

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def starttls(self, context=None):
            pass

        def login(self, *a):
            pass

        def send_message(self, m):
            pass

    class SMTP(_Conn):
        pass

    class SMTP_SSL(_Conn):
        pass

    monkeypatch.setattr(smtplib, "SMTP", SMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", SMTP_SSL)
    legacy = {"smtp server": "h.example.test", "from address": "a@example.test", "smtp port": 587, "use ssl": True}
    assert common.send_email(legacy, ["b@example.test"], "s", "b")
    assert used == ["SMTP"]
