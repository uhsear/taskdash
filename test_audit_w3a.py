"""Audit wave 3, lane A: the locked install folder (TD-004), Python 3.13 as the supported version
(TD-008, TD-075), plain-text mail in preflight (TD-197), the Sigstore signer identity (TD-198), the
ArcGIS role (TD-200) and the private reporting channel (TD-001). Static text checks and fakes only:
nothing here installs, signs in or sends mail."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

import taskdash_preflight as pf

ROOT = Path(__file__).resolve().parent
# What Fulcio put in the certificate of the v1.1.0 release bundles, read from the .sigstore.json files.
SIGNER_IDENTITY = "https://github.com/uhsear/taskdash/.github/workflows/release.yml@refs/tags/"


def _text(*parts):
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def _flat(*parts):
    return " ".join(_text(*parts).split())


# ------------------------------------------------------------------ TD-004

def test_the_manual_guide_runs_elevated_and_gives_the_daily_account_read_and_execute_only():
    guide = _text("docs", "INSTALL.md")
    step = guide[guide.index("## 1. Install TaskDash"):guide.index("## 2. Create the config")]
    assert "as administrator" in step and 'icacls C:\\TaskDash /setowner "*S-1-5-32-544"' in step
    assert "${env:USERNAME}:(OI)(CI)RX" in step and "${env:USERNAME}:(OI)(CI)F" not in step
    assert "normal PowerShell window" not in guide
    upgrade = _text("docs", "UPGRADE.md")
    assert "${env:USERNAME}:(OI)(CI)RX" in upgrade and "${env:USERNAME}:(OI)(CI)F" not in upgrade


def test_the_one_window_installer_gives_the_elevated_account_read_and_execute_only():
    script = _text("Install-TaskDash.ps1")
    assert '@($me, "ReadAndExecute")' in script and '@($me, "FullControl")' not in script


@pytest.mark.parametrize("name", ["SECURITY.md", "docs/APPROVAL.md"])
def test_security_and_approval_say_write_access_is_control_of_the_task_account(name):
    text = _flat(*name.split("/"))
    assert "Write access to this folder equals control of the task account" in text or \
        "Write access to the install folder equals control of the task account" in text
    assert "Read & Execute" in text and "fails while a non-administrator can write" in text


# ------------------------------------------------------------------ TD-008, TD-075

def test_the_installer_has_a_python_version_that_defaults_to_3_13_and_names_it_in_every_message():
    script = _text("Install-TaskDash.ps1")
    assert "[ValidatePattern('^3\\.\\d{1,2}$')][string]$PythonVersion = \"3.13\"" in script
    assert '& $launcher "-$PythonVersion" -I -m venv $venvDir' in script
    assert "-3.11" not in script


def test_the_lock_and_the_sbom_are_built_on_python_3_13_on_windows():
    workflow = _text(".github", "workflows", "release.yml")
    build = workflow[workflow.index("\n  build:"):workflow.index("\n  sign:")]
    assert 'python-version: "3.13"' in build and 'python-version: "3.11"' not in build
    assert "--generate-hashes" in build and "--extra smtp" in build and "runs-on: windows-latest" in build
    assert "Python 3.13 on Windows" in build


def test_pywin32_is_a_windows_dependency_that_the_lock_includes():
    project = tomllib.loads(_text("pyproject.toml"))["project"]
    assert "pywin32>=223; sys_platform == 'win32'" in project["dependencies"]
    assert project["requires-python"] == ">=3.11"


@pytest.mark.parametrize("name", ["README.md", "docs/INSTALL.md", "docs/INTRODUCTION.md"])
def test_every_guide_names_python_3_13_as_the_supported_version(name):
    text = _flat(*name.split("/"))
    assert "Python 3.13" in text and "not supported" in text
    assert "py -3.11" not in text


# ------------------------------------------------------------------ TD-197

class _Relay:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass


def _ctx(smtp):
    config = {"alerting": {"enabled": True, "to": ["ops@agency.test"]}, "rules": {"profiles": []},
              "smtp": {"host": "relay.agency.test", "from": "td@agency.test", **smtp}}
    return pf.Context(SimpleNamespace(config="", offline=False), config=config)


@pytest.fixture
def keyring_with_a_password(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", SimpleNamespace(get_password=lambda service, user: "pw-never-printed"))
    monkeypatch.setattr(pf.smtplib, "SMTP", _Relay)
    monkeypatch.setattr(pf.smtplib, "SMTP_SSL", _Relay)


@pytest.mark.parametrize("block", [
    {"user": "svc", "starttls": False, "ssl": False, "allow_plaintext_auth": True},
])
def test_preflight_fails_when_the_mail_password_would_cross_the_network_in_plain_text(keyring_with_a_password, block):
    status, message = pf.check_smtp(_ctx(block))
    assert status == pf.FAIL and "plain text" in message and "pw-never-printed" not in message


def test_preflight_fails_a_plain_login_even_when_the_send_would_be_refused(keyring_with_a_password):
    status, message = pf.check_smtp(_ctx({"user": "svc", "starttls": False, "ssl": False}))
    assert status == pf.FAIL


def test_preflight_warns_for_plain_mail_without_a_login_and_passes_an_encrypted_login(keyring_with_a_password):
    status, message = pf.check_smtp(_ctx({"starttls": False, "ssl": False}))
    assert status == pf.WARN and "plain text" in message and "STARTTLS" in message
    assert pf.check_smtp(_ctx({"user": "svc"}))[0] == pf.OK
    assert pf.check_smtp(_ctx({"user": "svc", "starttls": False, "ssl": True}))[0] == pf.OK


# ------------------------------------------------------------------ TD-198

def test_the_signer_identity_is_pinned_in_every_signing_doc_and_names_the_real_workflow():
    for parts in (("docs", "INSTALL.md"), ("docs", "APPROVAL.md"), ("SECURITY.md",)):
        text = _flat(*parts)
        assert SIGNER_IDENTITY in text, parts
        assert "python -m sigstore verify github" in text, parts
        assert "--cert-identity" in text and ".sigstore.json" in text, parts
    assert SIGNER_IDENTITY + "v1.1.0" in _flat("docs", "INSTALL.md")
    assert (ROOT / ".github" / "workflows" / "release.yml").is_file()
    # The identity names the private build repository, not the public release repository.
    assert "taskdash-releases" not in SIGNER_IDENTITY
    homepage = tomllib.loads(_text("pyproject.toml"))["project"]["urls"]["Homepage"]
    assert homepage.endswith("/taskdash-releases")


# ------------------------------------------------------------------ TD-200

def test_the_guides_list_every_privilege_the_preflight_checks_and_recommend_a_dedicated_account():
    keys = [key for key, _ in pf.REQUIRED_PRIVILEGES]
    assert "portal:publisher:publishFeatures" in keys, "setup publishes a hosted feature layer and a view"
    for parts in (("docs", "APPROVAL.md"), ("docs", "INSTALL.md")):
        text = _flat(*parts)
        for key in keys:
            assert key in text, (parts, key)
        assert "dedicated" in text and "custom role" in text, parts
    assert "at least once a year" in _flat("docs", "APPROVAL.md") and "(ROTATE.md)" in _flat("docs", "APPROVAL.md")


# ------------------------------------------------------------------ TD-001

def test_security_policy_says_private_reporting_is_on_for_the_release_repository():
    text = _flat("SECURITY.md")
    assert "Private vulnerability reporting is on for the release repository" in text
    assert "https://github.com/uhsear/taskdash-releases" in text


# ------------------------------------------------------------------ TD-098, TD-099

def test_replace_names_both_accounts_when_the_prefix_differs_and_the_template_version_is_unchanged():
    script = _text("install_task.ps1")
    note = script.index("NOTE: the task runs as '$existingUser'")
    assert script.index("if (-not $sameAccount)") < note < script.index("CHECK MODE - nothing registered")
    assert "Register-ScheduledTask @register" in script
    assert re.search(r'<Task version="1\.2" ', _text("taskdash_task.xml"))


def test_the_rename_guide_uses_replace_not_a_hand_made_xml_registration():
    guide = _flat("docs", "CONFIGURE.md")
    assert "install_task.ps1 -Replace" in guide and "not `Register-ScheduledTask -Xml`" in guide
    for name in ("INSTALL.md", "UPGRADE.md", "ROTATE.md", "CONFIGURE.md", "TROUBLESHOOTING.md"):
        for line in _text("docs", name).splitlines():
            if "Register-ScheduledTask" in line:
                assert "not `Register-ScheduledTask" in line, (name, line)


# ------------------------------------------------------------------ TD-069

def test_the_guides_say_run_taskdash_cmd_is_kept_as_the_folder_install_wrapper():
    assert "keeps `run_taskdash.cmd` on purpose, as the wrapper of a folder install" in _flat("docs", "INSTALL.md")
    assert "wrapper of a folder install and stays in the product" in _flat("docs", "TROUBLESHOOTING.md")
    assert "keeps as the wrapper of that install" in _flat("install_task.ps1")
    assert (ROOT / "run_taskdash.cmd").is_file()


# ------------------------------------------------------------------ TD-022

INSTALLER = ROOT / "Install-TaskDash.ps1"
POWERSHELL = shutil.which("powershell.exe")
needs_powershell = pytest.mark.skipif(not POWERSHELL, reason="powershell.exe not available")


def _installer(install_dir, *args):
    account = os.environ.get("USERDOMAIN", "HOST") + chr(92) + os.environ.get("USERNAME", "user")
    return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(INSTALLER), "-InstallDir",
                           str(install_dir), "-TaskAccount", account, *args],
                          capture_output=True, text=True, timeout=120)


@needs_powershell
def test_upgrade_with_nothing_installed_stops_and_changes_nothing(tmp_path):
    # Directly under the drive, so the parent-folder check passes and the missing install is what stops it.
    missing = Path(os.environ.get("SystemDrive", "C:") + chr(92) + "TaskDashW3aNeverInstalled")
    assert not missing.exists()
    result = _installer(missing, "-Upgrade", "-Wheel", str(tmp_path / "taskdash-9.9.9-py3-none-any.whl"))
    assert result.returncode == 1 and "nothing to upgrade" in result.stderr
    assert not missing.exists()


@needs_powershell
def test_rebuild_dashboard_needs_upgrade(tmp_path):
    result = _installer(tmp_path / "TaskDash", "-RebuildDashboard")
    assert result.returncode == 1 and "-RebuildDashboard works only with -Upgrade" in result.stderr


def _function(name):
    text = INSTALLER.read_text(encoding="utf-8")
    start = text.index(f"function {name}")
    return text[start:text.index("\n}\n", start) + 3]


def test_upgrade_runs_the_guide_steps_in_order_and_changes_nothing_before_the_apply_gate():
    body = _function("Step-Upgrade")
    gate = body.index("if (-not $Apply)")
    for check in ("Confirm-WheelHash $wheelPath", "Confirm-LockHash $lockPath", "Assert-TrustedInstallDir",
                  "Test-CollectorTask", "$venvPythonVersion -ne $PythonVersion"):
        assert body.index(check) < gate, check
    order = ["Disable-ScheduledTask", "Wait-CollectorIdle", "New-LockedFolder $backup -BackupOnly", "robocopy.exe",
             "Install-Venv $wheelPath", '"collect", "--migrate", "--apply"', '@("preflight")', "Enable-ScheduledTask -TaskName $collectorTask | Out-Null",
             "Invoke-CollectorOnce", '"dashboard", "--target", "prod", "--apply"', '@("status")']
    positions = [body.index(step, gate) for step in order]
    assert positions == sorted(positions)
    assert body.index("$script:rollbackHint = ") < body.index("Disable-ScheduledTask")
    # Nothing in the folder runs before the folder is trusted.
    assert body.index("Assert-TrustedInstallDir") < body.index("Get-InstalledVersion")


def test_the_dashboard_is_rebuilt_only_with_the_switch_and_a_backup_folder_opens_to_administrators_only():
    body = _function("Step-Upgrade")
    rebuild = body.index('"dashboard", "--target", "prod"')
    assert body.rindex("if ($RebuildDashboard)", 0, rebuild) > body.index("Invoke-CollectorOnce")
    assert "$grants = $grants[0..1]" in _function("New-LockedFolder")
    assert "$script:rollbackHint" in _function("Stop-Install")


def test_the_upgrade_guide_documents_the_installer_mode():
    guide = _flat("docs", "UPGRADE.md")
    for needle in ("-Upgrade", "-RebuildDashboard", "collect --migrate --apply", "run the check first", "rebuild the dashboard after the first run"):
        assert needle in guide, needle
