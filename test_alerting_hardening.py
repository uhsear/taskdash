"""Regression tests for the taskdash_publish.py alerting hardening pass (plan items 3.1, 3.2,
3.8, 3.9, 3.10) and main()'s error handling.

Each test targets one plan item and fails against the pre-hardening implementation. No network
writes, no ArcGIS Online/SMTP/HTTP calls -- urllib and send_email are always faked. Matches
test_taskdash.py / test_writer_hardening.py style.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import taskdash_publish as publish
from conftest import REFERENCE_PROFILES

NOW = datetime(2026, 9, 25, 14, 0, 0, tzinfo=timezone.utc)


def _inv(**kw):
    base = {"task_name": "Parcel Refresh", "last_run_failed": 0, "is_overdue": 0,
            "last_result_text": "", "next_run_in_hours": 5.0,
            "latest_log_path": "", "latest_log_name": ""}
    base.update(kw)
    return base


# ---------------------------------------------------------------- 3.2 stable signature

def test_alert_signature_has_no_changing_number():
    """'OVERDUE: X by N.Nh' changed every hour and reset should_alert's change comparison."""
    early = _inv(task_name="B", is_overdue=1, next_run_in_hours=-3.0)
    later = _inv(task_name="B", is_overdue=1, next_run_in_hours=-4.0)
    assert publish.alert_signature([early]) == publish.alert_signature([later]) == ["OVERDUE: B"]


def test_alert_details_still_carries_the_number():
    task = _inv(task_name="B", is_overdue=1, next_run_in_hours=-3.0)
    details = publish.alert_details([task])
    assert details["OVERDUE: B"] == "OVERDUE: B by 3.0 h"


def test_should_alert_realerts_a_still_open_problem_after_cooldown():
    previous = {"signature": ["FAILED: A"],
               "problem_last_alert_utc": {"FAILED: A": (NOW - timedelta(hours=25)).isoformat()}}
    send, why = publish.should_alert(previous, ["FAILED: A"], NOW, None, realert_hours=24)
    assert send and why == "realert"


def test_should_alert_does_not_realert_within_cooldown():
    previous = {"signature": ["FAILED: A"],
               "problem_last_alert_utc": {"FAILED: A": (NOW - timedelta(hours=1)).isoformat()}}
    send, _ = publish.should_alert(previous, ["FAILED: A"], NOW, None, realert_hours=24)
    assert send is False


def test_should_alert_realert_off_when_not_configured():
    """Existing 4-positional-arg callers (realert_hours defaulted) keep the old behaviour."""
    send, _ = publish.should_alert({"signature": ["FAILED: A"]}, ["FAILED: A"], NOW, None)
    assert send is False


def test_maybe_send_alert_persists_problem_last_alert_utc(tmp_path, monkeypatch):
    monkeypatch.setattr("taskdash_common.load_mail_settings",
                        lambda cfg: {"smtp server": "x", "from address": "a@b.c"})
    monkeypatch.setattr("taskdash_common.send_email", lambda *a, **k: True)
    config = {"alerting": {"enabled": True, "to": ["a@b.c"]}}
    collected = {"host_name": "H", "inventory": [_inv(task_name="A", last_run_failed=1)]}
    publish.maybe_send_alert(collected, config, tmp_path)

    state = json.loads((tmp_path / "alert_state.json").read_text(encoding="utf-8"))
    assert state["signature"] == ["FAILED: A"]
    assert "FAILED: A" in state["problem_last_alert_utc"]


# ---------------------------------------------------------------- 3.1 alert survives a publish() failure

def _run_publish_args(**overrides):
    args = publish.parse_args(["--apply"])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_alert_still_sent_when_publish_raises_and_error_is_reraised(monkeypatch, tmp_path):
    alerts = []
    monkeypatch.setattr(publish, "connect_agol", lambda cfg: object())
    monkeypatch.setattr(publish, "ensure_service",
                        lambda gis, cfg, tgt: type("I", (), {"title": "x", "id": tgt})())
    monkeypatch.setattr(publish, "get_tables", lambda item: {})
    monkeypatch.setattr(publish, "check_schema_drift", lambda tables: None)
    monkeypatch.setattr(publish, "collect_everything",
                        lambda *a, **k: {"host_name": "H", "inventory": [], "runs": [],
                                         "metrics": [], "log_summary": {}})
    monkeypatch.setattr(publish, "report", lambda collected: None)

    def boom(*a, **k):
        raise SystemExit("AGOL write failed")

    monkeypatch.setattr(publish, "publish", boom)
    monkeypatch.setattr(publish, "maybe_send_alert", lambda *a: alerts.append(a))

    args = _run_publish_args(target="prod")
    with pytest.raises(SystemExit):
        publish.run_publish(args, {}, tmp_path)

    assert len(alerts) == 1, "the alert must still be evaluated even though publish() raised"


