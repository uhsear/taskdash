"""H4-4: the PowerShell payloads work under ConstrainedLanguage (AppLocker or WDAC).

subprocess.run is faked, so nothing starts PowerShell and no machine state is read. The fake plays
both modes: FullLanguage prints JSON on stdout, ConstrainedLanguage writes it to the temp file the
wrapper names in the environment. Language rules: about_Language_Modes,
https://learn.microsoft.com/powershell/module/microsoft.powershell.core/about/about_language_modes
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import taskdash_collect as collect
import taskdash_common as common
import taskdash_preflight as pf

ROWS = [{"task_name": "Café Refresh", "state": "Ready"}]


def _install_fake(monkeypatch, tmp_path, mode="FullLanguage", returncode=0, stderr=b"", probe=None):
    """Replace subprocess.run. A call with the wrapper plays `mode`; any other call is the mode probe."""
    monkeypatch.setattr(common, "runtime_dir", lambda: tmp_path)
    seen = []

    def run(cmd, **kwargs):
        seen.append((list(cmd), kwargs))
        if "$tdScript" not in cmd[-1]:
            return SimpleNamespace(returncode=0, stdout=(probe or mode).encode() + b"\r\n", stderr=b"")
        payload = json.dumps(ROWS, ensure_ascii=False)
        if returncode:
            return SimpleNamespace(returncode=returncode, stdout=b"", stderr=stderr)
        if mode == "FullLanguage":
            return SimpleNamespace(returncode=0, stdout=payload.encode("utf-8"), stderr=b"")
        out = kwargs["env"][common.POWERSHELL_OUT_ENV]
        with open(out, "wb") as handle:                     # Set-Content -Encoding UTF8 writes a BOM
            handle.write(b"\xef\xbb\xbf" + payload.encode("utf-8") + b"\r\n")
        return SimpleNamespace(returncode=0, stdout="Caf�".encode("utf-8"), stderr=b"")

    monkeypatch.setattr(subprocess, "run", run)
    return seen


def test_full_language_reads_stdout_and_creates_no_file(monkeypatch, tmp_path):
    seen = _install_fake(monkeypatch, tmp_path)
    assert common.run_powershell_json("Get-Thing") == ROWS
    assert len(seen) == 1, "FullLanguage must not start a second PowerShell for the mode"
    assert list(tmp_path.iterdir()) == []


def test_the_script_runs_unchanged_inside_the_wrapper(monkeypatch, tmp_path):
    seen = _install_fake(monkeypatch, tmp_path)
    common.run_powershell_json("Get-Thing | ConvertTo-Json")
    command = seen[0][0][-1]
    assert "{\nGet-Thing | ConvertTo-Json\n}" in command
    assert "LanguageMode" in command and common.POWERSHELL_OUT_ENV in command


def test_constrained_language_reads_the_temp_file_as_utf8_and_removes_it(monkeypatch, tmp_path):
    _install_fake(monkeypatch, tmp_path, mode="ConstrainedLanguage")
    assert common.run_powershell_json("Get-Thing") == ROWS      # stdout was mojibake; the file wins
    assert list(tmp_path.iterdir()) == [], "the temp file must be removed after the read"


def test_every_call_gets_its_own_temp_path_in_the_runtime_folder(monkeypatch, tmp_path):
    seen = _install_fake(monkeypatch, tmp_path, mode="ConstrainedLanguage")
    common.run_powershell_json("a")
    common.run_powershell_json("b")
    paths = [kwargs["env"][common.POWERSHELL_OUT_ENV] for _, kwargs in seen]
    assert len(set(paths)) == 2
    assert all(str(tmp_path) in path for path in paths)


def test_a_failed_constrained_call_removes_a_half_written_file(monkeypatch, tmp_path):
    seen = _install_fake(monkeypatch, tmp_path, mode="ConstrainedLanguage", returncode=1, stderr=b"boom")
    original = subprocess.run

    def run(cmd, **kwargs):
        if "$tdScript" in cmd[-1]:
            Path(kwargs["env"][common.POWERSHELL_OUT_ENV]).write_bytes(b"partial")
        return original(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SystemExit):
        common.run_powershell_json("Get-Thing")
    assert list(tmp_path.iterdir()) == []
    assert seen


def test_a_failure_in_constrained_language_names_the_mode_and_the_fix(monkeypatch, tmp_path):
    _install_fake(monkeypatch, tmp_path, mode="ConstrainedLanguage", returncode=1,
                  stderr=b"Cannot invoke method. Method invocation is supported only on core types")
    with pytest.raises(SystemExit) as exc:
        common.run_powershell_json("Get-Thing")
    text = str(exc.value)
    assert "ConstrainedLanguage" in text and "AppLocker" in text and "WDAC" in text
    assert "allow rule" in text and "TaskDash folder" in text and "exempt" in text
    assert "Method invocation is supported only on core types" in text
    assert text.endswith(common.see_troubleshooting("PowerShell runs in ConstrainedLanguage mode"))


def test_a_failure_in_full_language_keeps_the_generic_message(monkeypatch, tmp_path):
    _install_fake(monkeypatch, tmp_path, returncode=1, stderr=b"Access is denied")
    with pytest.raises(SystemExit) as exc:
        common.run_powershell_json("Get-Thing")
    text = str(exc.value)
    assert text.startswith("PowerShell collection failed (exit 1): Access is denied")
    assert "ConstrainedLanguage" not in text


def test_a_failed_mode_probe_keeps_the_generic_message(monkeypatch, tmp_path):
    _install_fake(monkeypatch, tmp_path, returncode=1, stderr=b"Access is denied", probe="")
    with pytest.raises(SystemExit) as exc:
        common.run_powershell_json("Get-Thing")
    assert str(exc.value).startswith("PowerShell collection failed (exit 1)")


@pytest.mark.parametrize("raised", [OSError("no powershell"), subprocess.TimeoutExpired("x", 1)])
def test_the_mode_probe_never_raises(monkeypatch, raised):
    def run(cmd, **kwargs):
        raise raised
    monkeypatch.setattr(subprocess, "run", run)
    assert common.powershell_language_mode() == ""


# ------------------------------------------------------------------ the payload text

PAYLOADS = {
    "tasks": collect.POWERSHELL_TASK_QUERY,
    "health": collect.POWERSHELL_LOG_HEALTH_QUERY_TEMPLATE,
    "history": collect.POWERSHELL_HISTORY_QUERY_TEMPLATE,
    "newest": collect.POWERSHELL_NEWEST_RECORD_QUERY,
}


@pytest.mark.parametrize("name", PAYLOADS)
def test_payloads_avoid_what_constrained_language_refuses(name):
    """[pscustomobject]@{} and an XmlNode method call fail there (measured with
    $ExecutionContext.SessionState.LanguageMode = 'ConstrainedLanguage' on Windows PowerShell 5.1).
    [ordered]@{} and Select-Xml give the same JSON in both modes."""
    script = PAYLOADS[name]
    assert "[pscustomobject]@{" not in script
    assert "SelectSingleNode" not in script
    assert "Add-Type" not in script and "New-Object" not in script


@pytest.mark.parametrize("name", PAYLOADS)
def test_the_console_encoding_line_runs_only_in_full_language(name):
    for line in PAYLOADS[name].splitlines():
        if "[Console]::OutputEncoding" in line:
            assert "-eq 'FullLanguage'" in line, line


def test_the_history_payload_reads_events_by_position_in_constrained_language():
    """ToXml() is a method on a type outside the allowed list, so the CLM branch maps
    Properties[i] to the field names of the event's template (checked against real events)."""
    script = collect.POWERSHELL_HISTORY_QUERY_TEMPLATE
    table = dict(re.findall(r"^\s*(\d+)\s*=\s*@\(([^)]*)\)", script, re.MULTILINE))
    for event_id in collect.HISTORY_EVENT_IDS:
        assert str(event_id) in table, f"event {event_id} has no field-name row for ConstrainedLanguage"
    assert "'TaskName'" in table["200"] and "'ActionName'" in table["200"]
    assert table["201"].replace("'", "").replace(" ", "").split(",")[:4] == [
        "TaskName", "TaskInstanceId", "ActionName", "ResultCode"]
    assert ".ToXml()" in script and "Get-PropertyDataMap" in script


