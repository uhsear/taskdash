"""Phase 3 install pieces: the task XML template, install_task.ps1, the CI workflow and the
export script's file selection."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "_tools"))

import export_public  # noqa: E402

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
TEMPLATE = ROOT / "taskdash_task.xml"
SCRIPT = ROOT / "install_task.ps1"
WORKFLOW = ROOT / ".github" / "workflows" / "install.yml"
POWERSHELL = shutil.which("powershell.exe")
needs_powershell = pytest.mark.skipif(not POWERSHELL, reason="powershell.exe not available")


def _render(**overrides):
    values = {"AUTHOR": "svc", "USER_ID": "HOST\\svc", "LOGON_TYPE": "Password",
              "COMMAND": "C:\\td\\run_taskdash.cmd", "ARGUMENTS": "--apply",
              "WORKING_DIRECTORY": "C:\\td", "START_BOUNDARY": "2026-01-01T01:00:00"}
    values.update(overrides)
    text = TEMPLATE.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return ET.fromstring(text)


def _text(root, path):
    return root.find(path, NS).text


def test_template_has_no_placeholder_beyond_the_known_seven():
    text = TEMPLATE.read_text(encoding="utf-8")
    for key in ("AUTHOR", "USER_ID", "LOGON_TYPE", "COMMAND", "ARGUMENTS", "WORKING_DIRECTORY",
                "START_BOUNDARY"):
        text = text.replace("{{" + key + "}}", "")
    assert "{{" not in text


def test_template_renders_the_hourly_collector_task():
    root = _render()
    assert _text(root, "t:Principals/t:Principal/t:LogonType") == "Password"
    assert _text(root, "t:Principals/t:Principal/t:RunLevel") == "LeastPrivilege"
    repetition = root.find("t:Triggers/t:TimeTrigger/t:Repetition", NS)
    assert repetition.find("t:Interval", NS).text == "PT1H"
    assert repetition.find("t:Duration", NS) is None  # no Duration means it repeats indefinitely
    settings = root.find("t:Settings", NS)
    assert settings.find("t:ExecutionTimeLimit", NS).text == "PT2H"
    assert settings.find("t:StartWhenAvailable", NS).text == "true"
    assert settings.find("t:MultipleInstancesPolicy", NS).text == "IgnoreNew"
    for switch in ("DisallowStartIfOnBatteries", "StopIfGoingOnBatteries", "RunOnlyIfIdle"):
        assert settings.find(f"t:{switch}", NS).text == "false"
    assert _text(root, "t:Actions/t:Exec/t:Arguments") == "--apply"


def test_template_is_utf8_without_a_byte_order_mark():
    data = TEMPLATE.read_bytes()
    assert not data.startswith((b"\xef\xbb\xbf", b"\xff\xfe"))
    data.decode("utf-8")


def test_script_never_edits_a_registered_task():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "Set-ScheduledTask" not in text
    assert "Register-ScheduledTask @register" in text and "Xml = $xmlText" in text


def test_script_registers_only_after_the_apply_switch():
    text = SCRIPT.read_text(encoding="utf-8")
    check_mode_end = text.index("CHECK MODE - nothing registered. Re-run with -Apply.")
    assert text.count("Register-ScheduledTask @register") == 1
    assert text.index("Register-ScheduledTask @register") > check_mode_end


def test_replace_switch_reregisters_through_the_xml_with_force_and_never_sets_a_password():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "[switch]$Replace" in text
    # One registration call serves both modes; -Replace only adds -Force to its splat.
    assert text.count("Register-ScheduledTask @register") == 1
    assert "$register.Force = $true" in text and "Xml = $xmlText" in text
    assert not re.search(r"Set-ScheduledTask[^\n]*-Password", text)
    assert "Set-ScheduledTask" not in text


def test_replace_checks_the_existing_task_before_it_asks_for_the_password():
    text = SCRIPT.read_text(encoding="utf-8")
    register = text.index("Register-ScheduledTask @register")
    for needle in ("-Replace found no task named", "runs as '", "logs on as '", "Read-Host -AsSecureString"):
        assert 0 < text.index(needle) < register, needle
    assert text.index("-Replace found no task named") < text.index("Read-Host -AsSecureString")
    # Without -Replace the old refusal stays, now with the way out.
    assert "A task named '$TaskName' already exists." in text and "add -Replace" in text


def test_replace_keeps_the_existing_task_action_instead_of_the_install_default():
    text = SCRIPT.read_text(encoding="utf-8")
    block = text[text.index("if ($Replace) {\n    if (-not $existing)"):text.index("Register-ScheduledTask @register")]
    for needle in ("$existing.Actions", "$keep.Execute", "$keep.Arguments", "$keep.WorkingDirectory",
                   "$xmlText = $doc.OuterXml"):
        assert needle in block, needle


def _replace_dom_edit(tmp_path, exported_xml):
    """Run the DOM edit that -Replace does on the rendered template, with no scheduler involved.
    A function named Export-ScheduledTask shadows the cmdlet and returns exported_xml."""
    snippet = SCRIPT.read_text(encoding="utf-8")
    start = snippet.index("$namespaces = New-Object")
    end = snippet.index("$xmlText = $doc.OuterXml")
    exported = tmp_path / "exported.xml"
    exported.write_text(exported_xml, encoding="utf-8")
    script = tmp_path / "rewrite.ps1"
    script.write_text(
        "$doc = New-Object System.Xml.XmlDocument\n"
        f"$doc.Load('{TEMPLATE}')\n"
        "$TaskName = 'Synthetic Task'\n"
        "function Fail([string]$Message) { throw $Message }\n"
        f"function Export-ScheduledTask {{ param($TaskName) Get-Content -LiteralPath '{exported}' -Raw }}\n"
        "$keep = [pscustomobject]@{ Execute = 'C:\\td\\run_taskdash.cmd'; "
        "Arguments = '--apply --allow-host-mismatch & \"x\"'; WorkingDirectory = 'C:\\td' }\n"
        + snippet[start:end] + "$doc.OuterXml\n", encoding="utf-8")
    return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(script)],
                          capture_output=True, text=True, timeout=120)


def _template_text(**replacements):
    text = TEMPLATE.read_text(encoding="utf-8")
    for key in ("AUTHOR", "USER_ID", "LOGON_TYPE", "COMMAND", "ARGUMENTS", "WORKING_DIRECTORY"):
        text = text.replace("{{" + key + "}}", "x")
    text = text.replace("{{START_BOUNDARY}}", "2026-01-01T01:00:00")
    for old, new in replacements.items():
        assert old in text, old
        text = text.replace(old, new)
    return text


@needs_powershell
def test_replace_action_rewrite_keeps_arguments_and_escapes_them(tmp_path):
    result = _replace_dom_edit(tmp_path, _template_text())
    assert result.returncode == 0, result.stderr
    root = ET.fromstring(result.stdout)
    assert _text(root, "t:Actions/t:Exec/t:Arguments") == '--apply --allow-host-mismatch & "x"'
    assert _text(root, "t:Actions/t:Exec/t:Command") == "C:\\td\\run_taskdash.cmd"


@needs_powershell
def test_replace_keeps_a_hand_changed_schedule_and_run_limit(tmp_path):
    exported = _template_text(**{"PT1H": "PT30M", "PT2H": "PT4H", "2026-01-01T01:00:00": "2025-06-01T00:15:00"})
    result = _replace_dom_edit(tmp_path, exported)
    assert result.returncode == 0, result.stderr
    root = ET.fromstring(result.stdout)
    assert _text(root, "t:Triggers/t:TimeTrigger/t:Repetition/t:Interval") == "PT30M"
    assert _text(root, "t:Triggers/t:TimeTrigger/t:StartBoundary") == "2025-06-01T00:15:00"
    assert _text(root, "t:Settings/t:ExecutionTimeLimit") == "PT4H"
    assert _text(root, "t:Principals/t:Principal/t:LogonType") == "{{LOGON_TYPE}}"  # principal stays the template's
    assert len(root.findall("t:Triggers", NS)) == 1 and len(root.findall("t:Settings", NS)) == 1


@needs_powershell
def test_replace_refuses_an_existing_task_with_no_trigger_section(tmp_path):
    exported = _template_text()
    exported = exported[:exported.index("<Triggers>")] + exported[exported.index("</Triggers>") + len("</Triggers>"):]
    result = _replace_dom_edit(tmp_path, exported)
    assert result.returncode != 0 and "has no Triggers section to keep" in result.stderr


def test_replace_exports_the_existing_task_to_keep_its_trigger_and_settings():
    text = SCRIPT.read_text(encoding="utf-8")
    block = text[text.index("if ($Replace) {\n    if (-not $existing)"):text.index("Register-ScheduledTask @register")]
    assert "Export-ScheduledTask -TaskName $TaskName" in block
    assert '"Triggers", "Settings"' in block and "ReplaceChild" in block


@needs_powershell
def test_script_check_mode_with_replace_runs_the_read_only_refusals_and_registers_nothing():
    # No such task exists, so check mode now refuses as -Apply would, instead of printing a plan.
    result = _run("-InstallDir", str(ROOT), "-TaskName", "TaskDashTestNeverRegistered", "-Replace")
    assert result.returncode == 1
    assert "-Replace found no task named 'TaskDashTestNeverRegistered'" in result.stderr
    assert "Replace:           yes" in result.stdout and "Registered '" not in result.stdout


def test_replace_checks_run_before_check_mode_ends_and_say_what_apply_would_do():
    text = SCRIPT.read_text(encoding="utf-8")
    check_mode_end = text.index("CHECK MODE - nothing registered. Re-run with -Apply.")
    for needle in ("-Replace found no task named", "runs as '", "logs on as '", "has $($actions.Count) action(s)",
                   "Export-ScheduledTask -TaskName $TaskName", "Replace checks passed:"):
        assert 0 < text.index(needle) < check_mode_end, needle
    assert "Would keep" in text and "$verb = if ($Apply)" in text
    # The old-task refusal stays an apply-only check, so check mode without -Replace is unchanged.
    assert text.index("if (-not $Replace -and $existing)") > check_mode_end


def _account_check(tmp_path, existing_user, user):
    """Run the -Replace account comparison on its own: True when the scheduler's account is accepted."""
    text = SCRIPT.read_text(encoding="utf-8")
    snippet = text[text.index("$sameAccount = "):text.index("if (-not $sameAccount)")]
    script = tmp_path / "account.ps1"
    script.write_text(f"$existingUser = '{existing_user}'\n$User = '{user}'\n{snippet}$sameAccount\n",
                      encoding="utf-8")
    # This computer is NEW-PC and CORP is its domain, whatever machine runs the test.
    env = {**os.environ, "COMPUTERNAME": "NEW-PC", "USERDOMAIN": "CORP"}
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(script)],
                            capture_output=True, text=True, timeout=120, env=env)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip() == "True"