def test_alert_still_sent_when_connect_agol_raises_before_any_collection(monkeypatch, tmp_path):
    """An expired credential or an AGOL outage on the pre-publish connect -- before
    collect_everything ever runs -- must not silence a failed-task e-mail either."""
    alerts = []

    def boom_connect(cfg):
        raise RuntimeError("token expired")

    monkeypatch.setattr(publish, "connect_agol", boom_connect)
    monkeypatch.setattr(publish, "collect_everything",
                        lambda *a, **k: {"host_name": "H", "inventory": [], "runs": [],
                                         "metrics": [], "log_summary": {}})
    monkeypatch.setattr(publish, "report", lambda collected: None)
    monkeypatch.setattr(publish, "maybe_send_alert", lambda *a: alerts.append(a))

    args = _run_publish_args(target="prod")
    with pytest.raises(RuntimeError, match="token expired"):
        publish.run_publish(args, {}, tmp_path)

    assert len(alerts) == 1, "the alert must still be evaluated even when connect_agol raises"


def test_watermark_to_does_not_advance_when_publish_fails(monkeypatch, tmp_path):
    """No run actually landed if publish() failed, so the CollectorRuns audit row must report
    the watermark unchanged, not the highest id merely collected this pass."""
    monkeypatch.setattr(publish, "connect_agol", lambda cfg: object())
    monkeypatch.setattr(publish, "ensure_service",
                        lambda gis, cfg, tgt: type("I", (), {"title": "x", "id": tgt})())
    monkeypatch.setattr(publish, "get_tables", lambda item: {publish.TABLE_RUNS: object()})
    monkeypatch.setattr(publish, "check_schema_drift", lambda tables: None)
    monkeypatch.setattr(publish, "event_watermark", lambda *a, **k: 500)
    monkeypatch.setattr(publish, "collect_everything",
                        lambda *a, **k: {"host_name": "H", "inventory": [], "log_summary": {},
                                         "runs": [{"run_key": "r1", "max_event_record_id": 999}],
                                         "metrics": []})
    monkeypatch.setattr(publish, "report", lambda collected: None)
    monkeypatch.setattr(publish, "publish", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(publish, "maybe_send_alert", lambda *a: False)
    captured = {}
    monkeypatch.setattr(publish, "_write_collector_run_row",
                        lambda item, cfg, collected, **kw: captured.update(kw))

    args = _run_publish_args(target="prod", fixture="f.json")  # skip the newest_record_id PS call
    with pytest.raises(RuntimeError):
        publish.run_publish(args, {}, tmp_path)

    assert captured["watermark_from"] == 500.0
    assert captured["watermark_to"] == 500.0, \
        "a failed publish() means no run landed -- watermark_to must not jump to the collected max"


def test_no_alert_for_a_failed_test_target_publish(monkeypatch, tmp_path):
    """--target test must still never alert, even on a publish() failure."""
    alerts = []
    monkeypatch.setattr(publish, "connect_agol", lambda cfg: object())
    monkeypatch.setattr(publish, "ensure_service",
                        lambda gis, cfg, tgt: type("I", (), {"title": "x", "id": tgt})())
    monkeypatch.setattr(publish, "get_tables", lambda item: {})
    monkeypatch.setattr(publish, "check_schema_drift", lambda tables: None)
    monkeypatch.setattr(publish, "collect_everything",
                        lambda *a, **k: {"host_name": "H", "inventory": [], "runs": [],
                                         "metrics": [], "log_summary": {}})
    monkeypatch.setattr(publish, "report", lambda collected: None)
    monkeypatch.setattr(publish, "publish", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(publish, "maybe_send_alert", lambda *a: alerts.append(a))

    args = _run_publish_args(target="test")
    with pytest.raises(RuntimeError):
        publish.run_publish(args, {}, tmp_path)

    assert alerts == []


# ---------------------------------------------------------------- 3.9 log tail in the e-mail

def test_tail_log_for_email_truncates_and_scrubs_secrets(tmp_path):
    log = tmp_path / "task.log"
    lines = [f"line {i}" for i in range(30)]
    lines[-2] = "password=SuperSecret123 more text"
    lines[-1] = "connecting to https://user:hunter2@example.com/api"
    log.write_text("\n".join(lines), encoding="utf-8")

    tail = publish.tail_log_for_email(str(log))
    assert "SuperSecret123" not in tail
    assert "hunter2" not in tail
    assert tail.count("\n") <= 14
    assert len(tail.encode("utf-8")) <= 2048


def test_tail_log_for_email_missing_file_returns_empty_not_raises():
    assert publish.tail_log_for_email(str(Path("C:/does/not/exist_12345.log"))) == ""


def test_tail_log_for_email_empty_path_returns_empty():
    assert publish.tail_log_for_email("") == ""


def test_scrub_secrets_covers_underscore_keys_json_style_bearer_and_any_url_scheme():
    """A leading \\b (word boundary) never crosses an underscore or a quote, so
    ARCGIS_PASSWORD=, db_pwd=, client_secret=, access_token= and JSON-style "password": "x"
    all leaked before; Authorization/Bearer and non-http(s) scheme://user:pass@ URLs (this
    repo's own FTP scripts) were not covered by any pattern at all."""
    text = ('ARCGIS_PASSWORD=Sup3r db_pwd=Sup3r client_secret=Sup3r access_token=Sup3r '
           '"password": "Sup3r" Authorization: Bearer Sup3r '
           'ftp://user:Sup3r@example.com/path')
    scrubbed = publish._scrub_secrets(text)
    assert "Sup3r" not in scrubbed


# ---------------------------------------------------------------- 3.10 maintenance windows

def test_partition_maintenance_suppresses_a_matching_task():
    inv = [_inv(task_name="Parcel Refresh", last_run_failed=1),
           _inv(task_name="Other Task", last_run_failed=1)]
    windows = [{"task": "Parcel.*", "start": (NOW - timedelta(hours=1)).isoformat(),
               "end": (NOW + timedelta(hours=1)).isoformat(), "reason": "planned"}]
    active = publish._active_maintenance_windows(windows, NOW)
    alerted, suppressed = publish.partition_maintenance(inv, active)
    assert alerted == ["FAILED: Other Task"]
    assert suppressed == ["FAILED: Parcel Refresh"]


def test_expired_maintenance_window_is_ignored():
    windows = [{"task": "Parcel.*", "start": (NOW - timedelta(days=2)).isoformat(),
               "end": (NOW - timedelta(days=1)).isoformat(), "reason": "old"}]
    assert publish._active_maintenance_windows(windows, NOW) == []


def test_not_yet_started_maintenance_window_is_not_active():
    windows = [{"task": "Parcel.*", "start": (NOW + timedelta(days=1)).isoformat(),
               "end": (NOW + timedelta(days=2)).isoformat(), "reason": "future"}]
    assert publish._active_maintenance_windows(windows, NOW) == []


def test_alert_subject_says_in_maintenance_not_all_clear_when_every_open_problem_is_suppressed(
    monkeypatch, tmp_path,
):
    """partition_maintenance can empty out `current` even though a real problem is still open,
    just suppressed by a maintenance window -- should_alert then sees current=[] vs a non-empty
    prior signature and reports reason="recovered", but nothing has actually cleared. The
    subject (and the "cleared" body line) must say so, not "all clear"."""
    monkeypatch.setattr(publish, "utc_now", lambda: NOW)
    monkeypatch.setattr("taskdash_common.load_mail_settings",
                        lambda cfg: {"smtp server": "x", "from address": "a@b.c"})
    sent = {}
    monkeypatch.setattr(
        "taskdash_common.send_email",
        lambda mail, to, subject, body: sent.update(subject=subject, body=body) or True)

    (tmp_path / "alert_state.json").write_text(
        json.dumps({"signature": ["FAILED: A"]}), encoding="utf-8")
    config = {"alerting": {"enabled": True, "to": ["a@b.c"],
                           "maintenance_windows": [{"task": "A", "start": "2026-09-25T00:00:00Z",
                                                    "end": "2026-09-26T00:00:00Z"}]}}
    collected = {"host_name": "H", "inventory": [_inv(task_name="A", last_run_failed=1)]}

    assert publish.maybe_send_alert(collected, config, tmp_path) is True
    assert "all clear" not in sent["subject"].lower()
    assert "maintenance" in sent["subject"].lower()
    assert "(previously reported problems have cleared)" not in sent["body"]


# ---------------------------------------------------------------- 3.8 healthcheck dead man's switch

def test_ping_healthcheck_off_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: calls.append(a))
    publish.ping_healthcheck({}, "/start")
    assert calls == []


def test_ping_healthcheck_pings_the_configured_url_with_suffix(monkeypatch):
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=10: calls.append(url))
    publish.ping_healthcheck({"alerting": {"healthcheck_url": "https://hc-ping.com/xyz"}}, "/start")
    assert calls == ["https://hc-ping.com/xyz/start"]