# ------------------------------------------------------------------ preflight line

def _line(monkeypatch, mode):
    monkeypatch.setattr(pf, "powershell_language_mode", lambda: mode)
    return pf.check_language_mode(pf.Context(SimpleNamespace()))


def test_preflight_language_mode_is_ok_in_full_language(monkeypatch):
    status, message = _line(monkeypatch, "FullLanguage")
    assert status == pf.OK and "FullLanguage" in message


def test_preflight_warns_in_constrained_language_with_what_is_limited(monkeypatch):
    status, message = _line(monkeypatch, "ConstrainedLanguage")
    assert status == pf.WARN and message.startswith("WARNING:")
    assert "ConstrainedLanguage" in message and "temporary file" in message
    assert "AppLocker" in message and "WDAC" in message and "exempt" in message


def test_preflight_warns_when_the_mode_cannot_be_read(monkeypatch):
    status, message = _line(monkeypatch, "")
    assert status == pf.WARN and message.startswith("WARNING:") and "LanguageMode" in message


def test_preflight_names_an_unexpected_mode(monkeypatch):
    status, message = _line(monkeypatch, "RestrictedLanguage")
    assert status == pf.WARN and "RestrictedLanguage" in message


def test_the_language_mode_check_is_listed_and_needs_no_network():
    assert ("language mode", False, pf.check_language_mode) in pf.CHECKS