@needs_powershell
@pytest.mark.parametrize("existing_user, user, accepted", [
    ("CORP\\svc-taskdash", "corp\\SVC-TASKDASH", True),   # the full name, case-insensitive
    ("OLD-PC\\svc-taskdash", "NEW-PC\\svc-taskdash", True),   # a renamed computer's local account keeps its task
    ("CORP\\svc-taskdash", "NEW-PC\\svc-taskdash", False),   # a domain account is not the same-named local one
    ("NEW-PC\\svc-taskdash", "CORP\\svc-taskdash", False),   # and the local account is not the domain one
    ("OLD-PC\\svc-taskdash", "OTHER-PC\\svc-taskdash", False),  # -User must name this computer
    ("CORP\\svc-taskdash", "CORP\\other", False),         # another account name is another task
    ("svc-taskdash", "CORP\\svc-taskdash", True),         # the scheduler reports a bare name
    ("svc-taskdash", "CORP\\other", False),
    ("CORP\\svc-taskdash", "svc-taskdash", False),        # the scheduler names a domain, -User does not
])
def test_replace_compares_the_account_name_and_accepts_another_prefix(tmp_path, existing_user, user, accepted):
    assert _account_check(tmp_path, existing_user, user) is accepted


def _run(*args):
    return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT), *args],
                          capture_output=True, text=True, timeout=120)


