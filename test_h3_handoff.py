"""H3-7: `taskdash signin --for-task-account` seals the task account's sign-in into a hand-off file, and
the next collector run as that account stores it and deletes the file. No runas.

win32crypt, win32security, keyring and ArcGIS are fakes, so the tests run without pywin32 and touch
no real DPAPI, ACL, Credential Manager, scheduled task or network. The fake cipher is XOR, so a test
can tell sealed bytes from plaintext.
"""

from __future__ import annotations

import getpass
import json
import os
import sys
from types import SimpleNamespace

import pytest

import taskdash_cli as cli
import taskdash_handoff as handoff
import taskdash_preflight as pf
import taskdash_publish as publish
import taskdash_signin as signin

SECRET = "pw-never-print-4d8"
MAIL_SECRET = "mail-never-print-1b6"
URL = "https://org.example.com"
ACCOUNT = r"EXAMPLE\svc-demo"
LOCAL_MACHINE_FLAG = 0x4
ALL_PRIVILEGES = ["portal:user:createItem", "portal:publisher:publishFeatures", "portal:user:createGroup",
                  "portal:user:shareToGroup", "features:user:edit"]


def _xor(data: bytes) -> bytes:
    return bytes(b ^ 0x5A for b in data)


class FakeAcl:
    def __init__(self):
        self.aces = []

    def AddAccessAllowedAce(self, revision, mask, sid):
        self.aces.append((mask, sid))


class FakeHandle:
    """A Windows file handle. CREATE_NEW maps to open mode "x", so a leftover file fails like the real call."""

    def __init__(self, path):
        self.path = path
        self.file = open(path, "xb")
        self.closed = False

    def Close(self):
        self.file.close()
        self.closed = True


class Win32:
    """Stands in for win32crypt, win32security and win32file and records every call.

    SetNamedSecurityInfo is missing on purpose: it opens the file by path, which fails with a sharing
    violation while the writer holds the file with share mode 0. The ACL must go through the handle."""

    def __init__(self, monkeypatch, acl_error=None):
        self.protect_calls, self.acl_calls, self.acl_error = [], [], acl_error
        self.create_calls, self.handles = [], []
        crypt = SimpleNamespace(CryptProtectData=self._protect, CryptUnprotectData=self._unprotect)
        security = SimpleNamespace(
            ACL=FakeAcl, ACL_REVISION=2, SE_FILE_OBJECT=1, DACL_SECURITY_INFORMATION=4,
            PROTECTED_DACL_SECURITY_INFORMATION=0x80000000,
            LookupAccountName=lambda system, name: (f"SID<{name}>", "domain", 1),
            ConvertStringSidToSid=lambda text: f"SID<{text}>",
            SetSecurityInfo=self._set_security)
        files = SimpleNamespace(CreateFile=self._create, WriteFile=self._write)
        monkeypatch.setitem(sys.modules, "win32crypt", crypt)
        monkeypatch.setitem(sys.modules, "win32security", security)
        monkeypatch.setitem(sys.modules, "win32file", files)

    def _create(self, path, access, share, attributes, disposition, flags, template):
        self.create_calls.append({"path": path, "access": access, "share": share, "disposition": disposition})
        handle = FakeHandle(path)
        self.handles.append(handle)
        return handle

    def _write(self, handle, data):
        handle.file.write(data)
        return 0, len(data)

    def _protect(self, data, description, entropy, reserved, prompt, flags):
        self.protect_calls.append({"description": description, "flags": flags})
        return _xor(data)

    def _unprotect(self, blob, entropy, reserved, prompt, flags):
        return "TaskDash hand-off", _xor(blob)

    def _set_security(self, handle, kind, info, owner, group, dacl, sacl):
        if self.acl_error:
            raise self.acl_error
        self.acl_calls.append({"path": handle.path, "info": info, "aces": list(dacl.aces),
                               "size": os.path.getsize(handle.path)})


