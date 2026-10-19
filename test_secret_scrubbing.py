"""H5-6: the wider secret scrubber, the log filter, the health-check log line, the UTF-16 mail
tail and alerting.include_log_tail. Every secret here is made up. Nothing touches the network."""

from __future__ import annotations

import json
import logging
import sys
import urllib.error
from datetime import datetime, timezone

import pytest

import taskdash_common as common
import taskdash_metrics as metrics
import taskdash_publish as publish

# Built from pieces so a secret scanner never sees a whole token in this file.
GITHUB = "ghp_" + "a1B2c3D4e5" * 4
GITHUB_FINE = "github_pat_" + "11AAAAAA0" + "x9Y8z7W6v5U4t3S2r1Q0p9"
AWS = "AKIA" + "ABCD1234EFGH5678"
SLACK = "xoxb-" + "123456789012-" + "abcdefABCDEF1234"
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJ" + "zdWIiOiJ0ZXN0In0" + "." + "sig_Value-123"
SECRET = "Syn7hetic"

# (label, text, the part that must be gone)
REDACTED = [
    ("client-secret flag", f"run --client-secret {SECRET} --apply", SECRET),
    ("ClientSecret flag", f"run -ClientSecret {SECRET}", SECRET),
    ("access-token flag", f"run --access-token {SECRET}", SECRET),
    ("AccessToken flag", f"run -AccessToken {SECRET}", SECRET),
    ("auth-token flag", f"run --auth-token {SECRET}", SECRET),
    ("refresh-token flag", f"run --refresh-token {SECRET}", SECRET),
    ("GitHub classic token", f"cloning with {GITHUB} now", GITHUB),
    ("GitHub OAuth token", f"cloning with {GITHUB.replace('ghp_', 'gho_')} now", GITHUB.replace("ghp_", "gho_")),
    ("GitHub user token", f"cloning with {GITHUB.replace('ghp_', 'ghu_')} now", GITHUB.replace("ghp_", "ghu_")),
    ("GitHub server token", f"cloning with {GITHUB.replace('ghp_', 'ghs_')} now", GITHUB.replace("ghp_", "ghs_")),
    ("GitHub fine-grained token", f"cloning with {GITHUB_FINE} now", GITHUB_FINE),
    ("AWS access key", f"key {AWS} used", AWS),
    ("Slack bot token", f"posting with {SLACK}", SLACK),
    ("Slack user token", f"posting with {SLACK.replace('xoxb-', 'xoxp-')}", SLACK.replace("xoxb-", "xoxp-")),
    ("JWT", f"got {JWT} back", JWT),
    ("Passwort", f"Passwort: {SECRET}", SECRET),
    ("Kennwort", f"Kennwort={SECRET}", SECRET),
    ("mot_de_passe", f"mot_de_passe={SECRET}", SECRET),
    ("motdepasse", f"motdepasse: {SECRET}", SECRET),
    ("contrasena", f"contrasena={SECRET}", SECRET),
    ("contraseña", f"contraseña: {SECRET}", SECRET),
    ("senha", f"senha={SECRET}", SECRET),
    ("wachtwoord", f"wachtwoord: {SECRET}", SECRET),
    ("token in a query string", f"GET https://host.example/api?token={SECRET}&page=2", SECRET),
]

UNCHANGED = [
    ("GUID", "item 3f2504e0-4f89-11d3-9a0c-0305e82c3301 loaded"),
    ("40-character SHA", "commit 356a192b7913b04c54574d18c28d46e6395428ab built"),
    ("item ID", "layer 0a1b2c3d4e5f60718293a4b5c6d7e8f9 refreshed"),
    ("sentence", "The parcel refresh finished in 12 seconds for 3 tasks."),
    ("token count", "Parsed 120 tokens from the file and 3 secrets were found in the vault list."),
]


@pytest.mark.parametrize("label, text, secret", REDACTED, ids=[row[0] for row in REDACTED])
def test_each_new_form_is_redacted(label, text, secret):
    scrubbed = publish._scrub_secrets(text)
    assert secret not in scrubbed, f"{label} leaked: {scrubbed}"
    assert "<redacted>" in scrubbed


