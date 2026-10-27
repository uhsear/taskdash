"""H3-2: `taskdash signin` stores the ArcGIS sign-in profile that INSTALL step 3 used to do by hand.

Every ArcGIS call is a fake. Nothing signs in, nothing touches Credential Manager, and the password
comes only from a faked getpass.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

import taskdash_cli as cli
import taskdash_preflight as pf
import taskdash_signin as signin

SECRET = "pw-never-print-9f3"
URL = "https://org.example.com"
ALL_PRIVILEGES = [key for key, _ in pf.REQUIRED_PRIVILEGES]


def _config(tmp_path, **arcgis):
    base = {"url": URL, "profile": "p1", "username": "owner1"}
    base.update(arcgis)
    path = tmp_path / "TaskDash.config.json"
    path.write_text(json.dumps({"arcgis": base, "rules": {"profiles": []}}), encoding="utf-8")
    return path


class Fakes:
    """Records every sign-in and save, and answers the two prompts."""

    def __init__(self, monkeypatch, me="ok", privileges=None, sign_in_error=None, answers=(), props=None):
        self.opened, self.saved, self.prompts = [], [], []
        self.props = props
        self.answers = list(answers)
        self.me = (SimpleNamespace(username="owner1", privileges=ALL_PRIVILEGES if privileges is None else privileges)
                   if me == "ok" else me)
        self.sign_in_error = sign_in_error
        monkeypatch.setattr(signin.getpass, "getpass", lambda prompt="": SECRET)
        monkeypatch.setattr("builtins.input", self._input)
        monkeypatch.setattr(signin, "_open_gis", self._open)
        monkeypatch.setattr(signin, "save_profile", lambda *a: self.saved.append(a))

    def _input(self, prompt=""):
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else ""

    def _open(self, url, *args, **kwargs):
        self.opened.append((url, args, kwargs))
        if self.sign_in_error and not kwargs.get("profile"):
            raise self.sign_in_error
        return SimpleNamespace(users=SimpleNamespace(me=self.me), properties=self.props)


def _run(tmp_path, capsys, *flags, **arcgis):
    code = signin.main(["--config", str(_config(tmp_path, **arcgis)), *flags])
    return code, capsys.readouterr().out


def test_signin_is_a_registered_command():
    assert cli.SUBCOMMAND_MODULES["signin"] == "taskdash_signin"
    assert "signin" in cli.USAGE


def test_check_mode_signs_in_and_saves_nothing(tmp_path, monkeypatch, capsys):
    fakes = Fakes(monkeypatch)
    code, out = _run(tmp_path, capsys)
    assert code == 0 and fakes.saved == []
    assert fakes.opened == [(URL, ("owner1", SECRET), {})]
    assert "[ok] sign in" in out and "[ok] account" in out and "[ok] privileges" in out
    assert "Nothing saved" in out and "--apply" in out


def test_password_comes_from_getpass_and_is_never_printed(tmp_path, monkeypatch, capsys):
    fakes = Fakes(monkeypatch)
    argv = ["--config", str(_config(tmp_path)), "--apply"]
    monkeypatch.setattr(sys, "argv", ["taskdash", "signin", *argv])
    assert signin.main(argv) == 0
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err and SECRET not in " ".join(sys.argv)
    assert not any(SECRET in p for p in fakes.prompts)


def test_apply_saves_the_profile_and_proves_it_signs_in(tmp_path, monkeypatch, capsys):
    fakes = Fakes(monkeypatch)
    code, out = _run(tmp_path, capsys, "--apply")
    assert code == 0
    assert fakes.saved == [("p1", URL, "owner1", SECRET)]
    assert fakes.opened[-1] == (URL, (), {"profile": "p1"})
    assert "[ok] profile" in out and "task account" in out


def test_empty_users_me_fails_with_the_fix_and_saves_nothing(tmp_path, monkeypatch, capsys):
    fakes = Fakes(monkeypatch, me=None)
    code, out = _run(tmp_path, capsys, "--apply")
    assert code == 1 and fakes.saved == []
    assert "[!!] account" in out and "users.me is empty" in out and "user name and password" in out


def test_missing_privilege_fails_naming_it_and_saves_nothing(tmp_path, monkeypatch, capsys):
    fakes = Fakes(monkeypatch, privileges=["portal:user:createItem", "features:user:edit"])
    code, out = _run(tmp_path, capsys, "--apply")
    assert code == 1 and fakes.saved == []
    assert "[!!] privileges" in out and "portal:user:createGroup" in out and "portal:user:shareToGroup" in out


def test_wrong_password_says_what_to_do_and_hides_the_password(tmp_path, monkeypatch, capsys):
    fakes = Fakes(monkeypatch, sign_in_error=RuntimeError(f"Unable to generate token for {SECRET}"))
    code, out = _run(tmp_path, capsys, "--apply")
    assert code == 1 and fakes.saved == []
    assert SECRET not in out
    assert "[!!] sign in" in out and "built-in ArcGIS account" in out and "TROUBLESHOOTING.md" in out


def test_blank_url_is_asked_for_and_the_config_fix_is_printed(tmp_path, monkeypatch, capsys):
    fakes = Fakes(monkeypatch, answers=["https://typed.example.com", ""])
    code, out = _run(tmp_path, capsys, url="")
    assert code == 0
    assert fakes.opened[0][0] == "https://typed.example.com"
    assert "Set arcgis.url" in out and "https://typed.example.com" in out


def test_user_name_defaults_to_the_config_and_can_be_typed(tmp_path, monkeypatch, capsys):
    fakes = Fakes(monkeypatch, answers=["other1"])
    _run(tmp_path, capsys)
    assert fakes.opened[0][1][0] == "other1" and "owner1" in fakes.prompts[0]


def test_no_user_name_anywhere_stops_with_a_message(tmp_path, monkeypatch):
    Fakes(monkeypatch)
    with pytest.raises(SystemExit, match="no ArcGIS user name"):
        signin.main(["--config", str(_config(tmp_path, username=""))])


def test_blank_profile_stops_before_any_prompt(tmp_path, monkeypatch):
    fakes = Fakes(monkeypatch)
    with pytest.raises(SystemExit, match="arcgis.profile is empty"):
        signin.main(["--config", str(_config(tmp_path, profile=""))])
    assert fakes.prompts == [] and fakes.opened == []
