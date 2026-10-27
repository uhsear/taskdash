"""H3-6: `taskdash preflight --as-task` runs preflight as the task account without runas.

Task Scheduler is a fake COM service, icacls is a fake subprocess, and the password comes only from a
faked getpass. Nothing registers a real task, touches real folders or reaches the network.
"""

from __future__ import annotations

import json
import re
import sys
from types import SimpleNamespace

import pytest

import taskdash_astask as astask
import taskdash_cli as cli
import taskdash_preflight as pf

SECRET = "pw-never-print-7c2"
ACCOUNT = r"EXAMPLE\svc-demo"
COLLECTOR = "Demo Collector"
NOT_FOUND = -2147024894
ACCESS_DENIED = -2147024891
LOGON_FAILURE = -2147023570


class FakeComError(Exception):
    def __init__(self, hresult, text="com error"):
        super().__init__(text)
        self.hresult = hresult


class FakeTask:
    """State is read once per poll; the last value repeats."""

    def __init__(self, folder, name, states=(3,), result=0, output="[ok] config: fine\n", run_error=None):
        self.folder, self.name, self.result, self.output, self.run_error = folder, name, result, output, run_error
        self.states = list(states)
        self.stopped = False
        self.ran = False

    @property
    def State(self):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    @property
    def LastTaskResult(self):
        return self.result

    def Run(self, params):
        if self.run_error:
            raise self.run_error
        self.ran = True
        for out in self.folder.log_dir.glob("preflight_as_task_*.txt"):
            out.write_text(self.output, encoding="utf-8")

    def Stop(self, flags):
        self.stopped = True


class FakeFolder:
    def __init__(self, log_dir, real=None, **task_options):
        self.log_dir, self.real, self.task_options = log_dir, real, task_options
        self.registered, self.deleted, self.created = [], [], None

    def GetTask(self, name):
        if self.real and name == self.real["name"]:
            principal = SimpleNamespace(UserId=self.real["user"], LogonType=self.real["logon"])
            return SimpleNamespace(Definition=SimpleNamespace(Principal=principal))
        raise FakeComError(NOT_FOUND)

    def RegisterTask(self, name, xml, flags, user, password, logon):
        if "register_error" in self.task_options:
            raise self.task_options["register_error"]
        self.registered.append({"name": name, "xml": xml, "flags": flags, "user": user,
                                "password": password, "logon": logon})
        options = {k: v for k, v in self.task_options.items() if k not in ("register_error", "delete_error")}
        self.created = FakeTask(self, name, **options)
        return self.created

    def DeleteTask(self, name, flags):
        if self.task_options.get("delete_error"):
            raise self.task_options["delete_error"]
        self.deleted.append(name)


class Env:
    def __init__(self, tmp_path, monkeypatch, real_logon=1, with_real=True, **task_options):
        self.log_dir = tmp_path / "Logs"
        real = {"name": COLLECTOR, "user": ACCOUNT, "logon": real_logon} if with_real else None
        self.folder = FakeFolder(self.log_dir, real, **task_options)
        service = SimpleNamespace(GetFolder=lambda path: self.folder)
        self.argv_seen, self.prompts = [], []
        monkeypatch.setattr(astask, "connect_scheduler", lambda: service)
        monkeypatch.setattr(astask.subprocess, "run", self._run)
        monkeypatch.setattr(astask.getpass, "getpass", self._getpass)
        monkeypatch.setattr(astask, "RUN_TIMEOUT_SECONDS", 0.2)
        monkeypatch.setattr(astask, "POLL_SECONDS", 0.01)
        monkeypatch.setattr(astask, "NOT_RUN_GUARD_SECONDS", 0.05)
        config = {"scheduler": {"collector_task_name": COLLECTOR}, "logging": {"log_dir": str(self.log_dir)},
                  "rules": {"profiles": []}}
        self.config = tmp_path / "TaskDash.config.json"
        self.config.write_text(json.dumps(config), encoding="utf-8")

    def _run(self, argv, *args, **kwargs):
        self.argv_seen.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def _getpass(self, prompt=""):
        self.prompts.append(prompt)
        return SECRET

    def main(self, capsys, *flags):
        code = pf.main(["--config", str(self.config), "--as-task", *flags])
        captured = capsys.readouterr()
        return code, captured.out + captured.err

    def leftovers(self):
        return list(self.log_dir.glob("preflight_as_task_*.txt")) if self.log_dir.is_dir() else []