@pytest.mark.parametrize("label, text", UNCHANGED, ids=[row[0] for row in UNCHANGED])
def test_ordinary_text_is_not_changed(label, text):
    assert publish._scrub_secrets(text) == text


def test_a_query_string_token_keeps_the_rest_of_the_address():
    scrubbed = publish._scrub_secrets(f"GET https://host.example/api?token={SECRET}&page=2")
    assert scrubbed == "GET https://host.example/api?token=<redacted>&page=2"


def test_a_profile_can_add_secret_words(tmp_path):
    profile = tmp_path / "words.json"
    profile.write_text(json.dumps({"secret_words": ["geheimwort"]}), encoding="utf-8")
    assert publish._scrub_secrets(f"geheimwort={SECRET}") == f"geheimwort={SECRET}"
    try:
        metrics.configure_rules({"rules": {"profiles": [str(profile)]}})
        assert SECRET not in publish._scrub_secrets(f"geheimwort={SECRET}")
        assert SECRET not in publish._scrub_secrets(f"Passwort={SECRET}"), "the default words stay"
    finally:
        metrics.configure_rules({})


def test_a_bad_secret_words_entry_is_a_rules_error(tmp_path):
    profile = tmp_path / "words.json"
    profile.write_text(json.dumps({"secret_words": "geheimwort"}), encoding="utf-8")
    try:
        with pytest.raises(SystemExit, match="Rules problem"):
            metrics.configure_rules({"rules": {"profiles": [str(profile)]}})
    finally:
        metrics.configure_rules({})


# ------------------------------------------------------------------ the logging filter

def _written_log(tmp_path):
    for handler in logging.getLogger().handlers:
        handler.flush()
    (log,) = tmp_path.glob("taskdash_*.log")
    return log.read_text(encoding="utf-8")


def test_the_log_file_never_holds_a_token_from_a_message_or_an_exception(tmp_path):
    common.setup_logging(tmp_path)
    try:
        logging.info("calling the service with %s", GITHUB)
        try:
            raise RuntimeError(f"upload failed for ?token={SECRET}&n=1")
        except RuntimeError:
            logging.exception("publish failed")
        text = _written_log(tmp_path)
    finally:
        logging.getLogger().handlers.clear()
    assert "calling the service with" in text and "Traceback" in text
    assert GITHUB not in text and SECRET not in text
    assert "token=<redacted>&n=1" in text


def test_a_scrubber_failure_withholds_the_line_instead_of_raising_or_leaking(tmp_path, monkeypatch):
    def broken(text):
        raise RuntimeError("scrubber bug")
    monkeypatch.setattr(publish, "_scrub_secrets", broken)
    common.setup_logging(tmp_path)
    try:
        logging.info("password=%s", SECRET)
        text = _written_log(tmp_path)
    finally:
        logging.getLogger().handlers.clear()
    assert SECRET not in text and "log line withheld" in text


def test_emit_scrubs_the_stdout_copy(capsys):
    """run_taskdash.cmd keeps stdout in Logs\\bootstrap.log, so the printed line must be clean too."""
    common.emit(f"upload failed for https://host.example/api?token={SECRET}&n=1 with {GITHUB}")
    shown = capsys.readouterr().out
    assert SECRET not in shown and GITHUB not in shown
    assert "token=<redacted>&n=1" in shown


def test_emit_withholds_the_line_when_the_scrubber_fails(monkeypatch, capsys):
    def broken(text):
        raise RuntimeError("scrubber bug")
    monkeypatch.setattr(publish, "_scrub_secrets", broken)
    common.emit(f"password={SECRET}")
    shown = capsys.readouterr().out
    assert SECRET not in shown and "log line withheld" in shown


# ------------------------------------------------------------------ the health-check log line