def test_ping_healthcheck_failure_is_swallowed(monkeypatch):
    def boom(url, timeout=10):
        raise OSError("network down")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    publish.ping_healthcheck({"alerting": {"healthcheck_url": "https://hc-ping.com/xyz"}}, "/fail")
    # No exception means the run is not broken by a dead monitoring endpoint.


def test_ping_healthcheck_swallows_non_oserror_failures_too(monkeypatch):
    """http.client.HTTPException subclasses (InvalidURL, BadStatusLine) are neither URLError
    nor OSError nor ValueError -- they must be swallowed here just like a network failure."""
    import http.client

    def boom(url, timeout=10):
        raise http.client.BadStatusLine("garbage")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    publish.ping_healthcheck({"alerting": {"healthcheck_url": "https://hc-ping.com/xyz"}}, "/fail")
    # No exception means the run is not broken by a malformed healthcheck response either.


def test_main_pings_healthcheck_only_for_the_actual_prod_apply_publish(tmp_path, monkeypatch):
    """A manual dry run or a --target test run must not reset (or /fail) the dead man's switch
    -- only the real hourly --apply --target prod publish job represents it."""
    config = {"logging": {"log_dir": str(tmp_path)}, "rules": {"profiles": list(REFERENCE_PROFILES)},
             "alerting": {"healthcheck_url": "https://hc.example/x"}}
    config_path = tmp_path / "c.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    monkeypatch.setattr(publish, "_main_dispatch", lambda args, cfg, log_dir: 0)
    pings = []
    monkeypatch.setattr(publish, "ping_healthcheck", lambda cfg, suffix: pings.append(suffix))

    publish.main(["--config", str(config_path)])
    assert pings == [], "a dry run must not ping the dead man's switch"

    publish.main(["--config", str(config_path), "--apply", "--target", "test"])
    assert pings == [], "--target test must not ping it either"

    publish.main(["--config", str(config_path), "--apply"])  # --target defaults to prod
    assert pings == ["/start", ""], "the real prod --apply publish must still ping it"


