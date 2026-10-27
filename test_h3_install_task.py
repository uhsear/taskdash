"""H3-5: install_task.ps1 refusals (elevation, administrator task account), -EnableEventLog,
-StartNow and the culture-proof StartBoundary. Static text checks, parse-only syntax checks, and
snippets of the script run in a fresh PowerShell with the scheduler cmdlets shadowed. The script
itself never runs with -Apply and nothing here touches a real task."""

from __future__ import annotations

import datetime
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "install_task.ps1"
POWERSHELL = shutil.which("powershell.exe")
needs_powershell = pytest.mark.skipif(not POWERSHELL, reason="powershell.exe not available")


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
    return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(script)],
                          capture_output=True, text=True, timeout=120)


def _run(*args):
    return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT), *args],
                          capture_output=True, text=True, timeout=120)


@needs_powershell
def test_script_parses_without_errors():
    # Parse only: ParseFile never runs the script.
    command = ("$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
               f"'{SCRIPT}', [ref]$null, [ref]$e); if ($e) {{ $e | ForEach-Object {{ $_.Message }}; exit 1 }}")
    result = subprocess.run([POWERSHELL, "-NoProfile", "-Command", command],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_new_switches_exist_and_nothing_writes_before_apply():
    text = _text()
    assert "[switch]$EnableEventLog" in text and "[switch]$StartNow" in text
    check_mode_end = text.index("CHECK MODE - nothing registered. Re-run with -Apply.")
    # The calls come after check mode exits; the function that holds Start-ScheduledTask is only defined earlier.
    for needle in ("& $taskdashExe preflight --enable-event-log --apply", "= Start-TaskAndWait -Name"):
        assert text.index(needle) > check_mode_end, needle
    assert "Set-ScheduledTask" not in text


def test_event_log_runs_before_registering_and_start_runs_after_the_logon_type_check():
    text = _text()
    register = text.index("Register-ScheduledTask @register")
    assert text.index("& $taskdashExe preflight --enable-event-log --apply") < text.index("Read-Host -AsSecureString") < register
    assert text.index("if ($StartNow) {\n") > text.index("registered with logon type")


def test_refusals_name_the_fix():
    text = _text()
    for needle in ("not elevated", "Run as administrator", "net localgroup Administrators $User /add",
                   "local Administrators group"):
        assert needle in text, needle


def test_start_boundary_is_built_with_the_invariant_culture():
    line = re.search(r"^\$start = .*$", _text(), re.M).group(0)
    assert "InvariantCulture" in line and "'s'" in line
    assert "yyyy-MM-ddTHH:mm:ss" not in line


@needs_powershell
def test_start_boundary_is_iso_in_a_culture_with_another_calendar(tmp_path):
    # th-TH formats the year in the Buddhist calendar (2569); the old format string followed it.
    text = _text()
    now_line = re.search(r"^\$now = .*$", text, re.M)
    start_line = re.search(r"^\$start = .*$", text, re.M).group(0)
    prefix = now_line.group(0) + "\n" if now_line else ""
    result = _ps(tmp_path, "[System.Threading.Thread]::CurrentThread.CurrentCulture = "
                 "[System.Globalization.CultureInfo]::GetCultureInfo('th-TH')\n" + prefix + start_line + "\n$start\n")
    assert result.returncode == 0, result.stderr
    value = result.stdout.strip()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", value), value
    assert abs(int(value[:4]) - datetime.date.today().year) <= 1, value


@needs_powershell
@pytest.mark.parametrize("code, expected", [
    ("0", ["0x00000000", "succeeded"]),
    ("1", ["0x00000001", "taskdash status", "Logs"]),
    ("267009", ["0x00041301", "still running"]),
    ("267011", ["0x00041303", "never started", "Administrators"]),
    ("2147943726", ["0x8007052E", "password", "-Replace"]),
    ("-2147023570", ["0x8007052E", "password"]),          # the signed form of the same code
    ("2147943785", ["0x80070569", "batch job"]),
    ("12345", ["0x00003039", "History"]),
])
def test_task_result_codes_are_decoded(tmp_path, code, expected):
    result = _ps(tmp_path, _function("Get-TaskResultText") + f"Get-TaskResultText {code}\n")
    assert result.returncode == 0, result.stderr
    for needle in expected:
        assert needle in result.stdout, (needle, result.stdout)


@needs_powershell
@pytest.mark.parametrize("account, members, expected", [
    ("CORP\\svc", [("CORP\\svc", "User")], "direct"),
    ("corp\\SVC", [("CORP\\svc", "User")], "direct"),
    ("PC01\\svc", [("CORP\\svc", "User")], "none"),                       # same short name, other domain
    ("svc", [("PC01\\svc", "User")], "direct"),                           # a bare name matches by short name
    ("CORP\\svc", [("CORP\\Domain Admins", "Group"), ("PC01\\Administrator", "User")], "group"),
    ("CORP\\svc", [("PC01\\Administrator", "User")], "none"),
    ("CORP\\svc", [("CORP\\svc", "Group")], "group"),                     # a group of that name is not the user
])
def test_admin_membership_is_direct_nested_or_none(tmp_path, account, members, expected):
    rows = ",".join(f"[pscustomobject]@{{Name='{n}';ObjectClass='{c}'}}" for n, c in members)
    result = _ps(tmp_path, _function("Get-AdminMembership") +
                 f"Get-AdminMembership '{account}' @({rows})\n")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def _wait_harness(results):
    """Fake scheduler: the task shows Running once, then Ready with the next LastTaskResult."""
    return (
        "$script:calls = 0\n"
        "function Start-Sleep { param($Seconds) }\n"
        "function Start-ScheduledTask { param($TaskName) }\n"
        "function Get-ScheduledTask { param($TaskName) "
        "[pscustomobject]@{ State = $(if ($script:calls -lt 1) { 'Running' } else { 'Ready' }) } }\n"
        "function Get-ScheduledTaskInfo { param($TaskName) $script:calls++; "
        "[pscustomobject]@{ LastRunTime = $(if ($script:calls -le 1) { (Get-Date).AddDays(-1) } else { (Get-Date).AddMinutes(1) }); "
        f"LastTaskResult = {results} }} }}\n"
        + _function("Start-TaskAndWait"))


@needs_powershell
def test_start_and_wait_returns_the_result_of_the_new_run(tmp_path):
    result = _ps(tmp_path, _wait_harness("2147943726") + "Start-TaskAndWait -Name 'Synthetic Task' -TimeoutSeconds 60\n")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2147943726"


@needs_powershell
def test_start_and_wait_gives_up_after_the_timeout(tmp_path):
    forever = ("function Start-Sleep { param($Seconds) }\nfunction Start-ScheduledTask { param($TaskName) }\n"
               "function Get-ScheduledTask { param($TaskName) [pscustomobject]@{ State = 'Running' } }\n"
               "function Get-ScheduledTaskInfo { param($TaskName) [pscustomobject]@{ LastRunTime = $null; "
               "LastTaskResult = 267009 } }\n" + _function("Start-TaskAndWait") +
               "$r = Start-TaskAndWait -Name 'Synthetic Task' -TimeoutSeconds 0\n"
               "if ($null -eq $r) { 'timed out' } else { \"returned $r\" }\n")
    result = _ps(tmp_path, forever)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "timed out"


@needs_powershell
def test_check_mode_reports_elevation_and_the_new_steps_and_registers_nothing(tmp_path):
    exe = tmp_path / "Scripts" / "taskdash.exe"
    exe.parent.mkdir()
    exe.write_text("not a program", encoding="utf-8")
    result = _run("-InstallDir", str(ROOT), "-TaskName", "TaskDashTestNeverRegistered",
                  "-Python", str(exe), "-EnableEventLog", "-StartNow")
    assert result.returncode == 0, result.stderr
    assert "CHECK MODE - nothing registered. Re-run with -Apply." in result.stdout
    assert "Event log:" in result.stdout and "preflight --enable-event-log --apply" in result.stdout
    assert "Start now:" in result.stdout and "10 minutes" in result.stdout
    assert "Registered '" not in result.stdout


@needs_powershell
def test_enable_event_log_refuses_when_there_is_no_taskdash_exe():
    result = _run("-InstallDir", str(ROOT), "-EnableEventLog")
    assert result.returncode == 1
    assert "-EnableEventLog needs the venv's taskdash.exe" in result.stderr
    assert "-Python" in result.stderr