@needs_powershell
def test_script_refuses_s4u_with_the_reason():
    result = _run("-InstallDir", str(ROOT), "-LogonType", "S4U")
    assert result.returncode == 1
    assert "S4U cannot read stored credentials" in result.stderr
    assert "Use Password." in result.stderr


@needs_powershell
def test_script_check_mode_renders_and_registers_nothing():
    result = _run("-InstallDir", str(ROOT), "-TaskName", "TaskDashTestNeverRegistered")
    assert result.returncode == 0, result.stderr
    assert "CHECK MODE - nothing registered. Re-run with -Apply." in result.stdout
    assert "(Password)" in result.stdout and "run_taskdash.cmd --apply" in result.stdout
    exists = subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command",
         "if (Get-ScheduledTask -TaskName TaskDashTestNeverRegistered -ErrorAction SilentlyContinue) "
         "{ exit 1 }"], capture_output=True, text=True, timeout=120)
    assert exists.returncode == 0


@needs_powershell
def test_script_wraps_the_interpreter_override_and_runs_taskdash_exe_directly():
    wrapped = _run("-InstallDir", str(ROOT), "-Python", "C:\\py\\python.exe", "-LogonType", "Interactive")
    assert wrapped.returncode == 0, wrapped.stderr
    assert 'set "TASKDASH_PYTHON=C:\\py\\python.exe"' in wrapped.stdout
    assert "(Interactive)" in wrapped.stdout
    direct = _run("-InstallDir", str(ROOT), "-Python", "C:\\v\\Scripts\\taskdash.exe")
    assert direct.returncode == 0, direct.stderr
    assert "C:\\v\\Scripts\\taskdash.exe collect --apply" in direct.stdout


