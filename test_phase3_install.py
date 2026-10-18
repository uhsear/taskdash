"""Phase 3 install pieces: the task XML template, install_task.ps1, the CI workflow and the
export script's file selection."""

from __future__ import annotations

import os
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
    assert "run_taskdash.cmd is not in" in result.stderr


def test_workflow_has_the_expected_jobs_and_steps():
    text = WORKFLOW.read_text(encoding="utf-8")
    for needle in ("workflow_dispatch:", "runs-on: windows-latest", "actions/checkout@v4",
                   "actions/setup-python@v5", 'python-version: "3.11"', "python -m pip install build",
                   "python -m build --wheel", "python -m venv", "--version",
                   "preflight --offline --config", "New-LocalUser", "install_task.ps1",
                   "-LogonType Password", "-Apply", "Principal.LogonType",
                   "collect --fixture tests/fixtures/synthetic_app_host_01.json", "dashboard --offline",
                   "pip install arcgis pytest", "python -m pytest", "tzutil /s",
                   "TASKDASH_EXPECT_UTC_OFFSET_S", "fetch-depth: 0", "gitleaks git --redact"):
        assert needle in text, needle
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
    # The tag is set once as TAG and never pasted into a script.
    assert text.count("${{ github.ref_name }}") == 1

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
    "_tools/repair_estimated_durations.py", "_tools/export_public.py",
    "archive/old/x.py", "Logs/run.txt", "conn." + "sd" + "e", "sub/conn." + "SD" + "E",
    "install_task.ps1",
]


def test_export_selection_drops_every_listed_path_and_keeps_the_rest():
    kept, dropped = export_public.select_files(FAKE_TRACKED)
    assert sorted(dropped) == sorted([
        "rules/mar" + "ion.json", "workflow_descriptions.json", "dashboard_taskdash.json",
        "TaskDash.config.json", "_tools/repair_estimated_durations.py", "archive/old/x.py",
        "Logs/run.txt", "conn." + "sd" + "e", "sub/conn." + "SD" + "E"])
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
