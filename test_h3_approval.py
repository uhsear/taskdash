"""H3-10: the approval sheet must stay true to the code, and every guide must link it.

No network and no scheduled task: the sheet is text, and the code is read as text.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import taskdash_cli as cli
from test_h2_upgrade import _documented_invocations, _help_flags
from test_phase25_repo import _accepted_paths

ROOT = Path(__file__).resolve().parent
SHEET = ROOT / "docs" / "APPROVAL.md"
MODULES = sorted(ROOT.glob("taskdash_*.py"))

SECTIONS = ("What TaskDash does", "Endpoints it contacts", "Accounts", "Privileges and task settings",
            "Files it reads and writes", "Data that leaves the computer", "How to remove it",
            "Release integrity")

# Any module that can open a connection. socket is allowed only for gethostname.
NETWORK = re.compile(r"^\s*(?:import|from)\s+(smtplib|urllib\.request|urllib\.error|urllib3|requests|http\.client|"
                     r"ftplib|httpx|aiohttp|telnetlib|xmlrpc\.client)\b", re.M)
RAW_SOCKET = re.compile(r"\bsocket\.(?!gethostname\b)\w+")


def _text():
    return SHEET.read_text(encoding="utf-8")


def test_the_sheet_has_every_section_and_fits_about_one_printed_page():
    text = _text()
    headings = re.findall(r"^## (.+)$", text, re.M)
    assert headings == list(SECTIONS)
    assert len(text.split()) <= 1300, "an approval sheet is about one printed page"


def test_the_sheet_names_only_the_network_code_that_exists():
    texts = {m.name: m.read_text(encoding="utf-8") for m in MODULES}
    users = {name: set(NETWORK.findall(text)) for name, text in texts.items()}
    assert {name: kinds for name, kinds in users.items() if kinds} == {
        "taskdash_common.py": {"smtplib", "requests"},
        "taskdash_preflight.py": {"smtplib"},
        "taskdash_publish.py": {"urllib.request", "urllib.error"},
    }, "a new network path needs a line in the Endpoints section of docs/APPROVAL.md"
    assert [name for name, text in texts.items() if RAW_SOCKET.search(text)] == []


def test_the_sheet_says_there_is_no_telemetry_and_the_code_agrees():
    assert "no update check" in _text().lower()
    for module in MODULES:
        code = module.read_text(encoding="utf-8")
        assert "pypi.org" not in code and "api.github.com" not in code, module.name


def test_every_taskdash_command_and_flag_in_the_sheet_exists(capsys):
    pairs = _documented_invocations(_text())
    commands = {command for command, _ in pairs}
    assert {"remove", "preflight"} <= commands
    assert commands <= set(cli.SUBCOMMAND_MODULES) | {"status", "test-alert"}
    help_flags = {command: _help_flags(command, capsys) for command in commands}
    for command, flag in sorted(pairs, key=str):
        if flag is not None:
            assert flag in help_flags[command], f"taskdash {command} has no {flag}"


def test_every_config_key_in_the_sheet_is_a_real_key():
    keys = set(re.findall(r"`((?:arcgis|alerting|smtp|metrics|privacy|logging)\.[a-z_]+)`", _text()))
    assert {"metrics.log_roots", "alerting.include_log_tail", "alerting.healthcheck_url", "smtp.host",
            "privacy.hide_paths"} <= keys
    assert keys <= _accepted_paths(), sorted(keys - _accepted_paths())


def test_every_file_and_privilege_in_the_sheet_is_in_the_code():
    text = _text()
    code = "\n".join(m.read_text(encoding="utf-8") for m in MODULES) + (ROOT / "install_task.ps1").read_text(
        encoding="utf-8") + (ROOT / "taskdash_task.xml").read_text(encoding="utf-8")
    names = ["alert_state.json", "first_run_budget", "event_read_mark", "collector_runs_pending", "alert_preview_",
             "dashboard_backup_", "taskdash_setup_manifest.json", "taskdash_handoff.bin", "bootstrap_FAIL_",
             ".bak_setup_", "taskdash-smtp", "taskdash.lock", "preflight_as_task_", "bootstrap.log",
             "workflow_descriptions.json", "dashboard_taskdash.json", "alerting.mail_config_path", "taskdash_*.log"]
    for name in names:
        assert name in text, f"the sheet lost {name}"
    for name in names[:-1]:
        assert name in code, f"the code has no {name}"
    for privilege in ("portal:user:createItem", "portal:publisher:publishFeatures", "portal:user:createGroup",
                      "portal:user:shareToGroup", "features:user:edit"):
        assert privilege in text and privilege in code, privilege
    for setting in ("PT1H", "PT2H", "IgnoreNew", "StartWhenAvailable", "LeastPrivilege"):
        assert setting in text and setting in code, setting


def test_the_sheet_describes_the_hand_off_file_permissions_the_code_sets():
    # The ACL is the only control on a machine-scope blob, so the sheet must not understate who can open it.
    text = " ".join(_text().split())
    assert "open to the task account only" not in text
    assert "only the task account (read and delete), SYSTEM and Administrators" in text and "machine scope" in text
    source = (ROOT / "taskdash_handoff.py").read_text(encoding="utf-8")
    assert "for sid in (SYSTEM_SID, ADMINISTRATORS_SID)" in source


@pytest.mark.parametrize("name", ["README.md", "docs/INSTALL.md", "docs/INTRODUCTION.md"])
def test_the_approval_sheet_is_linked(name):
    assert "APPROVAL.md" in (ROOT / name).read_text(encoding="utf-8"), name
