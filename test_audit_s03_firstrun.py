"""Audit step s03, the first-run journey: sign-in messages, the hand-off file report, preflight marks
and dependent checks, the anonymous probe's trust, the stuck-lock message and the setup undo text.
Every ArcGIS, Windows and file call is a fake."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import ssl
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import taskdash_cli as cli
import taskdash_common as common
import taskdash_handoff as handoff
import taskdash_preflight as pf
import taskdash_publish as publish
import taskdash_setup as setup
import taskdash_signin as signin
from test_h3_handoff import ACCOUNT, Env as HandoffEnv
from test_h3_org_url import ORG, ORG_URL
from test_h3_signin import Fakes, SECRET, _config
from test_phase3_preflight import HOME, make_ctx, task

ROOT = Path(__file__).resolve().parent
POWERSHELL = shutil.which("powershell.exe")
needs_powershell = pytest.mark.skipif(not POWERSHELL, reason="powershell.exe not available")


# --------------------------------------------------------------------------- TD-067, TD-068, TD-074

def test_the_example_config_asks_for_no_mail_password():
    example = json.loads((ROOT / "TaskDash.config.example.json").read_text(encoding="utf-8"))
    assert example["smtp"]["user"] == ""


def test_the_profile_and_the_config_get_the_same_organization_url(tmp_path, monkeypatch, capsys):
    fakes = Fakes(monkeypatch, props=ORG)
    path = _config(tmp_path, url="")
    assert signin.main(["--config", str(path), "--apply"]) == 0
    assert fakes.saved == [("p1", ORG_URL, "owner1", SECRET)]
    assert json.loads(path.read_text(encoding="utf-8"))["arcgis"]["url"] == ORG_URL
    assert fakes.opened[-1] == (ORG_URL, (), {"profile": "p1"})


def test_the_last_line_names_the_hand_off_command(tmp_path, monkeypatch, capsys):
    Fakes(monkeypatch)
    assert signin.main(["--config", str(_config(tmp_path)), "--apply"]) == 0
    out = capsys.readouterr().out
    assert "--for-task-account --task-user" in out and "again as the task account" not in out


# --------------------------------------------------------------------------- TD-006, TD-057, TD-058

def test_a_profile_that_does_not_sign_in_leads_with_the_signin_command(monkeypatch):
    def refuse(*args, **kwargs):
        raise RuntimeError("Invalid credentials")
    monkeypatch.setattr(common, "_open_gis", refuse)
    with pytest.raises(SystemExit) as exc:
        common.connect_agol({"arcgis": {"profile": "p1"}})
    text = str(exc.value)
    assert "taskdash signin --apply" in text and "taskdash signin --for-task-account --apply" in text
    assert "GIS(" not in text and "The stored sign-in does not work" in text


def _timeout():
    import requests
    return requests.exceptions.ReadTimeout("slow")


def _server_error():
    import requests
    return requests.exceptions.HTTPError("503", response=SimpleNamespace(status_code=503))


@pytest.mark.parametrize("error", [_timeout(), _server_error(), ImportError("no arcgis"),
                                   AttributeError("broken package")])
def test_a_failure_that_is_not_a_credential_fault_keeps_its_traceback(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(common, "_open_gis", fail)
    with pytest.raises(type(error)):
        common.connect_agol({"arcgis": {"profile": "p1"}})


def test_a_wrong_password_from_the_environment_becomes_an_exit_without_the_password(monkeypatch):
    def refuse(url, user, password):
        raise RuntimeError(f"bad {password}")
    monkeypatch.setattr(common, "_open_gis", refuse)
    monkeypatch.setenv("TD_U", "owner1")
    monkeypatch.setenv("TD_P", "pw-never-print-77")
    with pytest.raises(SystemExit) as exc:
        common.connect_agol({"arcgis": {"username_env": "TD_U", "password_env": "TD_P"}})
    assert "pw-never-print-77" not in str(exc.value) and "taskdash signin --apply" in str(exc.value)


def test_missing_credentials_name_the_empty_environment_variable(monkeypatch):
    monkeypatch.setenv("TD_U", "owner1")
    monkeypatch.delenv("TD_P", raising=False)
    with pytest.raises(SystemExit) as exc:
        common.connect_agol({"arcgis": {"username_env": "TD_U", "password_env": "TD_P"}})
    text = str(exc.value)
    assert "'TD_P'" in text and "'TD_U'" not in text and "taskdash signin --apply" in text


def test_a_plaintext_password_message_has_no_manual_gis_call():
    with pytest.raises(SystemExit) as exc:
        common.connect_agol({"arcgis": {"username": "u", "password": "s3cret"}})
    text = str(exc.value)
    assert "s3cret" not in text and "GIS(" not in text and "taskdash signin --apply" in text


def test_the_keyring_hint_runs_the_python_that_runs_taskdash():
    assert f'"{sys.executable}" -m keyring set' in common.legacy_mail_warnings({})[0]


def test_an_ssl_failure_points_to_the_certificate_section(monkeypatch):
    import requests

    def refuse(*args, **kwargs):
        raise requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED")
    monkeypatch.setattr("arcgis.gis.GIS", refuse)
    with pytest.raises(SystemExit) as exc:
        common._open_gis("https://x.example.com")
    assert "section The certificate was not trusted (SSLError)" in str(exc.value)


def _run_publish_with(monkeypatch, step_error):
    """Run _run_publish up to the collect step, with the given failure in the sign-in step."""
    messages = []
    monkeypatch.setattr(publish, "emit", messages.append)
    monkeypatch.setattr(publish, "consume_handoff", lambda: None)
    monkeypatch.setattr(publish, "connect_agol", step_error)
    monkeypatch.setattr(publish, "collect_everything", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop")))
    args = argparse.Namespace(apply=True, allow_host_mismatch=False, target="prod", full_history=False,
                              fixture="", force=False)
    with pytest.raises(RuntimeError, match="stop"):
        publish._run_publish(args, {}, Path("."), publish.utc_now(), {"armed": False, "recorded": False})
    return " ".join(messages)


def test_a_sign_in_failure_is_labelled_as_one(monkeypatch):
    def refuse(config):
        raise SystemExit("arcgis.profile 'p1' did not sign in.")
    text = _run_publish_with(monkeypatch, refuse)
    assert "could not sign in to ArcGIS before publishing" in text and "reach AGOL" not in text


def test_a_failure_after_sign_in_is_still_labelled_as_unreachable(monkeypatch):
    monkeypatch.setattr(publish, "ensure_service", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    text = _run_publish_with(monkeypatch, lambda config: object())
    assert "could not reach AGOL before publishing" in text


# --------------------------------------------------------------------------- TD-011

def test_the_anonymous_probe_trusts_the_ca_bundle_that_arcgis_calls_trust(monkeypatch, tmp_path):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    assert publish._anonymous_ssl_context() is None
    loaded = []

    class Context:
        def load_verify_locations(self, **kwargs):
            loaded.append(kwargs)

    monkeypatch.setattr(ssl, "create_default_context", lambda: Context())
    bundle = tmp_path / "proxy.pem"
    bundle.write_text("x", encoding="utf-8")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
    assert isinstance(publish._anonymous_ssl_context(), Context)
    assert loaded == [{"cafile": str(bundle)}]


def test_the_anonymous_probe_passes_that_context_and_names_the_variable_on_a_certificate_error(monkeypatch):
    import urllib.error
    seen = {}

    def refuse(url, timeout=None, context=None):
        seen["context"] = context
        raise urllib.error.URLError(ssl.SSLCertVerificationError("self-signed"))
    monkeypatch.setattr("urllib.request.urlopen", refuse)
    monkeypatch.setattr(publish, "_anonymous_ssl_context", lambda: "ctx")
    answer = publish._anonymous_answer("https://h/svc/0", 5)
    assert seen["context"] == "ctx" and "REQUESTS_CA_BUNDLE" in answer and "inconclusive" in answer


# --------------------------------------------------------------------------- TD-056

def test_a_stuck_lock_exits_with_the_fix_and_the_process_id(tmp_path, monkeypatch):
    import datetime as dt
    with common.run_lock(tmp_path):
        monkeypatch.setattr(publish, "utc_now", lambda: common.utc_now() + dt.timedelta(hours=common.STALE_LOCK_HOURS + 1))
        monkeypatch.setattr(publish, "emit", lambda text: None)
        with pytest.raises(SystemExit) as exc:
            publish._run_under_lock(tmp_path, lambda: 0)
    text = str(exc.value)
    assert f"Stop-Process -Id {os.getpid()}" in text and "taskdash.lock" in text


# --------------------------------------------------------------------------- TD-010

def test_a_waiting_hand_off_file_reports_its_account_and_age(tmp_path, monkeypatch):
    env = HandoffEnv(tmp_path, monkeypatch)
    assert handoff.handoff_waiting() is None and handoff.handoff_waiting_text() == ""
    handoff.write_handoff({"password": "x"}, ACCOUNT)
    state = handoff.handoff_waiting()
    assert state["account"] == ACCOUNT and state["age_hours"] < 1
    assert ACCOUNT in handoff.handoff_waiting_text()
    assert json.loads((env.home / handoff.HANDOFF_INFO_FILE).read_text(encoding="utf-8")) == {"account": ACCOUNT}
    assert b"x" not in (env.home / handoff.HANDOFF_INFO_FILE).read_bytes().replace(ACCOUNT.encode(), b"")


def test_a_file_without_the_info_file_names_no_account(tmp_path, monkeypatch):
    env = HandoffEnv(tmp_path, monkeypatch)
    env.seal_file()
    assert handoff.handoff_waiting()["account"] == ""
    assert "not recorded" in handoff.handoff_waiting_text()


def test_the_run_that_stores_the_sign_in_removes_the_info_file_too(tmp_path, monkeypatch):
    env = HandoffEnv(tmp_path, monkeypatch, whoami="svc-demo")
    handoff.write_handoff({"version": 1, "account": ACCOUNT, "profile": "p1", "url": "https://org.example.com",
                           "username": "owner1", "password": "x", "smtp_user": "", "smtp_password": ""}, ACCOUNT)
    handoff.consume_handoff()
    assert not env.file.exists() and not (env.home / handoff.HANDOFF_INFO_FILE).exists()


def test_status_warns_about_a_waiting_file(tmp_path, monkeypatch, capsys):
    env = HandoffEnv(tmp_path, monkeypatch)
    handoff.write_handoff({"password": "x"}, ACCOUNT)
    cli._handoff_status()
    out = capsys.readouterr().out
    assert "WARNING" in out and ACCOUNT in out and "A hand-off file is waiting" in out
    env.file.unlink()
    cli._handoff_status()
    assert capsys.readouterr().out == ""


def test_preflight_reports_a_file_for_another_account_than_the_collectors(monkeypatch):
    monkeypatch.setattr(pf, "handoff_waiting", lambda: {"account": r"X\one", "age_hours": 3.0})
    monkeypatch.setattr(pf, "handoff_waiting_text", lambda: "the sign-in hand-off file for X\\one has waited 3.0 h.")
    ctx = make_ctx(tasks=[task("TaskDash Collector", "Password", run_as_user=r"X\two")])
    status, message = pf.check_handoff(ctx)
    assert status == pf.WARN and "runs as X\\two" in message and "--task-user X\\two" in message


def test_preflight_skips_when_no_file_waits(monkeypatch):
    monkeypatch.setattr(pf, "handoff_waiting", lambda: None)
    assert pf.check_handoff(make_ctx())[0] == pf.SKIP


CMD = r"C:\Windows\System32\cmd.exe"
CMD_WRAPPER = '/d /c "set "TASKDASH_PYTHON=C:\\Pro\\python.exe"&& "' + HOME + r'\run_taskdash.cmd" --apply"'


@pytest.mark.parametrize("workdir, program, expected, arguments", [
    (HOME, CMD, pf.OK, CMD_WRAPPER),
    (HOME, CMD, pf.WARN, '/d /c "C:\\OtherInstall\\run_taskdash.cmd --apply"'),
    (HOME, HOME + r"\venv\Scripts\taskdash.exe", pf.OK, ""),
    (HOME + "\\", '"' + HOME + r'\venv\Scripts\taskdash.exe"', pf.OK, ""),
    (r"C:\OtherInstall", HOME + r"\venv\Scripts\taskdash.exe", pf.WARN, ""),
    ("", HOME + r"\venv\Scripts\taskdash.exe", pf.WARN, ""),
    (HOME, r"C:\OtherInstall\venv\Scripts\taskdash.exe", pf.WARN, ""),
])
def test_the_collector_task_must_run_this_install(monkeypatch, workdir, program, expected, arguments):
    monkeypatch.setattr(pf, "home_dir", lambda: HOME)
    ctx = make_ctx(tasks=[task("TaskDash Collector", "Password", primary_working_directory=workdir,
                               primary_executable=program, primary_arguments=arguments)])
    status, message = pf.check_collector_install(ctx)
    assert status == expected
    if expected == pf.WARN:
        # -Replace keeps the old action and folder, so the advice must not name it.
        assert message.startswith("WARNING:") and "install_task.ps1 -Apply" in message
        assert "Unregister-ScheduledTask -TaskName 'TaskDash Collector'" in message
        assert "-Replace -Apply" not in message


# --------------------------------------------------------------------------- TD-007, TD-078

def test_a_warning_has_its_own_mark():
    assert pf.MARKS[pf.WARN] == "[w]" and len({pf.MARKS[k] for k in (pf.OK, pf.FAIL, pf.SKIP, pf.WARN)}) == 4


def test_one_missing_sign_in_is_one_problem(tmp_path, monkeypatch, capsys):
    def refuse(config):
        raise SystemExit("arcgis.profile 'p1' did not sign in.")
    monkeypatch.setattr(pf, "connect_agol", refuse)
    monkeypatch.setattr(pf, "CHECKS", tuple(c for c in pf.CHECKS if c[0] in (
        "credentials", "privileges", "service", "view", "sharing", "dashboard", "delete protection")))
    config = tmp_path / "cfg.json"
    config.write_text(json.dumps({"rules": {"profiles": []}, "arcgis": {"item_id": "svc1", "profile": "p1"}}),
                      encoding="utf-8")
    assert pf.main(["--config", str(config)]) == 1
    out = capsys.readouterr().out
    assert out.count("[!!]") == 1 and out.count("needs: credentials") == 6
    assert "Preflight: 1 problem(s), 0 warning(s)." in out


def test_unreadable_tasks_are_one_problem(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pf, "CHECKS", tuple(c for c in pf.CHECKS if c[0] in (
        "task definitions", "collector logon type", "collector account", "collector install", "tested scale")))
    monkeypatch.setattr(pf.collect, "collect_tasks", lambda *a: [])
    config = tmp_path / "cfg.json"
    config.write_text(json.dumps({"rules": {"profiles": []}, "arcgis": {"item_id": "svc1"}}), encoding="utf-8")
    assert pf.main(["--config", str(config)]) == 1
    out = capsys.readouterr().out
    assert out.count("[!!]") == 1 and out.count("needs: task definitions") == 4


# --------------------------------------------------------------------------- TD-072, TD-073

def test_the_created_list_names_the_remove_command_for_a_real_install_and_the_demo(monkeypatch):
    lines = []
    monkeypatch.setattr(setup, "emit", lines.append)
    setup.print_created([("TaskDash", "Feature Service", "abc")], ["abc"])
    setup.print_created([("TaskDash", "Feature Service", "abc")], ["abc"], demo=True)
    assert "taskdash remove --apply" in lines[0] and "by hand" not in lines[0]
    assert "taskdash demo --remove --apply" in lines[2]


def test_the_install_exists_refusal_gives_the_apply_form_and_the_troubleshooting_pointer():
    with pytest.raises(SystemExit) as exc:
        setup.refuse_existing_install({"arcgis": {"item_id": "x"}})
    text = str(exc.value)
    assert "taskdash dashboard --target prod --apply" in text and "python taskdash_" not in text
    assert "section The config already holds an install" in text


def test_a_protect_error_still_ends_in_the_actionable_exit():
    class Item:
        id = "abc"

        def protect(self, enable):
            raise RuntimeError("boom")
    with pytest.raises(SystemExit, match="Could not turn on delete protection for item abc"):
        setup.protect_items([Item()], [])


# --------------------------------------------------------------------------- the PowerShell scripts

def _installer():
    return (ROOT / "Install-TaskDash.ps1").read_text(encoding="utf-8")


def _function(name):
    text = _installer()
    start = text.index(f"function {name}")
    return text[start:text.index("\n}\n", start) + 3]


@needs_powershell
@pytest.mark.parametrize("info, account, expected", [
    ('{"account": "EXAMPLE\\\\svc-demo"}', r"EXAMPLE\svc-demo", "True"),
    ('{"account": "EXAMPLE\\\\svc-demo"}', "svc-demo", "True"),
    ('{"account": "EXAMPLE\\\\svc-old"}', r"EXAMPLE\svc-demo", "False"),
    (None, r"EXAMPLE\svc-demo", "False"),
])
def test_the_installer_replaces_a_hand_off_file_for_another_account(tmp_path, info, account, expected):
    (tmp_path / "taskdash_handoff.bin").write_bytes(b"sealed")
    if info:
        (tmp_path / "taskdash_handoff.json").write_text(info, encoding="utf-8")
    snippet = (_function("Test-HandoffFor") + f"$InstallDir = '{tmp_path}'\n"
               f"[bool](Test-HandoffFor '{account}')\n")
    script = tmp_path / "snippet.ps1"
    script.write_text(snippet, encoding="utf-8")
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(script)],
                            capture_output=True, text=True, timeout=120)
    assert result.stdout.strip() == expected, result.stderr


def test_the_installer_ends_with_the_dashboard_link_and_the_next_steps():
    text = _installer()
    tail = text[text.index("$dashboardId = Get-ConfigValue"):]
    for needle in ("/apps/dashboards/", "Invite members", "alerting.healthcheck_url", "Turn on alert e-mails",
                   "Done. Run"):
        assert needle in tail, needle
    assert text.index("Created by this run") < text.index("$dashboardId = Get-ConfigValue")


def test_the_installer_says_which_account_holds_the_sign_in_and_checks_the_spelling_once():
    text = _installer()
    assert "Sign-in stored for $me; run status and preflight as this account." in text
    assert text.count("is not an account that this computer knows") == 2  # New-LockedFolder and the early check
    assert "did not resolve to a SID" not in text
    assert text.index("$taskSid = (New-Object") < text.index("Get-AdminMembership $TaskAccount (Get-LocalGroupMember")


def test_install_task_runs_the_venv_exe_when_the_folder_has_no_wrapper():
    text = (ROOT / "install_task.ps1").read_text(encoding="utf-8")
    assert '$Python = $venvExe' in text and "-not (Test-Path -LiteralPath (Join-Path $InstallDir \"run_taskdash.cmd\")" in text
    assert "} catch {" in text[text.index("Register-ScheduledTask @register"):text.index("$plain = $null\n    $register.Remove")]
    assert "Run '$taskdashExe status'" in text