class Env:
    """An install folder, a config, a fake machine and a fake ArcGIS."""

    def __init__(self, tmp_path, monkeypatch, smtp_user="", sign_in_ok=True, save_error=None, whoami="svc-demo"):
        self.home = tmp_path / "install"
        self.home.mkdir()
        self.win32 = Win32(monkeypatch)
        self.saved, self.mail, self.opened, self.prompts = [], [], [], []
        self.sign_in_ok, self.save_error = sign_in_ok, save_error
        monkeypatch.setattr(handoff, "runtime_dir", lambda: self.home)
        monkeypatch.setattr(signin, "runtime_dir", lambda: self.home)
        monkeypatch.setattr(signin, "_open_gis", self._open)
        monkeypatch.setattr(signin, "save_profile", self._save)
        monkeypatch.setattr(signin.getpass, "getpass", self._getpass)
        monkeypatch.setattr("builtins.input", lambda prompt="": "")
        monkeypatch.setattr(getpass, "getuser", lambda: whoami)
        monkeypatch.setitem(sys.modules, "keyring",
                            SimpleNamespace(set_password=lambda *a: self.mail.append(a)))
        base = {"url": URL, "profile": "p1", "username": "owner1"}
        smtp = {"host": "mail.example.com", "from": "a@example.com", "user": smtp_user} if smtp_user else {}
        self.config = tmp_path / "TaskDash.config.json"
        self.config.write_text(json.dumps({"arcgis": base, "smtp": smtp, "rules": {"profiles": []},
                                           "scheduler": {"collector_task_name": "Demo Collector"}}),
                               encoding="utf-8")
        self.file = self.home / handoff.HANDOFF_FILE

    def _open(self, url, *args, **kwargs):
        self.opened.append((url, args, kwargs))
        if kwargs.get("profile") and not self.sign_in_ok:
            return SimpleNamespace(users=SimpleNamespace(me=None))
        me = SimpleNamespace(username="owner1", privileges=ALL_PRIVILEGES)
        return SimpleNamespace(users=SimpleNamespace(me=me))

    def _save(self, *args):
        if self.save_error:
            raise self.save_error
        self.saved.append(args)

    def _getpass(self, prompt=""):
        self.prompts.append(prompt)
        return MAIL_SECRET if "mail" in prompt.lower() else SECRET

    def signin(self, capsys, *flags):
        code = signin.main(["--config", str(self.config), *flags])
        out = capsys.readouterr()
        return code, out.out + out.err

    def seal_file(self, **overrides):
        payload = {"version": 1, "account": ACCOUNT, "profile": "p1", "url": URL, "username": "owner1",
                   "password": SECRET, "smtp_user": "", "smtp_password": ""}
        payload.update(overrides)
        self.file.write_bytes(handoff.seal(payload))


# --------------------------------------------------------------------------- writing the file

def test_the_flags_are_parsed_and_task_user_needs_the_hand_off(capsys):
    args = signin.parse_args(["--for-task-account", "--task-user", ACCOUNT, "--apply"])
    assert args.for_task_account and args.task_user == ACCOUNT
    with pytest.raises(SystemExit):
        signin.parse_args(["--task-user", ACCOUNT])
    capsys.readouterr()


def test_check_mode_prints_the_plan_and_writes_nothing(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch)
    code, out = env.signin(capsys, "--for-task-account", "--task-user", ACCOUNT)
    assert code == 0 and not env.file.exists()
    assert env.win32.protect_calls == [] and env.win32.acl_calls == [] and env.saved == []
    assert ACCOUNT in out and handoff.HANDOFF_FILE in out and "--apply" in out and "Nothing written" in out


def test_apply_writes_a_file_sealed_for_the_machine_and_saves_no_profile_for_the_admin(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch)
    code, out = env.signin(capsys, "--for-task-account", "--task-user", ACCOUNT, "--apply")
    assert code == 0 and env.saved == [], "the admin's own store is not touched"
    (call,) = env.win32.protect_calls
    assert call["flags"] == handoff.CRYPTPROTECT_LOCAL_MACHINE == LOCAL_MACHINE_FLAG
    blob = env.file.read_bytes()
    for plain in (SECRET, "owner1", URL, "p1"):
        assert plain.encode() not in blob
    payload = handoff.unseal(blob)
    assert (payload["account"], payload["profile"], payload["url"], payload["username"], payload["password"]) == (
        ACCOUNT, "p1", URL, "owner1", SECRET)
    assert "[ok] hand-off" in out and "next collector run" in out


def test_the_file_is_limited_to_the_task_account_system_and_administrators_without_inheritance(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch)
    env.signin(capsys, "--for-task-account", "--task-user", ACCOUNT, "--apply")
    (acl,) = env.win32.acl_calls
    assert acl["path"] == str(env.file)
    assert acl["info"] & 0x80000000, "protected: no inherited entries"
    assert {sid for _, sid in acl["aces"]} == {f"SID<{ACCOUNT}>", "SID<S-1-5-18>", "SID<S-1-5-32-544>"}
    masks = {sid: mask for mask, sid in acl["aces"]}
    assert masks["SID<S-1-5-18>"] == masks["SID<S-1-5-32-544>"] == handoff.FILE_ALL_ACCESS
    assert masks[f"SID<{ACCOUNT}>"] == handoff.FILE_GENERIC_READ | handoff.DELETE


