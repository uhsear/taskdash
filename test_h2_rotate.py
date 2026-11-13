"""H2-4: the rotation runbook must name real commands and flags, and every guide must link it.

No network and no scheduled task: the guide is text, and the installer is read as text.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import taskdash_cli as cli
from test_h2_upgrade import _documented_invocations, _help_flags

ROOT = Path(__file__).resolve().parent
ROTATE = ROOT / "docs" / "ROTATE.md"
INSTALLER = (ROOT / "install_task.ps1").read_text(encoding="utf-8")


def test_every_taskdash_command_and_flag_in_the_rotate_guide_exists(capsys):
    # The runas line ends in the example account name "svc-taskdash", which the extractor would read as a command.
    text = re.sub(r"runas /user:\S+", "runas", ROTATE.read_text(encoding="utf-8"))
    pairs = _documented_invocations(text)
    commands = {command for command, _ in pairs}
    assert {"test-alert", "status"} <= commands, "the guide lost a step"
    assert commands <= set(cli.SUBCOMMAND_MODULES) | {"status", "test-alert"}
    help_flags = {command: _help_flags(command, capsys) for command in commands}
    for command, flag in sorted(pairs, key=str):
        if flag is not None:
            assert flag in help_flags[command], f"taskdash {command} has no {flag}"


def test_every_installer_switch_in_the_rotate_guide_is_a_real_parameter():
    text = ROTATE.read_text(encoding="utf-8")
    block = text[text.index("install_task.ps1"):]
    block = block[:block.index("```")]
    switches = set(re.findall(r"(?<=\s)-([A-Z][A-Za-z]+)", block)) - {"ExecutionPolicy", "File"}
    assert {"Replace", "InstallDir", "Python", "User"} <= switches
    for switch in switches:
        assert re.search(rf"\$\w*{switch}\b", INSTALLER), f"install_task.ps1 has no -{switch}"
    assert "-Apply" in text


def test_the_guide_states_the_reset_fact_and_the_keyring_service():
    text = ROTATE.read_text(encoding="utf-8")
    assert "A reset is not a change" in text
    assert "taskdash-smtp" in text
    # The sign-in is rotated by the commands, not by a hard-coded profile name and runas.
    assert "ProfileManager" not in text
    for command in ("signin --apply", "signin --for-task-account --apply", "preflight --as-task --apply"):
        assert command in text, command


@pytest.mark.parametrize("name", ["README.md", "docs/UPGRADE.md", "docs/TROUBLESHOOTING.md",
                                  "docs/INTRODUCTION.md", "docs/INSTALL.md"])
def test_the_rotate_guide_is_linked(name):
    assert "ROTATE.md" in (ROOT / name).read_text(encoding="utf-8"), name