@needs_powershell
def test_script_rejects_an_install_dir_without_the_wrapper(tmp_path):
    result = _run("-InstallDir", str(tmp_path))
    assert result.returncode == 1
    assert r"Neither run_taskdash.cmd nor venv\Scripts\taskdash.exe is in" in result.stderr


@needs_powershell
def test_a_wheel_install_without_the_wrapper_defaults_to_the_venv_exe(tmp_path):
    exe = tmp_path / "venv" / "Scripts" / "taskdash.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    result = _run("-InstallDir", str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert f"{exe} collect --apply" in result.stdout


def test_workflow_has_the_expected_jobs_and_steps():
    text = WORKFLOW.read_text(encoding="utf-8")
    for needle in ("workflow_dispatch:", "runs-on: windows-latest", "actions/checkout@",
                   "actions/setup-python@", 'python-version: "3.11"', 'python -m pip install "build==',
                   "python -m build --wheel", "python -m venv", "--version",
                   "preflight --offline --config", "New-LocalUser", "install_task.ps1",
                   "-LogonType Password", "-Apply", "Principal.LogonType",
                   "collect --fixture tests/fixtures/synthetic_app_host_01.json", "dashboard --offline",
                   'pip install "arcgis>=2.4,<3" pytest', "python -m pytest", "tzutil /s",
                   "TASKDASH_EXPECT_UTC_OFFSET_S", "fetch-depth: 0", "gitleaks git --redact"):
        assert needle in text, needle
    # install_task.ps1 -Apply refuses a task account outside Administrators, so the throw-away user
    # joins the group by SID (S-1-5-32-544) before the script runs, on any Windows language.
    assert "Add-LocalGroupMember -SID S-1-5-32-544 -Member $name" in text
    assert text.index("Add-LocalGroupMember") < text.index(".\\install_task.ps1")
    assert text.count("runs-on: windows-latest") == 2
    assert "secrets." not in text
    # The fixture dry run must never reach ArcGIS Online.
    assert "collect --fixture" in text and "collect --apply" not in text