def test_flags_are_parsed_and_validated(capsys):
    assert pf.parse_args(["--as-task", "--task-user", ACCOUNT, "--apply", "--offline"]).as_task
    for bad in (["--task-user", ACCOUNT], ["--as-task", "--config-diff"], ["--as-task", "--enable-event-log"]):
        with pytest.raises(SystemExit):
            pf.parse_args(bad)
    capsys.readouterr()


def test_check_mode_prints_the_plan_and_changes_nothing(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch)
    code, out = env.main(capsys)
    assert code == 0
    assert env.folder.registered == [] and env.folder.deleted == [] and env.prompts == []
    assert env.argv_seen == [] and env.leftovers() == [] and not env.log_dir.exists()
    assert re.search(r"TaskDash preflight check [0-9a-f]{8}", out)
    assert ACCOUNT in out and "Password" in out and "-m taskdash_cli preflight" in out
    assert "Nothing registered" in out and "--apply" in out


def test_apply_registers_runs_prints_and_always_cleans_up(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch)
    code, out = env.main(capsys, "--apply")
    assert code == 0
    (registered,) = env.folder.registered
    assert registered["user"] == ACCOUNT and registered["password"] == SECRET
    assert registered["logon"] == astask.LOGON_PASSWORD and registered["flags"] == astask.TASK_CREATE
    assert env.folder.created.ran
    assert "[ok] config: fine" in out
    assert env.folder.deleted == [registered["name"]] and env.leftovers() == []


def test_the_task_uses_the_collector_template_without_a_trigger(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch)
    env.main(capsys, "--apply")
    xml = env.folder.registered[0]["xml"]
    assert "<LogonType>Password</LogonType>" in xml and f"<UserId>{ACCOUNT}</UserId>" in xml
    assert "<Triggers>" not in xml and "{{" not in xml
    assert "hourly collector" not in xml
    assert "<RunLevel>LeastPrivilege</RunLevel>" in xml and "<MultipleInstancesPolicy>IgnoreNew" in xml
    assert "-X utf8 -m taskdash_cli preflight" in xml and "preflight_as_task_" in xml


def test_rendered_xml_escapes_and_parses():
    from xml.etree import ElementTree  # our own rendered template, not untrusted input

    xml = astask.render_task_xml(r"EX\a&b", "Password", r"C:\x\cmd.exe", 'a > "b" 2>&1', r"C:\Task Dash")
    root = ElementTree.fromstring(xml)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find(".//t:UserId", ns).text == r"EX\a&b"
    assert root.find(".//t:Arguments", ns).text == 'a > "b" 2>&1'


def test_interactive_logon_matches_the_real_task_and_asks_for_no_password(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch, real_logon=3)
    code, _ = env.main(capsys, "--apply")
    (registered,) = env.folder.registered
    assert code == 0 and env.prompts == []
    assert registered["password"] is None and registered["logon"] == astask.LOGON_INTERACTIVE
    assert "<LogonType>InteractiveToken</LogonType>" in registered["xml"]


def test_a_failed_run_still_prints_the_output_and_unregisters(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch, result=1, output="[!!] smtp: cannot connect\n")
    code, out = env.main(capsys, "--apply")
    assert code == 1 and "[!!] smtp: cannot connect" in out
    assert len(env.folder.deleted) == 1 and env.leftovers() == []


def test_a_timeout_stops_the_task_and_unregisters(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch, states=(4,))
    code, out = env.main(capsys, "--apply")
    assert code == 1 and env.folder.created.stopped
    assert "did not finish" in out
    assert len(env.folder.deleted) == 1 and env.leftovers() == []