def test_nothing_secret_is_on_disk_until_the_acl_is_restricted(tmp_path, monkeypatch, capsys):
    # The install folder's own ACL applies to a new file. Machine-scope DPAPI lets any local process
    # decrypt the bytes, so the bytes must not exist while that ACL still applies.
    env = Env(tmp_path, monkeypatch)
    env.signin(capsys, "--for-task-account", "--task-user", ACCOUNT, "--apply")
    (acl,) = env.win32.acl_calls
    assert acl["size"] == 0 and env.file.stat().st_size > 0


def test_no_other_handle_can_exist_while_the_file_has_the_folders_acl(tmp_path, monkeypatch, capsys):
    # Windows checks access when a handle opens. A handle that another principal opens while the file is
    # empty and inherits the folder's ACL keeps read access after the ACL changes, so the writer holds
    # the file with share mode 0 from creation, and the ACL is replaced through that same handle.
    env = Env(tmp_path, monkeypatch)
    env.signin(capsys, "--for-task-account", "--task-user", ACCOUNT, "--apply")
    (create,) = env.win32.create_calls
    assert create["share"] == 0 and create["disposition"] == handoff.CREATE_NEW
    assert create["access"] & handoff.WRITE_DAC and create["access"] & handoff.GENERIC_WRITE
    (handle,) = env.win32.handles
    (acl,) = env.win32.acl_calls
    assert acl["path"] == handle.path and handle.closed
    assert handoff.unseal(env.file.read_bytes())["password"] == SECRET


def test_a_failed_acl_leaves_no_sealed_bytes_behind(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.win32.acl_error = OSError("denied")
    with pytest.raises(OSError):
        handoff.write_handoff({"password": SECRET}, ACCOUNT)
    assert not env.file.exists()


def test_no_mail_password_is_asked_without_smtp_user(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch)
    env.signin(capsys, "--for-task-account", "--task-user", ACCOUNT, "--apply")
    assert not any("mail" in p.lower() for p in env.prompts)
    assert handoff.unseal(env.file.read_bytes())["smtp_password"] == ""


def test_the_mail_password_is_asked_when_smtp_user_is_set_and_sealed_too(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch, smtp_user="alerts1")
    code, out = env.signin(capsys, "--for-task-account", "--task-user", ACCOUNT, "--apply")
    payload = handoff.unseal(env.file.read_bytes())
    assert code == 0 and payload["smtp_user"] == "alerts1" and payload["smtp_password"] == MAIL_SECRET
    assert MAIL_SECRET.encode() not in env.file.read_bytes()


def test_no_secret_reaches_the_output_the_arguments_or_a_prompt(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch, smtp_user="alerts1")
    argv = ["--config", str(env.config), "--for-task-account", "--task-user", ACCOUNT, "--apply"]
    monkeypatch.setattr(sys, "argv", ["taskdash", "signin", *argv])
    assert signin.main(argv) == 0
    out = capsys.readouterr()
    for secret in (SECRET, MAIL_SECRET):
        assert secret not in out.out + out.err + " ".join(sys.argv)
        assert not any(secret in p for p in env.prompts)


def test_a_failed_acl_removes_the_file_and_says_what_to_do(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch)
    env.win32.acl_error = OSError(f"denied {SECRET}")
    code, out = env.signin(capsys, "--for-task-account", "--task-user", ACCOUNT, "--apply")
    assert code == 1 and not env.file.exists()
    assert "[!!] hand-off" in out and "runas" in out and SECRET not in out


def test_the_task_account_is_read_from_the_collector_task_when_not_named(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch)
    principal = SimpleNamespace(UserId=ACCOUNT, LogonType=1)
    task = SimpleNamespace(Definition=SimpleNamespace(Principal=principal))
    folder = SimpleNamespace(GetTask=lambda name: task if name == "Demo Collector" else None)
    import taskdash_astask as astask
    monkeypatch.setattr(astask, "connect_scheduler", lambda: SimpleNamespace(GetFolder=lambda path: folder))
    code, out = env.signin(capsys, "--for-task-account", "--apply")
    assert code == 0 and ACCOUNT in out
    assert handoff.unseal(env.file.read_bytes())["account"] == ACCOUNT


def test_a_failed_sign_in_check_writes_no_file(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch)
    monkeypatch.setattr(signin, "_open_gis", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"bad {SECRET}")))
    code, out = env.signin(capsys, "--for-task-account", "--task-user", ACCOUNT, "--apply")
    assert code == 1 and not env.file.exists() and SECRET not in out


