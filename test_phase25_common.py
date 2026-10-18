"""Phase 2.5 common: unknown config keys, SMTP TLS hardening, BOM-tolerant mail config.
Fakes only: smtplib is mocked, nothing connects anywhere."""
import json
import smtplib
import ssl

import keyring
import pytest

import taskdash_common as common


@pytest.fixture
def messages(monkeypatch):
    out = []
    monkeypatch.setattr(common, "emit", out.append)
    return out


# --------------------------------------------------------------------------- unknown keys

def test_unknown_privacy_key_is_a_config_error(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"rules": {"profiles": []}, "privacy": {"hide_acounts": True}}))
    with pytest.raises(SystemExit) as exc:
        common.load_config(cfg)
    text = str(exc.value)
    assert "privacy.hide_acounts" in text and str(cfg) in text
    assert "hide_accounts" in text and "hide_paths" in text


def test_unknown_key_in_other_section_warns_and_underscore_keys_are_ignored(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({
        "rules": {"profiles": [], "_note": "x"},
        "alerting": {"realert_hrs": 4, "_comment": "x", "realert_hours": 4},
        "privacy": {"_note": "x", "hide_paths": True},
        "smtp": {"ssl": True, "allow_plaintext_auth": False}}))
    config, warnings = common.load_config(cfg, defer_warnings=True)
    assert len(warnings) == 1 and "alerting.realert_hrs" in warnings[0]
    assert config["privacy"]["hide_paths"] is True


def test_new_bool_keys_are_type_checked(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"rules": {"profiles": []}, "privacy": {"hide_paths": "yes"},
                               "smtp": {"ssl": 1}}))
    with pytest.raises(SystemExit, match="privacy.hide_paths must be a boolean"):
        common.load_config(cfg)


# --------------------------------------------------------------------------- SMTP

class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.context = host, port, context
        self.starttls_context = "not called"
        self.logged_in = False
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.starttls_context = context

    def login(self, user, password):
        self.logged_in = True

    def send_message(self, msg):
        pass


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP.instances


def _mail(**extra):
    """A dict as the smtp block builds it: strict TLS on."""
    return {"smtp server": "smtp.example.test", "from address": "a@example.test", "strict tls": True, **extra}


def _legacy_mail(**extra):
    """A dict as the legacy mail_config_path file gives it: no strict-TLS marker."""
    return {"smtp server": "smtp.example.test", "from address": "a@example.test", **extra}


def test_starttls_uses_a_verifying_context(fake_smtp, messages):
    ok = common.send_email(_mail(**{"smtp port": 587, "smtp username": "u", "smtp password": "p"}),
                           ["b@example.test"], "s", "body")
    assert ok
    ctx = fake_smtp[0].starttls_context
    assert ctx.check_hostname is True and ctx.verify_mode == ssl.CERT_REQUIRED
    assert fake_smtp[0].logged_in


def test_port_465_uses_smtp_ssl_with_a_verifying_context(monkeypatch, fake_smtp, messages):
    monkeypatch.setattr(keyring, "get_password", lambda *_: "pw")
    mail = common.load_mail_settings({"smtp": {
        "host": "smtp.example.test", "from": "a@example.test", "port": 465, "user": "u"}})
    assert common.send_email(mail, ["b@example.test"], "s", "body")
    conn = fake_smtp[0]
    assert conn.port == 465 and conn.context.check_hostname is True
    assert conn.context.verify_mode == ssl.CERT_REQUIRED
    assert conn.starttls_context == "not called"


def test_port_465_with_ssl_false_stays_plain_smtp(fake_smtp):
    mail = common.load_mail_settings({"smtp": {
        "host": "h.example.test", "from": "a@example.test", "port": 465, "ssl": False}})
    assert mail["use ssl"] is False


def test_login_is_refused_without_tls(fake_smtp, messages):
    mail = _mail(**{"use tls": False, "smtp username": "u", "smtp password": "p"})
    assert common.send_email(mail, ["b@example.test"], "s", "body") is False
    assert fake_smtp == []
    assert any("unencrypted" in m and "allow_plaintext_auth" in m for m in messages)


def test_login_without_tls_is_allowed_with_opt_in(fake_smtp):
    mail = _mail(**{"use tls": False, "smtp username": "u", "smtp password": "p",
                    "allow plaintext auth": True})
    assert common.send_email(mail, ["b@example.test"], "s", "body")
    assert fake_smtp[0].logged_in


def test_smtp_block_without_tls_disables_alerting_before_reading_keyring(monkeypatch, messages):
    monkeypatch.setattr(keyring, "get_password",
                        lambda *_: pytest.fail("keyring must not be read"))
    mail = common.load_mail_settings({"smtp": {
        "host": "h.example.test", "from": "a@example.test", "user": "u", "starttls": False}})
    assert mail is None
    assert any("allow_plaintext_auth" in m and "Alerting disabled" in m for m in messages)


def test_smtp_block_plaintext_auth_opt_in_is_carried(monkeypatch, messages):
    monkeypatch.setattr(keyring, "get_password", lambda *_: "pw")
    mail = common.load_mail_settings({"smtp": {
        "host": "h.example.test", "from": "a@example.test", "user": "u",
        "starttls": False, "allow_plaintext_auth": True}})
    assert mail["allow plaintext auth"] is True


def test_keyring_hint_names_the_task_account(monkeypatch, messages):
    monkeypatch.setattr(keyring, "get_password", lambda *_: None)
    common.load_mail_settings({"smtp": {"host": "h.example.test", "from": "a@example.test",
                                        "user": "mailer"}})
    assert any("as the account the scheduled task runs under" in m for m in messages)


def test_legacy_mail_config_with_bom_is_read(tmp_path, messages):
    path = tmp_path / "mail.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(
        {"email settings": {"smtp server": "s.example.test", "from address": "a@example.test"}}
    ).encode())
    mail = common.load_mail_settings({"alerting": {"mail_config_path": str(path)}})
    assert mail["smtp server"] == "s.example.test"


def test_legacy_mail_config_keeps_its_old_tls_and_login_behaviour(fake_smtp):
    """The legacy file's relay was never certificate-checked; turning that on could silently stop
    a working install's alerts, so it keeps a bare starttls() and its login as before."""
    assert common.send_email(_legacy_mail(**{"smtp port": 587, "smtp username": "u", "smtp password": "p"}),
                             ["b@example.test"], "s", "body")
    assert fake_smtp[0].starttls_context is None and fake_smtp[0].logged_in
    assert common.send_email(_legacy_mail(**{"use tls": False, "smtp username": "u", "smtp password": "p"}),
                             ["b@example.test"], "s", "body")
    assert fake_smtp[1].logged_in
