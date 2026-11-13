"""H3-8: Install-TaskDash.ps1, the one-window installer. Static text checks and a parse-only syntax
check. Two pure helper functions (the wheel version and the expected hash) run in a fresh PowerShell
against files in tmp_path. The script itself never runs, so nothing here installs, signs in or
registers anything."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "Install-TaskDash.ps1"
POWERSHELL = shutil.which("powershell.exe")
needs_powershell = pytest.mark.skipif(not POWERSHELL, reason="powershell.exe not available")

STEPS = ["Step-Venv", "Step-Config", "Step-Signin", "Step-Preflight", "Step-Setup", "Step-Task", "Step-Status"]


def _text():
    return SCRIPT.read_text(encoding="utf-8")


def _function(name):
    """The source of one top-level function, from its header to the closing brace at column 0."""
    text = _text()
    start = text.index(f"function {name}")
    return text[start:text.index("\n}\n", start) + 3]


def _ps(tmp_path, body):
    script = tmp_path / "snippet.ps1"
    script.write_text(body, encoding="utf-8")
    # Under a pwsh 7 parent (the GitHub runner's default shell) PSModulePath lists pwsh's modules,
    # and Windows PowerShell then cannot load Get-Acl or Get-FileHash. Without it, 5.1 uses its own.
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(script)],
                          capture_output=True, text=True, timeout=120, env=env)


@needs_powershell
def test_script_parses_without_errors():
    # Parse only: ParseFile never runs the script.
    command = ("$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
               f"'{SCRIPT}', [ref]$null, [ref]$e); if ($e) {{ $e | ForEach-Object {{ $_.Message }}; exit 1 }}")
    result = subprocess.run([POWERSHELL, "-NoProfile", "-Command", command],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_parameters_and_apply_switch():
    text = _text()
    for needle in ("[string]$InstallDir", "[string]$Wheel", "[string]$TaskAccount", "[switch]$Apply"):
        assert needle in text, needle
    assert "[Parameter(Mandatory = $true)][string]$InstallDir" in text
    assert "[Parameter(Mandatory = $true)][string]$TaskAccount" in text


def test_every_step_runs_once_in_the_order_of_the_roadmap():
    calls = re.findall(r"^(Step-\w+)$", _text(), re.M)
    assert calls == STEPS
    # The elevation check comes before the first step, and the final summary after the last.
    text = _text()
    assert text.index("IsInRole") < text.index("\nStep-Venv\n")
    assert text.index("\nStep-Status\n") < text.index("Created by this run")


def test_each_step_runs_the_command_the_roadmap_names():
    expected = {
        "Step-Venv": ["Confirm-WheelHash", "Install-Venv"],
        "Install-Venv": ["-m venv", "-m pip install", "New-LockedFolder"],
        "Confirm-WheelHash": ["Get-FileHash", ".sha256"],
        "Step-Config": ["TaskDash.config.example.json", "$configPath", "Copy-Item"],
        "Step-Signin": ['"signin", "--apply"', '"--for-task-account"', '"--task-user"'],
        "Step-Preflight": ['@("preflight")'],
        "Step-Setup": ['@("setup")', '@("setup", "--apply")'],
        "Step-Task": ["Invoke-InstallTask $true"],
        "Invoke-InstallTask": ["-EnableEventLog -StartNow -Apply"],
        "Step-Status": ['@("status")'],
    }
    for name, needles in expected.items():
        body = _function(name)
        for needle in needles:
            assert needle in body, (name, needle)
    # check, then --apply, in that order.
    setup = _function("Step-Setup")
    assert setup.index('@("setup")') < setup.index('@("setup", "--apply")')


def test_a_failed_command_stops_the_chain_and_lists_what_was_created():
    text = _text()
    run = _function("Invoke-Taskdash")
    assert "$LASTEXITCODE -ne 0" in run and "Stop-Install" in run
    stop = _function("Stop-Install")
    assert "Created so far" in stop and "exit 1" in stop and "$script:created" in stop
    # Every native command and the nested installer have their exit code read.
    assert text.count("$LASTEXITCODE -ne 0") >= 4
    task = _function("Invoke-InstallTask")
    assert task.index("-EnableEventLog -StartNow -Apply") < task.index("Stop-Install")
    assert "catch" in task, "a terminating error in install_task.ps1 must still print the created list"
    # Nothing keeps going after a failure: no -ErrorAction SilentlyContinue on a writing command.
    assert "$ErrorActionPreference = \"Stop\"" in text


@pytest.mark.parametrize("name, needles", [
    ("Step-Venv", ["Install-Venv $wheelPath"]),
    ("Step-Config", ["Copy-Item"]),
    ("Step-Signin", ['"signin", "--apply"', '"--for-task-account"']),
    ("Step-Preflight", ['"--enable-event-log"', '@("preflight")']),
    ("Step-Setup", ['@("setup")', '"--apply"']),
    ("Step-Task", ["Invoke-InstallTask $true"]),
    ("Step-Status", ['@("status")']),
])
def test_nothing_acts_before_the_apply_gate(name, needles):
    body = _function(name)
    gate = body.index("-not $Apply")
    for needle in needles:
        assert body.index(needle) > gate, (name, needle)


def test_check_mode_ends_without_acting_and_names_the_apply_switch():
    text = _text()
    assert "CHECK MODE - nothing changed. Re-run with -Apply." in text
    assert "Set-ScheduledTask" not in text


def test_a_second_run_reports_nothing_to_do_for_a_finished_step():
    # Status only reads, so it has no finished state to skip. Preflight skips the event-log fix.
    for name in set(STEPS) - {"Step-Status"}:
        assert "nothing to do" in _function(name), name
    assert "Nothing to do: every step was already done." in _text()


def test_the_event_log_fix_runs_only_when_the_log_is_not_ready():
    # A second -Apply run must not re-apply the fix: it reads the log state first, as preflight does.
    ready = _function("Test-EventLogReady")
    assert "wevtutil.exe" in ready and "Microsoft-Windows-TaskScheduler/Operational" in ready
    assert "64MB" in ready and "enabled" in ready
    body = _function("Step-Preflight")
    assert "Test-EventLogReady" in body
    assert body.index("Test-EventLogReady") < body.index('"--enable-event-log"')
    assert "nothing to do" in body


def test_the_script_never_reads_prints_or_passes_a_password():
    text = _text()
    code = "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))
    for needle in ("Read-Host", "SecureString", "GetNetworkCredential", "-AsPlainText", "-Credential",
                   "$password", "$plain", "-Password"):
        assert needle not in code, needle


def test_the_wheel_is_checked_before_it_is_installed_and_a_different_version_is_not_replaced():
    body = _function("Step-Venv")
    assert body.index("Confirm-WheelHash") < body.index("Install-Venv $wheelPath")
    assert "docs/UPGRADE.md" in body


def test_the_script_ships_in_the_wheel_data_files_and_the_deny_gate_scans_it():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "Install-TaskDash.ps1" in data["tool"]["setuptools"]["data-files"]["share/taskdash"]
    assert "Install-TaskDash.ps1" in (ROOT / "test_phase2_deny_gate.py").read_text(encoding="utf-8")


def test_install_guide_opens_with_a_two_command_quick_install():
    text = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    quick = text.index("## Quick install")
    assert quick < text.index("## Requirements") < text.index("## 1. Install TaskDash")
    section = text[quick:text.index("\n## ", quick + 5)]
    # Two commands: the check comes first, and only the second carries -Apply.
    commands = [line for line in section.splitlines() if line.startswith("powershell.exe")]
    assert len(commands) == 2
    assert "-Apply" not in commands[0] and commands[1].endswith(" -Apply")
    for command in commands:
        assert "-Wheel" in command and "-TaskAccount" in command and "-InstallDir" in command
    # The script and SHA256SUMS.txt come from the release page, so the section must say so.
    flat = " ".join(section.split())
    assert "Install-TaskDash.ps1" in flat and "SHA256SUMS.txt" in flat and "release page" in flat
    assert "`Install-TaskDash.ps1`, the wheel, `requirements.lock`, and `SHA256SUMS.txt`" in flat


def test_nothing_in_an_existing_folder_runs_before_it_is_trusted():
    # Any user can create C:\TaskDash and plant venv\Scripts\taskdash.exe or a config whose arcgis.url
    # collects the password; the elevated window must check the folder before it runs or reads them.
    step = _function("Step-Venv")
    assert step.index("Assert-TrustedInstallDir") < step.index("Get-InstalledVersion")
    assert "Test-LooseRules" in _function("Test-LooseAcl")


def test_a_new_folder_is_created_locked_and_owned_by_administrators():
    body = _function("New-LockedFolder")
    assert "SetAccessRuleProtection($true, $false)" in body
    assert 'SetOwner((& $sid "S-1-5-32-544"))' in body
    assert body.index("SetOwner") < body.index("CreateDirectory($Path, $security)")
    # A folder another user made first comes back unchanged, so the result is checked after the call.
    assert body.index("FileSystemAclExtensions") < body.index("Test-UntrustedOwner $Path")
    assert "Get-ChildItem -LiteralPath $Path -Force" in body
    install = _function("Install-Venv")
    assert "New-LockedFolder $InstallDir" in install and "icacls" not in install


def test_the_installed_wheel_is_the_locked_copy_that_was_hashed():
    body = _function("Install-Venv")
    copy = body.index("Copy-Item -LiteralPath $WheelPath -Destination $wheelCopy")
    assert body.index("New-LockedFolder") < copy < body.index("Confirm-WheelHash $WheelPath $wheelCopy")
    assert body.index("Confirm-WheelHash $WheelPath $wheelCopy") < body.index("pip install --no-deps $wheelCopy")
    assert "$WheelPath}" not in body and "install $WheelPath" not in body


def test_the_locked_packages_install_first_from_the_hashed_copy_of_the_lock():
    body = _function("Install-Venv")
    assert body.index("Confirm-LockHash $LockPath $lockCopy") < body.index("pip install --require-hashes -r $lockCopy")
    assert body.index("pip install --require-hashes -r $lockCopy") < body.index("pip install --no-deps $wheelCopy")
    # Step-Venv stops on a missing or wrong lock in check mode too, before the Apply gate.
    step = _function("Step-Venv")
    assert step.index("Confirm-LockHash $lockPath") < step.index("if (-not $Apply)") < step.index("Install-Venv $wheelPath")


@needs_powershell
def test_the_wheel_and_the_lock_need_sha256sums_and_a_matching_value(tmp_path):
    wheel = tmp_path / "taskdash-1.3.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    lock = tmp_path / "requirements.lock"
    lock.write_bytes(b"lock")
    sums = tmp_path / "SHA256SUMS.txt"
    head = ("function Report([string]$Step, [string]$Text) { Write-Host \"$Step $Text\" }\n"
            "function Stop-Install([string]$Message) { Write-Host \"STOP: $Message\"; exit 3 }\n" +
            _function("Get-ExpectedHash") + _function("Get-ListedHash") + _function("Confirm-WheelHash") +
            _function("Confirm-LockHash"))
    # No sums file: both stop instead of skipping the check.
    result = _ps(tmp_path, head + f"Confirm-WheelHash '{wheel}'\n'passed'\n")
    assert result.returncode == 3 and "STOP: There is no" in result.stdout and "passed" not in result.stdout
    result = _ps(tmp_path, head + f"Confirm-LockHash '{lock}'\n'passed'\n")
    assert result.returncode == 3 and "STOP: There is no SHA256SUMS.txt" in result.stdout
    # A sums file that lists the wheel but not the lock stops the lock only.
    sums.write_text(hashlib.sha256(b"wheel").hexdigest() + "  " + wheel.name + "\n", encoding="utf-8")
    result = _ps(tmp_path, head + f"Confirm-WheelHash '{wheel}'\nConfirm-LockHash '{lock}'\n'passed'\n")
    assert result.returncode == 3 and "wheel SHA-256 matches" in result.stdout
    assert "does not list 'requirements.lock'" in result.stdout
    # Listed and equal passes; a swapped copy stops.
    sums.write_text(sums.read_text(encoding="utf-8") + hashlib.sha256(b"lock").hexdigest() + "  requirements.lock\n",
                    encoding="utf-8")
    result = _ps(tmp_path, head + f"Confirm-LockHash '{lock}'\n'passed'\n")
    assert result.returncode == 0 and "lock SHA-256 matches" in result.stdout, result.stdout + result.stderr
    swapped = tmp_path / "copy.lock"
    swapped.write_bytes(b"changed after the check")
    result = _ps(tmp_path, head + f"Confirm-LockHash '{lock}' '{swapped}'\n'passed'\n")
    assert result.returncode == 3 and "STOP: The SHA-256 of" in result.stdout


@needs_powershell
def test_the_hash_is_taken_from_the_copy_not_the_download(tmp_path):
    wheel = tmp_path / "taskdash-1.3.0-py3-none-any.whl"
    wheel.write_bytes(b"synthetic wheel")
    (tmp_path / (wheel.name + ".sha256")).write_text(hashlib.sha256(b"synthetic wheel").hexdigest(), encoding="utf-8")
    copy = tmp_path / "copy.whl"
    copy.write_bytes(b"swapped after the check")
    snippet = ("function Report([string]$Step, [string]$Text) { Write-Host $Text }\n"
               "function Stop-Install([string]$Message) { Write-Host \"STOP: $Message\"; exit 3 }\n" +
               _function("Get-ExpectedHash") + _function("Get-ListedHash") + _function("Confirm-WheelHash") +
               f"Confirm-WheelHash '{wheel}'\nConfirm-WheelHash '{wheel}' '{copy}'\n'passed'\n")
    result = _ps(tmp_path, snippet)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "SHA-256 matches" in result.stdout and "STOP: The SHA-256 of" in result.stdout


def _untrusted_dir_snippet(home, loose, bad_owner):
    return (_function("Assert-TrustedInstallDir") + _function("Test-ReparsePoint") +
            f"function Test-LooseAcl([string]$Path) {{ ${loose} }}\n"
            f"function Test-UntrustedOwner([string]$Path) {{ ${bad_owner} }}\n"
            "function Stop-Install([string]$Message) { Write-Host \"STOP: $Message\"; exit 3 }\n"
            f"$InstallDir = '{home}'\n"
            "Assert-TrustedInstallDir\n'passed'\n")


@needs_powershell
@pytest.mark.parametrize("loose, bad_owner, files, stops", [
    ("true", "false", ["venv"], True),                        # writable by ordinary users, holds a venv
    ("false", "true", ["run_taskdash.cmd"], True),            # owned by an ordinary user
    ("true", "false", ["TaskDash.config.json"], True),        # a config there may send the password elsewhere
    ("true", "true", [], True),                               # empty, but the owner can still plant files
    ("false", "false", ["venv", "TaskDash.config.json"], False),  # trusted install: nothing to do
])
def test_an_untrusted_install_folder_stops_whatever_it_holds(tmp_path, loose, bad_owner, files, stops):
    home = tmp_path / "home"
    home.mkdir()
    for name in files:
        (home / name).mkdir() if "." not in name else (home / name).write_text("{}", encoding="utf-8")
    result = _ps(tmp_path, _untrusted_dir_snippet(home, loose, bad_owner))
    if stops:
        assert result.returncode == 3 and "STOP:" in result.stdout and "Move the folder away" in result.stdout
    else:
        assert result.returncode == 0 and "passed" in result.stdout, result.stdout + result.stderr


@needs_powershell
def test_a_missing_folder_passes_the_trust_check(tmp_path):
    result = _ps(tmp_path, _untrusted_dir_snippet(tmp_path / "missing", "true", "true"))
    assert result.returncode == 0 and "passed" in result.stdout, result.stdout + result.stderr


@needs_powershell
def test_a_junction_at_the_install_path_stops_even_when_its_target_is_trusted(tmp_path):
    # Get-Acl follows the junction, so only the link check sees that its maker can repoint it.
    target = tmp_path / "trusted"
    target.mkdir()
    home = tmp_path / "home"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(home), str(target)], check=True, capture_output=True)
    result = _ps(tmp_path, _untrusted_dir_snippet(home, "false", "false"))
    assert result.returncode == 3 and "STOP:" in result.stdout, result.stdout + result.stderr
    target.rmdir()  # a junction whose target is gone still stops
    result = _ps(tmp_path, _untrusted_dir_snippet(home, "false", "false"))
    assert result.returncode == 3 and "STOP:" in result.stdout, result.stdout + result.stderr


def _parents_snippet(install_dir, loose_path):
    return (_function("Assert-TrustedParents") + _function("Test-ReparsePoint") + _function("Test-LooseRoot") +
            _function("Test-LooseRules") + _function("Get-TrustedSids") +
            f"function Test-LooseAcl([string]$Path) {{ $Path -eq '{loose_path}' }}\n"
            "function Test-UntrustedOwner([string]$Path) { $false }\n"
            "function Stop-Install([string]$Message) { Write-Host \"STOP: $Message\"; exit 3 }\n"
            f"$InstallDir = '{install_dir}'\n"
            "Assert-TrustedParents\n'passed'\n")


@needs_powershell
def test_a_parent_an_ordinary_user_can_change_stops_but_a_default_drive_root_does_not(tmp_path):
    install_dir = tmp_path / "apps" / "TaskDash"
    result = _ps(tmp_path, _parents_snippet(install_dir, tmp_path / "apps"))
    assert result.returncode == 3 and "a parent folder of -InstallDir" in result.stdout, result.stdout + result.stderr
    root = Path(install_dir.anchor)
    result = _ps(tmp_path, _parents_snippet(install_dir, str(root)))
    assert result.returncode == 0 and "passed" in result.stdout, result.stdout + result.stderr


def test_the_folder_is_checked_again_after_the_create_and_parents_before_anything_runs():
    install = _function("Install-Venv")
    assert install.index("New-LockedFolder $InstallDir") < install.index("Assert-TrustedInstallDir") < install.index("Copy-Item")
    step = _function("Step-Venv")
    assert step.index("Assert-TrustedParents") < step.index("Assert-TrustedInstallDir") < step.index("Get-InstalledVersion")
    assert "Test-ReparsePoint $Path" in _function("New-LockedFolder")


def test_every_python_call_is_isolated_from_the_current_folder():
    script = SCRIPT.read_text(encoding="utf-8")
    calls = re.findall(r"& \$(?:launcher|venvPython) [^\n]*", script)
    assert len(calls) == 5
    for call in calls:
        assert " -I " in call, call


@needs_powershell
@pytest.mark.parametrize("rights, loose", [
    ("AppendData", False),                    # every user may add a folder to C:\
    ("Modify", False),                        # Modify cannot delete a locked child
    ("DeleteSubdirectoriesAndFiles", True),   # but this deletes or renames any child
    ("FullControl", True),
    ("ChangePermissions", True),
])
def test_the_drive_root_is_loose_only_when_users_can_remove_or_reclaim_its_folders(tmp_path, rights, loose):
    snippet = (_function("Test-LooseRules") + _function("Get-TrustedSids") +
               "$sid = New-Object System.Security.Principal.SecurityIdentifier 'S-1-5-11'\n"
               f"$rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, '{rights}', 'None', 'None', 'Allow')\n"
               "$root = [System.Security.AccessControl.FileSystemRights]'DeleteSubdirectoriesAndFiles, ChangePermissions, TakeOwnership'\n"
               "if (Test-LooseRules @($rule) $root 0x10000000) { 'loose' } else { 'tight' }\n")
    result = _ps(tmp_path, snippet)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("loose" if loose else "tight")
    assert "Test-LooseRules (Get-Acl -LiteralPath $Path).Access $rights 0x10000000" in _function("Test-LooseRoot")


@needs_powershell
def test_a_folder_this_user_created_has_a_trusted_owner(tmp_path):
    snippet = (_function("Test-UntrustedOwner") + _function("Get-TrustedSids") +
               f"if (Test-UntrustedOwner '{tmp_path}') {{ 'untrusted' }} else {{ 'trusted' }}\n"
               f"if (Test-UntrustedOwner '{tmp_path / 'missing'}') {{ 'untrusted' }} else {{ 'trusted' }}\n")
    result = _ps(tmp_path, snippet)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["trusted", "trusted"]


def test_the_wheel_hash_also_reads_sha256sums_beside_the_wheel_and_refuses_an_unlisted_wheel():
    body = _function("Confirm-WheelHash")
    assert "SHA256SUMS.txt" in body and "Get-ListedHash" in body
    assert body.index(".sha256") < body.index("SHA256SUMS.txt")
    assert "does not list" in body and "Stop-Install" in body


@needs_powershell
@pytest.mark.parametrize("rights, kind, sid, propagation, loose", [
    ("Modify", "Allow", "S-1-5-32-545", "None", True),            # Users
    ("Write", "Allow", "S-1-5-11", "None", True),                 # Authenticated Users
    ("FullControl", "Allow", "S-1-1-0", "None", True),            # Everyone
    ("Modify", "Allow", "S-1-5-4", "None", True),                 # INTERACTIVE
    ("Write", "Allow", "S-1-5-21-1-2-3-513", "None", True),       # a domain's Domain Users
    ("Modify", "Allow", "S-1-5-21-1-2-3-1001", "None", True),     # one named user
    ("FullControl", "Allow", "S-1-3-0", "InheritOnly", False),    # CREATOR OWNER
    ("FullControl", "Allow", "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464", "None", False),
    ("ReadAndExecute", "Allow", "S-1-5-32-545", "None", False),   # reading is allowed
    ("FullControl", "Deny", "S-1-5-32-545", "None", False),       # a deny grants nothing
    ("FullControl", "Allow", "S-1-5-32-544", "None", False),      # Administrators
    ("Modify", "Allow", "S-1-5-18", "None", False),               # SYSTEM
    # icacls (OI)(CI)(IO)(GW) and (GA) store the generic bits, which no specific right below covers.
    ("1073741824", "Allow", "S-1-5-32-545", "InheritOnly", True),   # GENERIC_WRITE for Users
    ("268435456", "Allow", "S-1-5-32-545", "InheritOnly", True),    # GENERIC_ALL for Users
    ("536870912", "Allow", "S-1-5-32-545", "InheritOnly", False),   # GENERIC_EXECUTE alone is no write right
])
def test_loose_rules_flag_a_write_right_for_any_untrusted_account(
        tmp_path, rights, kind, sid, propagation, loose):
    # In-memory rules only: nothing reads or changes a real folder's permissions.
    if rights.isdigit():
        # Get-Acl returns generic bits as the raw mask, but the FileSystemAccessRule constructor refuses
        # them, so a stand-in object carries the three properties the function reads.
        make_rule = ("$rule = [pscustomobject]@{ AccessControlType = '" + kind + "'; IdentityReference = $sid; "
                     "FileSystemRights = [System.Enum]::ToObject([System.Security.AccessControl.FileSystemRights], "
                     f"[int]{rights}) }}\n")
    else:
        make_rule = (f"$rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, '{rights}', "
                     f"'ContainerInherit,ObjectInherit', '{propagation}', '{kind}')\n")
    snippet = (_function("Test-LooseRules") + _function("Get-TrustedSids") +
               f"$sid = New-Object System.Security.Principal.SecurityIdentifier '{sid}'\n" + make_rule +
               "if (Test-LooseRules @($rule)) { 'loose' } else { 'tight' }\n")
    result = _ps(tmp_path, snippet)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("loose" if loose else "tight")


@needs_powershell
@pytest.mark.parametrize("lines, expected", [
    (["{h}  taskdash-1.3.0-py3-none-any.whl", "{o}  requirements.lock"], "{h}"),
    (["{o}  requirements.lock", "{h} *taskdash-1.3.0-py3-none-any.whl"], "{h}"),
    (["{o}  requirements.lock", "{o}  taskdash-1.2.0-py3-none-any.whl"], ""),
    ([], ""),
])
def test_listed_hash_finds_the_wheel_line_in_sha256sums(tmp_path, lines, expected):
    wheel, other = hashlib.sha256(b"wheel").hexdigest(), hashlib.sha256(b"other").hexdigest()
    sums = tmp_path / "SHA256SUMS.txt"
    sums.write_text("".join(line.format(h=wheel, o=other) + "\n" for line in lines), encoding="utf-8")
    result = _ps(tmp_path, _function("Get-ListedHash") +
                 f"$v = Get-ListedHash '{sums}' 'taskdash-1.3.0-py3-none-any.whl'\nif ($v) {{ $v }} else {{ '' }}\n")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected.format(h=wheel)


@needs_powershell
@pytest.mark.parametrize("name, expected", [
    ("taskdash-1.3.0-py3-none-any.whl", "1.3.0"),
    ("taskdash-1.3.0rc1-py3-none-any.whl", "1.3.0rc1"),
    ("taskdash-1.3.0-py3-none-any (1).whl", ""),
    ("other-1.3.0-py3-none-any.whl", ""),
])
def test_wheel_version_comes_from_the_file_name(tmp_path, name, expected):
    result = _ps(tmp_path, _function("Get-WheelVersion") +
                 f"$v = Get-WheelVersion 'C:\\x\\{name}'\nif ($v) {{ $v }} else {{ '' }}\n")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@needs_powershell
@pytest.mark.parametrize("line, valid", [
    ("{h}\n", True),
    ("{h}  taskdash-1.3.0-py3-none-any.whl\n", True),
    ("{H}\n", True),
    ("SHA256 hash of taskdash\n", False),
    ("abc123\n", False),
    ("", False),
])
def test_expected_hash_reads_the_first_64_hex_characters(tmp_path, line, valid):
    digest = hashlib.sha256(b"synthetic wheel").hexdigest()
    sums = tmp_path / "x.whl.sha256"
    sums.write_text(line.format(h=digest, H=digest.upper()), encoding="utf-8")
    result = _ps(tmp_path, _function("Get-ExpectedHash") + f"Get-ExpectedHash '{sums}'\n")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (digest if valid else "")


def test_admin_membership_rule_is_the_same_as_install_task():
    other = (ROOT / "install_task.ps1").read_text(encoding="utf-8")
    start = other.index("function Get-AdminMembership")
    assert _function("Get-AdminMembership") == other[start:other.index("\n}\n", start) + 3]


def test_task_account_is_checked_before_any_step_creates_something():
    text = _text()
    check = text.index("Get-AdminMembership $TaskAccount (Get-LocalGroupMember -SID S-1-5-32-544")
    assert check < text.index("\nStep-Venv\n")
    assert "if ($Apply) { Stop-Install $notAdmin }" in text
    # An account that cannot be confirmed would get Modify on a folder this window runs code from.
    unknown = text[text.index("$adminState = 'unknown'"):text.index("if ($adminState -eq 'none')")]
    assert "if ($Apply) { Stop-Install $unknownAdmin }" in unknown