# ---------------------------------------------------------------- main()'s error handling

def test_main_logs_a_systemexit_to_the_timestamped_log_before_reraising(tmp_path, monkeypatch):
    config = {"logging": {"log_dir": str(tmp_path)}, "rules": {"profiles": list(REFERENCE_PROFILES)}}
    config_path = tmp_path / "c.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    monkeypatch.setattr(publish, "_main_dispatch",
                        lambda args, cfg, log_dir: (_ for _ in ()).throw(SystemExit("kaboom")))

    with pytest.raises(SystemExit):
        publish.main(["--config", str(config_path)])

    log_files = list(tmp_path.glob("taskdash_*.log"))
    assert log_files, "setup_logging must have created a timestamped log file"
    assert "kaboom" in log_files[0].read_text(encoding="utf-8")


def test_main_logs_an_uncaught_exception_to_the_timestamped_log_before_reraising(tmp_path, monkeypatch):
    config = {"logging": {"log_dir": str(tmp_path)}, "rules": {"profiles": list(REFERENCE_PROFILES)}}
    config_path = tmp_path / "c.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    monkeypatch.setattr(publish, "_main_dispatch",
                        lambda args, cfg, log_dir: (_ for _ in ()).throw(RuntimeError("bad state")))

    with pytest.raises(RuntimeError):
        publish.main(["--config", str(config_path)])

    log_files = list(tmp_path.glob("taskdash_*.log"))
    assert log_files
    assert "bad state" in log_files[0].read_text(encoding="utf-8")