def test_a_failed_ping_logs_no_path(monkeypatch, caplog, capsys):
    ping_id = "9f8e7d6c-secret-ping-id"
    url = f"https://hc-ping.example/{ping_id}"

    def boom(target, timeout=10):
        raise urllib.error.URLError(f"cannot reach {target}")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    caplog.set_level("INFO")
    publish.ping_healthcheck({"alerting": {"healthcheck_url": url}}, "/fail")
    shown = caplog.text + capsys.readouterr().out
    assert ping_id not in shown
    assert "https://hc-ping.example/<redacted>" in shown


def test_a_failed_ping_with_an_unparseable_address_still_does_not_raise(monkeypatch):
    def boom(target, timeout=10):
        raise ValueError("bad address")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    publish.ping_healthcheck({"alerting": {"healthcheck_url": "http://[bad/ping-id"}}, "/fail")


# ------------------------------------------------------------------ the e-mail log tail

@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le"], ids=["with BOM", "no BOM"])
def test_a_utf16_log_tail_is_readable_and_scrubbed(tmp_path, encoding):
    log = tmp_path / "job.log"
    log.write_bytes(f"starting\r\npassword={SECRET}\r\nfinished\r\n".encode(encoding))
    tail = publish.tail_log_for_email(str(log))
    assert "\x00" not in tail and SECRET not in tail
    assert "starting" in tail and "finished" in tail


NOW = datetime(2026, 9, 25, 14, 0, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------ stderr (the bootstrap log)

def test_an_uncaught_exception_prints_a_scrubbed_traceback_to_stderr(monkeypatch, capsys):
    """run_taskdash.cmd keeps stderr in Logs\\bootstrap_FAIL_<id>.log."""
    def boom(argv=None):
        raise RuntimeError(f"upload failed for https://host.example/api?token={SECRET}&n=1")
    monkeypatch.setattr(publish, "main", boom)
    monkeypatch.setattr("sys.excepthook", sys.excepthook)  # restored after the test
    with pytest.raises(RuntimeError) as caught:
        publish._run_cli()
    sys.excepthook(caught.type, caught.value, caught.tb)
    shown = capsys.readouterr().err
    assert "Traceback" in shown and "RuntimeError" in shown
    assert SECRET not in shown and "token=<redacted>&n=1" in shown


def test_a_systemexit_message_is_scrubbed_before_the_interpreter_prints_it(monkeypatch):
    def quit_with(argv=None):
        raise SystemExit(f"cannot sign in: ?token={SECRET}")
    monkeypatch.setattr(publish, "main", quit_with)
    with pytest.raises(SystemExit) as caught:
        publish._run_cli()
    assert SECRET not in caught.value.code and "token=<redacted>" in caught.value.code


def test_a_numeric_systemexit_passes_through(monkeypatch):
    monkeypatch.setattr(publish, "main", lambda argv=None: (_ for _ in ()).throw(SystemExit(3)))
    with pytest.raises(SystemExit) as caught:
        publish._run_cli()
    assert caught.value.code == 3


def _message(tmp_path, alerting):
    log = tmp_path / "job.log"
    log.write_text("one\nlast line of the log\n", encoding="utf-8")
    task = {"task_name": "Job", "last_run_failed": 1, "is_overdue": 0, "last_result_text": "",
            "next_run_in_hours": 5.0, "latest_log_path": str(log), "latest_log_name": "job.log"}
    collected = {"host_name": "H", "inventory": [task]}
    return publish._alert_message(collected, {"alerting": alerting}, NOW, "problem", ["FAILED: Job"], [])[1]


def test_include_log_tail_is_on_by_default_and_off_when_false(tmp_path):
    assert "last line of the log" in _message(tmp_path, {})
    assert "last line of the log" in _message(tmp_path, {"include_log_tail": True})
    assert "last line of the log" not in _message(tmp_path, {"include_log_tail": False})


def test_include_log_tail_must_be_true_or_false():
    assert common._validate_config({"alerting": {"include_log_tail": True}}) == []
    errors = common._validate_config({"alerting": {"include_log_tail": "no"}})
    assert len(errors) == 1 and "alerting.include_log_tail must be a boolean" in errors[0]