def test_an_older_file_is_replaced(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch)
    env.seal_file(password="older-secret")
    code, _ = env.signin(capsys, "--for-task-account", "--task-user", ACCOUNT, "--apply")
    assert code == 0 and handoff.unseal(env.file.read_bytes())["password"] == SECRET


# --------------------------------------------------------------------------- consuming the file

def test_a_run_as_the_task_account_stores_the_sign_in_verifies_it_and_deletes_the_file(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch)
    env.seal_file(smtp_user="alerts1", smtp_password=MAIL_SECRET)
    handoff.consume_handoff()
    out = capsys.readouterr()
    assert env.saved == [("p1", URL, "owner1", SECRET)]
    assert env.mail == [("taskdash-smtp", "alerts1", MAIL_SECRET)]
    assert env.opened[-1] == (URL, (), {"profile": "p1"}), "it proves the saved profile signs in"
    assert not env.file.exists()
    assert "removed" in out.out.lower()
    assert SECRET not in out.out + out.err and MAIL_SECRET not in out.out + out.err


def test_the_arcpy_guard_is_on_before_anything_imports_arcgis(tmp_path, monkeypatch):
    # consume_handoff runs before connect_agol, which is the only other place that installs the guard.
    # Under a non-interactive task the first arcgis import probes arcpy and dies on the Pro licence.
    env = Env(tmp_path, monkeypatch)
    env.seal_file()
    order = []
    monkeypatch.setattr(handoff, "disable_arcpy_import", lambda: order.append("guard"))
    monkeypatch.setattr(signin, "save_profile", lambda *a: order.append("save_profile"))
    monkeypatch.setattr(signin, "_open_gis", lambda *a, **k: order.append("open_gis") or SimpleNamespace(
        users=SimpleNamespace(me=SimpleNamespace(username="owner1", privileges=ALL_PRIVILEGES))))
    handoff.consume_handoff()
    assert order == ["guard", "save_profile", "open_gis"]


