"""H5-4: every system binary is called by absolute System32 path, never by bare name.

A bare name is searched in the working folder and PATH first, so a planted powershell.exe or
wevtutil.exe would run as the admin task account. subprocess.run is faked: nothing executes.
"""
from __future__ import annotations

import ntpath
from pathlib import Path
from types import SimpleNamespace

import taskdash_common as common
import taskdash_preflight as pf

HERE = Path(__file__).resolve().parent


def _capture(monkeypatch, module, stdout):
    calls = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=stdout)
    monkeypatch.setattr(module.subprocess, "run", run)
    return calls


def _assert_system32(path, name):
    assert ntpath.isabs(path), path
    assert ntpath.normcase(path).startswith(ntpath.normcase("C:\\Windows\\System32\\")), path
    assert ntpath.basename(path).lower() == name


def test_system32_exe_uses_systemroot_and_falls_back(monkeypatch):
    monkeypatch.setenv("SystemRoot", r"D:\Win")
    assert ntpath.normcase(common.system32_exe("wevtutil.exe")) == ntpath.normcase(r"D:\Win\System32\wevtutil.exe")
    monkeypatch.delenv("SystemRoot")
    assert ntpath.normcase(common.system32_exe("wevtutil.exe")) == ntpath.normcase(r"C:\Windows\System32\wevtutil.exe")


def test_run_powershell_json_passes_absolute_path(monkeypatch):
    monkeypatch.delenv("SystemRoot", raising=False)
    calls = _capture(monkeypatch, common, b"[]")
    common.run_powershell_json("1")
    _assert_system32(calls[0][0], "powershell.exe")
    assert ntpath.normcase(calls[0][0]).endswith(ntpath.normcase(r"\WindowsPowerShell\v1.0\powershell.exe"))


def test_preflight_wevtutil_calls_pass_absolute_path(monkeypatch):
    monkeypatch.delenv("SystemRoot", raising=False)
    calls = _capture(monkeypatch, pf, "enabled: false\n  maxSize: 1048576\n")
    pf.read_log_state()
    pf.run_enable_event_log(apply=True, max_size_mb=8)
    assert len(calls) == 4
    for argv in calls:
        _assert_system32(argv[0], "wevtutil.exe")


def test_wrapper_and_installer_use_systemroot():
    cmd = (HERE / "run_taskdash.cmd").read_text(encoding="utf-8")
    assert r'"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"' in cmd
    assert "('powershell" not in cmd
    ps1 = (HERE / "install_task.ps1").read_text(encoding="utf-8")
    assert '$command = "cmd.exe"' not in ps1 and r"System32\cmd.exe" in ps1