def test_a_task_that_never_started_is_reported_and_unregistered(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch, states=(3,), result=astask.NOT_RUN_YET)
    code, out = env.main(capsys, "--apply")
    assert code == 1 and "never started" in out
    assert len(env.folder.deleted) == 1 and env.leftovers() == []


def test_an_error_while_starting_still_unregisters(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch, run_error=FakeComError(ACCESS_DENIED, f"denied for {SECRET}"))
    code, out = env.main(capsys, "--apply")
    assert code == 1 and SECRET not in out and "administrator" in out
    assert len(env.folder.deleted) == 1 and env.leftovers() == []


def test_a_rejected_registration_leaves_nothing_behind(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch, register_error=FakeComError(LOGON_FAILURE, f"bad {SECRET}"))
    code, out = env.main(capsys, "--apply")
    assert code == 1 and SECRET not in out and "password" in out.lower()
    assert env.folder.deleted == [] and env.leftovers() == []


def test_a_failed_delete_names_the_manual_fix_and_still_removes_the_file(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch, delete_error=FakeComError(ACCESS_DENIED))
    code, out = env.main(capsys, "--apply")
    assert code == 1 and "schtasks /delete" in out
    assert env.leftovers() == []


def test_a_file_that_cannot_be_deleted_is_named_and_does_not_crash_the_command(tmp_path, monkeypatch, capsys):
    # A task that was stopped after a timeout can still hold the output file for a moment.
    env = Env(tmp_path, monkeypatch)
    real_unlink = astask.Path.unlink

    def held(self, *args, **kwargs):
        if self.name.startswith(astask.TEMP_FILE_PREFIX):
            raise PermissionError(13, "in use")
        return real_unlink(self, *args, **kwargs)
    monkeypatch.setattr(astask.Path, "unlink", held)
    code, out = env.main(capsys, "--apply")
    assert code == 1 and len(env.folder.deleted) == 1
    assert "Could not delete the output file" in out and astask.TEMP_FILE_PREFIX in out
    assert "Removed the temporary task" not in out


def test_the_password_is_never_printed_or_passed_in_argv(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["taskdash", "preflight", "--as-task", "--apply"])
    code, out = env.main(capsys, "--apply")
    assert code == 0 and SECRET not in out
    assert not any(SECRET in part for argv in env.argv_seen for part in argv) and SECRET not in " ".join(sys.argv)
    assert not any(SECRET in p for p in env.prompts)
    assert SECRET not in env.folder.registered[0]["xml"]


def test_the_output_file_is_limited_to_the_account_you_and_administrators(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch)
    env.main(capsys, "--apply")
    (argv,) = env.argv_seen
    assert argv[0].lower().endswith("icacls.exe") and "/inheritance:r" in argv
    grants = " ".join(argv)
    assert f"{ACCOUNT}:(M)" in grants and "*S-1-5-32-544:(F)" in grants and "*S-1-5-18:(F)" in grants
    assert "/grant:r" in argv


def test_without_a_collector_task_the_account_must_be_named(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch, with_real=False)
    code, out = env.main(capsys, "--apply")
    assert code == 1 and "--task-user" in out
    assert env.folder.registered == [] and env.prompts == []
    code, out = env.main(capsys, "--apply", "--task-user", ACCOUNT)
    assert code == 0 and env.folder.registered[0]["user"] == ACCOUNT
    assert env.folder.registered[0]["logon"] == astask.LOGON_PASSWORD


def test_the_real_collector_task_is_only_read(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch)
    env.main(capsys, "--apply")
    assert COLLECTOR not in env.folder.deleted
    assert all(r["name"] != COLLECTOR for r in env.folder.registered)


def test_an_empty_password_is_refused_before_anything_registers(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch)
    monkeypatch.setattr(astask.getpass, "getpass", lambda prompt="": "")
    code, out = env.main(capsys, "--apply")
    assert code == 1 and "password" in out.lower()
    assert env.folder.registered == [] and env.leftovers() == []


def test_the_cli_routes_preflight_to_the_new_flag(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch)
    assert cli.main(["preflight", "--config", str(env.config), "--as-task"]) == 0
    assert "Nothing registered" in capsys.readouterr().out
