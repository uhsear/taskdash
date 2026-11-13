"""Audit lane s10: `taskdash signin --forget` removes the saved sign-in and mail password, and with
`--for-task-account` does it as the task account through the as-task runner (no runas).

ProfileManager, keyring and Task Scheduler are fakes. Nothing signs in or touches Credential Manager."""

from __future__ import annotations

import json
import sys
import types

import pytest

import taskdash_signin as signin
from test_h3_as_task import ACCOUNT, Env


class Store:
    """One fake ProfileManager and keyring. The fakes share these two collections."""

    def __init__(self, monkeypatch, profiles=("p1",), mail=None):
        self.profiles, self.mail, self.calls = list(profiles), dict(mail or {}), []
        store = self

        class ProfileManager:
            def list(self):
                return list(store.profiles)

            def delete(self, name):
                store.calls.append(("delete profile", name))
                store.profiles.remove(name)

        arcgis_gis = types.ModuleType("arcgis.gis")
        arcgis_gis.ProfileManager = ProfileManager
        keyring = types.ModuleType("keyring")
        keyring.get_password = lambda service, user: store.mail.get((service, user))
        keyring.delete_password = lambda service, user: (store.calls.append(("delete mail", service, user)),
                                                         store.mail.pop((service, user)))
        monkeypatch.setitem(sys.modules, "arcgis.gis", arcgis_gis)
        monkeypatch.setitem(sys.modules, "keyring", keyring)
        monkeypatch.setattr(signin, "disable_arcpy_import", lambda: None)


def _config(tmp_path, smtp_user=""):
    path = tmp_path / "TaskDash.config.json"
    path.write_text(json.dumps({"arcgis": {"profile": "p1"}, "smtp": {"user": smtp_user},
                                "rules": {"profiles": []}}), encoding="utf-8")
    return path


def test_check_mode_lists_the_profile_and_the_mail_password_and_deletes_nothing(tmp_path, monkeypatch, capsys):
    store = Store(monkeypatch, mail={("taskdash-smtp", "mailer"): "x"})
    assert signin.main(["--config", str(_config(tmp_path, "mailer")), "--forget"]) == 0
    out = capsys.readouterr().out
    assert "Would delete the sign-in profile 'p1'" in out and "Would delete the saved mail password for 'mailer'" in out
    assert "Nothing deleted" in out and store.calls == []


def test_apply_deletes_the_profile_and_the_mail_password(tmp_path, monkeypatch, capsys):
    store = Store(monkeypatch, mail={("taskdash-smtp", "mailer"): "x"})
    assert signin.main(["--config", str(_config(tmp_path, "mailer")), "--forget", "--apply"]) == 0
    assert store.calls == [("delete profile", "p1"), ("delete mail", "taskdash-smtp", "mailer")]
    assert store.profiles == [] and store.mail == {}
    assert "Deleting the sign-in profile 'p1'" in capsys.readouterr().out


def test_a_second_run_finds_nothing_and_is_not_an_error(tmp_path, monkeypatch, capsys):
    store = Store(monkeypatch, profiles=())
    assert signin.main(["--config", str(_config(tmp_path, "mailer")), "--forget", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "No sign-in profile 'p1'" in out and "No mail password for 'mailer'" in out and store.calls == []


def test_a_keyring_failure_is_reported_and_exits_1(tmp_path, monkeypatch, capsys):
    store = Store(monkeypatch, mail={("taskdash-smtp", "mailer"): "x"})
    sys.modules["keyring"].delete_password = lambda *a: (_ for _ in ()).throw(RuntimeError("backend"))
    assert signin.main(["--config", str(_config(tmp_path, "mailer")), "--forget", "--apply"]) == 1
    assert "Could not delete the saved sign-in (RuntimeError)" in capsys.readouterr().out
    assert store.calls == [("delete profile", "p1")]


def test_task_user_still_needs_for_task_account():
    with pytest.raises(SystemExit):
        signin.parse_args(["--forget", "--task-user", ACCOUNT])


def test_for_task_account_check_mode_registers_nothing(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch)
    assert signin.main(["--config", str(env.config), "--forget", "--for-task-account"]) == 0
    out = capsys.readouterr().out
    assert "-m taskdash_cli signin --forget --apply" in out and "Nothing registered" in out
    assert env.folder.registered == [] and env.prompts == []


def test_for_task_account_apply_runs_the_forget_command_as_the_task_account(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch, output="Deleting the sign-in profile 'p1' of svc-demo.\n")
    assert signin.main(["--config", str(env.config), "--forget", "--for-task-account", "--apply"]) == 0
    out = capsys.readouterr().out
    (registered,) = env.folder.registered
    assert registered["user"] == ACCOUNT and "signin --forget --apply" in registered["xml"]
    assert "Deleting the sign-in profile" in out and "Forget as" in out and "passed" in out
    assert env.folder.deleted == [registered["name"]] and env.leftovers() == []


def test_a_failed_forget_run_exits_1_with_its_own_wording(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch, result=1, output="Could not delete the saved sign-in (RuntimeError).\n")
    assert signin.main(["--config", str(env.config), "--forget", "--for-task-account", "--apply"]) == 1
    out = capsys.readouterr().out
    assert "Forget as" in out and "the command reported a problem" in out and "preflight found" not in out