def test_release_workflow_builds_on_tags_and_passes_the_tag_only_by_environment():
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    for needle in ('tags: ["v*"]', "contents: write", "python -m build --wheel", "SHA256SUMS.txt",
                   "GH_TOKEN: ${{ github.token }}", "gh release create $env:TAG", "--prerelease"):
        assert needle in text, needle
    assert "secrets." not in text
    # The tag is set as TAG once per job that needs it (build, publish) and never pasted into a script.
    assert text.count("${{ github.ref_name }}") == 2
    assert text.count("TAG: ${{ github.ref_name }}") == 2

def test_the_ci_leg_runs_in_the_zone_it_names():
    """A leg that silently stayed in UTC would prove nothing about time-zone handling. Only the CI
    legs set the variable; on any other machine there is no zone to check."""
    expected = os.environ.get("TASKDASH_EXPECT_UTC_OFFSET_S")
    if expected is None:
        pytest.skip("not a CI time-zone leg")
    assert -time.timezone == int(expected)

FAKE_TRACKED = [
    "taskdash_publish.py", "rules/default.json", "rules/mar" + "ion.json",
    "workflow_descriptions.json", "tests/fixtures/workflow_descriptions.json",
    "dashboard_taskdash.json", "TaskDash.config.json", "TaskDash.config.example.json",
    "_tools/export_public.py",
    "archive/old/x.py", "Logs/run.txt", "conn." + "sd" + "e", "sub/conn." + "SD" + "E",
    "install_task.ps1",
]


def test_export_selection_drops_every_listed_path_and_keeps_the_rest():
    kept, dropped = export_public.select_files(FAKE_TRACKED)
    assert sorted(dropped) == sorted([
        "rules/mar" + "ion.json", "dashboard_taskdash.json",
        "TaskDash.config.json", "archive/old/x.py",
        "Logs/run.txt", "conn." + "sd" + "e", "sub/conn." + "SD" + "E"])
    # The root file is a generic example now; the site copy lives outside the repository.
    assert "workflow_descriptions.json" in kept
    assert "tests/fixtures/workflow_descriptions.json" in kept
    assert "TaskDash.config.example.json" in kept
    assert "install_task.ps1" in kept
    assert len(kept) + len(dropped) == len(FAKE_TRACKED)


def test_export_allows_the_holder_name_only_on_the_exact_copyright_lines(tmp_path):
    holder = "As" + "ir"  # assembled: the name is on the private deny list
    private = tmp_path / "private.txt"
    private.write_text(holder + "\n", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    (out / "NOTICE").write_text(f"TaskDash\n{export_public.ALLOWED_LINES['NOTICE']}\n", encoding="utf-8")
    (out / "README.md").write_text(f"{export_public.ALLOWED_LINES['README.md']}\nThanks, {holder}.\n",
                                   encoding="utf-8")
    (out / "docs.md").write_text(export_public.ALLOWED_LINES["NOTICE"] + "\n", encoding="utf-8")
    hits = {Path(k).name: v for k, v in export_public.deny_hits(out, str(private)).items()}
    assert hits == {"README.md": 1, "docs.md": 1}


def test_export_writes_a_fake_tree_without_the_excluded_files(tmp_path, monkeypatch):
    source = tmp_path / "src"
    for name in FAKE_TRACKED:
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text("x", encoding="utf-8")

    class Blob:
        returncode = 0

        def __init__(self, data):
            self.stdout = data

    monkeypatch.setattr(export_public, "_git",
                        lambda *args, text=True: Blob((source / args[1][len("HEAD:"):]).read_bytes()))
    kept, _ = export_public.select_files(FAKE_TRACKED)
    out = tmp_path / "out"
    export_public.write_export(kept, out)
    written = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert written == sorted(kept)
    assert not (out / "dashboard_taskdash.json").exists()


def test_help_example_install_dir_matches_install_guide():
    # The task starts in -InstallDir, and home_dir() looks for the config there, so the
    # example must not point at the venv folder.
    assert "-InstallDir C:\\TaskDash\\venv" not in SCRIPT.read_text(encoding="utf-8")
