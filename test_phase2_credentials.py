"""Phase 2 credentials: ArcGIS profile sign-in, plaintext warning, and the smtp block."""
import arcgis.gis
import keyring
import pytest

import taskdash_common as common


class FakeGIS:
    calls = []

    def __init__(self, *args, **kwargs):
        FakeGIS.calls.append((args, kwargs))
        self.users = type("U", (), {"me": type("M", (), {"username": "svc"})()})()


@pytest.fixture
def gis_calls(monkeypatch):
    FakeGIS.calls = []
    monkeypatch.setattr(arcgis.gis, "GIS", FakeGIS)
    return FakeGIS.calls


@pytest.fixture
def messages(monkeypatch):
    out = []
    monkeypatch.setattr(common, "emit", out.append)
    return out


def test_profile_signs_in_without_username_or_password(gis_calls, messages):
    # trust_env=True on every sign-in: without it arcgis ignores HTTPS_PROXY and goes direct.
    common.connect_agol({"arcgis": {"url": "https://portal.example.test", "profile": "p1"}})
    assert gis_calls == [(("https://portal.example.test",), {"profile": "p1", "trust_env": True})]


def test_legacy_path_passes_username_and_password(gis_calls, messages):
    common.connect_agol({"arcgis": {"username": "u", "password": "s3cret",
                                    "allow_plaintext_password": True}})
    assert gis_calls == [(("https://www.arcgis.com", "u", "s3cret"), {"trust_env": True})]


def test_plaintext_password_without_opt_in_exits_without_leaking_it(gis_calls, messages):
    with pytest.raises(SystemExit) as exc:
        common.connect_agol({"arcgis": {"username": "u", "password": "s3cret"}})
    assert "arcgis.profile" in str(exc.value) and "allow_plaintext_password" in str(exc.value)
    assert "s3cret" not in str(exc.value)
    assert gis_calls == []


def test_allow_plaintext_password_silences_the_warning(gis_calls, messages):
    common.connect_agol({"arcgis": {"username": "u", "password": "s3cret",
                                    "allow_plaintext_password": True}})
    assert not any(m.startswith("WARNING") for m in messages)
    assert len(gis_calls) == 1


def test_profile_with_leftover_password_warns_without_leaking_it(gis_calls, messages):
    common.connect_agol({"arcgis": {"profile": "p1", "password": "ignored"}})
    assert messages[0] == ("WARNING: arcgis.password is unused with arcgis.profile; "
                           "delete it from the config.")
    assert not any("ignored" in m for m in messages)


def test_profile_without_password_does_not_warn(gis_calls, messages):
    common.connect_agol({"arcgis": {"profile": "p1"}})
    assert not any(m.startswith("WARNING") for m in messages)


def test_smtp_block_reads_password_from_keyring(monkeypatch, messages):
    seen = []

    def fake_get(service, user):
        seen.append((service, user))
        return "kr-pass"

    monkeypatch.setattr(keyring, "get_password", fake_get)
    mail = common.load_mail_settings({"smtp": {
        "host": "smtp.example.test", "port": 25, "from": "a@example.test",
        "user": "mailer"}})
    assert seen == [("taskdash-smtp", "mailer")]
    assert mail == {"smtp server": "smtp.example.test", "from address": "a@example.test",
                    "smtp port": 25, "use tls": True, "use ssl": False, "strict tls": True,
                    "smtp username": "mailer", "smtp password": "kr-pass",
                    "allow plaintext auth": False}
    assert not any("kr-pass" in m for m in messages)


def test_smtp_block_without_user_needs_no_keyring(monkeypatch):
    def boom(*_):
        raise AssertionError("keyring must not be read")

    monkeypatch.setattr(keyring, "get_password", boom)
    mail = common.load_mail_settings({"smtp": {"host": "h.example.test", "from": "a@example.test"}})
    assert mail["smtp server"] == "h.example.test"
    assert "smtp password" not in mail


def test_missing_keyring_entry_gives_an_actionable_warning(monkeypatch, messages):
    monkeypatch.setattr(keyring, "get_password", lambda *_: None)
    mail = common.load_mail_settings({"smtp": {
        "host": "h.example.test", "from": "a@example.test", "user": "mailer"}})
    assert mail is None
    text = " ".join(messages)
    assert "keyring set taskdash-smtp mailer" in text


def test_smtp_block_missing_host_disables_alerting(messages):
    assert common.load_mail_settings({"smtp": {"from": "a@example.test"}}) is None
    assert any("host and from" in m for m in messages)


def test_no_smtp_block_keeps_the_mail_file_adapter(tmp_path, messages):
    missing = tmp_path / "nope.json"
    mail = common.load_mail_settings({"alerting": {"mail_config_path": str(missing)}})
    assert mail is None
    assert any("mail config not found" in m for m in messages)


def test_profile_that_does_not_sign_in_is_an_actionable_exit(monkeypatch, messages):
    class Anonymous:
        def __init__(self, *args, **kwargs):
            self.users = type("U", (), {"me": None})()

    monkeypatch.setattr(arcgis.gis, "GIS", Anonymous)
    with pytest.raises(SystemExit, match="arcgis.profile 'p1' did not sign in"):
        common.connect_agol({"arcgis": {"profile": "p1"}})


def test_unreachable_url_is_an_actionable_exit(monkeypatch, messages):
    import requests

    def unreachable(*args, **kwargs):
        raise requests.exceptions.ConnectionError("Failed to resolve 'your-org.example.com'")

    monkeypatch.setattr(arcgis.gis, "GIS", unreachable)
    with pytest.raises(SystemExit, match=r"Cannot reach arcgis.url 'https://your-org.example.com/portal'"):
        common.connect_agol({"arcgis": {"url": "https://your-org.example.com/portal", "profile": "p1"}})



def test_missing_keyring_package_disables_alerting_instead_of_raising(monkeypatch, messages):
    monkeypatch.setitem(__import__("sys").modules, "keyring", None)
    mail = common.load_mail_settings({"smtp": {
        "host": "h.example.test", "from": "a@example.test", "user": "mailer"}})
    assert mail is None
    assert any("cannot send alerts" in m and "exits 1" in m for m in messages)