def test_no_file_means_the_guard_is_not_installed_here(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(handoff, "disable_arcpy_import", lambda: calls.append("guard"))
    handoff.consume_handoff()
    assert calls == [] and not env.file.exists()


def test_a_file_without_a_mail_password_leaves_keyring_alone(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.seal_file()
    handoff.consume_handoff()
    assert env.mail == [] and not env.file.exists()


def test_a_failed_verify_keeps_the_file_and_fails_the_run(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch, sign_in_ok=False)
    env.seal_file()
    before = env.file.read_bytes()
    with pytest.raises(SystemExit) as exc:
        handoff.consume_handoff()
    assert env.file.read_bytes() == before
    text = str(exc.value)
    assert "kept" in text and "signin --for-task-account" in text and "TROUBLESHOOTING.md" in text
    out = capsys.readouterr()
    assert SECRET not in text + out.out + out.err


def test_a_failed_save_keeps_the_file_and_hides_the_secret(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch, save_error=RuntimeError(f"cannot store {SECRET}"))
    env.seal_file()
    with pytest.raises(SystemExit) as exc:
        handoff.consume_handoff()
    assert env.file.exists() and SECRET not in str(exc.value)
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err


def test_a_file_that_cannot_be_opened_is_kept_and_named(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.file.write_bytes(b"not sealed by this machine")
    monkeypatch.setattr(sys.modules["win32crypt"], "CryptUnprotectData",
                        lambda *a: (_ for _ in ()).throw(OSError("wrong machine")), raising=False)
    with pytest.raises(SystemExit, match="could not be opened"):
        handoff.consume_handoff()
    assert env.file.exists() and env.saved == []


def test_a_run_by_another_account_leaves_the_file_for_the_task_account(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch, whoami="admin1")
    env.seal_file()
    handoff.consume_handoff()
    assert env.file.exists() and env.saved == [] and env.opened == []
    assert ACCOUNT in capsys.readouterr().out


def test_an_account_that_cannot_read_the_restricted_file_leaves_it_alone(tmp_path, monkeypatch, capsys):
    # The file's ACL names the task account, SYSTEM and Administrators. A non-elevated admin or another
    # account gets PermissionError at the read, before the sealed account name can be compared.
    env = Env(tmp_path, monkeypatch, whoami="admin1")
    env.seal_file()

    def denied(self):
        raise PermissionError(13, "Access is denied")
    monkeypatch.setattr(handoff.Path, "read_bytes", denied)
    handoff.consume_handoff()
    assert env.file.exists() and env.saved == [] and env.opened == []
    out = capsys.readouterr().out
    assert "left it alone" in out and "WARNING" in out and handoff.HANDOFF_FILE in out


def test_the_account_match_ignores_domain_case_and_upn_form(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, whoami="SVC-Demo")
    env.seal_file(account="svc-demo@example.com")
    handoff.consume_handoff()
    assert env.saved and not env.file.exists()


def test_no_file_means_no_work(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    for name in ("win32crypt", "win32security"):
        monkeypatch.delitem(sys.modules, name)
    handoff.consume_handoff()
    assert env.saved == [] and env.opened == []


# --------------------------------------------------------------------------- the collector

def _collect_fakes(monkeypatch, tmp_path, calls):
    monkeypatch.setattr(publish, "consume_handoff", lambda: calls.append("consume"))
    monkeypatch.setattr(publish, "connect_agol", lambda cfg: calls.append("connect") or (_ for _ in ()).throw(
        ConnectionError("stop here")))
    monkeypatch.setattr(publish, "collect_everything",
                        lambda *a, **k: {"host_name": "H", "inventory": [], "runs": [], "metrics": [],
                                         "log_summary": {}})
    monkeypatch.setattr(publish, "report", lambda collected: None)
    monkeypatch.setattr(publish, "maybe_send_alert", lambda *a: True)


def test_collect_consumes_the_hand_off_before_it_connects(tmp_path, monkeypatch):
    calls = []
    _collect_fakes(monkeypatch, tmp_path, calls)
    with pytest.raises(ConnectionError):
        publish.run_publish(publish.parse_args(["--apply", "--target", "prod"]), {}, tmp_path)
    assert calls == ["consume", "connect"]


def test_a_failed_hand_off_fails_the_run_like_a_sign_in_failure_and_records_it(tmp_path, monkeypatch):
    calls = []
    _collect_fakes(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(publish, "consume_handoff", lambda: (_ for _ in ()).throw(
        SystemExit("The hand-off sign-in was kept; fix it")))
    with pytest.raises(SystemExit, match="hand-off sign-in"):
        publish.run_publish(publish.parse_args(["--apply", "--target", "prod"]), {}, tmp_path)
    rows = json.loads((tmp_path / "collector_runs_pending_prod.json").read_text(encoding="utf-8"))
    assert [r["exit_status"] for r in rows] == ["failed"] and "hand-off" in rows[0]["error_text"]


def test_a_dry_run_leaves_the_hand_off_alone(tmp_path, monkeypatch):
    calls = []
    _collect_fakes(monkeypatch, tmp_path, calls)
    publish.run_publish(publish.parse_args(["--target", "prod"]), {}, tmp_path)
    assert calls == []


def _credentials_check(monkeypatch, **flags):
    calls = []
    me = SimpleNamespace(username="u1", userType="creatorUT", role="org_admin")
    monkeypatch.setattr(pf, "consume_handoff", lambda: calls.append("consume"))
    monkeypatch.setattr(pf, "connect_agol",
                        lambda cfg: calls.append("connect") or SimpleNamespace(users=SimpleNamespace(me=me)))
    status, _ = pf.check_credentials(pf.Context(args=SimpleNamespace(**flags), config={}))
    return status, calls


def test_the_as_task_preflight_stores_the_hand_off_before_its_credentials_check(monkeypatch):
    status, calls = _credentials_check(monkeypatch, consume_handoff=True)
    assert status == pf.OK and calls == ["consume", "connect"]


def test_a_plain_preflight_never_stores_or_deletes_the_hand_off(monkeypatch):
    # Plain preflight is documented as read-only, even when the task account runs it by hand.
    for flags in ({}, {"consume_handoff": False}):
        status, calls = _credentials_check(monkeypatch, **flags)
        assert status == pf.OK and calls == ["connect"]


def test_only_the_as_task_child_asks_to_consume_the_hand_off(tmp_path):
    from taskdash_astask import preflight_command
    assert pf.parse_args(["--consume-handoff"]).consume_handoff is True
    assert pf.parse_args([]).consume_handoff is False
    _, arguments = preflight_command(tmp_path / "out.txt", "", False)
    assert " preflight --consume-handoff" in arguments


def test_signin_is_still_a_registered_command():
    assert cli.SUBCOMMAND_MODULES["signin"] == "taskdash_signin"
